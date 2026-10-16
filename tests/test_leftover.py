"""ツールが「止めた」と答えたのに Python が残る (all-tools の報告・1.7.7)

all-tools で、本体を組み立てられなかったツール (日報) の Python が待機画面で
理由を出したまま残った。待機画面が止める合図に答えず、stop.bat は確かめずに
「終わってもらいました」と言い、フォルダーを消せなかった (タスクマネージャー
で切るしかなかった)。

ランチャーにも同じ穴があった:

* 止めるとき、ツールの答え (終了の入口・stop.bat の 0・起動確認) だけで
  「正常に終了しました」としていた。**プロセスが消えたかを見ていなかった**
* 起動に失敗したとき、入口のあるツールは「起こしたものは片付けない」で、
  残った Python を誰も止められなかった (記録が無いので［ツール停止］も
  stop.bat も届かない)

試験では、all-tools と同じく**ツールのフォルダーの中のフォルダー**
(`tools/nippou`) から、止める合図に答えない Python を起こしておく。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_app_manager import ManagerTestCase  # noqa: E402
from test_tool_entries import _script, make_entry_tool  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State  # noqa: E402
from launcher import tool_entries, tool_registry  # noqa: E402

# 止める合図に答えない Python。子を1つ起こす (ツールが起こした既定のブラウザーの
# 代わり。フォルダーの外のもので、**止めてはいけない**)
STUCK_PY = """\
import pathlib, subprocess, sys, time
here = pathlib.Path(__file__).resolve().parent
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         stdin=subprocess.DEVNULL)
(here / "child.txt").write_text(str(child.pid))
import os
(here / "stuck_pid.txt").write_text(str(os.getpid()))
(here / "stuck.txt").write_text("")
while True:
    time.sleep(0.2)
