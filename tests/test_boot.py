"""ランチャー自身の起動 (起動中の窓に出す段)"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

import boot  # noqa: E402
import launch_guard  # noqa: E402
from app_manager import ToolManager  # noqa: E402
from launcher import distribution, health, tool_registry  # noqa: E402


class BootTests(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(launch_guard.remove_lock)
        self.reported: list[int] = []

    def test_段が順に進む(self) -> None:
        result = boot.run(self.reported.append)
        self.assertTrue(result.started)
        self.assertEqual(self.reported,
                         [boot.GUARD, boot.SETTINGS, boot.ADOPT, boot.BAR])
        self.assertIsInstance(result.manager, ToolManager)
        # どこが遅いかを後から追えるよう、段ごとの秒を残す
        self.assertEqual([name for name, _ in result.timings], list(boot.STEPS))
        # ロックを持ったまま返す (外すのはバーを閉じたとき)
        self.assertTrue(launch_guard.lock_path().exists())

    def test_二重起動なら最初の段で止まる(self) -> None:
        refused = launch_guard.GuardResult(should_start=False,
                                           reason="すでに動いています")
        with mock.patch.object(launch_guard, "acquire", return_value=refused), \
                mock.patch.object(tool_registry, "initialize") as initialize:
            result = boot.run(self.reported.append)
        self.assertFalse(result.started)
        self.assertEqual(result.reason, "すでに動いています")
        self.assertEqual(self.reported, [boot.GUARD])
        initialize.assert_not_called()

    def test_知らせは返すだけでダイアログは出さない(self) -> None:
        """別スレッドから tkinter を触ると落ちる。窓を閉じてから出す。"""
        distribution.folder().mkdir(parents=True, exist_ok=True)
        distribution.settings_path().write_text("{broken", encoding="utf-8")
        result = boot.run()
        titles = [title for title, _ in result.warnings]
        self.assertIn("配布先フォルダの設定を読めません", titles)
        self.assertTrue(result.started)

    def test_途中で失敗したらロックを外す(self) -> None:
        with mock.patch.object(ToolManager, "adopt_running",
                               side_effect=RuntimeError("壊れた")):
            with self.assertRaises(RuntimeError):
                boot.run()
        self.assertFalse(launch_guard.lock_path().exists())

    def test_別スレッドで回しても段が届く(self) -> None:
        """起動中の窓と同じ回し方 (別スレッド + キュー)。"""
        import queue
        import threading

        steps: queue.Queue = queue.Queue()
        box = {}
        thread = threading.Thread(target=lambda: box.update(r=boot.run(steps.put)))
        thread.start()
        thread.join(30)
        got = []
        while not steps.empty():
            got.append(steps.get())
        self.assertEqual(got, [0, 1, 2, 3])
        self.assertTrue(box["r"].started)


class ParallelProbeTests(LocalAreaTestCase):
    """動いているツールを探すとき、ツールの数だけ待たない。"""

    def test_同時に当たる(self) -> None:
        tool_registry.initialize()
        tools = tool_registry.all_tools()
        self.assertGreaterEqual(len(tools), 4)

        def slow_probe(url, **kwargs):
            time.sleep(0.4)             # 閉じたポートで断られるまでの待ち
            return None

        with mock.patch.object(health, "probe", side_effect=slow_probe):
            began = time.monotonic()
            ToolManager().adopt_running()
            took = time.monotonic() - began
        # 1つずつなら 0.4 × ツール数 (4つで1.6秒)。同時なら 0.4秒ほど
        self.assertLess(took, 0.4 * len(tools) * 0.6,
                        f"{len(tools)}ツールで {took:.2f}秒かかりました")

    def test_動いているものをすべて引き継ぐ(self) -> None:
        """ツールは同時に動く。応答したものはすべて引き継ぐ。"""
        tool_registry.initialize()
        tools = tool_registry.all_tools()
        second, third = tools[1], tools[2]

        def probe(url, **kwargs):
            for tool in (second, third):
                if url == tool.health_url:
                    return {"app_id": tool.app_id, "ready": True,
                            "pid": 1, "port": tool.port}
            return None

        with mock.patch.object(health, "probe", side_effect=probe):
            adopted = ToolManager().adopt_running()
        self.assertEqual([r.app_id for r in adopted],
                         [second.app_id, third.app_id])


if __name__ == "__main__":
    unittest.main()
