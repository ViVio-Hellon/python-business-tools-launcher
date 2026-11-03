"""ランチャー自身の更新 — 全 PC で閉じて、更新が終わったら起動し直す (1.8.11)

* 管理者が［ランチャーの更新のため全 PC で閉じる］を押すと、各 PC のランチャーは
  **ツールを止めずに**閉じる。閉じる前に、その PC の中に「待ち番」を置く
* 印を付けた PC (管理者) のランチャーは閉じない
* 更新中に起動しても「更新しています」と出して閉じる (混ざった版で動かさない)
* 印が外れたら、待ち番がランチャーを起動し直して自分は終わる
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, have_tk, release_tk  # noqa: E402

from launcher import app_config, maintenance, restart_waiter, tool_master, tool_registry  # noqa: E402

LOGIN, PC = "Xfa-Ngykonpo", "NLM-NGY-240638"
ADMIN = ("Xfa-Admin", "NLM-NGY-999999")


class UpdateTestCase(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"USERNAME": LOGIN, "COMPUTERNAME": PC})
        patcher.start()
        self.addCleanup(patcher.stop)
        shared = self.work_root / "share"
        tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, str(shared))
        self.path = tool_master.path_for(str(shared))
        tool_master.ensure(self.path)

    def mark_by_admin(self) -> None:
        with mock.patch.object(tool_master, "this_pc", return_value=ADMIN):
            tool_master.begin_maintenance(self.path, tool_master.LAUNCHER_MARK, "ランチャー")


class WaiterTests(UpdateTestCase):

    def test_印を読む(self) -> None:
        self.assertIs(restart_waiter.marked(str(self.path)), False)
        self.mark_by_admin()
        self.assertIs(restart_waiter.marked(str(self.path)), True)
        self.assertIsNone(restart_waiter.marked(str(self.work_root / "無い.sqlite3")))

    def test_本物の待ち番が印が外れたら起動し直して終わる(self) -> None:
        self.mark_by_admin()
        started = self.work_root / "started.txt"
        pid_file = self.work_root / "waiter.pid"
        command = [sys.executable, "-c", f"open(r'{started}', 'w').write('ok')"]
        waiter = subprocess.Popen(
            [sys.executable, "-I", str(Path(restart_waiter.__file__)), "--master", str(self.path),
             "--command", json.dumps(command), "--pid-file", str(pid_file), "--every", "0.2"])
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        time.sleep(1.0)
        self.assertIsNone(waiter.poll(), "印があるのに終わりました")
        self.assertFalse(started.exists(), "印があるのに起動しました")
        self.assertTrue(pid_file.exists())
        tool_master.end_maintenance(self.path, tool_master.LAUNCHER_MARK)
        self.assertEqual(waiter.wait(timeout=10), 0)
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(started.read_text(), "ok", "ランチャーを起動し直していません")
        self.assertFalse(pid_file.exists())

    def test_待ち番はこのPCの中にコピーして動かす(self) -> None:
        with mock.patch.object(maintenance.subprocess, "Popen") as popen:
            self.assertTrue(maintenance.start_waiter(self.path))
        command = popen.call_args.args[0]
        script = Path(command[2])
        self.assertEqual(script.parent, app_config.local_dir("runtime"))
        self.assertTrue(script.is_file())
        self.assertIn("--master", command)
        self.assertEqual(json.loads(command[command.index("--command") + 1]),
                         maintenance.restart_command())


class WatcherTests(UpdateTestCase):

    def test_ほかのPCが付けた印なら閉じるよう伝える(self) -> None:
        self.mark_by_admin()
        result = maintenance.Watcher(mock.Mock(starting_ids=[], running={})).poll()
        self.assertIsNotNone(result.close_launcher)
        self.assertEqual(result.blocked, {}, "ランチャーの印でツールを止めています")

    def test_自分で付けた印なら閉じない(self) -> None:
        tool_master.begin_maintenance(self.path, tool_master.LAUNCHER_MARK, "ランチャー")
        result = maintenance.Watcher(mock.Mock(starting_ids=[], running={})).poll()
        self.assertIsNone(result.close_launcher)
        self.assertIsNone(maintenance.launcher_hold())

    def test_更新中は起動しない(self) -> None:
        import boot

        self.mark_by_admin()
        result = boot.BootResult(started=True)
        with mock.patch.object(maintenance, "start_waiter", return_value=True) as waiter:
            self.assertTrue(boot._held_for_update(result))
        waiter.assert_called_once()
        self.assertFalse(result.started)
        self.assertTrue(result.held)
        self.assertIn("更新しています", result.reason)
        tool_master.end_maintenance(self.path, tool_master.LAUNCHER_MARK)
        self.assertFalse(boot._held_for_update(boot.BootResult(started=True)))


@unittest.skipUnless(have_tk(), "tkinter と画面が要る")
class ScreenTests(UpdateTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()

    def make_bar(self):
        from app_manager import ToolManager
        from launcher.ui import bar as bar_module

        bar = bar_module.LauncherBar(ToolManager())
        self.addCleanup(_destroy, bar.root)
        return bar

    def test_ツールを止めずに閉じる(self) -> None:
        import tkinter as tk

        self.mark_by_admin()
        bar = self.make_bar()
        mark = tool_master.under_maintenance(self.path)[tool_master.LAUNCHER_MARK]
        with mock.patch.object(maintenance, "start_waiter", return_value=True) as waiter, \
                mock.patch.object(bar.root, "after") as after:
            bar._apply_maintenance(maintenance.PollResult(close_launcher=mark, master=self.path))
        waiter.assert_called_once_with(self.path)
        notes = [w.title() for w in bar.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertIn("ランチャーの更新", notes)
        self.assertEqual(tool_master.reports(self.path, tool_master.LAUNCHER_MARK)[0].state,
                         tool_master.CLOSED)
        self.assertEqual(after.call_args.args[1], bar._finish_close_for_update)
        with mock.patch.object(bar.manager, "shutdown") as shutdown:
            bar._finish_close_for_update()
        shutdown.assert_called_once_with(stop_tools=False)
        self.assertFalse(_alive(bar.root), "ランチャーを閉じていません")

    def test_起動の途中なら閉じない(self) -> None:
        self.mark_by_admin()
        bar = self.make_bar()
        mark = tool_master.under_maintenance(self.path)[tool_master.LAUNCHER_MARK]
        with mock.patch.object(type(bar.manager), "starting_ids", ["nlm.nippou"]), \
                mock.patch.object(maintenance, "start_waiter") as waiter:
            bar._apply_maintenance(maintenance.PollResult(close_launcher=mark, master=self.path))
        waiter.assert_not_called()
        self.assertFalse(bar._closing_for_update)

    def test_管理者の一覧はこのPC以外の行を待つ(self) -> None:
        import tkinter as tk

        from launcher.ui import settings_dialog

        tool_master.save_pc(self.path, tool_master.MasterPc(0, login_id=LOGIN, pc_name=PC))
        tool_master.save_pc(self.path, tool_master.MasterPc(0, login_id="u", pc_name="PC-B"))
        tool_master.begin_maintenance(self.path, tool_master.LAUNCHER_MARK, "ランチャー")
        with mock.patch.object(tool_master, "this_pc", return_value=("u", "PC-B")):
            tool_master.report(self.path, tool_master.LAUNCHER_MARK, tool_master.CLOSED)
        root = tk.Tk()
        self.addCleanup(root.destroy)
        seen = {}
        holder = {}
        real = settings_dialog.UpdateDialog.__init__

        def init(dialog, *a, **k):
            holder["d"] = dialog
            real(dialog, *a, **k)

        def wait(top):
            top.update()
            seen["rows"] = holder["d"].rows()
            seen["verdict"] = holder["d"].verdict.cget("text")
            top.destroy()

        with mock.patch.object(settings_dialog.UpdateDialog, "__init__", init), \
                mock.patch.object(tk.Toplevel, "wait_window", wait), \
                mock.patch.object(tk.Toplevel, "grab_set", lambda self: None), \
                mock.patch.object(tk.Tk, "grab_set", lambda self: None):
            settings_dialog.UpdateDialog(root, self.path, tool_master.LAUNCHER_MARK,
                                         "ランチャー", launcher=True)
        self.assertEqual([(r[0], r[2]) for r in seen["rows"]], [("PC-B", "閉じた")])
        self.assertIn("全 PC でランチャーを閉じました (1 台)", seen["verdict"])


def _alive(root) -> bool:
    import tkinter as tk

    try:
        return bool(root.winfo_exists())
    except tk.TclError:
        return False


def _destroy(root) -> None:
    if _alive(root):
        root.destroy()


if __name__ == "__main__":
    unittest.main()
