"""ブラウザー画面の開閉 (要件定義書 §8.3)

`webbrowser.open()` では開いたタブへの手がかりが返らないので、閉じられない。
専用プロファイルのアプリウィンドウとして開くことで、

    ・ランチャーの子プロセスになる          → 閉じられる
    ・利用者のふだんのブラウザーとは別物    → 巻き添えにしようがない

の2つを同時に満たす。ここではその組み立てと、実際のブラウザーでの
開閉を確かめる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

import process_manager  # noqa: E402
from launcher import app_config, browser  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402


def _real_browser() -> str:
    """この端末にある本物の Chromium系ブラウザー。無ければ空文字。"""
    _, executable = browser.find_browser()
    if executable:
        return executable
    # Playwright が置いている Chromium (開発環境)
    for path in Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"):
        if path.is_file():
            return str(path)
    return ""


_REAL = _real_browser()
_SKIP_REAL = "本物の Chromium系ブラウザーが無いためスキップ"


class CommandTests(LocalAreaTestCase):
    """起動行の組み立て。"""

    def test_アプリウィンドウとして開く(self) -> None:
        command = browser.build_command(
            "chrome.exe", "http://127.0.0.1:8733/",
            browser.profile_dir("nlm.nippou-tool"))

        self.assertEqual(command[0], "chrome.exe")
        self.assertIn("--app=http://127.0.0.1:8733/", command)
        # **専用プロファイルが要点。** これが無いと、既存の
        # ブラウザーに合流してこちらの手がかりが消える
        profile = [a for a in command if a.startswith("--user-data-dir=")]
        self.assertEqual(len(profile), 1, "専用プロファイルの指定がありません")
        self.assertIn(str(app_config.local_dir("browser")), profile[0])

    def test_プロファイルはツールごとに分かれる(self) -> None:
        """前の窓が閉じきる前に次を起こしても掴み合わないように。"""
        a = browser.profile_dir("nlm.nippou-tool")
        b = browser.profile_dir("nlm.kanban-system")
        self.assertNotEqual(a, b)
        self.assertEqual(a.parent, app_config.local_dir("browser"))

    def test_利用者のプロファイルを使わない(self) -> None:
        """§8.3.1 通常利用のブラウザーには干渉しない。"""
        profile = browser.profile_dir("nlm.nippou-tool")
        # ランチャー専用のローカル領域の中に閉じている
        self.assertTrue(str(profile).startswith(str(app_config.local_root())))


class FallbackTests(LocalAreaTestCase):
    """Chromium系が無い端末。"""

    def test_見つからなければ既定ブラウザーへ落ちる(self) -> None:
        with mock.patch.object(browser, "find_browser", return_value=("", "")), \
             mock.patch.object(browser.webbrowser, "open") as opened:
            session = browser.open_window("x", "http://127.0.0.1:1/")

        opened.assert_called_once_with("http://127.0.0.1:1/")
        # **閉じられないことを記録に残す。** 黙って閉じた扱いにしない
        self.assertFalse(session.closable)
        self.assertEqual(session.pid, 0)
        self.assertEqual(session.profile_dir, "")

    def test_設定で既定ブラウザーを選べる(self) -> None:
        settings = app_config.load().setdefault("browser", {})
        original = settings.get("mode")
        settings["mode"] = "default"
        try:
            self.assertEqual(browser.mode(), "default")
            with mock.patch.object(browser.webbrowser, "open") as opened:
                session = browser.open_window("x", "http://127.0.0.1:1/")
            opened.assert_called_once()
            self.assertFalse(session.closable)
        finally:
            if original is None:
                settings.pop("mode", None)
            else:
                settings["mode"] = original

    def test_URLが無ければ何もしない(self) -> None:
        with mock.patch.object(browser.webbrowser, "open") as opened:
            session = browser.open_window("x", "")
        opened.assert_not_called()
        self.assertFalse(session.opened)


@unittest.skipUnless(_REAL, _SKIP_REAL)
class RealBrowserTests(LocalAreaTestCase):
    """本物のブラウザーで開いて閉じる。

    画面の無い環境で動かすため `--headless=new` を足している。
    確かめたいのは表示ではなく、**旗が受け付けられ、こちらの
    プロセスとして残り、照合して閉じられること**。
    """

    def setUp(self) -> None:
        super().setUp()
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)

    def _launch(self, app_id: str, url: str) -> RunningTool:
        profile = browser.profile_dir(app_id)
        profile.mkdir(parents=True, exist_ok=True)
        command = browser.build_command(_REAL, url, profile)
        command.append("--headless=new")
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        return RunningTool(app_id=app_id, display_name="日報",
                           browser_pid=proc.pid, browser_profile=str(profile))

    def _wait_cmdline(self, pid: int, timeout: float = 10.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            command = process_manager.process_command_line(pid)
            if command:
                return command
            time.sleep(0.05)
        return ""

    def test_旗が受け付けられて動き続ける(self) -> None:
        running = self._launch("fake.real", "http://127.0.0.1:9/")
        command = self._wait_cmdline(running.browser_pid)

        self.assertTrue(command, "ブラウザーのコマンドラインを取れません")
        self.assertIn("--app=", command)
        self.assertTrue(process_manager.is_browser_open(running),
                        "ブラウザーがすぐ終了しました(旗が拒否された可能性)")

    def test_照合して閉じられる(self) -> None:
        running = self._launch("fake.real2", "http://127.0.0.1:9/")
        self._wait_cmdline(running.browser_pid)

        self.assertTrue(process_manager.close_browser(running))
        self.assertFalse(process_manager._is_alive(running.browser_pid))

    def test_別プロファイルのブラウザーは閉じない(self) -> None:
        """§8.3.3 の要。**利用者のブラウザーに手が届かないこと。**"""
        mine = self._launch("fake.mine", "http://127.0.0.1:9/")
        theirs = self._launch("fake.theirs", "http://127.0.0.1:9/")
        self._wait_cmdline(mine.browser_pid)
        self._wait_cmdline(theirs.browser_pid)

        # 自分のものだけ閉じる
        self.assertTrue(process_manager.close_browser(mine))
        self.assertFalse(process_manager._is_alive(mine.browser_pid))
        self.assertTrue(process_manager._is_alive(theirs.browser_pid),
                        "別プロファイルのブラウザーまで閉じました")

        # 記録のPIDだけ差し替えても、照合が通らないので閉じない
        spoofed = RunningTool(app_id="fake.mine", browser_pid=theirs.browser_pid,
                              browser_profile=mine.browser_profile)
        self.assertFalse(process_manager.close_browser(spoofed))
        self.assertTrue(process_manager._is_alive(theirs.browser_pid))


if __name__ == "__main__":
    unittest.main()
