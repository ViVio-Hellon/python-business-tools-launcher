"""ツールを起動・停止しても、ランチャー自身は落とさない

現場で「ツールを起動したらランチャーが閉じる」。考えられる道すじ:

* Windows の「親のPID」は、親が終わっても番号のまま残る。番号はすぐ
  使い回される。Start.vbs の wscript が終わり、**その番号でツールが
  起動すると、ランチャーがツールの子に見える。** ツールの子までまとめて
  止める (`taskkill /T`・片付け) と、ランチャーも落ちる
* ツールのフォルダーにランチャーのフォルダーが入っている置き方では、
  ランチャーを起こした pyw.exe がツールのものに見え、その子のランチャーも
  ツールの一部になる

ここでは作ったプロセスの一覧 (`_processes`) と起動時刻で、その形を再現する。
あわせて、ランチャーが**終了の手順を通らずに**終わったことを次の起動で
知らせることを確かめる。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

import boot  # noqa: E402
import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from launcher import desktop, trace  # noqa: E402

ME = os.getpid()
NOW = time.time()


class FakeTable:
    """プロセスの一覧と起動時刻を差し替える。"""

    def __init__(self, rows, starts, images=None, commands=None) -> None:
        self.rows = rows                      # (PID, 親のPID, 名前)
        self.starts = starts
        self.images = images or {}
        self.commands = commands or {}

    def __enter__(self):
        self.patches = [
            mock.patch.object(desktop, "_processes", lambda: list(self.rows)),
            mock.patch.object(desktop, "process_started_at",
                              lambda pid: self.starts.get(pid)),
            mock.patch.object(desktop, "process_image",
                              lambda pid: self.images.get(pid, "")),
            mock.patch.object(desktop, "command_line",
                              lambda pid: self.commands.get(pid, "")),
            mock.patch.object(desktop, "_is_zombie", lambda pid: False),
        ]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc) -> None:
        for patch in self.patches:
            patch.stop()


# 番号の使い回し: Start.vbs の wscript (PID 700) がランチャーを起こして終わり、
# あとでランチャーが起こしたツール (cmd.exe) が同じ 700 を使った
REUSED = FakeTable(
    rows=[(1, 0, "init"), (300, 1, "explorer.exe"),
          (ME, 700, "pythonw.exe"),           # 親の番号 700 が残っている
          (700, ME, "cmd.exe"),               # ランチャーが起こしたツール
          (701, 700, "python.exe")],          # ツールの本体
    starts={1: NOW - 9000, 300: NOW - 8000, ME: NOW - 600,
            700: NOW - 30, 701: NOW - 29})


class ProcessTreeTests(unittest.TestCase):

    def test_番号を使い回されてもランチャーはツールの子にならない(self) -> None:
        with REUSED:
            tree = desktop.process_tree(700)
        self.assertEqual(tree, {700, 701})

    def test_親より前に起動した子は別のプロセスの子(self) -> None:
        table = FakeTable(
            rows=[(1, 0, "init"), (ME, 1, "pythonw.exe"),
                  (900, ME, "tool.exe"), (950, 900, "edge.exe"),
                  (960, 900, "old.exe")],      # 前に居た 900 の子 (番号が残った)
            starts={1: NOW - 9000, ME: NOW - 600, 900: NOW - 30,
                    950: NOW - 20, 960: NOW - 3000})
        with table:
            self.assertEqual(desktop.process_tree(900), {900, 950})

    def test_ランチャーを起こしたものからたどってもランチャーは入らない(self) -> None:
        """ツールのフォルダーにランチャーのフォルダーが入っている置き方。"""
        folder = os.path.join(os.sep, "share", "業務ツール", "梱包")
        table = FakeTable(
            rows=[(1, 0, "init"), (300, 1, "explorer.exe"),
                  (400, 300, "pyw.exe"),      # ランチャーを起こした py ランチャー
                  (ME, 400, "pythonw.exe"),
                  (500, ME, "tool.exe")],
            starts={1: NOW - 9000, 300: NOW - 8000, 400: NOW - 601,
                    ME: NOW - 600, 500: NOW - 10},
            images={400: r"C:\Windows\pyw.exe", ME: r"C:\Python\pythonw.exe",
                    500: r"C:\tools\tool.exe"},
            commands={400: f'pyw.exe "{folder}/ランチャー/launcher.py"'})
        with table:
            self.assertEqual(desktop.protected_pids(), {ME, 400, 300, 1})
            self.assertEqual(desktop.processes_in_folder(folder), [])
            self.assertEqual(desktop.process_tree(400), set())
            self.assertEqual(desktop.related_pids(folder, {500}), {500})

    def test_自分から下はたどれる(self) -> None:
        """自分の子 (ランチャーが起こしたツール) は、これまでどおり見える。"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        tree = desktop.process_tree(ME)
        self.assertIn(proc.pid, tree)
        self.assertNotIn(ME, tree)


