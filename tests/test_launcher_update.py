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

from _isolation import LocalAreaTestCase, have_tk, have_tkinter, release_tk  # noqa: E402

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


    def _waiter_pid_file(self, pid: int, *, written: float) -> Path:
        folder = app_config.local_dir("runtime")
        folder.mkdir(parents=True, exist_ok=True)
        pid_file = folder / maintenance.WAITER_PID
        pid_file.write_text(str(pid), encoding="utf-8")
        os.utime(pid_file, (written, written))
        return pid_file

    def test_再起動のあと番号を使い回した別のプログラムを待ち番と取り違えない(self) -> None:
        """待ち番が動いたまま PC を終了すると、番号のファイルが残る。再起動のあと
        同じ番号を持つ別のプログラム (ファイルより後に起動した) は待ち番ではない。"""
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        time.sleep(0.3)
        # ファイルは 1 時間前 (再起動の前) に書かれた
        self._waiter_pid_file(other.pid, written=time.time() - 3600)
        with mock.patch.object(maintenance, "WAITER_NAME", "python"):
            # 名前が合っても、起動の時刻がファイルより後なら別のプロセス
            self.assertFalse(maintenance.waiter_running())

    def test_コマンドラインが読めないときも起動の時刻で決める(self) -> None:
        import process_manager

        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        time.sleep(0.3)
        with mock.patch.object(process_manager, "process_command_line", return_value=""):
            self._waiter_pid_file(other.pid, written=time.time() - 3600)
            self.assertFalse(maintenance.waiter_running(), "別のプログラムを待ち番とみなしました")
            # 番号を書いたのがそのプロセス自身 (書いた時刻より前に起動していた)
            self._waiter_pid_file(other.pid, written=time.time())
            self.assertTrue(maintenance.waiter_running())

    def test_番号が使い回されていたら待ち番を置き直す(self) -> None:
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: (other.kill(), other.wait()))
        time.sleep(0.3)
        self._waiter_pid_file(other.pid, written=time.time() - 3600)
        with mock.patch.object(maintenance.subprocess, "Popen") as popen:
            self.assertTrue(maintenance.start_waiter(self.path))
        popen.assert_called_once()


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


class ForgottenMarkTests(UpdateTestCase):
    """［更新を終える］を押し忘れて帰ると、全 PC で起動しなくなった (1.8.15)。"""

    def age_mark(self, hours: float) -> None:
        import sqlite3

        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - hours * 3600))
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE maintenance SET started_at = ? WHERE app_id = ?",
                         (stamp + ".000000", tool_master.LAUNCHER_MARK))

    def test_古い印では起動を止めない(self) -> None:
        self.mark_by_admin()
        self.assertIsNotNone(maintenance.launcher_hold())
        self.age_mark(maintenance.LAUNCHER_MARK_MAX_HOURS + 0.5)
        self.assertIsNone(maintenance.launcher_hold())

    def test_古い印では動いているランチャーも閉じない(self) -> None:
        self.mark_by_admin()
        self.age_mark(maintenance.LAUNCHER_MARK_MAX_HOURS + 0.5)
        result = maintenance.Watcher(mock.Mock())._poll()
        self.assertIsNone(result.close_launcher)

    def test_起動できないPCから管理者が外せる(self) -> None:
        self.mark_by_admin()
        with mock.patch.object(maintenance, "waiter_running", return_value=True):
            text = maintenance.end_launcher_update_by_admin()
        self.assertIn("自動でランチャーが起動します", text)
        self.assertNotIn(tool_master.LAUNCHER_MARK, tool_master.under_maintenance(self.path))

    def test_待ち番がいなければその場で起動する(self) -> None:
        self.mark_by_admin()
        with mock.patch.object(maintenance, "waiter_running", return_value=False), \
                mock.patch.object(maintenance.subprocess, "Popen") as popen:
            text = maintenance.end_launcher_update_by_admin()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], maintenance.restart_command())
        self.assertIn("起動します", text)


