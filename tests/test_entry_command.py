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



SHARE = "\\\\nlmfangyshrd\\各課共有\\0130_日軽稲沢\\梱包課\\AIM\\【◎】_Python_Distribution"
NEON = SHARE + "\\neon-coil-zero--claude-zealous-edison-x6vfes"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class SharedFolderTests(unittest.TestCase):
    """共有フォルダー (\\\\サーバー\\…) の .bat は **1.8.12 と同じに呼ぶ** (1.8.18)。

    1.8.13〜1.8.17 はランチャーがドライブ文字を割り当てて呼んでいたが、実機で
    確かめないまま入れ、pushd を使う日報複合ツールの起動確認を壊した。
    """

    def test_ファイルを選ぶ画面のスラッシュの道をバックスラッシュにする(self) -> None:
        with _Windows():
            command = paths.entry_command("C:/Tools/neon/start.bat")
        self.assertIn('""C:\\Tools\\neon\\start.bat""', command)

    def test_共有フォルダーのbatは書き方にかかわらずそのまま呼ぶ(self) -> None:
        for name in ("start.bat", "launcher_check.bat"):
            with self.subTest(name=name), _Windows():
                command = paths.entry_command(NEON.replace("\\", "/") + "/" + name, ["--x"])
                self.assertEqual(command, 'C:\\Windows\\system32\\cmd.exe /d /s /c '
                                          f'""{NEON}\\{name}" --x"')

    def test_ランチャーは作業フォルダーを変えない(self) -> None:
        self.assertFalse(hasattr(paths, "entry_cwd"))
        self.assertFalse(hasattr(paths, "unc_wrapper"))


class UncBatProblemTests(unittest.TestCase):
    """共有フォルダーから動かない書き方を見分けて、障害記録で知らせる (1.8.18)。"""

    def setUp(self) -> None:
        import tempfile

        self.folder = Path(tempfile.mkdtemp())
        paths._help_cache.clear()

    def copy(self, fixture: str, name: str) -> Path:
        target = self.folder / name
        target.write_bytes((FIXTURES / fixture).read_bytes())
        return target

    def test_日報複合ツールの起動確認は問題なし(self) -> None:
        bat = self.copy("all-tools_launcher_check.bat", "launcher_check.bat")
        self.assertFalse(paths.needs_unc_help(bat))

    def test_NEON_COIL_ZEROのstartは共有フォルダーで動かない書き方(self) -> None:
        bat = self.copy("neon-coil-zero_start.bat", "start.bat")
        self.assertTrue(paths.needs_unc_help(bat))

    def test_読めない_無い_どちらも無いものは問題なしとする(self) -> None:
        self.assertFalse(paths.needs_unc_help(self.folder / "無い.bat"))
        plain = self.folder / "plain.bat"
        plain.write_text("@echo off\r\npython main.py\r\n", encoding="ascii")
        self.assertFalse(paths.needs_unc_help(plain))

    def test_共有フォルダーのときだけ説明を出す(self) -> None:
        bat = self.copy("neon-coil-zero_start.bat", "start.bat")
        self.assertEqual(paths.unc_bat_problem(bat), "", "この PC の中なら問題ない")
        unc = NEON + "\\start.bat"
        with mock.patch.object(paths, "needs_unc_help", return_value=True):
            text = paths.unc_bat_problem(unc)
        self.assertIn('pushd "%~dp0"', text)
        self.assertIn("共有フォルダー", text)
        with mock.patch.object(paths, "needs_unc_help", return_value=False):
            self.assertEqual(paths.unc_bat_problem(unc), "")
        self.assertEqual(paths.unc_bat_problem(NEON + "\\app.exe"), "")

    def test_中身が変わったら読み直す(self) -> None:
        import os
        import time

        bat = self.copy("neon-coil-zero_start.bat", "start.bat")
        self.assertTrue(paths.needs_unc_help(bat))
        bat.write_text('@echo off\r\npushd "%~dp0"\r\n', encoding="ascii")
        later = time.time() + 5
        os.utime(bat, (later, later))
        self.assertFalse(paths.needs_unc_help(bat))


if __name__ == "__main__":
    unittest.main()
