"""ツールが多いときのバー:「選ぶ ▼」と［開く］(1.8.4)

ボタンを全部並べるとバーが画面の端まで伸びる (現場の報告)。決めた数を超えたら、
1つ選んで［開く］にする。**選んだだけでは起動しない。**
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, have_tk, release_tk  # noqa: E402
from test_ui_flows import make_web_tool  # noqa: E402

from launcher import tool_registry  # noqa: E402


class ThresholdTests(LocalAreaTestCase):

    def test_既定は6個を超えたら(self) -> None:
        self.assertEqual(tool_registry.bar_select_over(), 6)
        self.assertFalse(tool_registry.uses_select(6))
        self.assertTrue(tool_registry.uses_select(7))

    def test_0ならいつもボタン(self) -> None:
        tool_registry.set_bar_select_over(0)
        self.assertFalse(tool_registry.uses_select(30))


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class SelectBarTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        for index in range(8):
            start = make_web_tool(self.work_root, f"t{index}", app_id=f"nlm.t{index}",
                                  port=8700 + index)
            tool_registry.save(tool_registry.Tool(
                app_id=f"nlm.t{index}", display_name=f"ツール{index}", port=8700 + index,
                start_command=str(start), order_no=10 * (index + 1)))

    def make_bar(self):
        from app_manager import ToolManager
        from launcher.ui import bar as bar_module

        manager = ToolManager()
        select = mock.patch.object(manager, "select").start()
        self.addCleanup(mock.patch.stopall)
        bar = bar_module.LauncherBar(manager)
        self.addCleanup(lambda: bar.root.destroy())
        bar.root.update()
        return bar, select

    def test_多ければ選ぶ形になり選んだだけでは起動しない(self) -> None:
        bar, select = self.make_bar()
        self.assertIsNotNone(bar.selector)
        self.assertTrue(bar.selector.winfo_ismapped())
        self.assertFalse(any(b.winfo_ismapped() for b in bar.buttons.values()))
        bar._fill_selector_menu()
        self.assertEqual(bar.selector_menu.index("end"), 7)
        bar.selector_menu.invoke(3)
        select.assert_not_called()
        self.assertEqual(bar.selected_var.get(), "nlm.t3")
        self.assertIn("ツール3", bar.selector.cget("text"))
        bar.open_selected()
        select.assert_called_once_with("nlm.t3")
        # 次に起動したときも同じものを出しておく
        self.assertEqual(tool_registry.get_pc_setting(tool_registry.BAR_SELECTED_KEY),
                         "nlm.t3")

    def test_動いているツールは印を付ける(self) -> None:
        from app_manager import State, Status

        bar, _ = self.make_bar()
        bar.selected_var.set("nlm.t2")
        bar._paint_buttons(Status(state=State.RUNNING, app_id="nlm.t2",
                                  running_ids=("nlm.t2",)))
        self.assertTrue(bar.selector.cget("text").startswith("● ツール2"))
        self.assertEqual(bar.open_button.cget("text"), "前に出す")

    def test_決めた数以下ならボタンを並べる(self) -> None:
        tool_registry.set_bar_select_over(10)
        bar, select = self.make_bar()
        self.assertIsNone(bar.selector)
        self.assertTrue(all(b.winfo_ismapped() for b in bar.buttons.values()))


if __name__ == "__main__":
    unittest.main()
