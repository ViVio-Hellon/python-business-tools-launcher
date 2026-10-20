"""新しく登録する3ツール (run-run-panic・db-viewer・photo-cleaner) の形 (1.7.8)

* run-run-panic (DODGE CHAOS) と db-viewer は、これまでの Web のツールと同じ形
  (`config/app.json` のポート・`/api/health`・`Start.vbs`・`stop.bat`)。そのまま
  登録できる。アプリIDに `nlm.` が付かない (`dodge-chaos`) ことだけが違う
* photo-cleaner は **Web サーバーを持たず、自分の窓 (tkinter) を出す** Python の
  ツール。`config/app.json` も無い。起動ファイルは `start.bat` で、
  `start "" pyw main.py` で本体を起こして**すぐ終わる**。以前は「Web サーバーを
  持たないアプリは exe を直接指定してください」と断っていた。窓のプロセスを
  **ツールのフォルダーから起動したもの**で見分けて、exe のアプリと同じように扱う
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402
from test_tool_entries import _script  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State  # noqa: E402
from launcher import desktop, tool_registry  # noqa: E402
from launcher.tool_registry import UI_APP, Tool  # noqa: E402

# 窓の代わり (止められるまで動く。tkinter の窓は画面の無い試験環境では出せない)
MAIN_PY = """\
import time
# import tkinter  (本物は tkinter の窓を出す)
while True:
    time.sleep(0.2)
