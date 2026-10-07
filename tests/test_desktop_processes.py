"""ツールのプロセス一式と、待ち受けているポートの見つけ方 (`launcher.desktop`)

現場で、起動用の exe (`梱包資材総合ツール.exe`) が本体を起こして**すぐ
戻り値 0 で終わり**、本体は設定と違うポートで動いていた。ランチャーは
起こした exe と設定のポートしか見ていなかったので、92秒待って「起動
できなかった」とし、もう一度押すと**二重に起動しようとした。**

そこで「そのツール」を、起こしたプロセスではなく**ツールのフォルダーから
起動したプロセス一式**で見る。ここではその見分け方を確かめる:

* 広すぎるフォルダー (ホーム・デスクトップ・ドライブ直下) では見分けない
* コマンドラインで見るのは、スクリプトを動かすだけの実行ファイル
  (python・cmd など) だけ。**メモ帳で app.json を開いているだけのものを、
  ツールとして止めない**
* 待ち受けているポートを、プロセスから引ける
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: E402,F401 - 道を通す

from launcher import desktop  # noqa: E402


def _wait_for(check, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    return check()


class FolderRuleTests(unittest.TestCase):

    def test_広すぎるフォルダーでは見分けない(self) -> None:
        home = os.path.expanduser("~")
        for folder in ("", os.sep, home, os.path.join(home, "Desktop"),
                       os.path.join(home, "Documents"), tempfile.gettempdir(),
                       os.path.join(os.sep, "tools")):
            with self.subTest(folder=folder):
                self.assertFalse(desktop.folder_is_specific(folder))
                self.assertEqual(desktop.processes_in_folder(folder), [])

    def test_ツールのフォルダーなら見分ける(self) -> None:
        folder = os.path.join(os.path.expanduser("~"), "Desktop", "Python_DB_HTML",
                              "梱包資材総合ツール_VER4.6.0")
        self.assertTrue(desktop.folder_is_specific(folder))

    def test_コマンドラインで見るのはスクリプトを動かすものだけ(self) -> None:
        for image in ("python.exe", "pythonw.exe", "python3.12", "py.exe",
                      "cmd.exe", "wscript.exe", "node.exe", "javaw.exe",
                      r"C:\Python39\python.exe"):
            with self.subTest(image=image):
                self.assertTrue(desktop._is_script_host(image))
        for image in ("notepad.exe", "梱包資材総合ツール.exe", "EXCEL.EXE",
                      "explorer.exe", "msedge.exe", "tail", "pythonista.exe"):
            with self.subTest(image=image):
                self.assertFalse(desktop._is_script_host(image))


@unittest.skipIf(desktop.IS_WINDOWS, "Windows 以外で /proc から確かめる")
class FolderProcessTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # 一時フォルダーの直下は広すぎるので、もう1段深くする
        self.folder = Path(self._tmp.name) / "業務ツール" / "梱包資材"
        self.folder.mkdir(parents=True)
        (self.folder / "app.json").write_text("{}", encoding="utf-8")
        self.script = self.folder / "server.py"
        self.script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    def spawn(self, args) -> subprocess.Popen:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        return proc

    def test_フォルダーのスクリプトを動かしているPythonを見つける(self) -> None:
        proc = self.spawn([sys.executable, str(self.script)])
        found = _wait_for(lambda: desktop.processes_in_folder(str(self.folder)))
        self.assertEqual(found, [proc.pid])
        self.assertIn(proc.pid, desktop.related_pids(str(self.folder)))

    def test_設定ファイルを開いているだけのものはツールにしない(self) -> None:
        """メモ帳で app.json を開いている、の代わりに tail で開いておく。"""
        viewer = self.spawn(["tail", "-f", str(self.folder / "app.json")])
        _wait_for(lambda: desktop.command_line(viewer.pid))
        self.assertIn(str(self.folder), desktop.command_line(viewer.pid))
        self.assertEqual(desktop.processes_in_folder(str(self.folder)), [])

    def test_起こしたプロセスが終わっていても本体の子を追う(self) -> None:
        """起動用 exe の代わりに、本体を起こしてすぐ終わる Python。"""
        starter = subprocess.run(
            [sys.executable, "-c",
             "import subprocess, sys; subprocess.Popen([sys.executable, sys.argv[1]],"
             " start_new_session=True, stdout=subprocess.DEVNULL,"
             " stderr=subprocess.DEVNULL)", str(self.script)],
            timeout=10)
        self.assertEqual(starter.returncode, 0)
        found = _wait_for(lambda: desktop.related_pids(str(self.folder)))
        self.assertEqual(len(found), 1)
        pid = next(iter(found))
        try:
            self.assertIn(str(self.script), desktop.command_line(pid))
        finally:
            os.kill(pid, 9)
            _wait_for(lambda: not desktop.processes_in_folder(str(self.folder)))


@unittest.skipIf(desktop.IS_WINDOWS, "Windows 以外で /proc から確かめる")
class ListeningPortTests(unittest.TestCase):

    def test_プロセスが待ち受けているポートを引ける(self) -> None:
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        self.assertIn(("127.0.0.1", port), desktop.listening_ports(os.getpid()))
        self.assertIn(("127.0.0.1", port), desktop.listening_ports({os.getpid()}))
        self.assertTrue(desktop.port_listening(port))

    def test_待ち受けていないポートと空の指定(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        self.assertFalse(desktop.port_listening(port))
        self.assertFalse(desktop.port_listening(0))
        self.assertEqual(desktop.listening_ports(()), [])

    def test_ほかのプロセスのポートは混ぜない(self) -> None:
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import socket, sys, time\n"
             "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
             "print(s.getsockname()[1], flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait, 5)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.kill)
        port = int(child.stdout.readline())
        self.assertIn(("127.0.0.1", port), desktop.listening_ports(child.pid))
        self.assertNotIn(port, [p for _, p in desktop.listening_ports(os.getpid())])

    def test_取れなければ分からないと答える(self) -> None:
        from unittest import mock

        with mock.patch.object(desktop, "_listeners_proc",
                               side_effect=PermissionError(13, "拒否")):
            self.assertIsNone(desktop.port_listening(8713))
            self.assertEqual(desktop.listening_ports(os.getpid()), [])


if __name__ == "__main__":
    unittest.main()
