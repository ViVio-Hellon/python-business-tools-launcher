"""PC のスリープ明け・画面の休止で、ツールを誤って止めない・外さない (1.7.13)

他ツールで「スリープ復帰時の時計のずれやブラウザーのタブ休止で問い合わせが
止まり、サーバーが誤って終了する」不具合があった。ランチャーは Web サーバーを
持たないので同じ不具合は無いが、時間で決める所が同じ形で誤作動していた:

1. 起動を待っているあいだに PC が眠ると、復帰した瞬間に時間切れになり
   「起動できませんでした」と出して、立ち上がりかけのツールのプロセスを止めた
   (Windows の Python の時計はスリープ中も進む)
2. ランチャーの専用画面は、裏に回ると (最小化) ブラウザーがタイマーを間引く。
   画面からの心拍が途切れて、ツールが「誰も見ていない」と自分で終わりうる
3. 見回り: スリープ明けすぐはツールの答えが遅れることがある。数回答えない
   だけで「終了しました」と一覧から外していた

試験では、時計が進む (= スリープした) ことを `time.monotonic` を差し替えて作る。
"""
from __future__ import annotations

import os
import signal
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State  # noqa: E402
from launcher import app_config, browser, desktop, health, stopwatch, tool_registry  # noqa: E402

_REAL_MONOTONIC = time.monotonic


class SleepClock:
    """`after` 秒たったところで、時計を `jump` 秒進める (PC が眠った)。"""

    def __init__(self, *, after: float, jump: float) -> None:
        self.began = _REAL_MONOTONIC()
        self.after = after
        self.jump = jump

    def __call__(self) -> float:
        now = _REAL_MONOTONIC()
        return now + (self.jump if now - self.began > self.after else 0.0)


class StopwatchTests(unittest.TestCase):

    def test_大きく飛んだ分は数えない(self) -> None:
        clock = [100.0]
        with mock.patch.object(stopwatch.time, "monotonic", lambda: clock[0]):
            watch = stopwatch.Stopwatch()
            clock[0] = 101.0
            self.assertAlmostEqual(watch.elapsed(), 1.0)
            clock[0] = 101.0 + 3600                # 1 時間眠った
            self.assertAlmostEqual(watch.elapsed(), 1.0)
            clock[0] += 2.0
            self.assertAlmostEqual(watch.elapsed(), 3.0)
        self.assertTrue(watch.crossed_sleep)
        self.assertIn("約 60 分", watch.note())

    def test_ふだんの間隔はそのまま数える(self) -> None:
        clock = [0.0]
        with mock.patch.object(stopwatch.time, "monotonic", lambda: clock[0]):
            watch = stopwatch.Stopwatch()
            for _ in range(9):                     # 起動確認の入口は 1 回 10 秒まで
                clock[0] += 10.0
                watch.elapsed()
            self.assertAlmostEqual(watch.elapsed(), 90.0)
        self.assertFalse(watch.crossed_sleep)
        self.assertEqual(watch.note(), "")


class StartAcrossSleepTests(ManagerTestCase):
    """1. 起動を待っているあいだのスリープ。"""

    def make_tool(self, *, delay: float, app_id: str = "nlm.slow"):
        """本体が `delay` 秒たってから待ち受ける (PC が眠ったとき、まだ立ち上がりかけ)。"""
        port = free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port)
        start = root / "start.bat"
        start.write_text(start.read_text().replace("#!/bin/sh\n", f"#!/bin/sh\nsleep {delay}\n"))
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name="遅いツール", port=port,
            start_command=str(start), start_args="--no-browser"))
        return tool_registry.get(app_id), port

    def test_スリープした時間は待った時間に数えない(self) -> None:
        """以前は復帰した瞬間に「起動できませんでした」と出し、プロセスを止めていた。"""
        tool, port = self.make_tool(delay=3)
        with mock.patch("time.monotonic", SleepClock(after=1.0, jump=120.0)):
            self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING, status.detail)
        self.assertIn(tool.app_id, self.manager.running)
        self.assertTrue(health.is_tool(health.probe(f"http://127.0.0.1:{port}/api/health"),
                                       tool.app_id))

    def test_スリープをまたいで起動できなくても止めずに残す(self) -> None:
        """待ち受けないまま時間切れ。ふだんは片付けるが、スリープをまたいだら止めない。"""
        tool, _port = self.make_tool(delay=30)
        ui = app_config.load()["ui"]
        with mock.patch.dict(ui, {"start_timeout_seconds": 2}), \
                mock.patch("time.monotonic", SleepClock(after=0.5, jump=600.0)):
            self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("スリープ", status.detail)
        self.assertIn("止めていません", status.detail)
        self.assertIn(tool.app_id, self.manager.running, "記録を残していません")
        folder = str(Path(tool.start_command).parent)
        self.assertTrue(desktop.processes_in_folder(folder) or
                        self.manager._processes.get(tool.app_id),
                        "立ち上がりかけのツールを止めました")


class WatchAfterSleepTests(ManagerTestCase):
    """3. 見回り: スリープ明けすぐの応答の遅れでは外さない。"""

    def setUp(self) -> None:
        super().setUp()
        self.tool = self.register("nlm.sleepy", "日報")
        self.start(self.tool)
        self.pid = self.manager.running[self.tool.app_id].pid
        self.assertTrue(self.pid)
        self.addCleanup(self._kill)

    def _resume(self) -> None:
        try:
            os.kill(self.pid, signal.SIGCONT)
        except OSError:
            pass

    def _kill(self) -> None:
        """一覧から外れたツールは片付けの対象にならないので、ここで止める。"""
        self._resume()
        process_manager.terminate(self.pid, force=True)

    def freeze(self) -> None:
        """ツールが答えない (スリープ明けで遅れている)。ポートは開いたまま。"""
        os.kill(self.pid, signal.SIGSTOP)

    def test_スリープ明けは応答が無くても外さない(self) -> None:
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        self.manager.poll_health()
        self.freeze()
        self.manager._last_poll = time.monotonic() - 600     # 10 分眠っていた
        for _ in range(limit + 1):
            self.manager.poll_health()
        self.assertIn(self.tool.app_id, self.manager.running,
                      "スリープ明けに、動いているツールを外しました")
        self._resume()
        self.assertTrue(self.manager.poll_health())
        self.assertEqual(self.manager._failures.get(self.tool.app_id), 0)

    def test_ふだんは答えなければ外す(self) -> None:
        """猶予はスリープ明けだけ (これまでどおり)。"""
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        self.manager.poll_health()
        self.freeze()
        for _ in range(limit):
            self.manager.poll_health()
        self.assertNotIn(self.tool.app_id, self.manager.running)

    def test_スリープ明けでも終わったのが確かならすぐ外す(self) -> None:
        self.manager.poll_health()
        process_manager.terminate(self.pid, force=True)
        deadline = time.monotonic() + 5
        while process_manager.is_pid_alive(self.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.manager._last_poll = time.monotonic() - 600
        self.manager.poll_health()
        self.assertNotIn(self.tool.app_id, self.manager.running)


class BrowserFlagTests(unittest.TestCase):
    """2. 専用画面は、裏に回ってもタイマーを遅らせない。"""

    def test_タイマーを間引かせない指定(self) -> None:
        command = browser.build_command("msedge.exe", "http://127.0.0.1:8713/",
                                        Path("profile"))
        for flag in ("--disable-background-timer-throttling",
                     "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding"):
            self.assertIn(flag, command)


if __name__ == "__main__":
    unittest.main()