"""

# photo-cleaner の start.bat と同じ形: pyw で本体を起こして、すぐ終わる
START_BAT_WINDOWS = ('@echo off\r\nstart "" pyw -3 "%~dp0main.py" %*\r\n'
                     'exit /b 0\r\n')


def make_desktop_tool(base: Path, name: str = "photo-cleaner", *,
                      readme: str = "# Photo Cleaner\n", runnable: bool = False) -> Path:
    """Web サーバーを持たない、窓のツールのフォルダー。起動ファイルの道を返す。"""
    root = base / name
    root.mkdir(parents=True)
    (root / "main.py").write_text(MAIN_PY, encoding="utf-8")
    if readme:
        (root / "README.md").write_text(readme, encoding="utf-8")
    if runnable:
        # 試験で動かす版。中身は start.bat と同じ (本体を起こしてすぐ終わる)
        return _script(root / "start.bat",
                       f'"{sys.executable}" "$PWD/main.py" </dev/null >/dev/null 2>&1 &\n'
                       "exit 0\n")
    start = root / "start.bat"
    start.write_text(START_BAT_WINDOWS, encoding="utf-8")
    return start


class ProbeTests(LocalAreaTestCase):

    def test_窓のツールと見分けて名前とアプリIDを決める(self) -> None:
        start = make_desktop_tool(self.work_root)
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual(found["ui_mode"], UI_APP)
        self.assertEqual(found["display_name"], "Photo Cleaner")
        self.assertEqual(found["app_id"], "app.photo-cleaner")
        args, reason = tool_registry.recommend_start_args(str(start))
        self.assertEqual(args, "")
        self.assertIn("自分で窓を出すアプリ", reason)

    def test_READMEが無ければフォルダー名(self) -> None:
        start = make_desktop_tool(self.work_root, "写真整理", readme="")
        self.assertEqual(tool_registry.probe_tool_folder(str(start))["display_name"], "写真整理")

    def test_tkinterを使うだけでも見分ける(self) -> None:
        start = make_desktop_tool(self.work_root)
        start.write_text('@echo off\r\npython "%~dp0main.py"\r\n', encoding="utf-8")
        (start.parent / "main.py").write_text("import tkinter\n", encoding="utf-8")
        self.assertEqual(tool_registry.probe_tool_folder(str(start)).get("ui_mode"), UI_APP)

    def test_ポートのあるWebのツールは窓のツールにしない(self) -> None:
        """Web のツールの Start.vbs も pythonw で起こす。ポートがあれば Web のツール。"""
        start = make_desktop_tool(self.work_root, "run-run-panic")
        (start.parent / "config").mkdir()
        (start.parent / "config" / "app.json").write_text(json.dumps(
            {"app_id": "dodge-chaos", "display_name": "DODGE CHAOS",
             "server": {"port": 8765}}), encoding="utf-8")
        found = tool_registry.probe_tool_folder(str(start))
        self.assertNotIn("ui_mode", found)
        self.assertEqual((found["app_id"], found["port"]), ("dodge-chaos", 8765))

    def test_入口のあるツールは窓のツールにしない(self) -> None:
        start = make_desktop_tool(self.work_root)
        (start.parent / "launcher_check.bat").write_text("@echo off\r\n", encoding="utf-8")
        self.assertNotIn("ui_mode", tool_registry.probe_tool_folder(str(start)))

    def test_startbatの窓のツールを登録できる(self) -> None:
        start = make_desktop_tool(self.work_root)
        tool = Tool(app_id="app.photo-cleaner", display_name="Photo Cleaner",
                    start_command=str(start), ui_mode=UI_APP)
        self.assertEqual(tool.ui_problem(), "")
        self.assertIn("窓の × を押したのと同じように閉じます",
                      "\n".join(tool_registry.setting_hints(tool)))
        # 「自動」のままならヒントで知らせ、保存のときも理由を言う
        auto = Tool(app_id="app.photo-cleaner", display_name="Photo Cleaner",
                    start_command=str(start))
        self.assertIn("画面を「アプリの窓」にしてください",
                      "\n".join(tool_registry.setting_hints(auto)))
        self.assertIn("「アプリの窓」", auto.ui_problem())

    def test_ほかのツールとフォルダーを分け合っていれば断る(self) -> None:
        start = make_desktop_tool(self.work_root)
        tool_registry.save(Tool(app_id="other", display_name="ほか",
                                start_command=str(start.parent / "other.bat")))
        tool = Tool(app_id="app.photo-cleaner", display_name="Photo Cleaner",
                    start_command=str(start), ui_mode=UI_APP)
        self.assertIn("フォルダーを分けて", tool.ui_problem())


class DesktopToolLifecycleTests(ManagerTestCase):
    """起動 → 見回り → もう一度押す → 止める / 利用者が窓を閉じる。"""

    def setUp(self) -> None:
        super().setUp()
        start = make_desktop_tool(self.work_root, runnable=True)
        self.root = start.parent
        found = tool_registry.probe_tool_folder(str(start))
        tool_registry.save(Tool(app_id=found["app_id"], display_name=found["display_name"],
                                start_command=str(start), ui_mode=found["ui_mode"]))
        self.tool = tool_registry.get("app.photo-cleaner")
        self.addCleanup(self._kill_left)

    def _kill_left(self) -> None:
        for pid in desktop.processes_in_folder(str(self.root)):
            process_manager.terminate(pid, force=True, tree=False)

    def tool_pids(self) -> list[int]:
        return sorted(desktop.processes_in_folder(str(self.root)))

    def test_起動して見回り止める(self) -> None:
        self.start(self.tool)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING, status.detail)
        pids = self.tool_pids()
        self.assertTrue(pids)
        self.assertFalse(self.opened, "窓のツールにブラウザーを開きました")
        for _ in range(4):
            self.assertTrue(self.manager.poll_health())
            self.assertIn(self.tool.app_id, self.manager.running,
                          "窓が出ているのに、応答が無いとして外しました")
        # もう一度押しても2つ目を起こさない
        self.start(self.tool)
        self.assertEqual(self.tool_pids(), pids)
        self.assertTrue(self.manager._stop_blocking(self.tool.app_id))
        deadline = time.monotonic() + 5
        while self.tool_pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.tool_pids(), [])
        self.assertNotIn(self.tool.app_id, self.manager.running)

    def test_利用者が窓を閉じたら外す(self) -> None:
        self.start(self.tool)
        for pid in self.tool_pids():
            process_manager.terminate(pid, force=False, tree=False)
        deadline = time.monotonic() + 5
        while self.tool_pids() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.manager.poll_health()
        self.assertNotIn(self.tool.app_id, self.manager.running)
        self.assertNotEqual(self.manager.status.state, State.ERROR)

    def test_ランチャーの外で開いていたら引き継ぐ(self) -> None:
        import subprocess

        outside = subprocess.Popen([sys.executable, str(self.root / "main.py")],
                                   stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: (outside.kill(), outside.wait(5)))
        self.start(self.tool)
        self.assertEqual(self.tool_pids(), [outside.pid], "2つ目を起こしました")
        self.assertIn(self.tool.app_id, self.manager.running)


if __name__ == "__main__":
    unittest.main()