@unittest.skipUnless(have_tkinter(), "tkinter が要る (画面の部品を読む)")
class AdminListTests(UpdateTestCase):
    """管理者の「ランチャーの更新」の一覧 (1.8.14: 「返事なし」ばかりで使えなかった)。"""

    def rows(self):
        from launcher.ui.settings_dialog import UpdateDialog

        dialog = UpdateDialog.__new__(UpdateDialog)
        dialog.path, dialog.app_id, dialog.launcher = self.path, tool_master.LAUNCHER_MARK, True
        dialog.master_no = 0
        with mock.patch.object(tool_master, "this_pc", return_value=ADMIN):
            rows = dialog.rows()
        return dialog, {r[0]: r for r in rows}

    def as_pc(self, login: str, pc: str):
        return mock.patch.object(tool_master, "this_pc", return_value=(login, pc))

    def test_動いていることと版を書く(self) -> None:
        watcher = maintenance.Watcher(mock.Mock())
        watcher._poll()
        from launcher import __version__

        seen = tool_master.launchers_seen(self.path)
        self.assertEqual([(s.pc_name, s.version) for s in seen], [(PC, __version__)])
        with mock.patch.object(tool_master, "heartbeat") as beat:
            watcher._poll()                     # 1 分に 1 回
        beat.assert_not_called()

    def test_前の版のマスタにも書ける(self) -> None:
        import sqlite3

        with sqlite3.connect(self.path) as conn:
            conn.execute("DROP TABLE launchers")
        self.assertEqual(tool_master.launchers_seen(self.path), [])
        tool_master.heartbeat(self.path, "1.8.14")
        self.assertEqual(len(tool_master.launchers_seen(self.path)), 1)

    def test_前の版の返事は古い版として数える(self) -> None:
        """1.8.8〜1.8.10 はランチャーの印をツールの印と取り違え「使っていない」と書く。"""
        tool_master.save_pc(self.path, tool_master.MasterPc(0, login_id="a", pc_name="PC-OLD"))
        self.mark_by_admin()
        with self.as_pc("a", "PC-OLD"):
            tool_master.report(self.path, tool_master.LAUNCHER_MARK, tool_master.NOT_RUNNING)
        dialog, rows = self.rows()
        self.assertEqual(rows["PC-OLD"][2], dialog.OLD_VERSION)
        self.assertFalse(dialog._launcher_done(rows["PC-OLD"]))

    def test_ログインIDが違ってもPC名で照らし合わせる(self) -> None:
        tool_master.save_pc(self.path, tool_master.MasterPc(0, login_id="登録した人",
                                                            pc_name="PC-B"))
        self.mark_by_admin()
        with self.as_pc("いまの人", "pc-b"):
            tool_master.report(self.path, tool_master.LAUNCHER_MARK, tool_master.CLOSED)
        dialog, rows = self.rows()
        self.assertEqual(list(rows), ["PC-B"])
        self.assertEqual(rows["PC-B"][2], "閉じた")

    def test_返事なしに版と最後に見えた時刻を添える(self) -> None:
        for name in ("PC-NEW", "PC-GONE", "PC-NONE"):
            tool_master.save_pc(self.path, tool_master.MasterPc(0, login_id="", pc_name=name))
        self.mark_by_admin()
        with self.as_pc("a", "PC-NEW"):
            tool_master.heartbeat(self.path, "1.8.14")
        with self.as_pc("b", "PC-GONE"):
            tool_master.heartbeat(self.path, "1.8.14")
        import sqlite3

        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE launchers SET seen_at = '2026-11-01 08:00:00' "
                         "WHERE pc_name = 'PC-GONE'")
        dialog, rows = self.rows()
        self.assertEqual(rows["PC-NEW"][2], "返事なし")
        self.assertIn("版 1.8.14 が動いています", rows["PC-NEW"][3])
        self.assertFalse(dialog._launcher_done(rows["PC-NEW"]))
        self.assertEqual(rows["PC-GONE"][2], "動いていない")
        self.assertTrue(dialog._launcher_done(rows["PC-GONE"]))
        self.assertIn("1.8.14 より前の版か、起動していない", rows["PC-NONE"][3])

    def test_マスタに行の無いPCも動いていれば並べる(self) -> None:
        self.mark_by_admin()
        with self.as_pc("x", "PC-NOROW"):
            tool_master.heartbeat(self.path, "1.8.14")
        with self.as_pc(*ADMIN):
            tool_master.heartbeat(self.path, "1.8.14")      # 印を付けた PC は並べない
        _dialog, rows = self.rows()
        self.assertEqual(list(rows), ["PC-NOROW"])

    def test_待っているPCは強制で閉じられる(self) -> None:
        self.mark_by_admin()
        with self.as_pc("a", "PC-W"):
            maintenance.report_waiting(self.path, "「設定」の窓を開いている")
        _dialog, rows = self.rows()
        self.assertEqual(rows["PC-W"][2], "閉じるのを待っている")
        self.assertTrue(rows["PC-W"][5])
        with mock.patch.object(tool_master, "this_pc", return_value=ADMIN):
            tool_master.request_force(self.path, tool_master.LAUNCHER_MARK, "a", "PC-W")
        with self.as_pc("a", "PC-W"):
            result = maintenance.Watcher(mock.Mock())._poll()
        self.assertTrue(result.force_close)

    def test_閉じたの書き込みは失敗したら書き直す(self) -> None:
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise tool_master.MasterError("database is locked")

        with mock.patch.object(tool_master, "report", side_effect=flaky):
            self.assertTrue(maintenance.report_closed(self.path, pause=0))
        self.assertEqual(len(calls), 3)


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
        bar._closed_report.join(10)           # 「閉じた」は別のスレッドで書く
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

    def test_待っているあいだも理由を返事し強制なら閉じる(self) -> None:
        """黙って待つと、管理者には「返事なし」に見える (1.8.14)。"""
        import threading

        self.mark_by_admin()
        bar = self.make_bar()
        mark = tool_master.under_maintenance(self.path)[tool_master.LAUNCHER_MARK]
        with mock.patch.object(type(bar.manager), "starting_ids", ["nlm.nippou"]), \
                mock.patch.object(maintenance, "start_waiter", return_value=True) as waiter:
            bar._apply_maintenance(maintenance.PollResult(close_launcher=mark, master=self.path))
            for thread in threading.enumerate():
                if thread.name == "report-waiting":
                    thread.join(10)
            report = tool_master.reports(self.path, tool_master.LAUNCHER_MARK)[0]
            self.assertEqual(report.state, tool_master.WAITING)
            self.assertIn("起動の途中", report.message)
            waiter.assert_not_called()
            # 管理者が［強制で閉じる］を押した
            with mock.patch.object(bar.root, "after"):
                bar._apply_maintenance(maintenance.PollResult(
                    close_launcher=mark, master=self.path, force_close=True))
        waiter.assert_called_once()
        self.assertTrue(bar._closing_for_update)
        bar._closed_report.join(10)
        self.assertEqual(tool_master.reports(self.path, tool_master.LAUNCHER_MARK)[0].state,
                         tool_master.CLOSED)

    def test_見回りが固まったら次を始める(self) -> None:
        bar = self.make_bar()
        bar._maintenance_busy = True
        bar._maintenance_started = time.monotonic() - bar.MAINTENANCE_STUCK_SEC - 1
        started = []
        with mock.patch.object(bar._maintenance, "poll", side_effect=lambda: started.append(1)), \
                mock.patch.object(bar.root, "after"):
            bar._poll_maintenance()
        deadline = time.monotonic() + 5
        while not started and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(started, [1])

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