"""


def _alive(pid: int) -> bool:
    return process_manager.is_pid_alive(pid)


class LeftoverCase(ManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.root: Path | None = None
        self.stuck: subprocess.Popen | None = None

    def tearDown(self) -> None:
        for pid in self._pids():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        if self.stuck is not None:
            try:
                self.stuck.wait(timeout=5)
            except subprocess.SubprocessError:
                pass
        if self.root is not None and (self.root / "pid.txt").exists():
            (self.root / "stop.txt").write_text("")
            deadline = time.monotonic() + 3
            while (self.root / "pid.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        super().tearDown()

    def _pids(self) -> list[int]:
        pids = []
        if self.stuck is not None:
            pids.append(self.stuck.pid)
        if self.root is not None:
            for name in ("child.txt", "stuck_pid.txt"):
                text = self.nested / name
                if text.exists():
                    pids.append(int(text.read_text()))
        return pids

    @property
    def nested(self) -> Path:
        return self.root / "tools" / "nippou"

    def start_stuck(self) -> int:
        """ツールのフォルダーの中のフォルダーから、止める合図に答えない Python を起こす。"""
        self.nested.mkdir(parents=True, exist_ok=True)
        script = self.nested / "stuck.py"
        script.write_text(STUCK_PY, encoding="utf-8")
        self.stuck = subprocess.Popen([sys.executable, str(script)],
                                      stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (self.nested / "stuck.txt").exists():
            self.assertLess(time.monotonic(), deadline, "止まらない Python が起きません")
            time.sleep(0.05)
        return self.stuck.pid

    def child_pid(self) -> int:
        return int((self.nested / "child.txt").read_text())

    def register_entry_tool(self, name: str = "all-tools") -> tool_registry.Tool:
        start = make_entry_tool(self.work_root, name, ready_after=0.2)
        self.root = start.parent
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.all-tools", display_name="統合ツール", start_command=str(start)))
        return tool_registry.get("nlm.all-tools")


class StopConfirmsGoneTests(LeftoverCase):
    """ツールが「止めた」と答えても、プロセスが残っていれば止まったと言わない。"""

    def setUp(self) -> None:
        super().setUp()
        self.patch_wait = unittest.mock.patch.object(process_manager, "LEFTOVER_WAIT_SEC", 1.0,
                                                     create=True)
        self.patch_wait.start()
        self.addCleanup(self.patch_wait.stop)

    def test_残っていれば止まったと言わず強制終了なら止める(self) -> None:
        tool = self.register_entry_tool()
        self.start(tool)
        stuck = self.start_stuck()
        running = self.manager.running[tool.app_id]

        # ふつうの停止: 入口は「止めた」(0) と答え、確認口も「動いていない」と言う
        result = process_manager.stop(running, timeout=5)
        self.assertFalse(result.stopped, result.message)
        self.assertEqual(result.leftover, [stuck])
        self.assertIn("プロセスが残っています", result.message)
        self.assertIn(f"PID {stuck}", result.message)
        self.assertTrue(result.busy, "強制終了するかを聞けるように、断りとして返す")
        self.assertTrue(_alive(stuck), "強制終了でないのに止めました")

        # 強制終了: 残っているものを止める。**フォルダーの外の子 (ブラウザーの
        # 代わり) には触らない**
        result = process_manager.stop(running, force=True, timeout=5)
        self.assertTrue(result.stopped, result.message)
        self.assertIn("残っていたプロセスも止めました", result.message)
        self.stuck.wait(timeout=5)
        self.assertTrue(_alive(self.child_pid()), "ツールが起こしただけの子まで止めました")

    def test_バーは断りとして出し強制終了で止める(self) -> None:
        tool = self.register_entry_tool()
        self.start(tool)
        stuck = self.start_stuck()

        self.assertFalse(self.manager._stop_blocking(tool.app_id))
        status = self.manager.status
        self.assertTrue(status.refused)
        first = status.detail.splitlines()[0]
        self.assertIn("止まったと答えましたが、プロセスが残っています", first)
        self.assertIn(f"PID {stuck}", first)
        self.assertIn(tool.app_id, self.manager.running, "残っているのに一覧から外しました")

        self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        self.stuck.wait(timeout=5)
        self.assertNotIn(tool.app_id, self.manager.running)

    def test_何も残っていなければこれまでどおり(self) -> None:
        tool = self.register_entry_tool()
        self.start(tool)
        began = time.monotonic()
        self.assertTrue(self.manager._stop_blocking(tool.app_id))
        self.assertLess(time.monotonic() - began, 8)
        self.assertTrue(self.manager.status.message.startswith("統合ツールを終了しました"),
                        self.manager.status.message)
        self.assertFalse(self.manager.status.refused)

    def test_stopbatの画面でも残りを知らせる(self) -> None:
        """ランチャーの stop.bat (process_manager.py) も、確かめずに「済」と言わない。"""
        import io
        from contextlib import redirect_stdout

        tool = self.register_entry_tool()
        self.start(tool)
        stuck = self.start_stuck()
        out = io.StringIO()
        with redirect_stdout(out):
            code = process_manager.main([])
        self.assertEqual(code, 1)
        self.assertIn("プロセスが残っています", out.getvalue())
        self.assertIn("--force", out.getvalue())
        self.assertTrue(_alive(stuck))
        out = io.StringIO()
        with redirect_stdout(out):
            code = process_manager.main(["--force"])
        self.assertEqual(code, 0, out.getvalue())
        self.stuck.wait(timeout=5)


class FailedStartTests(LeftoverCase):
    """起動できなかったツールのプロセスを、誰も止められないまま残さない。

    起動ファイルは Start.vbs と同じく、本体 (止める合図に答えない Python) を
    起こして**すぐ終わる**。
    """

    def setUp(self) -> None:
        super().setUp()
        from unittest import mock

        from launcher import app_config

        ui = app_config.load()["ui"]
        patcher = mock.patch.dict(ui, {"start_timeout_seconds": 2,
                                       "stop_timeout_seconds": 2})
        patcher.start()
        self.addCleanup(patcher.stop)
        mock.patch.object(process_manager, "LEFTOVER_WAIT_SEC", 1.0, create=True).start()
        self.addCleanup(mock.patch.stopall)

    def make_failing_tool(self, *, check: str = "", stop: str = "",
                          start_exit: int = 1) -> tool_registry.Tool:
        """本体を組み立てられないツール。起動ファイルは待機画面の Python を残して終わる。"""
        self.root = self.work_root / "all-tools"
        nested = self.nested
        nested.mkdir(parents=True)
        (nested / "stuck.py").write_text(STUCK_PY, encoding="utf-8")
        start = _script(self.root / "start.bat",
                        f'"{sys.executable}" "{nested / "stuck.py"}" '
                        "</dev/null >/dev/null 2>&1 &\n"
                        "while [ ! -f tools/nippou/stuck.txt ]; do sleep 0.05; done\n"
                        f"exit {start_exit}\n")
        if check:
            _script(self.root / tool_entries.CHECK_FILE, check)
        if stop:
            _script(self.root / tool_entries.STOP_FILE, stop)
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.all-tools", display_name="統合ツール", start_command=str(start),
            port=0 if check else _free_port()))
        return tool_registry.get("nlm.all-tools")

    def stuck_pid(self) -> int:
        return int((self.nested / "stuck_pid.txt").read_text())

    def wait_gone(self, pid: int) -> bool:
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not _alive(pid)

    def test_入口が動いていないと言うのに残ったPythonを片付ける(self) -> None:
        """all-tools の形: 確認口は「動いていない」、終了口は止めたと答えるが止まらない。"""
        tool = self.make_failing_tool(check='echo "動いていません"\nexit 1\n',
                                      stop="exit 0\n")
        self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertEqual(status.message, "統合ツールを起動できませんでした")
        stuck = self.stuck_pid()
        self.assertTrue(self.wait_gone(stuck), "残った Python を片付けていません")
        self.assertIn(f"残っていた統合ツールのプロセスを止めました", status.detail)
        self.assertIn(f"PID {stuck}", status.detail)
        self.assertNotIn(tool.app_id, self.manager.running)
        self.assertTrue(_alive(self.child_pid()), "ツールが起こしただけの子まで止めました")

    def test_入口が準備中と言うなら止めずに記録を残す(self) -> None:
        """起動に時間がかかっているだけかもしれない。止められるように記録だけ残す。"""
        check = ('pid=$(cat tools/nippou/stuck_pid.txt 2>/dev/null)\n'
                 '[ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && '
                 '{ echo "ブラウザ版を準備しています"; exit 2; }\n'
                 'echo "動いていません"\nexit 1\n')
        tool = self.make_failing_tool(check=check, stop="exit 0\n", start_exit=0)
        self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        stuck = self.stuck_pid()
        self.assertTrue(_alive(stuck), "準備中と答えているのに止めました")
        self.assertIn("プロセスはまだ動いています", status.detail)
        self.assertIn("［ツール停止］", status.detail)
        self.assertIn(tool.app_id, self.manager.running)
        self.assertFalse(self.manager.running[tool.app_id].confirmed)
        self.assertIn(tool.app_id, status.running_ids)

        # ［ツール停止］: 終了口は止めたと答えるが止まらない → 断りとして出し、強制終了で止める
        self.assertFalse(self.manager._stop_blocking(tool.app_id))
        self.assertTrue(self.manager.status.refused)
        self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        self.assertTrue(self.wait_gone(stuck))
        self.assertNotIn(tool.app_id, self.manager.running)

    def test_終了口が断ったら止めずに記録を残す(self) -> None:
        tool = self.make_failing_tool(check='echo "動いていません"\nexit 1\n',
                                      stop='echo "取り込みの途中です"\nexit 1\n')
        self.start(tool)
        status = self.manager.status
        self.assertTrue(_alive(self.stuck_pid()), "ツールが断ったのに止めました")
        self.assertIn("取り込みの途中です", status.detail)
        self.assertIn(tool.app_id, self.manager.running)

    def test_入口の無いツールもすぐ終わる起動ファイルなら残りを片付ける(self) -> None:
        """以前は起動ファイルが動いているときだけ片付けていた (Start.vbs は戻り値 0 ですぐ終わる)。"""
        tool = self.make_failing_tool(start_exit=0)
        self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        stuck = self.stuck_pid()
        self.assertTrue(self.wait_gone(stuck), "残った Python を片付けていません")
        self.assertIn("残っていた統合ツールのプロセスを止めました", status.detail)
        self.assertTrue(_alive(self.child_pid()))


def _free_port() -> int:
    from _fake_tool_support import free_port

    return free_port()


if __name__ == "__main__":
    unittest.main()
