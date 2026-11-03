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

from . import fileprobe, tool_master, tool_registry
from .logging_utils import get_logger
from .paths import normalize

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
    """更新中の印を見て、この PC のツールを止める。`poll` は別のスレッドから呼ぶ。

    **返事を書いたかはマスタで確かめる** (覚えておくのではなく)。管理者が印を
    付け直しても (前の返事は消える)、そのつど書き返す。
    """

    def __init__(self, manager) -> None:
        self.manager = manager
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
        local = tool_registry.all_tools(include_disabled=True)
        # 印 → この PC のツールのアプリID (アプリIDが違っても、起動ファイルで見つける)
        targets = {app_id: _local_ids(mark, local) for app_id, mark in marks.items()}
        blocked = {target: (mark.name or app_id)
                   for app_id, mark in marks.items() for target in targets[app_id]}
        self.manager.set_blocked(set(blocked))
        result = PollResult(blocked=blocked)
        for app_id, mark in marks.items():
            try:
                self._handle(path, mark, targets[app_id], result)
            except Exception:                 # noqa: BLE001 - ほかの印は続ける
                log.warning("更新のための停止で失敗しました: %s", app_id, exc_info=True)
        # 終わった印の覚えを片付ける
        live = {(a, m.started_at) for a, m in marks.items()}
        for key in [k for k in self._tried if k not in live]:
            self._tried.pop(key, None)
        self._asked &= live
        return result

    def _handle(self, path, mark: tool_master.Maintenance, local_ids: set[str],
                result: PollResult) -> None:
        manager = self.manager
        key = (mark.app_id, mark.started_at)
        name = mark.name or mark.app_id
        for app_id in local_ids & set(manager.starting_ids):
            manager.stop(app_id)              # 起動の途中ならやめる (動き出す前に)
        running = [a for a in local_ids if a in manager.running]
        previous = _my_report(path, mark.app_id)
        if not running:
            stopped_before = previous is not None and previous.state in (
                tool_master.STOPPED, tool_master.BUSY, tool_master.FAILED)
            self._report(path, mark.app_id, previous,
                         tool_master.STOPPED if stopped_before else tool_master.NOT_RUNNING,
                         previous.message if stopped_before and previous.state ==
                         tool_master.STOPPED else "")
            return
        if any(manager.is_busy(a) for a in running):
            return                            # 利用者が止めている最中。次に見たとき
        force = _take_force(path, mark.app_id)
        now = time.monotonic()
        last = self._tried.get(key)
        if not force and last is not None:
            if previous is not None and previous.state == tool_master.FAILED:
                return                        # 止められなかった。強制終了を頼まれるまで待つ
            if now - last < RETRY_SEC:
                return                        # 処理中。少し置いてから止め直す
        self._tried[key] = now
        busy_message = ""
        failed_message = ""
        for app_id in running:
            log.info("更新のため止めます: %s%s", app_id,
                     " (強制終了を頼まれた)" if force else "")
            if manager._stop_blocking(app_id, force=force):
                continue
            status = manager.status
            lines = (status.detail or "").strip().splitlines() \
                if status.app_id == app_id else []
            if manager.stop_refused(app_id):
                busy_message = lines[0] if lines else "実行中の処理があります"
            else:
                failed_message = lines[0] if lines else "止められませんでした"
        if failed_message:
            self._report(path, mark.app_id, previous, tool_master.FAILED, failed_message)
        elif busy_message:
            self._report(path, mark.app_id, previous, tool_master.BUSY, busy_message)
            if key not in self._asked:
                self._asked.add(key)
                result.ask_user.append((running[0], name))
        else:
            self._report(path, mark.app_id, previous, tool_master.STOPPED,
                         "管理者に頼まれて強制終了した" if force else "")
            manager.notify(f"管理者が{name}を更新するため、{name}を終了しました。\n"
                           "更新が終わるまで、このツールは起動できません。")

    def _report(self, path, app_id: str, previous, state: str, message: str = "") -> None:
        """書き返す。**マスタの返事と同じなら書かない** (共有のマスタに何度も書かない)。

        書けなくても (読み取り専用の共有フォルダー・ほかの PC が書いている最中)、
        止めることは続ける。次に見たときに書き直す。
        """
        if previous is not None and previous.state == state and \
                previous.message == message[:300]:
            return
        try:
            tool_master.report(path, app_id, state, message)
        except tool_master.MasterError as exc:
            log.warning("更新の返事を書けませんでした: %s", exc)


def _local_ids(mark: tool_master.Maintenance, local) -> set[str]:
    """印のツールにあたる、この PC のツールのアプリID。"""
    ids = {mark.app_id}
    if mark.start_command.strip():
        starts = {normalize(c) for c in
                  tool_master.MasterTool(0, start_command=mark.start_command).candidates()}
        ids |= {t.app_id for t in local
                if t.start_command and normalize(t.start_command) in starts}
    return ids


def _my_report(path, app_id: str):
    try:
        return tool_master.my_report(path, app_id)
    except tool_master.MasterError as exc:
        log.info("前の返事を読めません: %s", exc)
        return None


def _take_force(path, app_id: str) -> bool:
    """強制終了の依頼を受け取る。**書けなくても止めることは続ける** (依頼は無いものとする)。"""
    try:
        return tool_master.take_force_request(path, app_id)
    except tool_master.MasterError as exc:
        log.warning("強制終了の依頼を確かめられません: %s", exc)
        return False


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
