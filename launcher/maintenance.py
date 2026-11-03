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

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
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
    # ランチャー自身の更新の印 (この PC で閉じる)。印を付けた PC では None
    close_launcher: Optional[tool_master.Maintenance] = None
    master: Optional[Path] = None


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
        launcher_mark = marks.pop(tool_master.LAUNCHER_MARK, None)
        local = tool_registry.all_tools(include_disabled=True)
        # 印 → この PC のツールのアプリID (アプリIDが違っても、起動ファイルで見つける)
        targets = {app_id: _local_ids(mark, local) for app_id, mark in marks.items()}
        blocked = {target: (mark.name or app_id)
                   for app_id, mark in marks.items() for target in targets[app_id]}
        self.manager.set_blocked(set(blocked))
        result = PollResult(blocked=blocked, master=path)
        if launcher_mark is not None and not launcher_mark.set_by_this_pc():
            # ランチャー自身の更新。閉じるのはバー (画面) の仕事
            result.close_launcher = launcher_mark
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


# ------------------------------------------------------------------
# ランチャー自身の更新 (1.8.11)
# ------------------------------------------------------------------
WAITER_NAME = "restart_waiter.py"
WAITER_PID = "restart_waiter.pid"


def launcher_hold() -> Optional[tool_master.Maintenance]:
    """ランチャー更新中の印 (この PC が付けたものは除く)。無い・読めなければ None。

    起動したときに見る。印があれば起動せず、待ち番に任せる。
    """
    try:
        path = tool_master.current_path()
        if path is None or not fileprobe.probe(path).found:
            return None
        marks = _read(path) or {}
    except Exception:                         # noqa: BLE001 - 起動を止めない
        log.warning("ランチャー更新中の印を確かめられませんでした", exc_info=True)
        return None
    mark = marks.get(tool_master.LAUNCHER_MARK)
    if mark is None or mark.set_by_this_pc():
        return None
    return mark


def restart_command() -> list[str]:
    """待ち番がランチャーを起動し直すコマンド (更新したあとの、新しい版の起動ファイル)。"""
    from . import app_config

    start = app_config.APP_ROOT / "Start.vbs"
    if os.name == "nt" and start.is_file():
        return ["wscript.exe", str(start)]
    return [sys.executable, str(app_config.APP_ROOT / "launcher.py")]


# 番号のファイルを書いた時刻と、プロセスの起動の時刻の差の許し (秒)
WAITER_REUSE_MARGIN_SEC = 2.0


def waiter_running() -> bool:
    """待ち番が動いているか (番号が使い回されていても取り違えない)。

    待ち番が動いたまま PC を終了すると、番号のファイルが残る。再起動のあと、
    別のプログラムが同じ番号を持つことがある (Windows は番号を使い回す)。
    取り違えると待ち番を置かずに閉じ、**更新のあとランチャーが起動しない**。

    * ファイルを書いた時刻より後に起動したプロセスは、書いた本人ではない
    * コマンドラインが読めれば、待ち番の名前が入っているかで決める
    * どちらも分からなければ「動いていない」とする (もう1つ置いても害は無い:
      ランチャーは二重には起動しない)
    """
    from . import app_config, desktop

    import process_manager

    pid_file = app_config.local_dir("runtime") / WAITER_PID
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        written = pid_file.stat().st_mtime
    except (OSError, ValueError):
        return False
    if not process_manager._is_alive(pid):
        return False
    started = desktop.process_started_at(pid)
    if started is not None and started > written + WAITER_REUSE_MARGIN_SEC:
        log.info("待ち番の番号 %s は別のプロセスでした (番号の使い回し)", pid)
        return False
    command = process_manager.process_command_line(pid)
    if command:
        return WAITER_NAME in command
    return started is not None


def start_waiter(master: Path) -> bool:
    """待ち番を**この PC の中にコピーして**動かす。動いていればそのまま。動かせたら真。"""
    from . import app_config

    if waiter_running():
        return True
    folder = app_config.local_dir("runtime")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        script = folder / WAITER_NAME
        shutil.copyfile(Path(__file__).with_name(WAITER_NAME), script)
        command = [_pythonw(), "-I", str(script), "--master", str(master),
                   "--command", json.dumps(restart_command(), ensure_ascii=False),
                   "--pid-file", str(folder / WAITER_PID)]
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen(command, cwd=str(folder), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=flags,
                         start_new_session=os.name != "nt")
    except (OSError, ValueError) as exc:
        log.error("ランチャーの更新の待ち番を動かせませんでした: %s", exc)
        return False
    log.info("ランチャーの更新の待ち番を動かしました: %s", script)
    return True


def _pythonw() -> str:
    """窓を出さない Python (Windows の pythonw.exe)。無ければいまの Python。"""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "python.exe":
        quiet = exe.with_name("pythonw.exe")
        if quiet.is_file():
            return str(quiet)
    return str(exe)


def report_closed(master: Path) -> None:
    try:
        tool_master.report(master, tool_master.LAUNCHER_MARK, tool_master.CLOSED,
                           "更新が終わると自動で起動します")
    except tool_master.MasterError as exc:
        log.warning("ランチャーを閉じたことを書き返せませんでした: %s", exc)


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
