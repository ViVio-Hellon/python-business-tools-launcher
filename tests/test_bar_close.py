"""ランチャーを閉じるときの確かめ (「終了」ボタンと窓の ×)

画面 (tkinter) と表示先が要る。無い環境では飛ばす (判断の中身は
`test_bar_geometry.CloseTextTests` が tkinter 無しで確かめる)。

確かめること:

* × と「終了」は**同じ確かめ**を通る
* 確かめの既定のボタンは「はい」(全部止める) ではない
* 「いいえ」なら止めずに閉じる、「キャンセル」なら閉じない
* 起動の最中のツールがあれば、それも聞く (以前は聞かずに起動をやめていた)
* 設定画面などを開いているあいだは、× で閉じない
  (× は「終了」ボタンと違い、そのあいだも押せる)
* 止められなかったときの「中断しますか」の既定は「いいえ」
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, release_tk  # noqa: E402


def _can_show_windows() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001 - tkinter が無い・画面が無い
        return False


HAVE_TK = _can_show_windows()


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class CloseTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter

        from app_manager import ToolManager
        from launcher.runtime_state import RunningTool
        from launcher.ui import bar as bar_module

        self.tk = tkinter
        self.bar_module = bar_module
        self.RunningTool = RunningTool
        self.manager = ToolManager()
        self.shutdown = mock.patch.object(self.manager, "shutdown",
                                          return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.ask = mock.patch.object(bar_module.messagebox, "askyesnocancel",
                                     return_value=None).start()
        self.ask_force = mock.patch.object(bar_module.messagebox, "askyesno",
                                           return_value=False).start()
        mock.patch.object(bar_module.messagebox, "showerror").start()
        self.bar = None

    def tearDown(self) -> None:
        if self.bar is not None and self.alive():
            self.bar.root.destroy()
        super().tearDown()

    def make_bar(self):
        self.bar = self.bar_module.LauncherBar(self.manager)
        self.bar.root.update()
        return self.bar

    def run_tool(self, app_id: str = "nlm.daily", name: str = "日報") -> None:
        self.manager._running[app_id] = self.RunningTool(app_id=app_id,
                                                         display_name=name)

    def alive(self) -> bool:
        try:
            return bool(self.bar.root.winfo_exists())
        except self.tk.TclError:
            return False

    def press_x(self) -> None:
        """窓の × (Alt+F4・タスクバーの「閉じる」も同じ通知)。"""
        handler = self.bar.root.tk.call("wm", "protocol", self.bar.root,
                                        "WM_DELETE_WINDOW")
        self.assertTrue(handler, "× に何も結ばれていません")
        self.bar.root.tk.eval(handler)

    def press_quit(self) -> None:
        """バーの「終了」ボタン。"""
        buttons = [w for w in self._walk(self.bar.root)
                   if isinstance(w, self.tk.Button) and w.cget("text") == "終了"]
        self.assertEqual(len(buttons), 1)
        buttons[0].invoke()

    def _walk(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk(child)

    # --------------------------------------------------------------
    def test_窓の閉じるボタンと終了ボタンは同じ確かめを通る(self) -> None:
        self.run_tool()
        self.make_bar()
        self.press_quit()
        self.press_x()
        self.assertEqual(self.ask.call_count, 2)
        first, second = self.ask.call_args_list
        self.assertEqual(first, second, "× と「終了」で確かめが違います")
        self.assertTrue(self.alive(), "キャンセルしたのに閉じました")

    def test_既定のボタンははいではない(self) -> None:
        """うっかり Enter を押しただけで、全部のツールが止まらないように。"""
        self.run_tool()
        self.make_bar()
        self.press_x()
        self.assertEqual(self.ask.call_args.kwargs.get("default"), "cancel")

    def test_いいえなら止めずに閉じる(self) -> None:
        self.run_tool()
        self.ask.return_value = False
        self.make_bar()
        self.press_x()
        self.shutdown.assert_called_once_with(stop_tools=False)
        self.assertFalse(self.alive())

    def test_はいなら止めてから閉じる(self) -> None:
        self.run_tool()
        self.ask.return_value = True
        self.make_bar()
        self.press_quit()
        self.shutdown.assert_called_once_with(stop_tools=True)
        self.assertFalse(self.alive())

    def test_キャンセルなら閉じない(self) -> None:
        self.run_tool()
        self.make_bar()
        self.press_x()
        self.shutdown.assert_not_called()
        self.assertTrue(self.alive())

    def test_何も動いていなければ聞かずに閉じる(self) -> None:
        self.make_bar()
        self.press_x()
        self.ask.assert_not_called()
        self.shutdown.assert_called_once_with(stop_tools=False)
        self.assertFalse(self.alive())

    def test_起動の最中のツールも聞く(self) -> None:
        self.manager._starting.add("nlm.kanban")
        self.make_bar()
        self.press_x()
        self.ask.assert_called_once()
        self.assertIn("起動中のツール", self.ask.call_args.args[1])
        self.assertTrue(self.alive())

    def test_設定画面を開いているあいだは窓の閉じるボタンで閉じない(self) -> None:
        """× は、設定画面などが前を塞いでいても押せる (「終了」ボタンは押せない)。"""
        self.run_tool()
        self.ask.return_value = False
        self.make_bar()
        dialog = self.tk.Toplevel(self.bar.root)
        dialog.transient(self.bar.root)
        dialog.update()
        dialog.grab_set()
        self.press_x()
        self.ask.assert_not_called()
        self.assertTrue(self.alive())

        dialog.grab_release()
        dialog.destroy()
        self.press_x()
        self.ask.assert_called_once()
        self.assertFalse(self.alive())

    def test_続けて押しても確かめは1回(self) -> None:
        self.run_tool()
        self.make_bar()
        self.ask.side_effect = lambda *a, **k: self.press_x()   # 問いの最中にもう一度
        self.press_x()
        self.assertEqual(self.ask.call_count, 1)

    def test_止められなければ中断の既定はいいえ(self) -> None:
        self.run_tool()
        self.ask.return_value = True
        self.shutdown.return_value = False         # 実行中の処理があった
        self.make_bar()
        self.press_x()
        kwargs = self.ask_force.call_args.kwargs
        self.assertEqual(kwargs.get("default"), "no")
        self.assertEqual(kwargs.get("icon"), "warning")
        # 「いいえ」: 中断しない。ランチャーも閉じない (問いの文のとおり)
        self.assertIn("ツールもランチャーもそのまま", self.ask_force.call_args.args[1])
        self.shutdown.assert_called_once_with(stop_tools=True)
        self.assertTrue(self.alive())


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class StartupDisplayTests(LocalAreaTestCase):
    """起動の下ごしらえで決まったことを、バーが最初から出す。"""

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        from app_manager import ToolManager
        from launcher.ui import bar as bar_module

        self.manager = ToolManager()
        self.bar_module = bar_module
        self.bar = None

    def tearDown(self) -> None:
        if self.bar is not None:
            try:
                self.bar.root.destroy()
            except Exception:                 # noqa: BLE001
                pass
        super().tearDown()

    def test_起動ファイルを確かめられない案内を最初から出す(self) -> None:
        self.manager.notify("次のツールの起動ファイルを確かめられません: 日報")
        self.bar = self.bar_module.LauncherBar(self.manager)
        self.bar.root.update()
        self.assertIn("確かめられません", self.bar._last_detail)
        self.assertTrue(self.bar.detail_button.winfo_ismapped())

    def test_確かめられない起動ファイルを未設定と塗らない(self) -> None:
        from launcher import tool_registry

        old = os.path.join(os.sep, "old-share", "日報", "start.bat")
        tool_registry.save(tool_registry.Tool(app_id="nlm.daily", display_name="日報",
                                              port=8733, start_command=old))
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if os.fspath(path).startswith(os.path.join(os.sep, "old-share")):
                raise PermissionError(13, "アクセスが拒否されました")
            return real_stat(path, *args, **kwargs)

        with mock.patch("os.stat", denied):
            self.bar = self.bar_module.LauncherBar(self.manager)   # 以前は例外
        self.assertTrue(self.bar._configured["nlm.daily"])


if __name__ == "__main__":
    unittest.main()
