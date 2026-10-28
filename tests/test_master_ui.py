"""［設定］の画面 (1.8.0 で作り直した: 見出し・ツールの一覧と詳細・PC × ツールの表)

画面の部品をそのまま動かし、ダイアログ (messagebox・filedialog) と待ち
(wait_window) だけを差し替える。

1.7.14 で言われたこと:
* ［＋］でツールを足しても、マスタを開くと消えている (一覧が2つあった)
* PC の行を足そうとすると「先に［ツール］でツールを登録してください」と断られる
→ ツールの一覧は1つ。PC の行はツールより先に足せる。［保存］は1つ。
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

        from launcher.ui import settings_dialog

        self.tk = tk
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
        self.errors: list[str] = []
        for name, effect in (("showerror", self._error), ("showinfo", None),
                             ("askyesno", self._ask), ("askyesnocancel", self._ask)):
            patch = mock.patch.object(settings_dialog.messagebox, name, side_effect=effect)
            patch.start()
            self.addCleanup(patch.stop)

    def _error(self, title, text, **_):
        self.errors.append(text)

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


class SettingsTestCase(UiTestCase):

    def run_settings(self, scenario):
        return self.run_modal(self.settings_module.SettingsDialog, self.parent,
                              scenario=scenario)

    def use_master(self) -> None:
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(self.shared))
        tool_master.ensure(self.path)

    def pick(self, start: Path):
        """［＋ ツールを追加］のファイル選択で `start` を選ぶ。"""
        return mock.patch.object(self.settings_module.filedialog, "askopenfilename",
                                 return_value=str(start))

    @staticmethod
    def row(dialog, app_id: str):
        return next(r for r in dialog.rows if r.app_id_var.get() == app_id)

    def shown(self) -> list[str]:
        return [t.display_name for t in tool_registry.all_tools()]


class LayoutTests(SettingsTestCase):

    def test_見出しを切り替えても入力は残り保存は1つ(self) -> None:
        seen = {}

        def scenario(dialog):
            self.assertEqual([k for k, _ in self.settings_module.SECTIONS],
                             ["tools", "pcs", "local", "records", "password"])
            self.row(dialog, "nlm.nippou").name_var.set("日報(2ライン)")
            for key, _ in self.settings_module.SECTIONS:
                dialog.show_section(key)
                dialog.top.update()
                seen.setdefault("saves", []).append(sum(
                    1 for w in _walk(dialog.top) if isinstance(w, self.tk.Button)
                    and w.cget("text") == "保存" and w.winfo_ismapped()))
            dialog.mode_var.set("中板")
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved)
        self.assertEqual(seen["saves"], [1, 1, 1, 1, 1], "［保存］がいつも1つ見えていません")
        self.assertEqual(tool_registry.get("nlm.nippou").display_name, "日報(2ライン)")
        self.assertEqual(tool_registry.pc_mode(), "中板")

    def test_一覧で選ぶと右に詳しい設定が出る(self) -> None:
        seen = {}

        def scenario(dialog):
            dialog.top.update()
            seen["first"] = dialog.rows[0].box.winfo_ismapped()
            dialog.select(dialog.rows[1])
            dialog.top.update()
            seen["mapped"] = [r.box.winfo_ismapped() for r in dialog.rows]
            dialog.move_selected(-1)
            dialog.save()

        self.run_settings(scenario)
        self.assertTrue(seen["first"])
        self.assertEqual(seen["mapped"], [False, True])
        # 並べ替えはバーの並びになる
        self.assertEqual(self.shown(), ["看板", "日報"])

    def test_パスワードの欄がある(self) -> None:
        seen = {}

        def scenario(dialog):
            seen["state"] = dialog.password_state.cget("text")

        self.run_settings(scenario)
        self.assertEqual(seen["state"], "(既定のまま)")
        distribution.set_password("line-01")
        self.run_settings(scenario)
        self.assertEqual(seen["state"], "(変えてあります)")


class MasterTests(SettingsTestCase):

    def test_ここで場所を決めると足したツールもマスタに入る(self) -> None:
        """1.7.14 の「＋で足してもマスタを開くと消えている」。"""
        third = make_web_tool(self.work_root, "点検表", app_id="nlm.inspection", port=8736)

        def scenario(dialog):
            with self.pick(third):
                dialog.add_tool()
            dialog.show_section("pcs")
            dialog.master_var.set(str(self.shared))
            dialog.use_master()
            self.assertTrue(dialog.shared)
            self.assertEqual([r.label() for r in dialog.rows], ["日報", "看板", "点検表"])
            dialog.add_this_pc()
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved, self.errors)
        master = tool_master.read(self.path)
        self.assertEqual([t.name for t in master.tools], ["日報", "看板", "点検表"])
        self.assertEqual([t.app_id for t in master.tools],
                         ["nlm.nippou", "nlm.kanban", "nlm.inspection"])
        self.assertEqual(master.tools[2].port, 8736)
        self.assertEqual(len(master.pcs), 1)
        self.assertEqual((master.pcs[0].login_id, master.pcs[0].pc_name), (LOGIN, PC))
        self.assertEqual(len(master.pcs[0].tool_nos), 3)
        self.assertEqual(self.shown(), ["日報", "看板", "点検表"])
        self.assertEqual(tool_master.last_result().state, tool_master.APPLIED)

    def test_PCの行はツールより先に足せる(self) -> None:
        """1.7.14 の「先に［ツール］でツールを登録してください」。"""
        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        self.use_master()

        def scenario(dialog):
            self.assertEqual(dialog.rows, [])
            dialog.show_section("pcs")
            dialog.add_pc()
            dialog.pc_rows[-1].login_var.set("Xfa-Ngyshizai")
            dialog.pc_rows[-1].pc_var.set("NLM-NGY-250962")
            dialog.pc_rows[-1].note_var.set("倉庫右")
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved, self.errors)
        master = tool_master.read(self.path)
        self.assertEqual([(p.login_id, p.pc_name, p.note, p.tool_nos) for p in master.pcs],
                         [("Xfa-Ngyshizai", "NLM-NGY-250962", "倉庫右", ())])

    def test_足したツールはこの端末の行に入る(self) -> None:
        self.use_master()
        a = tool_master.save_tool(self.path, MasterTool(0, name="日報",
                                                        start_command=str(self.daily)))
        tool_master.save_pc(self.path, MasterPc(0, login_id=LOGIN, pc_name=PC, tool_nos=(a,)))
        tool_master.sync()
        self.assertEqual(self.shown(), ["日報"])
        third = make_web_tool(self.work_root, "点検表", app_id="nlm.inspection", port=8736)

        def scenario(dialog):
            with self.pick(third):
                dialog.add_tool()
            self.assertIn("この端末の行 (管理番号 1)", dialog.rows[-1].note.cget("text"))
            dialog.save()

        self.run_settings(scenario)
        self.assertEqual(self.shown(), ["日報", "点検表"])

    def test_デスクトップのツールはDESKTOPで書く(self) -> None:
        self.use_master()
        home = self.work_root / "Users" / "Aさん"
        start = make_web_tool(home / "Desktop", "点検表", app_id="nlm.inspection", port=8736)
        seen = {}

        def scenario(dialog):
            with self.pick(start):
                dialog.add_tool()
            row = dialog.rows[-1]
            seen["path"] = row.path_var.get()
            seen["place"] = row.path_place.cget("text")
            dialog.save()

        with mock.patch.multiple(tool_master, desktop_dir=lambda: str(home / "Desktop"),
                                 profile_dir=lambda: str(home)):
            dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved, self.errors)
        self.assertTrue(seen["path"].startswith("%DESKTOP%"), seen["path"])
        self.assertIn("この端末にあります", seen["place"])
        master = tool_master.read(self.path)
        self.assertTrue(master.tools[-1].start_command.startswith("%DESKTOP%"))
        self.assertEqual(Path(tool_registry.get("nlm.inspection").start_command), start)

    def test_表のチェックで出すツールを決める(self) -> None:
        self.use_master()
        tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        master = tool_master.read(self.path)
        tool_master.save_pc(self.path, MasterPc(0, login_id=LOGIN, pc_name=PC,
                                                tool_nos=tuple(t.tool_no for t in master.tools)))
        tool_master.save_pc(self.path, MasterPc(0, login_id=LOGIN, pc_name="OTHER-PC"))
        seen = {}

        def scenario(dialog):
            dialog.show_section("pcs")
            dialog.top.update()
            mine = dialog._this_pc_row()
            seen["mine"] = mine.pc_no
            kanban = self.row(dialog, "nlm.kanban")
            mine.set(kanban, False)
            dialog.pc_rows[1].set(kanban, True)
            dialog.save()

        self.run_settings(scenario)
        self.assertEqual(seen["mine"], 1)
        self.assertEqual(self.shown(), ["日報"])
        master = tool_master.read(self.path)
        kanban = next(t.tool_no for t in master.tools if t.app_id == "nlm.kanban")
        self.assertEqual(master.pcs[1].tool_nos, (kanban,))

    def test_マスタから消すとこの端末からも消える(self) -> None:
        self.use_master()
        tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        tool_master.sync()

        def scenario(dialog):
            self.row(dialog, "nlm.kanban").delete_var.set(True)
            dialog.save()

        self.run_settings(scenario)
        self.assertTrue(any("全 PC のバーから消えます" in q for q in self.questions))
        self.assertEqual([t.name for t in tool_master.read(self.path).tools], ["日報"])
        self.assertIsNone(tool_registry.get("nlm.kanban"))

    def test_同じログインIDとPC名の行は保存しない(self) -> None:
        self.use_master()

        def scenario(dialog):
            dialog.add_this_pc()
            dialog.add_pc()
            dialog.pc_rows[-1].login_var.set(LOGIN.lower())
            dialog.pc_rows[-1].pc_var.set(PC)
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertFalse(dialog.saved)
        self.assertIn("同じログインID・PC名の行が2つあります", self.errors[0])
        self.assertEqual(tool_master.read(self.path).pcs, ())

    def test_開いたあとでほかのPCが足したツールは消さない(self) -> None:
        self.use_master()
        tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        other = {}

        def scenario(dialog):
            other["no"] = tool_master.save_tool(
                self.path, MasterTool(0, name="点検表",
                                      start_command=r"C:\業務ツール\点検表\Start.vbs"))
            pc = tool_master.save_pc(self.path, MasterPc(0, login_id="X", pc_name="Y",
                                                         tool_nos=(other["no"],)))
            other["pc"] = pc
            dialog.save()

        self.run_settings(scenario)
        master = tool_master.read(self.path)
        self.assertIn("点検表", [t.name for t in master.tools])
        self.assertEqual(next(p for p in master.pcs if p.pc_no == other["pc"]).tool_nos,
                         (other["no"],))

    def test_起動ファイルは候補をいくつも書ける(self) -> None:
        self.use_master()
        tool_master.import_tools(self.path, tool_registry.all_tools(include_disabled=True))
        both = r"C:\無い場所\日報\start.bat;" + str(self.daily)

        def scenario(dialog):
            row = self.row(dialog, "nlm.nippou")
            row.path_var.set(both)
            row.refresh_hints()
            self.assertEqual(row.entry_path(), str(self.daily))
            self.assertTrue(row.here())
            dialog.save()

        dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved, self.errors)
        self.assertEqual(tool_master.read(self.path).tools[0].start_command, both)
        self.assertEqual(tool_registry.get("nlm.nippou").start_command, str(self.daily))

    def test_読めないマスタならこの端末だけ直す(self) -> None:
        self.use_master()
        seen = {}

        def scenario(dialog):
            seen["shared"] = dialog.shared
            seen["status"] = dialog.status.cget("text")
            self.row(dialog, "nlm.nippou").port_var.set("8799")
            dialog.save()

        with mock.patch.object(tool_master, "load",
                               side_effect=tool_master.MasterError("届きません: 応答なし")), \
                mock.patch.object(tool_master, "sync"):
            dialog = self.run_settings(scenario)
        self.assertTrue(dialog.saved, self.errors)
        self.assertFalse(seen["shared"])
        self.assertIn("読めません", seen["status"])
        self.assertEqual(tool_registry.get("nlm.nippou").port, 8799)
        self.assertEqual(tool_master.read(self.path).tools, ())


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


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
