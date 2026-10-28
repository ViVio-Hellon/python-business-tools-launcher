"""ツール管理マスタの画面と、［設定］のマスタ・パスワードの欄 (1.7.14)

画面の部品をそのまま動かし、ダイアログ (messagebox) と待ち (wait_window) だけを
差し替える。
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, have_tk, release_tk  # noqa: E402
from test_ui_flows import make_web_tool  # noqa: E402

from launcher import distribution, tool_master, tool_registry  # noqa: E402
from launcher.tool_master import MasterPc, MasterTool  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402

LOGIN, PC = "Xfa-Ngykonpo", "NLM-NGY-240638"


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class UiTestCase(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter as tk

        from launcher.ui import master_dialog, settings_dialog

        self.tk = tk
        self.master_module = master_dialog
        self.settings_module = settings_dialog
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.parent = tk.Tk()
        self.addCleanup(self.parent.destroy)
        self.parent.geometry("600x56+300+700")
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        self.daily = make_web_tool(self.work_root, "日報", app_id="nlm.nippou", port=8733)
        self.board = make_web_tool(self.work_root, "看板", app_id="nlm.kanban", port=8734)
        tool_registry.save(Tool(app_id="nlm.nippou", display_name="日報", port=8733,
                                start_command=str(self.daily), order_no=10))
        tool_registry.save(Tool(app_id="nlm.kanban", display_name="看板", port=8734,
                                start_command=str(self.board), order_no=20))
        self.shared = self.work_root / "share"
        self.path = tool_master.path_for(str(self.shared))
        self.questions: list[str] = []
        self.answer: object = True
        for module in (master_dialog, settings_dialog):
            for name, effect in (("showerror", None), ("showinfo", None),
                                 ("askyesno", self._ask), ("askyesnocancel", self._ask)):
                patch = mock.patch.object(module.messagebox, name,
                                          side_effect=effect)
                patch.start()
                self.addCleanup(patch.stop)

    def _ask(self, title, text, **_):
        self.questions.append(text)
        return self.answer

    def run_modal(self, cls, *args, scenario, **kwargs):
        """`cls(*args)` を開き、最初の窓で `scenario(画面)` を動かして閉じる。"""
        holder: dict = {}
        tk = self.tk
        real_init = cls.__init__

        def init(dialog, *a, **k):
            holder.setdefault("dialog", dialog)
            real_init(dialog, *a, **k)

        def wait(top):
            top.update()
            try:
                if "ran" not in holder:
                    holder["ran"] = True
                    scenario(holder["dialog"])
            finally:
                if top.winfo_exists():
                    top.destroy()

        with mock.patch.object(cls, "__init__", init), \
                mock.patch.object(tk.Toplevel, "wait_window", wait), \
                mock.patch.object(tk.Toplevel, "grab_set", lambda self: None):
            cls(*args, **kwargs)
        return holder.get("dialog")


class MasterDialogTests(UiTestCase):

    def labels(self, dialog) -> list[str]:
        return [w.cget("text") for w in dialog.table.inner.winfo_children()
                if isinstance(w, self.tk.Label)]

    def test_開くとマスタを作って書ける(self) -> None:
        seen = {}

        def scenario(dialog):
            self.assertTrue(self.path.exists(), "マスタを作っていません")
            self.assertEqual(dialog.store_tool(MasterTool(0, name="日報",
                                                          start_command=str(self.daily))), "")
            self.assertEqual(dialog.store_tool(MasterTool(0, name="看板",
                                                          start_command=str(self.board))), "")
            dialog.show_tab(self.master_module.PC_TAB)
            nos = tuple(t.tool_no for t in dialog.master.tools)
            self.assertEqual(dialog.store_pc(MasterPc(0, login_id=LOGIN, pc_name=PC,
                                                      tool_nos=nos[:1], note="管理用")), "")
            seen["labels"] = self.labels(dialog)
            seen["here"] = dialog.here.cget("text")
            # 同じ組み合わせの行はもう作れない (理由を返し、編集の窓は閉じない)
            seen["dup"] = dialog.store_pc(MasterPc(0, login_id=LOGIN, pc_name=PC))

        dialog = self.run_modal(self.master_module.MasterDialog, self.parent, self.path,
                                scenario=scenario)
        self.assertTrue(dialog.changed)
        self.assertIn("管理用", seen["labels"])
        self.assertIn("日報", seen["labels"])
        self.assertIn("管理番号 1 の行で並べます (日報)", seen["here"])
        self.assertIn("管理番号 1", seen["dup"])

    def test_今の設定から取り込んで消す(self) -> None:
        def scenario(dialog):
            dialog.show_tab(self.master_module.TOOL_TAB)
            dialog.import_current()
            self.assertEqual([t.label for t in dialog.master.tools], ["日報", "看板"])
            dialog.delete_tool(dialog.master.tools[1])
            self.assertEqual([t.label for t in dialog.master.tools], ["日報"])

        self.run_modal(self.master_module.MasterDialog, self.parent, self.path,
                       scenario=scenario)
        self.assertTrue(any("消します" in q for q in self.questions))

    def test_ツールが無ければ先にツールを登録させる(self) -> None:
        def scenario(dialog):
            with mock.patch.object(self.master_module, "PcEditor") as editor:
                dialog.add_this_pc()
            editor.assert_not_called()
            self.assertEqual(dialog.tab, self.master_module.TOOL_TAB)

        self.run_modal(self.master_module.MasterDialog, self.parent, self.path,
                       scenario=scenario)


class EditorTests(UiTestCase):

    def test_PCの行の編集はこの端末の値と選んだツールを返す(self) -> None:
        tool_master.ensure(self.path)
        a = tool_master.save_tool(self.path, MasterTool(0, name="日報",
                                                        start_command=str(self.daily)))
        b = tool_master.save_tool(self.path, MasterTool(0, name="看板",
                                                        start_command=str(self.board)))
        master = tool_master.read(self.path)
        saved = []
        answers = iter(["同じ行があります", ""])

        def on_save(pc):
            saved.append(pc)
            return next(answers)

        def scenario(editor):
            dict(editor.tool_vars)[b].set(True)
            editor.note_var.set("HVC")
            editor.save()
            self.assertIn("同じ行があります", editor.error.cget("text"))
            editor.save()

        self.run_modal(self.master_module.PcEditor, self.parent, master,
                       MasterPc(0, login_id=LOGIN, pc_name=PC, tool_nos=(a,)),
                       on_save=on_save, scenario=scenario)
        self.assertEqual(saved[-1], MasterPc(0, login_id=LOGIN, pc_name=PC,
                                             tool_nos=(a, b), enabled=True, note="HVC"))

    def test_ツールの編集(self) -> None:
        saved = []

        def scenario(editor):
            editor.name_var.set("日報")
            editor.start_var.set(str(self.daily))
            editor.save()

        self.run_modal(self.master_module.ToolEditor, self.parent, MasterTool(0),
                       on_save=lambda tool: saved.append(tool) or "", scenario=scenario)
        self.assertEqual(saved[0].start_command, str(self.daily))


class SettingsMasterTests(UiTestCase):

    def run_settings(self, scenario):
        return self.run_modal(self.settings_module.SettingsDialog, self.parent,
                              scenario=scenario)

    def test_場所を入れて保存するとマスタを作り反映する(self) -> None:
        def scenario(dialog):
            dialog.master_var.set(str(self.shared))
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved)
        self.assertEqual(tool_registry.get_pc_setting(tool_registry.TOOL_MASTER_KEY),
                         str(self.shared))
        self.assertTrue(self.path.exists())
        self.assertEqual(tool_master.last_result().state, tool_master.NO_ROW)

    def test_マスタの行があればそのツールだけにして案内を出す(self) -> None:
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(self.shared))
        tool_master.ensure(self.path)
        a = tool_master.save_tool(self.path, MasterTool(0, name="日報",
                                                        start_command=str(self.daily)))
        tool_master.save_pc(self.path, MasterPc(0, login_id=LOGIN, pc_name=PC, tool_nos=(a,)))
        tool_master.sync()
        seen = {}

        def scenario(dialog):
            seen["banner"] = dialog.master_banner.cget("text")
            seen["state"] = dialog.master_state.cget("text")
            # ［使う］を外しても、保存すればマスタのとおりに戻る
            row = next(r for r in dialog.rows if r.tool.app_id == "nlm.nippou")
            row.enabled_var.set(False)
            dialog.save()

        self.run_settings(scenario)
        self.assertIn("管理番号 1", seen["banner"])
        self.assertIn("管理番号 1 の行で並べています", seen["state"])
        self.assertEqual([t.display_name for t in tool_registry.all_tools()], ["日報"])

    def test_保存していない変更があればマスタを開く前に聞く(self) -> None:
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(self.shared))
        self.answer = None                    # ［キャンセル］

        def scenario(dialog):
            dialog.rows[0].name_var.set("日報(2ライン)")
            with mock.patch.object(self.master_module, "MasterDialog") as opened:
                dialog.open_master()
            opened.assert_not_called()

        self.run_settings(scenario)
        self.assertTrue(any("保存していない変更" in q for q in self.questions))

    def test_マスタを閉じたら行を並べ直してバーを描き直す(self) -> None:
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(self.shared))
        tool_master.ensure(self.path)
        a = tool_master.save_tool(self.path, MasterTool(0, name="看板",
                                                        start_command=str(self.board)))

        class FakeMaster:
            def __init__(fake, parent, path):
                tool_master.save_pc(path, MasterPc(0, login_id=LOGIN, pc_name=PC,
                                                   tool_nos=(a,)))
                fake.changed = True

        seen = {}

        def scenario(dialog):
            with mock.patch.object(self.master_module, "MasterDialog", FakeMaster):
                dialog.open_master()
            seen["rows"] = {r.tool.app_id: r.enabled_var.get() for r in dialog.rows}

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.tools_changed)
        self.assertEqual(seen["rows"], {"nlm.nippou": False, "nlm.kanban": True})

    def test_パスワードの欄が見える所にある(self) -> None:
        seen = {}

        def scenario(dialog):
            seen["state"] = dialog.password_state.cget("text")

        self.run_settings(scenario)
        self.assertEqual(seen["state"], "(既定のまま)")
        distribution.set_password("line-01")
        self.run_settings(scenario)
        self.assertEqual(seen["state"], "(変えてあります)")


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class PasswordPromptTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter as tk

        self.tk = tk
        self.parent = tk.Tk()
        self.addCleanup(self.parent.destroy)

    def test_変えるときは伏せ字にしない(self) -> None:
        from launcher.ui import password

        shown = []

        def wait(top):
            entry = next(w for w in top.winfo_children() if isinstance(w, self.tk.Entry))
            shown.append(entry.cget("show"))
            entry.insert(0, "line-01")
            buttons = [w for frame in top.winfo_children()
                       for w in frame.winfo_children() if isinstance(w, self.tk.Button)]
            next(b for b in buttons if b.cget("text") == "OK").invoke()
            if top.winfo_exists():
                top.destroy()

        with mock.patch.object(self.tk.Toplevel, "wait_window", wait), \
                mock.patch.object(self.tk.Toplevel, "grab_set", lambda self: None), \
                mock.patch.object(password.messagebox, "showinfo"):
            self.assertTrue(password.change(self.parent))
            self.assertTrue(distribution.verify_password("line-01"))
            self.assertEqual(shown, [""], "入力欄を伏せ字にしています")
            shown.clear()
            with mock.patch.object(password.messagebox, "showerror"):
                password.unlock(self.parent)
            self.assertEqual(shown, ["*"], "開くときの入力欄は伏せ字のまま")


if __name__ == "__main__":
    unittest.main()
