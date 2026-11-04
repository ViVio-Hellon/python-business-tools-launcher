"""「更新」の流れを最後まで通す試験 (1.8.16)

部品ごとの試験は通っていたのに、現場で「［更新を終える］を押したのに、全 PC で
『更新中』のまま起動できない」が起きた。止める側だけを試し、外す側を同じ条件
(上書きでアプリIDが変わる) で試していなかった。ここでは、管理者の PC と利用者の
PC を 1 つの試験の中で入れ替えながら、**押す順番どおりに最後まで**たどる。

* 利用者の PC (PC-B): この試験の `ToolManager` と `Watcher` (本物の偽ツールを動かす)
* 管理者の PC (PC-A): `this_pc` を差し替えて、［設定］・状況の窓のコードを呼ぶ
* ツール管理マスタ: 一時フォルダーの本物の SQLite
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import have_tk, have_tkinter, release_tk  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

from app_manager import State  # noqa: E402
from launcher import maintenance, tool_master, tool_registry  # noqa: E402

USER = ("xfa-line", "PC-B")
ADMIN = ("Xfa-Admin", "PC-A")


class FlowTestCase(ManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": USER[0], "COMPUTERNAME": USER[1]})
        patcher.start()
        self.addCleanup(patcher.stop)
        shared = self.work_root / "share"
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(shared))
        self.path = tool_master.path_for(str(shared))
        tool_master.ensure(self.path)
        self.watcher = maintenance.Watcher(self.manager)

    # --- 管理者の PC で ----------------------------------------------------
    def as_admin(self):
        return mock.patch.object(tool_master, "this_pc", return_value=ADMIN)

    def admin_dialog(self, app_id: str, *, start_command: str = "", launcher: bool = False):
        """状況の窓 (画面は出さない。並べ方・判断・ボタンの中身だけ使う)。"""
        from launcher.ui.settings_dialog import UpdateDialog

        dialog = UpdateDialog.__new__(UpdateDialog)
        dialog.path, dialog.app_id, dialog.name = self.path, app_id, app_id
        dialog.master_no, dialog.launcher = 0, launcher
        dialog.start_command, dialog.ended = start_command, False
        dialog.top = mock.Mock()
        dialog._job = None
        dialog.verdict = mock.Mock()
        dialog.all_stopped = False
        return dialog

    def admin_rows(self, dialog):
        with self.as_admin():
            return {r[0]: r for r in dialog.rows()}

    def admin_press_end(self, dialog) -> None:
        """［更新を終える］→「終わりましたか?」に［はい］。"""
        from tkinter import messagebox

        with self.as_admin(), mock.patch.object(messagebox, "askyesno", return_value=True), \
                mock.patch.object(messagebox, "showerror") as error, \
                mock.patch.object(messagebox, "showwarning"):
            dialog.end()
        self.assertFalse(error.called, error.call_args)
        self.assertTrue(dialog.ended)

    # --- 利用者の PC で ----------------------------------------------------
    def user_poll(self):
        return self.watcher.poll()

    def age(self, app_id: str, hours: float) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE maintenance SET started_at = ? WHERE app_id = ?",
                         (stamp + ".000000", app_id))


@unittest.skipUnless(have_tkinter(), "tkinter が要る (管理者の窓のコード)")
class ToolUpdateFlowTests(FlowTestCase):

    def test_止める_上書きでアプリIDが変わる_更新を終える_また使える(self) -> None:
        """現場で起きた順番。"""
        from launcher.ui.settings_dialog import SettingsDialog

        tool = self.register("nlm.nippou", "日報看板")
        self.start(tool)
        # 1. 管理者が［更新のため全 PC で止める］(begin_update と同じ呼び方)
        with self.as_admin():
            tool_master.begin_maintenance(self.path, "nlm.nippou", "日報看板",
                                          start_command=tool.start_command)
        # 2. 利用者の PC が止めて「止めた」と書く
        self.assertIn("nlm.nippou", self.user_poll().blocked)
        self.assertNotIn("nlm.nippou", self.manager.running)
        dialog = self.admin_dialog("nlm.nippou", start_command=tool.start_command)
        self.assertEqual(self.admin_rows(dialog)["PC-B"][2], "止めた")
        # 3. 上書き。新しい版でアプリIDが変わる (app.json が入った など)。
        #    **フォルダーの中身を本当に入れ替える** (起動ファイルの道は同じ)
        folder = Path(tool.start_command).parent
        for name in ("start.bat", "config/app.json"):
            target = folder / name
            target.write_text(target.read_text(encoding="utf-8").replace(
                "'--app-id' 'nlm.nippou'", "'--app-id' 'app.nippou-v2'").replace(
                '"app_id": "nlm.nippou"', '"app_id": "app.nippou-v2"'), encoding="utf-8")
        self.assertIn("app.nippou-v2", (folder / "start.bat").read_text(encoding="utf-8"))
        tool_registry.delete_tool("nlm.nippou")
        new = tool_registry.Tool(app_id="app.nippou-v2", display_name="日報看板",
                                 port=tool.port, start_command=tool.start_command,
                                 start_args=tool.start_args)
        tool_registry.save(new)
        self.assertIn("app.nippou-v2", self.user_poll().blocked, "起動ファイルの道で止め続ける")
        # 4. 管理者が［設定］を開き直す。**新しいアプリIDでも「更新中」が見える**
        settings = SettingsDialog.__new__(SettingsDialog)
        settings._marks = lambda fresh=False: tool_master.under_maintenance(self.path)
        row = mock.Mock()
        row.tool.app_id = "app.nippou-v2"
        row.path_var.get.return_value = new.start_command
        app_id, mark = settings._mark_of(row)
        self.assertIsNotNone(mark, "［設定］に「更新中」が出ません (終わったように見える)")
        # 5. ［更新の状況を見る］→［更新を終える］
        self.admin_press_end(self.admin_dialog(app_id, start_command=new.start_command))
        self.assertEqual(tool_master.under_maintenance(self.path), {})
        # 6. 利用者の PC で、また使える
        self.assertEqual(self.user_poll().blocked, {})
        self.start(tool_registry.get("app.nippou-v2"))
        self.assertEqual(self.manager.status.state, State.RUNNING, self.manager.status.detail)

    def test_処理中_利用者に頼む_管理者が強制終了_止まる_終える(self) -> None:
        from tkinter import messagebox

        tool = self.register("nlm.kanban", "看板", busy=True)
        self.start(tool)
        with self.as_admin():
            tool_master.begin_maintenance(self.path, "nlm.kanban", "看板",
                                          start_command=tool.start_command)
        self.assertEqual(self.user_poll().ask_user, [("nlm.kanban", "看板")])
        dialog = self.admin_dialog("nlm.kanban", start_command=tool.start_command)
        row = self.admin_rows(dialog)["PC-B"]
        self.assertEqual(row[2], "処理中 (待っています)")
        self.assertTrue(row[5], "［強制終了］が出ません")
        with self.as_admin(), mock.patch.object(messagebox, "askyesno", return_value=True), \
                mock.patch.object(dialog, "refresh"):
            dialog.force(row[0], row[1])
        self.user_poll()
        self.assertNotIn("nlm.kanban", self.manager.running)
        self.assertEqual(self.admin_rows(dialog)["PC-B"][2], "止めた")
        self.admin_press_end(dialog)
        self.assertEqual(self.user_poll().blocked, {})

    def later(self, hours: float):
        """この PC のランチャーが、印を見続けて `hours` 時間たった (時計は動かさない)。"""
        now = time.monotonic() + hours * 3600
        return mock.patch.object(maintenance.time, "monotonic", return_value=now)

    def test_押し忘れても3時間で外れ_前の版のPCも戻る(self) -> None:
        tool = self.register("nlm.nippou", "日報看板")
        with self.as_admin():
            tool_master.begin_maintenance(self.path, "nlm.nippou", "日報看板",
                                          start_command=tool.start_command)
        self.assertIn("nlm.nippou", self.user_poll().blocked)
        with self.later(maintenance.MARK_MAX_HOURS - 0.5):
            self.assertIn("nlm.nippou", self.user_poll().blocked, "まだ外さない")
        with self.later(maintenance.MARK_MAX_HOURS + 0.5):
            self.assertEqual(self.user_poll().blocked, {})
        # マスタから外れている = 印を読むだけの前の版のランチャーも、もとに戻る
        self.assertEqual(tool_master.under_maintenance(self.path), {})
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING, self.manager.status.detail)


class LauncherMarkFlowTests(FlowTestCase):

    def test_ランチャーの印の押し忘れも外れて起動できる(self) -> None:
        with self.as_admin():
            tool_master.begin_maintenance(self.path, tool_master.LAUNCHER_MARK, "ランチャー")
        self.assertIsNotNone(maintenance.launcher_hold())
        # 夜のあいだ押し忘れ、朝に起動した (時計の上で 12 時間より前)
        self.age(tool_master.LAUNCHER_MARK, maintenance.MARK_WALL_HOURS + 0.5)
        self.assertIsNone(maintenance.launcher_hold(), "古い印で起動を止めました")
        self.assertIsNone(self.user_poll().close_launcher,
                          "朝に起動したランチャーが、古い印ですぐ閉じました")
        later = time.monotonic() + maintenance.MARK_SEEN_MIN_SEC + 1
        with mock.patch.object(maintenance.time, "monotonic", return_value=later):
            self.assertIsNone(self.user_poll().close_launcher)
        self.assertEqual(tool_master.under_maintenance(self.path), {})


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class ScreenFlowTests(FlowTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()

    def make_bar(self):
        from launcher.ui import bar as bar_module

        bar = bar_module.LauncherBar(self.manager)
        self.addCleanup(_destroy, bar.root)
        return bar

    def test_バーの更新中から管理者がその場で終える(self) -> None:
        from tkinter import messagebox

        from launcher.ui import password

        tool = self.register("nlm.nippou", "日報看板")
        with self.as_admin():
            tool_master.begin_maintenance(self.path, "nlm.nippou", "日報看板",
                                          start_command=tool.start_command)
        bar = self.make_bar()
        bar._apply_maintenance(self.user_poll())
        self.assertIn("nlm.nippou", bar._blocked)
        prompt = mock.Mock()
        prompt.return_value.value = "nisk"
        with mock.patch.object(messagebox, "askyesno", return_value=True) as ask, \
                mock.patch.object(messagebox, "showinfo") as info, \
                mock.patch.object(password, "_Prompt", prompt), \
                mock.patch("launcher.distribution.verify_password", return_value=True):
            bar.on_select("nlm.nippou")
        self.assertIn("Xfa-Admin / PC-A", ask.call_args.args[1], "誰が始めたかが出ません")
        self.assertIn("更新を終えました", info.call_args.args[1])
        self.assertEqual(tool_master.under_maintenance(self.path), {})
        bar._apply_maintenance(self.user_poll())
        self.assertNotIn("nlm.nippou", bar._blocked)

    def test_ランチャーの更新_閉じる_返事_終える_待ち番が起動する(self) -> None:
        started = self.work_root / "relaunched.txt"
        relaunch = [sys.executable, "-c", f"open(r'{started}', 'w').write('ok')"]
        with self.as_admin():
            tool_master.begin_maintenance(self.path, tool_master.LAUNCHER_MARK, "ランチャー")
        bar = self.make_bar()
        result = self.user_poll()
        self.assertIsNotNone(result.close_launcher)
        with mock.patch.object(maintenance, "restart_command", return_value=relaunch), \
                mock.patch.object(maintenance, "POLL_SEC", 0.2), \
                mock.patch.object(bar.root, "after"):
            bar._apply_maintenance(result)
        self.addCleanup(_kill_waiter)
        bar._closed_report.join(10)
        with mock.patch.object(self.manager, "shutdown") as shutdown:
            bar._finish_close_for_update()
        shutdown.assert_called_once_with(stop_tools=False)
        dialog = self.admin_dialog(tool_master.LAUNCHER_MARK, launcher=True)
        rows = self.admin_rows(dialog)
        self.assertEqual(rows["PC-B"][2], "閉じた")
        self.assertTrue(all(dialog._launcher_done(r) for r in rows.values()))
        self.assertFalse(started.exists(), "更新を終える前に起動しました")
        self.admin_press_end(dialog)
        deadline = time.monotonic() + 30
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(started.exists(), "待ち番がランチャーを起動し直しません")


def _kill_waiter() -> None:
    from launcher import app_config

    pid_file = app_config.local_dir("runtime") / maintenance.WAITER_PID
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, 9)
    except OSError:
        pass


def _destroy(root) -> None:
    import tkinter as tk

    try:
        root.destroy()
    except tk.TclError:
        pass


if __name__ == "__main__":
    unittest.main()
