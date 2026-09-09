"""どの版が入っていて、どの版が動いているか

現場でいちばん困るのは「入れ替えたはずなのに直っていない」状態。
置いてある版と動いている版を並べて出せることを確かめる。
"""
from __future__ import annotations

import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

import process_manager  # noqa: E402
from launcher import app_config, health, tool_registry, version_info  # noqa: E402


class LauncherVersionTests(LocalAreaTestCase):
    """ランチャー自身の版。"""

    def test_版と居場所を出せる(self) -> None:
        info = version_info.launcher()
        self.assertEqual(info.version, app_config.version_label())
        self.assertTrue(info.version.startswith("v"))
        self.assertEqual(info.app_root, str(app_config.APP_ROOT))
        self.assertIn(sys.version.split()[0], info.python)
        self.assertEqual(info.problem, "")

    def test_版の書き方がおかしければ知らせる(self) -> None:
        """並べて比べられない書き方は、起動は止めずに知らせる。"""
        raw = app_config.load()
        original = raw["version"]
        try:
            for bad in ("", "1.0", "v1.0.0", "最新"):
                with self.subTest(bad):
                    raw["version"] = bad
                    self.assertNotEqual(app_config.version_problem(), "")
            raw["version"] = "2.10.3"
            self.assertEqual(app_config.version_problem(), "")
        finally:
            raw["version"] = original


class InstalledVersionTests(LocalAreaTestCase):
    """置いてある版 (`config/app.json`)。"""

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()

    def register(self, app_id: str, name: str, **kwargs):
        port = kwargs.pop("port", None) or free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port,
                             display_name=name, **kwargs)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name, port=port,
            start_command=str(root / "start.bat"), start_args="--no-browser"))
        return tool_registry.get(app_id), root

    def find(self, app_id: str, **kwargs):
        for item in version_info.tools(**kwargs):
            if item.app_id == app_id:
                return item
        self.fail(f"{app_id} が一覧にありません")

    def test_停止中でも置いてある版が分かる(self) -> None:
        """**動かさずに版を知れること。** 起動しないと分からないのでは遅い。"""
        self.register("fake.v", "日報", version="3.2.1")
        item = self.find("fake.v", probe_running=False)

        self.assertEqual(item.installed, "3.2.1")
        self.assertEqual(item.running, "")
        self.assertEqual(item.state, "停止中")
        self.assertEqual(item.version_text, "3.2.1")
        self.assertFalse(item.mismatched)

    def test_未設定のツールは未設定と出る(self) -> None:
        item = self.find("nlm.nippou-tool", probe_running=False)
        self.assertEqual(item.state, "未設定")
        self.assertEqual(item.version_text, "—")

    def test_設定を読めなくても一覧には出る(self) -> None:
        root = self.work_root / "no-config"
        root.mkdir(parents=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        tool_registry.save(tool_registry.Tool(
            app_id="fake.bare", display_name="素", port=free_port(),
            start_command=str(root / "start.bat")))

        item = self.find("fake.bare", probe_running=False)
        self.assertEqual(item.installed, "")
        self.assertEqual(item.version_text, "—")


class RunningVersionTests(LocalAreaTestCase):
    """動いている版 (`/api/health`)。"""

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.addCleanup(self._stop_all)
        self.running: list = []

    def _stop_all(self) -> None:
        from launcher.runtime_state import RunningTool
        for app_id, port in self.running:
            process_manager.stop(
                RunningTool(app_id=app_id, port=port,
                            health_url=f"http://127.0.0.1:{port}/api/health"),
                force=True, timeout=5)

    def launch(self, app_id: str, name: str, *, version: str,
               installed_version: str | None = None):
        import subprocess
        port = free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port,
                             display_name=name, version=version,
                             installed_version=installed_version)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name, port=port,
            start_command=str(root / "start.bat")))

        proc = subprocess.Popen([str(root / "start.bat")],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, proc)
        self.running.append((app_id, port))

        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if health.is_tool(health.probe(url), app_id):
                return port
            time.sleep(0.05)
        self.fail(f"{app_id} が起動しませんでした")

    @staticmethod
    def _reap(proc) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def find(self, app_id: str):
        for item in version_info.tools():
            if item.app_id == app_id:
                return item
        self.fail(f"{app_id} が一覧にありません")

    def test_動いている版が分かる(self) -> None:
        self.launch("fake.run", "日報", version="3.0.0")
        item = self.find("fake.run")

        self.assertTrue(item.alive)
        self.assertEqual(item.running, "3.0.0")
        self.assertEqual(item.installed, "3.0.0")
        self.assertEqual(item.state, "動作中")
        self.assertFalse(item.mismatched)

    def test_入れ替えたのに古いプロセスが残っていると分かる(self) -> None:
        """**この試験がこの機能の目的。**

        フォルダーには新しい版が置かれているのに、動いているのは古い版。
        並べて出さないと、端末まで見に行くことになる。
        """
        self.launch("fake.stale", "日報",
                    version="3.0.0", installed_version="3.1.0")
        item = self.find("fake.stale")

        self.assertTrue(item.mismatched)
        self.assertEqual(item.state, "版ちがい")
        self.assertIn("3.1.0", item.version_text)
        self.assertIn("3.0.0", item.version_text)

        # コンソールにも直し方まで出る
        text = version_info.describe()
        self.assertIn("版ちがい", text)
        self.assertIn("stop.bat", text)

    def test_動いているほうの場所を出す(self) -> None:
        """設定と違うフォルダーが動いていることがある。それ自体が答え。"""
        port = self.launch("fake.where", "日報", version="1.0.0")
        item = self.find("fake.where")
        self.assertTrue(item.app_root)
        self.assertEqual(item.port, port)


class DescribeTests(LocalAreaTestCase):
    """コンソールに出す1枚。"""

    def test_全角の桁が揃う(self) -> None:
        """「日報」と「カレンダー」が同じ列に出ること。"""
        self.assertEqual(version_info.display_width("日報"), 4)
        self.assertEqual(version_info.display_width("カレンダー"), 10)
        self.assertEqual(version_info.display_width("abc"), 3)
        self.assertEqual(version_info.display_width(""), 0)
        for text in ("日報", "カレンダー", "abc"):
            with self.subTest(text):
                self.assertEqual(
                    version_info.display_width(version_info.pad(text, 12)), 12)

    def test_ツールが無くても出せる(self) -> None:
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        text = version_info.describe(probe_running=False)
        self.assertIn("バージョン情報", text)
        self.assertIn("登録されているツールがありません", text)


if __name__ == "__main__":
    unittest.main()
