"""ツール設定の保存と保存時チェック (要件定義書 §13 / §14 / §15)"""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import app_config, tool_registry  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402


class SeedTests(LocalAreaTestCase):
    """初回起動で既定のツールが入る。"""

    def test_4つのツールが登録される(self) -> None:
        tool_registry.initialize()
        names = [t.display_name for t in tool_registry.all_tools()]
        self.assertEqual(names, ["日報", "カレンダー", "看板", "総合ツール"],
                         "並び順が要件定義書 §6 と違います")

    def test_startbatは既定では空(self) -> None:
        """端末ごとに違うので、もっともらしいパスを入れない。"""
        tool_registry.initialize()
        for tool in tool_registry.all_tools():
            with self.subTest(tool.display_name):
                self.assertEqual(tool.start_command, "")
                self.assertFalse(tool.is_configured)

    def test_初回投入は一度だけ(self) -> None:
        """利用者が消したツールが、次の起動で復活しないこと。"""
        tool_registry.initialize()
        first = tool_registry.get("nlm.nippou-tool")
        tool_registry.save(replace(first, display_name="日報(改)"))

        tool_registry.initialize()
        self.assertEqual(tool_registry.get("nlm.nippou-tool").display_name,
                         "日報(改)")

    def test_起動確認URLがポートから決まる(self) -> None:
        tool_registry.initialize()
        tool = tool_registry.get("nlm.nippou-tool")
        self.assertEqual(tool.health_url, "http://127.0.0.1:8733/api/health")
        self.assertEqual(tool.home_url, "http://127.0.0.1:8733/")

    def test_起動確認URLは差し替えられる(self) -> None:
        """要件定義書 §14「起動確認URL」を設定として持てること。"""
        tool_registry.initialize()
        tool = tool_registry.get("nlm.nippou-tool")
        tool_registry.save(replace(tool, health_url_override="http://127.0.0.1:9/health"))
        self.assertEqual(tool_registry.get("nlm.nippou-tool").health_url,
                         "http://127.0.0.1:9/health")


class ValidationTests(LocalAreaTestCase):
    """要件定義書 §13.2 保存時チェック。"""

    def test_実在するbatは通る(self) -> None:
        path = self.work_root / "start.bat"
        path.write_text("@echo off\n", encoding="utf-8")
        self.assertEqual(tool_registry.validate_start_command(str(path)), "")

    def test_存在しないファイルは弾く(self) -> None:
        path = self.work_root / "ない.bat"
        problem = tool_registry.validate_start_command(str(path))
        self.assertIn("見つかりません", problem)

    def test_bat以外は弾く(self) -> None:
        path = self.work_root / "start.exe"
        path.write_text("x", encoding="utf-8")
        problem = tool_registry.validate_start_command(str(path))
        self.assertIn(".bat", problem)

    def test_相対パスは弾く(self) -> None:
        """端末や起動元によって指す先が変わってしまうため。"""
        problem = tool_registry.validate_start_command("start.bat")
        self.assertIn("絶対パス", problem)

    def test_未設定は許す(self) -> None:
        """4つ全部を入れていない端末でもランチャーを使えるようにする。"""
        self.assertEqual(tool_registry.validate_start_command(""), "")
        self.assertEqual(tool_registry.validate_start_command("   "), "")

    def test_引用符を外して扱う(self) -> None:
        """エクスプローラの「パスのコピー」は引用符付きで入る。"""
        path = self.work_root / "start.bat"
        path.write_text("@echo off\n", encoding="utf-8")
        self.assertEqual(tool_registry.validate_start_command(f'"{path}"'), "")
        tool_registry.initialize()
        tool = tool_registry.set_start_command("nlm.nippou-tool", f'"{path}"')
        self.assertEqual(tool.start_command, str(path))

    def test_ポートの範囲を見る(self) -> None:
        self.assertEqual(tool_registry.validate_port(8733), "")
        self.assertEqual(tool_registry.validate_port(0), "")
        self.assertIn("65535", tool_registry.validate_port(70000))
        self.assertIn("65535", tool_registry.validate_port(-1))


class WorkDirTests(LocalAreaTestCase):
    """作業ディレクトリ (要件定義書 §14)。"""

    def test_未指定ならbatの置き場所(self) -> None:
        tool = Tool(app_id="x", display_name="日報",
                    start_command=str(self.work_root / "日報" / "start.bat"))
        self.assertEqual(tool.resolved_work_dir,
                         str(self.work_root / "日報"))

    def test_指定があればそちら(self) -> None:
        tool = Tool(app_id="x", display_name="日報",
                    start_command=str(self.work_root / "日報" / "start.bat"),
                    work_dir=str(self.work_root))
        self.assertEqual(tool.resolved_work_dir, str(self.work_root))


class ExtensibilityTests(LocalAreaTestCase):
    """要件定義書 §14「将来5個目、6個目のツールを追加しやすい構造」。"""

    def test_5個目を足せる(self) -> None:
        tool_registry.initialize()
        tool_registry.save(Tool(app_id="nlm.new-tool", display_name="新ツール",
                                order_no=50, port=8800))
        tools = tool_registry.all_tools()
        self.assertEqual(len(tools), 5)
        self.assertEqual(tools[-1].display_name, "新ツール")

    def test_使わないツールは隠せる(self) -> None:
        tool_registry.initialize()
        tool = tool_registry.get("nlm.packaging-tool")
        tool_registry.save(replace(tool, enabled=False))
        self.assertNotIn("総合ツール",
                         [t.display_name for t in tool_registry.all_tools()])
        self.assertIn("総合ツール",
                      [t.display_name
                       for t in tool_registry.all_tools(include_disabled=True)])


class PcSettingTests(LocalAreaTestCase):
    """要件定義書 §15 PCごとのモード設定。"""

    def test_モードを保存して読み直せる(self) -> None:
        tool_registry.initialize()
        self.assertEqual(tool_registry.pc_mode(), "")
        tool_registry.set_pc_mode("中板")
        self.assertEqual(tool_registry.pc_mode(), "中板")
        # 保存済みを使う。毎回選ばせない
        tool_registry.set_pc_mode("小板")
        self.assertEqual(tool_registry.pc_mode(), "小板")


class BackupTests(LocalAreaTestCase):
    """設定の控え (基盤仕様書 4.6)。"""

    def test_控えを取れる(self) -> None:
        tool_registry.initialize()
        backup = tool_registry.backup()
        self.assertIsNotNone(backup)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.parent, app_config.local_dir("backup"))


if __name__ == "__main__":
    unittest.main()
