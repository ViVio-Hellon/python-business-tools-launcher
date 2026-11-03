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
        # この組の試験は「cd /d で始まる .bat」(中身は WrapOnlyCdBatTests で確かめる)
        helper = mock.patch.object(paths, "needs_unc_help", return_value=True)
        helper.start()
        self.addCleanup(helper.stop)

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


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class WrapOnlyCdBatTests(unittest.TestCase):
    """包むのは `cd /d "%~dp0"` の .bat だけ (1.8.17)。

    1.8.13 で共有フォルダーの .bat を全部ドライブ文字の道で呼ぶようにしたら、
    `pushd "%~dp0"` を使う日報複合ツールの起動確認 (launcher_check.bat) が
    「指定されたパスが見つかりません」で答えなくなり、もう起動しているのに 91 秒待った。
    """

    def setUp(self) -> None:
        import tempfile

        self.folder = Path(tempfile.mkdtemp())
        paths._help_cache.clear()

    def copy(self, fixture: str, name: str) -> Path:
        target = self.folder / name
        target.write_bytes((FIXTURES / fixture).read_bytes())
        return target

    def test_日報複合ツールの起動確認は包まない(self) -> None:
        bat = self.copy("all-tools_launcher_check.bat", "launcher_check.bat")
        self.assertFalse(paths.needs_unc_help(bat))

    def test_NEON_COIL_ZEROのstartは包む(self) -> None:
        bat = self.copy("neon-coil-zero_start.bat", "start.bat")
        self.assertTrue(paths.needs_unc_help(bat))

    def test_読めない_無い_どちらも無いものは包まない(self) -> None:
        self.assertFalse(paths.needs_unc_help(self.folder / "無い.bat"))
        plain = self.folder / "plain.bat"
        plain.write_text("@echo off\r\npython main.py\r\n", encoding="ascii")
        self.assertFalse(paths.needs_unc_help(plain))

    def test_pushdを使うbatは1_8_12と同じ呼び方(self) -> None:
        """共有フォルダーでも、包まず・ドライブ文字にも変えず・作業フォルダーもそのまま。"""
        unc = SHARE + "\\日報複合ツール\\launcher_check.bat"
        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        with _Windows(), mock.patch.object(paths, "needs_unc_help", return_value=False), \
                mock.patch.object(paths, "network_drives", return_value={"z:": root}), \
                mock.patch.object(paths, "unc_wrapper", return_value=WRAPPER) as wrapper:
            command = paths.entry_command(unc)
            cwd = paths.entry_cwd(unc, SHARE + "\\日報複合ツール")
        self.assertEqual(command, 'C:\\Windows\\system32\\cmd.exe /d /s /c '
                                  f'""{unc}""')
        self.assertEqual(cwd, SHARE + "\\日報複合ツール")
        wrapper.assert_not_called()

    def test_中身が変わったら読み直す(self) -> None:
        import os
        import time

        bat = self.copy("neon-coil-zero_start.bat", "start.bat")
        self.assertTrue(paths.needs_unc_help(bat))
        bat.write_text('@echo off\r\npushd "%~dp0"\r\n', encoding="ascii")
        later = time.time() + 5
        os.utime(bat, (later, later))
        self.assertFalse(paths.needs_unc_help(bat))


class SamePathTwoNamesTests(unittest.TestCase):
    """ドライブ文字の道と共有フォルダーの道を同じものとして見る (1.8.17)。"""

    def setUp(self) -> None:
        root = paths.normalize("\\\\nlmfangyshrd\\各課共有")
        patcher = mock.patch.object(paths, "network_drives", return_value={"s:": root})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ドライブ文字で登録したツールが自分のプロセスを見分ける(self) -> None:
        """1.8.13 では見る側だけ共有フォルダーの道にそろえ、S:\\… で登録したツールが外れた。"""
        command = paths.normalize('python "S:\\tools\\nippou\\main.py"')
        self.assertTrue(paths.contains_path(command, paths.normalize("S:\\tools\\nippou")))

    def test_共有フォルダーで登録したツールがドライブ文字のプロセスを見分ける(self) -> None:
        command = paths.normalize('python "S:\\tools\\nippou\\main.py"')
        self.assertTrue(paths.contains_path(
            command, paths.normalize("\\\\nlmfangyshrd\\各課共有\\tools\\nippou")))

    def test_別のフォルダーは見分けない(self) -> None:
        command = paths.normalize('python "S:\\tools\\other\\main.py"')
        self.assertFalse(paths.contains_path(command, paths.normalize("S:\\tools\\nippou")))
        self.assertFalse(paths.contains_path(
            command, paths.normalize("\\\\nlmfangyshrd\\各課共有\\tools\\nippou")))
