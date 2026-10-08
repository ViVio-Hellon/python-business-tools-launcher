"""Start.vbs (Python 版) と exe (Tauri 版) のツールが混ざっているとき

現場では、同じツールの Python 版と exe 版を両方登録しうる (python-web-tools
の梱包資材総合ツールは、同じリポジトリに src-tauri を持つ)。起こりうること:

* **同じポート** … 片方が動いているときにもう片方を押すと、ツールは先に
  動いているほうへ合流するかポートを取れずに終わり、ランチャーは時間切れ
  (90秒) まで待っていた → 押した時点で断り、どれが使っているかを出す
* **片方のフォルダーがもう片方の中** … 外側のツールのプロセスに内側の
  ツールが混ざり、片方を止めると両方止まる → フォルダーで見分けない
* ［設定］で同じポートのツールを保存するとき、知らせる

あわせて、画面をツールが自分でふだんのブラウザーに開くツールを、動いて
いるときに押したら、バーの1行で伝えるだけにする (［詳細 ！］にしない)。
"""
from __future__ import annotations

import dataclasses
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

import app_manager  # noqa: E402
from app_manager import State  # noqa: E402
from launcher import tool_registry  # noqa: E402


class SamePortTests(ManagerTestCase):

    def test_同じポートの別のツールが動いていれば押した時点で断る(self) -> None:
        port = free_port()
        python_version = self.register("fake.packaging", "資材ツール", port=port)
        self.start(python_version)
        self.assertEqual(self.manager.status.state, State.RUNNING)

        root = make_tool_dir(self.work_root, app_id="exe.packaging", port=port,
                             display_name="梱包資材総合ツール")
        tool_registry.save(tool_registry.Tool(
            app_id="exe.packaging", display_name="梱包資材総合ツール", port=port,
            start_command=str(root / "start.bat"), start_args="--no-browser"))
        exe_version = tool_registry.get("exe.packaging")

        began = time.monotonic()
        self.manager._select_blocking(exe_version)
        self.assertLess(time.monotonic() - began, 3, "時間切れまで待たせています")
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("資材ツール", status.detail)
        self.assertIn(f"ポート {port}", status.detail)
        self.assertNotIn("exe.packaging", self.manager._processes, "起動しようとしました")
        self.assertIn("fake.packaging", self.manager.running, "先に動いていたほうを止めました")


class SelfOpenedScreenTests(ManagerTestCase):

    def test_ふだんのブラウザーに出ている画面はバーの1行で伝える(self) -> None:
        tool = self.register("fake.selfopen", "資材ツール")
        tool_registry.save(dataclasses.replace(tool, start_args=""))
        tool = tool_registry.get("fake.selfopen")
        self.assertFalse(tool.suppresses_browser)
        self.start(tool)
        self.manager._select_blocking(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertEqual(status.message, "資材ツールは動いています (画面はブラウザーにあります)")
        self.assertEqual(status.detail, "", "押すたびに［詳細 ！］を出しています")
        self.assertEqual(self.opened, [], "ランチャーが画面を開きました")


class NestedFolderTests(ManagerTestCase):

    def test_片方のフォルダーがもう片方の中ならフォルダーで見分けない(self) -> None:
        outer = self.work_root / "資材"
        inner = outer / "src-tauri" / "target" / "release"
        inner.mkdir(parents=True)
        (outer / "Start.vbs").write_text("' vbs\n", encoding="utf-8")
        (inner / "梱包資材総合ツール.exe").write_bytes(b"MZ")
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.packaging-tool", display_name="資材ツール", port=8713,
            start_command=str(outer / "Start.vbs")))
        tool_registry.save(tool_registry.Tool(
            app_id="exe.packaging", display_name="梱包資材総合ツール",
            start_command=str(inner / "梱包資材総合ツール.exe")))
        for app_id in ("nlm.packaging-tool", "exe.packaging"):
            with self.subTest(app_id=app_id):
                self.assertEqual(app_manager._own_folder(tool_registry.get(app_id)), "")

    def test_となりのフォルダーどうしは見分ける(self) -> None:
        for name in ("資材", "資材2"):            # 名前の頭が同じでも中ではない
            folder = self.work_root / name
            folder.mkdir()
            (folder / "start.bat").write_text("@echo off\n", encoding="utf-8")
            tool_registry.save(tool_registry.Tool(
                app_id=f"nlm.{name}", display_name=name, port=free_port(),
                start_command=str(folder / "start.bat")))
        self.assertTrue(app_manager._own_folder(tool_registry.get("nlm.資材")))
        self.assertTrue(app_manager._own_folder(tool_registry.get("nlm.資材2")))


def _have_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_have_tkinter(), "設定画面の部品が tkinter を読み込む")
class SettingsWarningTests(unittest.TestCase):

    def test_同じポートのツールを保存するとき知らせる(self) -> None:
        from launcher.ui.settings_dialog import _shared_port_warnings

        tools = [tool_registry.Tool(app_id="a", display_name="資材ツール", port=8713),
                 tool_registry.Tool(app_id="b", display_name="梱包資材総合ツール", port=8713),
                 tool_registry.Tool(app_id="c", display_name="日報", port=8733),
                 tool_registry.Tool(app_id="d", display_name="止めたツール", port=8733,
                                    enabled=False),
                 tool_registry.Tool(app_id="e", display_name="窓のアプリ", port=0),
                 tool_registry.Tool(app_id="f", display_name="窓のアプリ2", port=0)]
        warnings = _shared_port_warnings(tools)
        self.assertEqual(len(warnings), 1)
        self.assertIn("資材ツール、梱包資材総合ツール が同じポート 8713", warnings[0])


if __name__ == "__main__":
    unittest.main()
