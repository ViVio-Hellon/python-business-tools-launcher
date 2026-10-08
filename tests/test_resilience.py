"""開いたままにしたときに起きること

ツールを長く開いたままにする運用で、実際に踏む不具合を固定する。

    ・画面を閉じるとツールがまもなく自分から終わる (各ツールの idle_exit)
    ・応答が1回途切れただけで「終了した」と断じない
    ・ツールの出力ログが際限なく育たない
    ・使われなくなった画面プロファイルが残り続けない
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

import app_manager  # noqa: E402
import process_manager  # noqa: E402
from app_manager import State, ToolManager  # noqa: E402
from launcher import app_config, browser, logging_utils  # noqa: E402
from launcher import desktop, runtime_state, tool_registry  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402

_FAKE_BROWSER = Path(__file__).resolve().parent / "_fake_browser.py"


class HealthFailureTests(LocalAreaTestCase):
    """応答が途切れたときの判断 (③)。

    タイムアウトは1.5秒しかない。スリープ復帰直後や、監視レベル2の
    ツールが重い処理をしているあいだは応答が遅れる。**1回で断じると、
    動いているツールを落ちた扱いにして記録まで消す** ── そうなると
    `stop.bat` からも止められなくなる。
    """

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.manager = ToolManager()
        self.running = RunningTool(
            app_id="fake.alive", display_name="日報", pid=1, port=9,
            health_url="http://127.0.0.1:9/api/health")
        self.manager._running = {self.running.app_id: self.running}
        runtime_state.put(self.running)
        self.manager._set(State.RUNNING, "動作中：日報", self.running,
                          responding=True)

    def test_1回の失敗では終了扱いにしない(self) -> None:
        with mock.patch.object(process_manager, "is_running", return_value=False):
            self.assertTrue(self.manager.poll_health())

        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertIsNotNone(runtime_state.read(),
                             "1回の失敗で記録を消しています")
        self.assertIn("fake.alive", self.manager.running)

    def test_続けて失敗すれば終了扱いにする(self) -> None:
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        with mock.patch.object(process_manager, "is_running", return_value=False):
            for _ in range(limit):
                self.manager.poll_health()

        # 画面の手がかりが無いツール。画面を閉じて自分で終わったのと
        # 見分けがつかないので、エラーにはせず「終了しました」と出す
        self.assertNotIn("fake.alive", self.manager.running)
        self.assertIn("日報は終了しました", self.manager.status.message)
        self.assertIsNone(runtime_state.read())

    def test_途中で戻れば数え直す(self) -> None:
        """一時的に応答が遅れただけなら、何事もなかったことにする。"""
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        with mock.patch.object(process_manager, "is_running", return_value=False):
            for _ in range(limit - 1):
                self.manager.poll_health()
        with mock.patch.object(process_manager, "is_running", return_value=True), \
             mock.patch.object(process_manager, "is_browser_open",
                               return_value=True):
            self.manager.poll_health()

        # 数え直されているので、また limit 回ぶん耐える
        with mock.patch.object(process_manager, "is_running", return_value=False):
            for _ in range(limit - 1):
                self.manager.poll_health()
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertIsNotNone(runtime_state.read())


class BrowserClosedTests(LocalAreaTestCase):
    """画面を閉じたときの伝え方 (①)。

    各ツールは「誰も見ていなければ終了する」見張り (idle_exit) を持つ。
    画面の心拍が途切れると数秒で落ちるので、**「画面だけ閉じた状態が
    続く」かのように見せてはいけない。**
    """

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.manager = ToolManager()
        self.running = RunningTool(
            app_id="fake.win", display_name="日報", pid=1, port=9,
            health_url="http://127.0.0.1:9/api/health",
            browser_pid=1234, browser_profile=str(self.work_root / "prof"))
        self.manager._running = {self.running.app_id: self.running}
        # ランチャーが開いた画面 (開いたことを見ている)
        self.manager._browser_seen[self.running.app_id] = True

    def test_まもなく終了することを伝える(self) -> None:
        with mock.patch.object(process_manager, "is_running", return_value=True), \
             mock.patch.object(process_manager, "is_browser_open",
                               return_value=False):
            self.manager.poll_health()

        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertFalse(status.browser_open)
        self.assertIn("まもなく自動で終了", status.detail)
        self.assertIn("もう一度ボタンを押して", status.detail)

    def test_画面が開いているあいだは何も出さない(self) -> None:
        with mock.patch.object(process_manager, "is_running", return_value=True), \
             mock.patch.object(process_manager, "is_browser_open",
                               return_value=True):
            self.manager.poll_health()
        self.assertEqual(self.manager.status.detail, "")


class AdoptedToolTests(LocalAreaTestCase):
    """ランチャー外で起動されたツールを引き継いだとき (⑤)。"""

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.addCleanup(self._stop)
        self.proc = None

    def _stop(self) -> None:
        for running in runtime_state.read_all().values():
            process_manager.stop(running, force=True, timeout=5)
        if self.proc is not None:
            # start.bat の下の本体ごと止める (start.bat だけ止めると本体が残る)
            for pid in desktop.process_tree({self.proc.pid}):
                process_manager._terminate(pid, force=True)
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)

    def test_画面を閉じられないことを伝える(self) -> None:
        """**黙っていると「閉じたはずの画面が残っている」と見える。**"""
        import subprocess

        port = free_port()
        root = make_tool_dir(self.work_root, app_id="fake.adopt", port=port,
                             display_name="日報")
        tool_registry.save(tool_registry.Tool(
            app_id="fake.adopt", display_name="日報", port=port,
            start_command=str(root / "start.bat")))

        self.proc = subprocess.Popen([str(root / "start.bat")],
                                     stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        manager = ToolManager()
        while time.monotonic() < deadline:
            if manager.adopt_running():
                break
            time.sleep(0.1)

        self.assertEqual(manager.status.state, State.RUNNING)
        self.assertFalse(manager.current.browser_managed)
        self.assertIn("手で閉じて", manager.status.detail)


class LogRotationTests(LocalAreaTestCase):
    """ツールの出力ログが際限なく育たないこと (②)。"""

    def test_大きくなったら退ける(self) -> None:
        path = app_config.local_dir("logs") / "tool_fake.out.log"
        path.write_text("x" * 2048, encoding="utf-8")

        logging_utils.rotate_if_large(path, max_bytes=1024, keep=2)

        self.assertFalse(path.exists(), "本体が退けられていません")
        self.assertTrue(path.with_suffix(".log.1").exists())

    def test_小さければ触らない(self) -> None:
        path = app_config.local_dir("logs") / "tool_fake.out.log"
        path.write_text("小さい", encoding="utf-8")
        logging_utils.rotate_if_large(path, max_bytes=1024, keep=2)
        self.assertTrue(path.exists())

    def test_世代の上限を守る(self) -> None:
        path = app_config.local_dir("logs") / "tool_fake.out.log"
        for round_no in range(4):
            path.write_text(f"{round_no}" * 2048, encoding="utf-8")
            logging_utils.rotate_if_large(path, max_bytes=1024, keep=2)

        kept = sorted(p.name for p in
                      app_config.local_dir("logs").glob("tool_fake.out.log*"))
        self.assertEqual(kept, ["tool_fake.out.log.1", "tool_fake.out.log.2"])

    def test_古いツールログも片付け対象(self) -> None:
        """日付で分かれないので、日数での片付けからも漏らさない。"""
        self.assertIn("tool_*.out.log", logging_utils.LOG_PATTERNS)
        self.assertIn("launcher_*.log", logging_utils.LOG_PATTERNS)

    def test_無い場所でも失敗しない(self) -> None:
        logging_utils.rotate_if_large(self.work_root / "ない.log",
                                      max_bytes=1, keep=2)


class ProfilePurgeTests(LocalAreaTestCase):
    """使われなくなった画面プロファイル (④)。"""

    def make_profile(self, app_id: str) -> Path:
        path = browser.profile_dir(app_id)
        path.mkdir(parents=True, exist_ok=True)
        (path / "Default").mkdir(exist_ok=True)
        (path / "Default" / "Cache").write_text("x" * 100, encoding="utf-8")
        return path

    def test_登録の無いものだけ消す(self) -> None:
        keep = self.make_profile("nlm.nippou-tool")
        drop = self.make_profile("nlm.retired")

        removed = browser.purge_unused(["nlm.nippou-tool"])

        self.assertTrue(keep.exists(), "使っているツールのぶんを消しました")
        self.assertFalse(drop.exists())
        self.assertEqual(removed, [drop.name])

    def test_消すものが無ければ何もしない(self) -> None:
        self.make_profile("nlm.nippou-tool")
        self.assertEqual(browser.purge_unused(["nlm.nippou-tool"]), [])

    def test_フォルダーが無くても失敗しない(self) -> None:
        self.assertEqual(browser.purge_unused(["x"]), [])


if __name__ == "__main__":
    unittest.main()
