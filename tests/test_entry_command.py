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


SHARE = "\\\\nlmfangyshrd\\各課共有\\0130_日軽稲沢\\梱包課\\AIM\\【◎】_Python_Distribution"
NEON = SHARE + "\\neon-coil-zero--claude-zealous-edison-x6vfes"
WRAPPER = "C:\\Users\\u\\AppData\\Local\\BusinessToolsLauncher\\runtime\\launcher_unc_call.bat"


class SharedFolderTests(unittest.TestCase):
    """共有フォルダー (\\\\サーバー\\…) の .bat (1.8.13)。

    neon-coil-zero の start.bat が 0.3 秒・戻り値 1 で終わった。cmd は
    \\\\サーバー\\… を「いまのフォルダー」にできず、`cd /d "%~dp0"` が通らないため。
    """

    def setUp(self) -> None:
        patcher = mock.patch.object(paths, "unc_wrapper", return_value=WRAPPER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def drives(self, value: dict):
        return mock.patch.object(paths, "network_drives", return_value=value)

    def test_ファイルを選ぶ画面のスラッシュの道をバックスラッシュにする(self) -> None:
        with _Windows(), self.drives({}):
            command = paths.entry_command("C:/Tools/neon/start.bat")
        self.assertIn('""C:\\Tools\\neon\\start.bat""', command)

    def test_ドライブ文字が無ければ包みを通す(self) -> None:
        slashed = NEON.replace("\\", "/") + "/start.bat"
        with _Windows(), self.drives({}):
            command = paths.entry_command(slashed, ["--debug"])
        self.assertEqual(command, 'C:\\Windows\\system32\\cmd.exe /d /s /c '
                                  f'""{WRAPPER}" "{NEON}\\start.bat" --debug"')

    def test_割り当て済みのドライブ文字があればその道で呼ぶ(self) -> None:
        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        with _Windows(), self.drives({"z:": root}):
            command = paths.entry_command(NEON + "\\start.bat")
        expected = NEON.replace("\\\\nlmfangyshrd\\各課共有", "Z:")
        self.assertEqual(command, 'C:\\Windows\\system32\\cmd.exe /d /s /c '
                                  f'""{expected}\\start.bat""')

    def test_共有フォルダーのvbsとexeは包まない(self) -> None:
        with _Windows(), self.drives({}):
            self.assertEqual(paths.entry_command(NEON + "\\Start.vbs"),
                             f'wscript.exe "{NEON}\\Start.vbs"')
            self.assertEqual(paths.entry_command(NEON + "\\app.exe"), f'"{NEON}\\app.exe"')

    def test_包みはドライブ文字の道で呼び戻り値を返し割り当てを外さない(self) -> None:
        text = paths.UNC_WRAPPER
        self.assertIn('pushd "%~dp1" || exit /b 1', text)
        self.assertIn('call "%CD%\\%~nx1" %2', text)
        self.assertIn("exit /b %ERRORLEVEL%", text)
        # 裏で動くサーバーの足元を消さない
        self.assertNotIn("popd", text.lower().replace("rem ", "\n").split("pushd")[-1])
        text.encode("ascii")                  # cmd がどの文字コードでも読める
        self.assertIn("\r\n", text)

    def test_作業フォルダー(self) -> None:
        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        local = Path(self.tmp())                # os.name を変える前に作る
        with _Windows():
            with self.drives({"z:": root}):
                self.assertEqual(paths.entry_cwd(NEON + "\\start.bat", NEON),
                                 NEON.replace("\\\\nlmfangyshrd\\各課共有", "Z:"))
                # vbs は wscript が共有フォルダーで動ける。そのまま
                self.assertEqual(paths.entry_cwd(NEON + "\\Start.vbs", NEON), NEON)
            with self.drives({}), mock.patch("launcher.app_config.local_dir",
                                             return_value=local):
                self.assertEqual(paths.entry_cwd(NEON + "\\start.bat", NEON), self.tmp())
            self.assertEqual(paths.entry_cwd("C:\\T\\start.bat", "C:\\T"), "C:\\T")

    def tmp(self) -> str:
        if not hasattr(self, "_tmp"):
            import tempfile
            self._tmp = tempfile.mkdtemp()
        return self._tmp

    def test_ドライブ文字の道を共有フォルダーの道で見る(self) -> None:
        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        with self.drives({"z:": root}):
            seen = paths.as_unc(paths.normalize('py -3.13 "Z:\\0130_日軽稲沢\\neon\\main.py"'))
            self.assertIn(root + "/0130_日軽稲沢/neon/main.py", seen)
            self.assertEqual(paths.as_unc("az:/x"), "az:/x")

    def test_ドライブ文字から動くツールのプロセスを見分ける(self) -> None:
        from launcher import desktop

        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        game = 4321
        with self.drives({"z:": root}), \
                mock.patch.object(desktop, "IS_WINDOWS", True), \
                mock.patch.object(desktop, "folder_is_specific", return_value=True), \
                mock.patch.object(desktop, "_processes", return_value=[(game, 1, "python.exe")]), \
                mock.patch.object(desktop, "protected_pids", return_value=set()), \
                mock.patch.object(desktop, "process_image",
                                  return_value="C:\\Python313\\python.exe"), \
                mock.patch.object(desktop, "is_script_host", return_value=True), \
                mock.patch.object(desktop, "command_line",
                                  return_value='python "Z:\\0130_日軽稲沢\\neon\\main.py"'):
            found = desktop.processes_in_folder("\\\\nlmfangyshrd\\各課共有\\0130_日軽稲沢\\neon")
        self.assertEqual(found, [game])

    def test_ドライブ文字が無ければ見分けの規則は変わらない(self) -> None:
        with self.drives({}):
            self.assertEqual(paths.as_unc("z:/a/b"), "z:/a/b")


class WrapperFileTests(unittest.TestCase):

    def test_包みをこのPCの中に置く(self) -> None:
        import tempfile

        folder = Path(tempfile.mkdtemp())
        with mock.patch("launcher.app_config.local_dir", return_value=folder):
            first = paths.unc_wrapper()
            self.assertEqual(Path(first), folder / paths.UNC_WRAPPER_NAME)
            self.assertEqual(Path(first).read_bytes(), paths.UNC_WRAPPER.encode("ascii"))
            Path(first).write_text("古い", encoding="utf-8")
            paths.unc_wrapper()                 # 中身が違えば書き直す
            self.assertEqual(Path(first).read_bytes(), paths.UNC_WRAPPER.encode("ascii"))
