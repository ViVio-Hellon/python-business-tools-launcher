"""更新のため全 PC で止める — 各 PC のランチャーの見張り (1.8.8)

共有サーバーのツールを上書きするには、全 PC でそのツールを止めておく必要がある。
管理者がツール管理マスタに「更新中」の印を付けると、各 PC のランチャーは:

1. そのツールが動いていれば、ふだんの［ツール停止］と同じ手順で止める
   (起動の途中なら起動をやめる)
2. どうなったかをマスタに書き返す (止めた / 使っていない / 処理中 / 止められない)
3. **処理中で止まらなければ、利用者に「終了してください」と知らせて待つ**
   (作業中のデータを失わない)。1 分ごとに止め直す
4. 管理者がこの PC に強制終了を頼んでいれば、強制終了する
5. 印があるあいだ、そのツールは起動しない (バーに「更新中」と出す)

**例外を出さない** (マスタに届かない・遅いときは何もしない)。画面 (tkinter) には
触らない ── 呼び出し側 (バー) が別のスレッドで `poll` を呼び、結果を画面に渡す。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import fileprobe, tool_master
from .logging_utils import get_logger

log = get_logger("maintenance")

# 印を見に行く間隔 (秒)。共有フォルダーの SQLite を読むだけなので軽い
POLL_SEC = 10.0
# 処理中で止まらなかったツールを、止め直すまでの間隔 (秒)
RETRY_SEC = 60.0
# マスタを読むのを待つ上限 (秒)。遅い共有フォルダーでバーを待たせない
READ_TIMEOUT_SEC = 5.0


@dataclass
class PollResult:
    blocked: dict[str, str] = field(default_factory=dict)    # 更新中のツール (アプリID → 表示名)
    ask_user: list[tuple[str, str]] = field(default_factory=list)  # 終了を頼む (アプリID, 表示名)


class Watcher:
    """更新中の印を見て、この PC のツールを止める。`poll` は別のスレッドから呼ぶ。"""

    def __init__(self, manager) -> None:
        self.manager = manager
        self._lock = threading.Lock()
        # (アプリID, 印を付けた時刻) → 最後に書き返した状態。同じなら書かない
        self._reported: dict[tuple[str, str], str] = {}
        # (アプリID, 印を付けた時刻) → 最後に止めようとした時刻
        self._tried: dict[tuple[str, str], float] = {}
        # 利用者に「終了してください」と知らせたもの (印ごとに1回)
        self._asked: set[tuple[str, str]] = set()

    def poll(self) -> Optional[PollResult]:
        """1回見る。マスタを使っていない・届かないときは None (何もしない)。"""
        try:
            return self._poll()
        except Exception:                     # noqa: BLE001 - バーを止めない
            log.warning("更新中の印を確かめられませんでした", exc_info=True)
            return None

    def _poll(self) -> Optional[PollResult]:
        path = tool_master.current_path()
        if path is None:
            return None
        found = fileprobe.probe(path)
        if not found.found:
            return None
        marks = _read(path)
        if marks is None:
            return None
        result = PollResult(blocked={a: (m.name or a) for a, m in marks.items()})
        self.manager.set_blocked(set(marks))
        for app_id, mark in marks.items():
            self._handle(path, mark, result)
        # 終わった印の覚えを片付ける
        live = {(a, m.started_at) for a, m in marks.items()}
        with self._lock:
            for store in (self._reported, self._tried):
                for key in [k for k in store if k not in live]:
                    store.pop(key, None)
            self._asked &= live
        return result

    def _handle(self, path, mark: tool_master.Maintenance, result: PollResult) -> None:
        manager = self.manager
        app_id = mark.app_id
        key = (app_id, mark.started_at)
        name = mark.name or app_id
        if app_id in manager.starting_ids:
            manager.stop(app_id)              # 起動をやめる (動き出す前に)
        running = app_id in manager.running
        if not running:
            state = (tool_master.STOPPED if self._reported.get(key) in
                     (tool_master.STOPPED, tool_master.BUSY, tool_master.FAILED)
                     else tool_master.NOT_RUNNING)
            self._report(path, key, state)
            return
        force = tool_master.take_force_request(path, app_id)
        now = time.monotonic()
        last = self._tried.get(key)
        if not force and last is not None and now - last < RETRY_SEC:
            return                            # 処理中。少し置いてから止め直す
        self._tried[key] = now
        log.info("更新のため止めます: %s%s", app_id, " (強制終了を頼まれた)" if force else "")
        if manager._stop_blocking(app_id, force=force):
            self._report(path, key, tool_master.STOPPED,
                         "管理者に頼まれて強制終了した" if force else "")
            return
        detail = (manager.status.detail or "").strip().splitlines()
        message = detail[0] if detail else ""
        if manager.stop_refused(app_id):
            self._report(path, key, tool_master.BUSY, message, force=True)
            if key not in self._asked:
                self._asked.add(key)
                result.ask_user.append((app_id, name))
        else:
            self._report(path, key, tool_master.FAILED, message, force=True)

    def _report(self, path, key: tuple[str, str], state: str, message: str = "", *,
                force: bool = False) -> None:
        """書き返す。前と同じ状態なら書かない (共有のマスタに何度も書かない)。"""
        if not force and self._reported.get(key) == state:
            return
        try:
            tool_master.report(path, key[0], state, message)
            self._reported[key] = state
        except tool_master.MasterError as exc:
            log.warning("更新の返事を書けませんでした: %s", exc)


def _read(path) -> Optional[dict[str, tool_master.Maintenance]]:
    """印を読む。**`READ_TIMEOUT_SEC` より待たない。** 読めなければ None。"""
    box: dict = {}

    def work() -> None:
        try:
            box["marks"] = tool_master.under_maintenance(path)
        except Exception as exc:              # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=work, name="maintenance-read", daemon=True)
    worker.start()
    worker.join(READ_TIMEOUT_SEC)
    if worker.is_alive() or "error" in box:
        if "error" in box:
            log.info("更新中の印を読めません: %s", box["error"])
        return None
    return box["marks"]
