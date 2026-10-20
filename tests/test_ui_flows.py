"""画面の操作の流れ (1.7.9 で足した試験。本体は変えていない)

構造調査で、試験で通っていない大事な道として挙がったもの:

* ［設定］の保存 (入力の確かめ・警告・削除の確かめ・バーの位置・このPCのモード)
* ［＋ ツールを追加］・［参照］・［配布先フォルダを作る］
* バーの［ツール停止］ (1つのとき・断られたとき・2つ以上のときの選び方)

画面の部品をそのまま動かし、ダイアログ (messagebox・filedialog) だけを
差し替える。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, release_tk  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

from launcher import distribution, tool_registry  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402


def _have_tk() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001
        return False


HAVE_TK = _have_tk()


def make_web_tool(base: Path, name: str, *, app_id: str, port: int,
                  exe: str = "") -> Path:
    """config/app.json のある Web のツール。起動ファイル (start.bat) の道を返す。"""
    root = base / name
    (root / "config").mkdir(parents=True)
    (root / "config" / "app.json").write_text(json.dumps(
        {"app_id": app_id, "display_name": name, "server": {"port": port}},
        ensure_ascii=False), encoding="utf-8")
    start = root / "start.bat"
    start.write_text("@echo off\r\npython start_app.py %*\r\n", encoding="utf-8")
    (root / "start_app.py").write_text('p.add_argument("--no-browser")\n', encoding="utf-8")
    if exe:
        (root / exe).write_bytes(b"MZ" + b"\x00" * 64)
    return start


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class SettingsSaveTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter as tk

        from launcher.ui import settings_dialog

        self.module = settings_dialog
        self.tk = tk
        self.parent = tk.Tk()
        self.addCleanup(self.parent.destroy)
        self.parent.geometry("600x56+300+700")
        self.daily = make_web_tool(self.work_root, "日報", app_id="nlm.nippou", port=8733)
        self.board = make_web_tool(self.work_root, "看板", app_id="nlm.kanban", port=8734)
        # 試験の既定の4ツール (_sample_tools) は使わない。この2つだけにする
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        tool_registry.save(Tool(app_id="nlm.nippou", display_name="日報", port=8733,
                                start_command=str(self.daily), order_no=10))
        tool_registry.save(Tool(app_id="nlm.kanban", display_name="看板", port=8734,
                                start_command=str(self.board), order_no=20))
        self.errors: list[str] = []
        self.questions: list[str] = []
        self.answer = True
        patches = [
            mock.patch.object(settings_dialog.messagebox, "showerror",
                              side_effect=lambda title, text, **_: self.errors.append(text)),
            mock.patch.object(settings_dialog.messagebox, "showinfo"),
            mock.patch.object(settings_dialog.messagebox, "askyesno",
                              side_effect=self._ask),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _ask(self, title, text, **_):
        self.questions.append(text)
        return self.answer

    def run_dialog(self, scenario) -> object:
        """設定画面を開き、`scenario(dialog)` を実行して閉じる。開いた画面を返す。"""
        holder = {}
        real_center = self.module.SettingsDialog._center_on

        def capture(dialog, parent):
            holder["dialog"] = dialog
            real_center(dialog, parent)

        def wait(top):
            top.update()
            try:
                scenario(holder["dialog"])
            finally:
                if top.winfo_exists():
                    top.destroy()

        with mock.patch.object(self.module.SettingsDialog, "_center_on", capture), \
                mock.patch.object(self.tk.Toplevel, "wait_window", wait), \
                mock.patch.object(self.tk.Toplevel, "grab_set", lambda self: None):
            self.module.SettingsDialog(self.parent)
        return holder["dialog"]

    @staticmethod
    def row(dialog, app_id: str):
        return next(r for r in dialog.rows if r.tool.app_id == app_id)

    def test_変えた値を保存する(self) -> None:
        def scenario(dialog):
            row = self.row(dialog, "nlm.nippou")
            row.name_var.set("日報 (現場)")
            row.port_var.set("8735")
            row.stop_var.set("stop_bat")
            dialog.idle_position_var.set("右上")
            dialog.active_position_var.set("中央下")
            dialog.mode_var.set("中板")
            dialog.save()

        dialog = self.run_dialog(scenario)
        self.assertTrue(dialog.saved)
        self.assertEqual(self.errors, [])
        tool = tool_registry.get("nlm.nippou")
        self.assertEqual((tool.display_name, tool.port, tool.stop_method),
                         ("日報 (現場)", 8735, "stop_bat"))
        self.assertEqual(tool_registry.idle_bar_position(), "top_right")
        self.assertEqual(tool_registry.active_bar_position(), "bottom_center")
        self.assertEqual(tool_registry.pc_mode(), "中板")
        self.assertEqual(tool_registry.get("nlm.kanban").port, 8734, "触っていない行が変わった")

    def test_入力に誤りがあれば何も保存しない(self) -> None:
        def scenario(dialog):
            self.row(dialog, "nlm.kanban").name_var.set("看板2")
            self.row(dialog, "nlm.nippou").port_var.set("abc")
            dialog.save()

        dialog = self.run_dialog(scenario)
        self.assertFalse(dialog.saved)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("日報: ポート番号は数字で入力してください", self.errors[0])
        self.assertEqual(tool_registry.get("nlm.kanban").display_name, "看板",
                         "誤りがあるのに、ほかの行だけ保存した")

    def test_同じアプリIDの行があれば保存しない(self) -> None:
        def scenario(dialog):
            self.row(dialog, "nlm.kanban").app_id_var.set("nlm.nippou")
            dialog.save()

        dialog = self.run_dialog(scenario)
        self.assertFalse(dialog.saved)
        self.assertIn("アプリID nlm.nippou が重複しています", self.errors[0])

    def test_削除は確かめてから(self) -> None:
        def scenario(dialog):
            self.row(dialog, "nlm.kanban").delete_var.set(True)
            self.row(dialog, "nlm.kanban")._on_delete_toggled()
            dialog.save()

        self.answer = False
        dialog = self.run_dialog(scenario)
        self.assertFalse(dialog.saved)
        self.assertIsNotNone(tool_registry.get("nlm.kanban"), "いいえなのに消した")
        self.assertTrue(any("1件のツールの登録を消します" in q for q in self.questions))

        self.answer = True
        self.questions.clear()
        dialog = self.run_dialog(scenario)
        self.assertTrue(dialog.saved)
        self.assertIsNone(tool_registry.get("nlm.kanban"))
        self.assertIsNotNone(tool_registry.get("nlm.nippou"))

    def test_警告にいいえなら保存しない(self) -> None:
        exe_start = make_web_tool(self.work_root, "資材", app_id="nlm.packaging-tool",
                                  port=8713, exe="資材.exe")

        def scenario(dialog):
            row = self.row(dialog, "nlm.nippou")
            row.path_var.set(str(exe_start.parent / "資材.exe"))
            row.port_var.set("8713")          # exe の行にブラウザー版のポート
            row.ui_var.set("ブラウザー")
            dialog.save()

        self.answer = False
        dialog = self.run_dialog(scenario)
        self.assertFalse(dialog.saved)
        self.assertTrue(any("ポート 8713" in q for q in self.questions), self.questions)
        self.assertEqual(tool_registry.get("nlm.nippou").start_command, str(self.daily))

    def test_ツールを追加すると素性を読み取って保存できる(self) -> None:
        start = make_web_tool(self.work_root, "点検表", app_id="nlm.inspection", port=8736)

        def scenario(dialog):
            with mock.patch.object(self.module.filedialog, "askopenfilename",
                                   return_value=str(start)):
                dialog.add_tool()
            row = dialog.rows[-1]
            self.assertTrue(row.is_new)
            self.assertEqual((row.app_id_var.get(), row.name_var.get(), row.port_var.get(),
                              row.args_var.get()),
                             ("nlm.inspection", "点検表", "8736", "--no-browser"))
            dialog.save()

        self.assertTrue(self.run_dialog(scenario).saved)
        tool = tool_registry.get("nlm.inspection")
        self.assertEqual((tool.port, tool.start_args, tool.order_no), (8736, "--no-browser", 30))

    def test_登録済みのアプリIDを追加しても保存しない(self) -> None:
        def scenario(dialog):
            with mock.patch.object(self.module.filedialog, "askopenfilename",
                                   return_value=str(self.daily)):
                dialog.add_tool()
            dialog.save()

        self.assertFalse(self.run_dialog(scenario).saved)
        self.assertIn("アプリID nlm.nippou はすでに登録されています", self.errors[0])

    def test_参照でexeを選ぶとブラウザー版のポートを空にしアプリの窓にする(self) -> None:
        exe_start = make_web_tool(self.work_root, "資材", app_id="nlm.packaging-tool",
                                  port=8733, exe="資材.exe")

        def scenario(dialog):
            row = self.row(dialog, "nlm.nippou")
            with mock.patch.object(self.module.filedialog, "askopenfilename",
                                   return_value=str(exe_start.parent / "資材.exe")):
                row.browse()
            self.assertEqual(row.port_var.get(), "")
            self.assertEqual(row.ui_var.get(), "アプリの窓")
            self.assertIn("ポートは空にしました", row.note.cget("text"))

        self.run_dialog(scenario)

    def test_配布先フォルダは保存してから作る(self) -> None:
        def scenario(dialog):
            self.row(dialog, "nlm.nippou").name_var.set("日報2")
            dialog.export_distribution()

        dialog = self.run_dialog(scenario)
        self.assertTrue(dialog.saved)
        self.assertEqual(tool_registry.get("nlm.nippou").display_name, "日報2")
        names = {item["app_id"]: item["display_name"] for item in distribution.tools()}
        self.assertEqual(names.get("nlm.nippou"), "日報2", "保存する前の内容で作った")

    def test_キャンセルなら何も変えない(self) -> None:
        def scenario(dialog):
            self.row(dialog, "nlm.nippou").name_var.set("変えた")
            dialog.cancel()

        self.assertFalse(self.run_dialog(scenario).saved)
        self.assertEqual(tool_registry.get("nlm.nippou").display_name, "日報")


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class StopButtonTests(ManagerTestCase):
    """バーの［ツール停止］。"""

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        from launcher.ui import bar as bar_module

        self.bar_module = bar_module
        self.answer = True
        self.asked: list[str] = []
        self.stop = mock.patch.object(self.manager, "stop").start()
        mock.patch.object(bar_module.messagebox, "askyesno",
                          side_effect=lambda title, text, **_:
                          (self.asked.append(text), self.answer)[1]).start()
        self.addCleanup(mock.patch.stopall)
        self.bar = bar_module.LauncherBar(self.manager)
        self.addCleanup(self._destroy)
        self.menus: list = []

    def _destroy(self) -> None:
        try:
            self.bar.root.destroy()
        except Exception:                     # noqa: BLE001
            pass

    def running(self, *names: str) -> None:
        for name in names:
            self.manager._running[f"nlm.{name}"] = RunningTool(
                app_id=f"nlm.{name}", display_name=name, port=0)

    def press(self) -> list[str]:
        """［ツール停止］を押す。メニューが出たら、その項目の文字を返す。"""
        menus = []

        def popup(menu, x, y):
            menus.append(menu)

        with mock.patch.object(self.bar_module.tk.Menu, "tk_popup", popup):
            self.bar.on_stop_tool()
        self.menus = menus
        if not menus:
            return []
        menu = menus[0]
        return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
                if menu.type(i) == "command"]

    def choose(self, label: str) -> None:
        menu = self.menus[0]
        for i in range(menu.index("end") + 1):
            if menu.type(i) == "command" and menu.entrycget(i, "label") == label:
                menu.invoke(i)
                return
        self.fail(f"メニューに「{label}」がありません")

    def test_1つなら確かめて止める(self) -> None:
        self.running("日報")
        self.answer = False
        self.assertEqual(self.press(), [])
        self.stop.assert_not_called()
        self.assertIn("日報 を終了しますか?", self.asked[0])
        self.answer = True
        self.press()
        self.stop.assert_called_once_with("nlm.日報")
        self.assertIn("nlm.日報", self.bar._stop_asked, "断られたときに聞けるように覚える")

    def test_1つで断られていれば強制終了を聞く(self) -> None:
        self.running("日報")
        self.manager._refused.add("nlm.日報")
        self.press()
        self.assertIn("強制終了 (中断) しますか?", self.asked[0])
        self.stop.assert_called_once_with("nlm.日報", force=True)

    def test_2つ以上なら選ばせる(self) -> None:
        self.running("日報", "看板")
        self.manager._refused.add("nlm.看板")
        labels = self.press()
        self.assertEqual(labels, ["日報 を止める", "看板 を強制終了する", "すべて止める"])
        self.choose("日報 を止める")
        self.stop.assert_called_once_with("nlm.日報")

    def test_すべて止めるは確かめてから(self) -> None:
        self.running("日報", "看板")
        stop_all = mock.patch.object(self.manager, "stop_all").start()
        self.press()
        self.answer = False
        self.choose("すべて止める")
        stop_all.assert_not_called()
        self.answer = True
        self.choose("すべて止める")
        stop_all.assert_called_once_with()
        self.assertEqual(self.bar._stop_asked, {"nlm.日報", "nlm.看板"})


if __name__ == "__main__":
    unittest.main()
