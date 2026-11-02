"""起動ファイル・停止ファイルを実行するコマンド (1.8.6)

フォルダー名「コイルライン用　包装仕様表示」(全角の空白) の stop.bat を、ランチャーが
「'C:\\…\\コイルライン用' は、内部コマンドまたは外部コマンド…として認識されていません」
で動かせなかった (Coil_Packaging-Specifications の報告)。Python の `subprocess` は
半角の空白とタブがあるときしか " で囲まず、日本語の cmd は全角の空白でも区切るため。
**道はいつも " で囲み、.bat は `cmd /d /s /c ""道" 引数"` で呼ぶ。**
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from launcher import paths  # noqa: E402

FOLDER = "C:\\Users\\xfa-ngykonpo\\Desktop\\Python_Distribution\\コイルライン用\u3000包装仕様表示"


class _Windows:
    """`os.name` を "nt" にする (この呼び出しのあいだだけ)。"""

    def __enter__(self):
        self.name = os.name
        self.env = mock.patch.dict(os.environ, {"COMSPEC": "C:\\Windows\\system32\\cmd.exe"})
        self.env.start()
        os.name = "nt"
        return self

    def __exit__(self, *exc):
        os.name = self.name
        self.env.stop()


class WindowsCommandTests(unittest.TestCase):

    def test_Pythonは全角の空白を囲まない(self) -> None:
        # 直す前の呼び方 (引数のリスト) では、道が " で囲まれない
        self.assertEqual(subprocess.list2cmdline([FOLDER + "\\stop.bat"]),
                         FOLDER + "\\stop.bat")

    def test_batは道を囲んでcmdのsで呼ぶ(self) -> None:
        with _Windows():
            command = paths.entry_command(FOLDER + "\\stop.bat", ["--force"])
        self.assertEqual(command,
                         'C:\\Windows\\system32\\cmd.exe /d /s /c '
                         f'""{FOLDER}\\stop.bat" --force"')

    def test_半角の空白や括弧やアンドの道も一語(self) -> None:
        folder = "C:\\Program Files (x86)\\A&B ツール"
        with _Windows():
            command = paths.entry_command(folder + "\\start.bat")
        self.assertTrue(command.endswith(f'/d /s /c ""{folder}\\start.bat""'), command)

    def test_vbsとexe(self) -> None:
        with _Windows():
            vbs = paths.entry_command(FOLDER + "\\Start.vbs")
            exe = paths.entry_command(FOLDER + "\\包装仕様.exe", ["--port", "8 0"])
        self.assertEqual(vbs, f'wscript.exe "{FOLDER}\\Start.vbs"')
        self.assertEqual(exe, f'"{FOLDER}\\包装仕様.exe" --port "8 0"')

    def test_ほかのOSではこれまでどおり(self) -> None:
        self.assertEqual(paths.entry_command("/opt/t/start.bat", ["--x"]),
                         ["/opt/t/start.bat", "--x"])
        self.assertEqual(paths.entry_command("/opt/t/Start.vbs"),
                         ["wscript.exe", "/opt/t/Start.vbs"])


class CallSiteTests(unittest.TestCase):
    """起動・停止・起動確認の入口が、どれもこの呼び方を使う。"""

    def test_呼び出し元(self) -> None:
        root = Path(__file__).resolve().parent.parent
        for name, needle in (("app_manager.py", "paths.entry_command("),
                             ("process_manager.py", "_paths.entry_command("),
                             ("launcher/tool_entries.py", "paths.entry_command(")):
            self.assertIn(needle, (root / name).read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