class TerminateTests(unittest.TestCase):

    def test_ランチャー自身と祖先は止めない(self) -> None:
        # 守りが外れても試験そのものを落とさないよう、止める手は偽物にする
        with mock.patch.object(process_manager.os, "kill") as kill, \
                mock.patch.object(process_manager.subprocess, "run") as run:
            self.assertFalse(process_manager._terminate(ME, force=True))
            self.assertFalse(process_manager._terminate(os.getppid(), force=True))
            with REUSED, mock.patch.object(process_manager.os, "name", "nt"):
                self.assertFalse(process_manager._terminate(ME, force=True))
        kill.assert_not_called()
        run.assert_not_called()

    def test_Windowsでは確かめた子だけを指定し_Tを使わない(self) -> None:
        """`taskkill /T` は残った親の番号で子をたどる (ランチャーまで落ちる)。"""
        with REUSED, mock.patch.object(process_manager.os, "name", "nt"), \
                mock.patch.object(process_manager.subprocess, "run") as run:
            self.assertTrue(process_manager._terminate(700, force=True))
        command = run.call_args.args[0]
        self.assertNotIn("/T", command)
        pids = [int(command[i + 1]) for i, part in enumerate(command) if part == "/PID"]
        self.assertEqual(sorted(pids), [700, 701])
        self.assertNotIn(ME, pids)
        self.assertIn("/F", command)


class UncleanEndTests(LocalAreaTestCase):
    """前のランチャーが終了の手順を通らずに終わっていたら、次の起動で知らせる。"""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(launch_guard.remove_lock)

    def leave_lock(self, started_at: float) -> None:
        """強制終了されたランチャーが残したロック (その PID はもう居ない)。"""
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        launch_guard.lock_path().parent.mkdir(parents=True, exist_ok=True)
        launch_guard.lock_path().write_text(json.dumps({
            "app_id": "nlm.business-tools-launcher", "pid": gone.pid,
            "started_at": started_at, "python": sys.executable}),
            encoding="utf-8")

    def test_ふつうに終わればロックは残らず何も言わない(self) -> None:
        first = launch_guard.acquire()
        self.assertIsNone(first.leftover)
        launch_guard.remove_lock()
        self.assertIsNone(launch_guard.acquire().leftover)

    def test_残ったロックで前回の異常終了を知らせる(self) -> None:
        self.leave_lock(started_at=time.time() - 60)
        result = boot.run()
        self.assertTrue(result.started)
        status = result.manager.status
        self.assertIn("終了の手順を通らずに終わっていました", status.detail)
        self.assertTrue(status.incident)
        text, _ = trace.read_record(status.incident)
        self.assertIn("外から止められた可能性", text)
        self.assertIn("前回の異常終了", [r["種類"] for r in trace.read_events()])

    def test_落ちたときの記録があれば添える(self) -> None:
        started = time.time() - 60
        self.leave_lock(started_at=started)
        crash = trace.crash_log_path()
        crash.parent.mkdir(parents=True, exist_ok=True)
        crash.write_text("--- 2026/10/08 09:00:00 ランチャー起動 pid=1 ---\n"
                         "Windows fatal exception: access violation\n\n"
                         'Thread 0x1 (most recent call first):\n  File "desktop.py", '
                         "line 790 in _windows_of\n", encoding="utf-8")
        result = boot.run()
        text, _ = trace.read_record(result.manager.status.incident)
        self.assertIn("Python ごと落ちた", text)
        self.assertIn("access violation", text)

    def test_電源断のあとなら障害記録にしない(self) -> None:
        self.leave_lock(started_at=time.time() - 600)
        with mock.patch.object(desktop, "system_boot_time",
                               return_value=time.time() - 60):
            result = boot.run()
        self.assertNotIn("終了の手順", result.manager.status.detail)
        self.assertEqual(result.manager.status.incident, "")
        self.assertIn("前回の終わり方", [r["種類"] for r in trace.read_events()])

    def test_落ちたときの記録を用意する(self) -> None:
        import faulthandler

        self.addCleanup(lambda: trace._crash_stream and trace._crash_stream.close())
        self.addCleanup(faulthandler.disable)
        trace.enable_crash_log()
        self.assertTrue(faulthandler.is_enabled())
        self.assertIn(f"ランチャー起動 pid={ME}",
                      trace.crash_log_path().read_text(encoding="utf-8"))
        self.assertEqual(trace.crash_lines_since(time.time() - 60), [])


if __name__ == "__main__":
    unittest.main()
