"""exe のツール (Tauri などのアプリ・Python から作ったサーバー)

* 画面の出し方を決める決まり (自動 / ブラウザー / アプリの窓)
* exe を選んだときに素性を読み取る (app.json・Tauri の見分け・名前)
* 戻り値の読み方、プロセスの見つけ方
* 進み具合の窓・配布先フォルダ・設定の変わり目
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import make_app_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import desktop, distribution, startup_progress, tool_registry  # noqa: E402
from launcher.tool_registry import Tool  # noqa: E402


class UiModeTests(unittest.TestCase):
    """画面の出し方 (`resolved_ui_mode`)。"""

    def tool(self, start: str, **kwargs) -> Tool:
        return Tool(app_id="a", display_name="A", start_command=start, **kwargs)

    def test_ポートの無いexeはアプリの窓(self) -> None:
        tool = self.tool("C:/tools/App.exe")
        self.assertEqual(tool.resolved_ui_mode, "app")
        self.assertTrue(tool.watches_window)
        self.assertEqual(tool.ui_problem(), "")

    def test_ポートのあるexeはブラウザー(self) -> None:
        """Python から作った Web サーバーの exe。"""
        tool = self.tool("C:/tools/server.exe", port=8733)
        self.assertEqual(tool.resolved_ui_mode, "browser")
        self.assertFalse(tool.watches_window)

    def test_batはこれまでどおりブラウザー(self) -> None:
        self.assertEqual(self.tool("C:/tools/start.bat", port=8733).resolved_ui_mode,
                         "browser")

    def test_指定すればそれに従う(self) -> None:
        tool = self.tool("C:/tools/tauri.exe", port=8733, ui_mode="app")
        self.assertTrue(tool.is_app)
        self.assertFalse(tool.watches_window, "Web サーバーがあれば応答で確かめる")

    def test_噛み合わない組み合わせは先に言う(self) -> None:
        self.assertIn("exe を直接指定",
                      self.tool("C:/t/start.bat", ui_mode="app").ui_problem())
        self.assertIn("ポートが要ります", self.tool("C:/t/start.bat").ui_problem())
        self.assertIn("アプリの窓",
                      self.tool("C:/t/a.exe", ui_mode="browser").ui_problem())
        self.assertEqual(self.tool("").ui_problem(), "", "未設定は起動時に言う")


class ProbeExeTests(LocalAreaTestCase):
    """［＋ ツールを追加］で exe を選んだとき。"""

    def exe(self, name: str, body: bytes = b"MZ\x90\x00") -> Path:
        folder = self.work_root / "app"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(body)
        return path

    def test_appjsonが無くても名前とアプリIDを決める(self) -> None:
        found = tool_registry.probe_tool_folder(str(self.exe("Daily Report.exe")))
        self.assertEqual(found["app_id"], "exe.daily-report")
        self.assertEqual(found["display_name"], "Daily Report")
        self.assertEqual(found["ui_mode"], "app", "開く URL が無いのでアプリの窓")
        self.assertEqual(tool_registry.validate_app_id(found["app_id"]), "")

    def test_Tauriのexeはアプリの窓(self) -> None:
        body = b"MZ" + b"\x00" * 100 + b"__TAURI_INTERNALS__" + b"\x00" * 100
        path = self.exe("kanban.exe", body)
        (path.parent / "app.json").write_text(
            '{"app_id": "nlm.kanban", "display_name": "看板",'
            ' "server": {"port": 8741}}', encoding="utf-8")
        found = tool_registry.probe_tool_folder(str(path))
        self.assertEqual(found["app_id"], "nlm.kanban")
        self.assertEqual(found["port"], 8741)
        # ポートがあっても、Tauri なら自分の窓 (中にサーバーを持つ形)
        self.assertEqual(found["ui_mode"], "app")
        self.assertEqual(found["kind"], "tauri")

    def test_サーバーのexeはブラウザーのまま(self) -> None:
        path = self.exe("server.exe")
        (path.parent / "config").mkdir()
        (path.parent / "config" / "app.json").write_text(
            '{"app_id": "nlm.daily", "server": {"port": 8733}}', encoding="utf-8")
        found = tool_registry.probe_tool_folder(str(path))
        self.assertEqual(found["port"], 8733)
        self.assertNotIn("ui_mode", found)

    def test_起動引数を決める(self) -> None:
        args, reason = tool_registry.recommend_start_args(str(self.exe("app.exe")))
        self.assertEqual(args, "")
        self.assertIn("窓", reason)

        server = self.exe("server.exe", b"MZ...argparse --no-browser ...")
        (server.parent / "app.json").write_text('{"server": {"port": 8733}}',
                                                encoding="utf-8")
        args, _ = tool_registry.recommend_start_args(str(server))
        self.assertEqual(args, "--no-browser")

    def test_画面の出し方を保存して読み戻す(self) -> None:
        tool_registry.save(Tool(app_id="exe.app", display_name="App",
                                start_command=str(self.exe("app.exe")),
                                ui_mode="app"))
        self.assertEqual(tool_registry.get("exe.app").ui_mode, "app")

    def test_配布先フォルダにも入る(self) -> None:
        tool_registry.save(Tool(app_id="exe.app", display_name="App",
                                start_command=str(self.exe("app.exe")),
                                ui_mode="app"))
        tool_registry.export_distribution(relative=False)
        item = next(t for t in distribution.tools() if t["app_id"] == "exe.app")
        self.assertEqual(item["ui_mode"], "app")

        tool_registry.save(Tool(app_id="exe.app", display_name="App",
                                start_command=item["start_command"]))
        tool_registry.reload_from_distribution()
        self.assertEqual(tool_registry.get("exe.app").ui_mode, "app")

    def test_設定の変わり目は呼び名で残す(self) -> None:
        before = [Tool(app_id="a", display_name="日報")]
        after = [Tool(app_id="a", display_name="日報", ui_mode="app")]
        self.assertEqual(tool_registry.describe_changes(before, after),
                         ["日報: 画面 「自動」→「アプリの窓」"])


class DesktopTests(unittest.TestCase):
    """窓とプロセス (Windows 以外で確かめられるところ)。"""

    def test_戻り値の意味(self) -> None:
        self.assertEqual(desktop.describe_exit_code(101),
                         "101 (Rust の panic (Tauri・Rust 製アプリの想定外のエラー))")
        self.assertIn("0xC0000135", desktop.describe_exit_code(0xC0000135))
        self.assertIn("DLL が見つからない", desktop.describe_exit_code(0xC0000135))
        # Python から見た Windows の戻り値は負になることもある
        self.assertIn("0xC0000005", desktop.describe_exit_code(-1073741819))
        self.assertEqual(desktop.describe_exit_code(3), "3")
        self.assertEqual(desktop.describe_exit_code(None), "不明")

    def test_Tauriの印が区切りをまたいでも見つける(self) -> None:
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.exe"
            # 読み込みの区切り (ここでは 8 バイト) をまたいで "tauri" を置く
            path.write_bytes(b"MZxxxxTA" + b"URI://localhost")
            with mock.patch.object(desktop, "_SCAN_CHUNK", 8):
                self.assertTrue(desktop.looks_like_tauri(path))
            path.write_bytes(b"MZ" + b"x" * 64)
            self.assertFalse(desktop.looks_like_tauri(path))
            self.assertFalse(desktop.looks_like_tauri(Path(tmp) / "start.bat"))

    @unittest.skipIf(os.name == "nt", "偽の exe は Windows 以外で動かす")
    def test_exeの道でプロセスを見つける(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            exe = make_app_dir(Path(tmp), app_id="find.me")
            proc = subprocess.Popen([str(exe)])
            try:
                deadline = time.monotonic() + 5
                while not desktop.find_by_exe(str(exe)) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertEqual(desktop.find_by_exe(str(exe)), [proc.pid])
                self.assertTrue(desktop.runs_exe(proc.pid, str(exe)))
                self.assertFalse(desktop.runs_exe(proc.pid, str(Path(tmp) / "other.exe")))
                self.assertIn(proc.pid, desktop.process_tree(os.getpid()))
            finally:
                proc.terminate()
                proc.wait(timeout=5)
            # 終わったら (引き取ったあとは) 見つからない
            self.assertFalse(desktop.runs_exe(proc.pid, str(exe)))
            self.assertEqual(desktop.find_by_exe(str(exe)), [])

    def test_窓の無い環境では分からないと答える(self) -> None:
        if desktop.IS_WINDOWS:
            self.skipTest("Windows では窓がある")
        self.assertIsNone(desktop.has_window(os.getpid()))
        self.assertIsNone(desktop.close_windows(os.getpid()))
        self.assertFalse(desktop.bring_to_front(os.getpid()))
        self.assertEqual(desktop.file_description(sys.executable), "")


class ProgressTests(unittest.TestCase):
    """アプリの窓のツールは「画面を開く」段が無い。"""

    def status(self, phase: str, **kwargs):
        from app_manager import State, Status
        return Status(state=State.STARTING, app_id="a", display_name="日報App",
                      phase=phase, ui_mode="app", **kwargs)

    def test_アプリは起動と確認の2段(self) -> None:
        tracker = startup_progress.ProgressTracker(clock=lambda: 0.0)
        tracker.update(self.status(startup_progress.SPAWN))
        view = tracker.update(self.status(startup_progress.WAIT,
                                          stage="窓が出るのを待っています"))
        self.assertEqual([s.label for s in view.steps],
                         ["日報Appを起動する", "日報Appの起動を確かめる"])
        self.assertEqual(view.note, "窓が出るのを待っています")


if __name__ == "__main__":
    unittest.main()
