"""更新のため全 PC で止める (1.8.8)

共有サーバーのツールを上書きする前に、全 PC でそのツールを止める。管理者が
ツール管理マスタに「更新中」の印を付けると、各 PC のランチャーが:

* 動いていれば止めて「止めた」と書き返す。使っていなければ「使っていない」
* 処理中で止まらなければ「処理中」と書き返し、**利用者に終了を頼んで待つ**
  (1 分ごとに止め直す)。管理者が PC ごとに強制終了を頼めば、強制終了する
* 印があるあいだは起動しない。印が外れたら、もとに戻る
"""
from __future__ import annotations

import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, have_tk, release_tk  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

from app_manager import State  # noqa: E402
from launcher import maintenance, tool_master, tool_registry  # noqa: E402

LOGIN, PC = "Xfa-Ngykonpo", "NLM-NGY-240638"


class MasterTableTests(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = tool_master.path_for(str(self.work_root / "share"))

    def test_印を付けて返事を集めて外す(self) -> None:
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        marks = tool_master.under_maintenance(self.path)
        self.assertEqual(list(marks), ["nlm.nippou"])
        self.assertIn(PC, marks["nlm.nippou"].started_by)
        tool_master.report(self.path, "nlm.nippou", tool_master.BUSY, "保存中")
        reports = tool_master.reports(self.path, "nlm.nippou")
        self.assertEqual([(r.pc_name, r.label, r.message) for r in reports],
                         [(PC, "処理中 (待っています)", "保存中")])
        tool_master.request_force(self.path, "nlm.nippou", LOGIN, PC)
        self.assertTrue(tool_master.take_force_request(self.path, "nlm.nippou"))
        self.assertFalse(tool_master.take_force_request(self.path, "nlm.nippou"), "2回受け取りました")
        tool_master.end_maintenance(self.path, "nlm.nippou")
        self.assertEqual(tool_master.under_maintenance(self.path), {})
        self.assertEqual(tool_master.reports(self.path, "nlm.nippou"), [])

    def test_前の版のマスタでも読める(self) -> None:
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE tools (tool_no INTEGER PRIMARY KEY)")
        self.assertEqual(tool_master.under_maintenance(self.path), {})
        tool_master.ensure(self.path)                 # 書くときに表を足す
        tool_master.begin_maintenance(self.path, "a", "A")
        self.assertEqual(list(tool_master.under_maintenance(self.path)), ["a"])


class WatcherTests(ManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        shared = self.work_root / "share"
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(shared))
        self.path = tool_master.path_for(str(shared))
        tool_master.ensure(self.path)
        self.watcher = maintenance.Watcher(self.manager)

    def state(self, app_id: str) -> str:
        found = tool_master.reports(self.path, app_id)
        return found[0].state if found else ""

    def test_動いていれば止めて印のあいだは起動しない(self) -> None:
        tool = self.register("nlm.nippou", "日報")
        self.start(tool)
        self.assertIn("nlm.nippou", self.manager.running)
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        result = self.watcher.poll()
        self.assertEqual(result.blocked, {"nlm.nippou": "日報"})
        self.assertNotIn("nlm.nippou", self.manager.running)
        self.assertEqual(self.state("nlm.nippou"), tool_master.STOPPED)
        # 印のあいだは押しても起動しない
        self.manager.select("nlm.nippou")
        self.assertEqual(self.manager.status.message, "日報は更新中です")
        self.assertNotIn("nlm.nippou", self.manager.running)
        # 印が外れたら起動できる
        tool_master.end_maintenance(self.path, "nlm.nippou")
        self.assertEqual(self.watcher.poll().blocked, {})
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING, self.manager.status.detail)

    def test_使っていなければそう返す(self) -> None:
        self.register("nlm.nippou", "日報")
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        self.watcher.poll()
        self.assertEqual(self.state("nlm.nippou"), tool_master.NOT_RUNNING)

    def test_処理中なら知らせて待ち頼まれたら強制終了する(self) -> None:
        tool = self.register("nlm.kanban", "看板", busy=True)
        self.start(tool)
        tool_master.begin_maintenance(self.path, "nlm.kanban", "看板")
        result = self.watcher.poll()
        self.assertEqual(result.ask_user, [("nlm.kanban", "看板")])
        self.assertEqual(self.state("nlm.kanban"), tool_master.BUSY)
        self.assertIn("nlm.kanban", self.manager.running, "処理中のツールを止めました")
        # すぐには止め直さない・利用者には1回だけ知らせる
        with mock.patch.object(self.manager, "_stop_blocking") as stop:
            self.assertEqual(self.watcher.poll().ask_user, [])
            stop.assert_not_called()
        # 管理者が強制終了を頼んだ
        tool_master.request_force(self.path, "nlm.kanban", LOGIN, PC)
        self.watcher.poll()
        self.assertNotIn("nlm.kanban", self.manager.running)
        reports = tool_master.reports(self.path, "nlm.kanban")
        self.assertEqual(reports[0].state, tool_master.STOPPED)
        self.assertIn("強制終了", reports[0].message)

    # --- 精査で見つけた穴 (1.8.8) -------------------------------------------
    def test_共有フォルダーに書けなくても止める(self) -> None:
        tool = self.register("nlm.nippou", "日報")
        self.start(tool)
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        broken = tool_master.MasterError("読み取り専用です")
        with mock.patch.object(tool_master, "take_force_request", side_effect=broken), \
                mock.patch.object(tool_master, "report", side_effect=broken):
            result = self.watcher.poll()
        self.assertIsNotNone(result)
        self.assertNotIn("nlm.nippou", self.manager.running, "書けないので止めずに終わりました")

    def test_PCによってアプリIDが違っても起動ファイルで見つけて止める(self) -> None:
        tool = self.register("app.nippou-local", "日報")
        self.start(tool)
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報",
                                      start_command=r"C:\\無い\\start.bat;" + tool.start_command)
        result = self.watcher.poll()
        self.assertIn("app.nippou-local", result.blocked)
        self.assertNotIn("app.nippou-local", self.manager.running)
        self.assertEqual(self.state("nlm.nippou"), tool_master.STOPPED)

    def test_印を付け直したらまた返事を書く(self) -> None:
        self.register("nlm.nippou", "日報")
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        self.watcher.poll()
        tool_master.end_maintenance(self.path, "nlm.nippou")
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")   # 同じ秒でも別の印
        self.assertEqual(self.state("nlm.nippou"), "")
        self.watcher.poll()
        self.assertEqual(self.state("nlm.nippou"), tool_master.NOT_RUNNING)

    def test_利用者が止めている最中なら待つ(self) -> None:
        tool = self.register("nlm.nippou", "日報")
        self.start(tool)
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        with mock.patch.object(self.manager, "is_busy", return_value=True), \
                mock.patch.object(self.manager, "_stop_blocking") as stop:
            self.watcher.poll()
            stop.assert_not_called()
        self.assertEqual(self.state("nlm.nippou"), "", "「止められない」と誤って返しました")

    def test_止められなければ頼まれるまで止め直さない(self) -> None:
        tool = self.register("nlm.nippou", "日報")
        self.start(tool)
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        with mock.patch.object(self.manager, "_stop_blocking", return_value=False) as stop:
            self.watcher.poll()
            self.assertEqual(self.state("nlm.nippou"), tool_master.FAILED)
            for key in self.watcher._tried:
                self.watcher._tried[key] -= maintenance.RETRY_SEC + 1
            self.watcher.poll()
            self.assertEqual(stop.call_count, 1, "止められないツールを止め直しました")
            tool_master.request_force(self.path, "nlm.nippou", LOGIN, PC)
            self.watcher.poll()
            self.assertEqual(stop.call_args.kwargs, {"force": True})

    def test_マスタから消したツールの印は外す(self) -> None:
        number = tool_master.save_tool(self.path, tool_master.MasterTool(
            0, name="日報", start_command=r"C:\\t\\start.bat", app_id="nlm.nippou"))
        tool_master.begin_maintenance(self.path, "nlm.nippou", "日報")
        tool_master.write_catalog(self.path, [], deleted_tools=[number], known_tools=[number])
        self.assertEqual(tool_master.under_maintenance(self.path), {})

    def test_マスタを使っていなければ何もしない(self) -> None:
        tool_registry.clear_pc_setting(tool_registry.TOOL_MASTER_KEY)
        self.assertIsNone(self.watcher.poll())


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class ScreenTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        from test_ui_flows import make_web_tool

        for tool in tool_registry.all_tools(include_disabled=True):
            tool_registry.delete_tool(tool.app_id)
        start = make_web_tool(self.work_root, "日報", app_id="nlm.nippou", port=8733)
        tool_registry.save(tool_registry.Tool(app_id="nlm.nippou", display_name="日報",
                                              port=8733, start_command=str(start)))

    def test_バーは更新中と出し利用者に終了を頼む(self) -> None:
        import tkinter as tk

        from app_manager import ToolManager
        from launcher.ui import bar as bar_module

        bar = bar_module.LauncherBar(ToolManager())
        self.addCleanup(lambda: bar.root.destroy())
        bar._apply_maintenance(maintenance.PollResult(blocked={"nlm.nippou": "日報"},
                                                      ask_user=[("nlm.nippou", "日報")]))
        bar.root.update()
        self.assertEqual(bar.buttons["nlm.nippou"].cget("text"), "日報（更新中）")
        notes = [w for w in bar.root.winfo_children() if isinstance(w, tk.Toplevel)
                 and w.title() == "ツールの更新"]
        self.assertEqual(len(notes), 1)
        # 更新中のツールを押しても、起動もしないしバーも動かさない
        with mock.patch.object(bar.manager, "select") as select:
            bar.on_select("nlm.nippou")
        select.assert_called_once_with("nlm.nippou")
        self.assertFalse(bar._engaged, "起動しないのにバーを動かしました")
        bar._apply_maintenance(maintenance.PollResult(blocked={}))
        self.assertEqual(bar.buttons["nlm.nippou"].cget("text"), "日報")

    def test_管理者の一覧に返事の無いPCも出る(self) -> None:
        import tkinter as tk

        from launcher.ui import settings_dialog

        shared = self.work_root / "share"
        path = tool_master.path_for(str(shared))
        number = tool_master.save_tool(path, tool_master.MasterTool(
            0, name="日報", start_command=tool_registry.get("nlm.nippou").start_command,
            app_id="nlm.nippou"))
        tool_master.save_pc(path, tool_master.MasterPc(0, login_id=LOGIN, pc_name=PC,
                                                       tool_nos=(number,)))
        tool_master.save_pc(path, tool_master.MasterPc(0, login_id="Xfa-Ngyshizai",
                                                       pc_name="NLM-NGY-250962",
                                                       tool_nos=(number,), note="倉庫右"))
        tool_master.begin_maintenance(path, "nlm.nippou", "日報")
        tool_master.report(path, "nlm.nippou", tool_master.BUSY, "保存中")
        root = tk.Tk()
        self.addCleanup(root.destroy)
        seen = {}

        def wait(top):
            top.update()
            dialog = holder["dialog"]
            seen["rows"] = dialog.rows()
            seen["summary"] = dialog.summary.cget("text")
            with mock.patch.object(settings_dialog.messagebox, "askyesno", return_value=True):
                dialog.force(PC, LOGIN)
                dialog.end()

        holder = {}
        real = settings_dialog.UpdateDialog.__init__

        def init(dialog, *a, **k):
            holder["dialog"] = dialog
            real(dialog, *a, **k)

        with mock.patch.object(settings_dialog.UpdateDialog, "__init__", init), \
                mock.patch.object(tk.Toplevel, "wait_window", wait), \
                mock.patch.object(tk.Toplevel, "grab_set", lambda self: None), \
                mock.patch.object(tk.Tk, "grab_set", lambda self: None):
            dialog = settings_dialog.UpdateDialog(root, path, "nlm.nippou", "日報",
                                                  master_no=number)
        self.assertEqual([(r[0], r[2], r[5]) for r in seen["rows"]],
                         [(PC, "処理中 (待っています)", True),
                          ("NLM-NGY-250962", "返事なし", False)])
        self.assertIn("処理中 (待っています) 1", seen["summary"])
        self.assertIn("返事なし 1", seen["summary"])
        self.assertTrue(dialog.ended)
        self.assertEqual(tool_master.under_maintenance(path), {})


if __name__ == "__main__":
    unittest.main()
