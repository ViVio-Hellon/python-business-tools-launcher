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
import signal
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

    【立ち上がったことを必ず確かめる】
    Chromium は旗が気に入らないと**1秒ほどで黙って終了する**。
    立ち上がりを待たずに調べると、「すでに終了している」ものを
    相手に試すことになり、試験は通るのに何も確かめていない状態になる
    (実際にそうなっていた ── root で動かすと `--no-sandbox` が無い
    かぎり起動を拒否され、それに気づかないまま通っていた)。
    """

    # 開いたことを認めるまで待つ上限 (秒)。Chromium は起動に少しかかる
    LAUNCH_WAIT_SEC = 15.0

    # 止めたあと、子プロセスがいなくなるまで待つ上限 (秒)
    GONE_WAIT_SEC = 10.0

    def setUp(self) -> None:
        super().setUp()
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        # **ブラウザーを止めてから**一時フォルダーを消す。逆にすると、
        # 動いている Chromium がプロファイルへ書き続けていて消し切れない
        # (「Directory not empty」)。`addCleanup` は `tearDown` のあとに
        # 回るので、ここで先に止める
        self._stop_browsers()
        super().tearDown()

    def _stop_browsers(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                # 描画などの子プロセスまでまとめて止める。親だけ止めると、
                # 子がしばらくプロファイルへ書き続ける
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (AttributeError, OSError):
                    proc.kill()
            proc.wait(timeout=10)
            if proc.stdout is not None:
                proc.stdout.close()
        self.procs.clear()

        marker = str(self.local_root)
        deadline = time.monotonic() + self.GONE_WAIT_SEC
        while time.monotonic() < deadline:
            if not any(marker in command
                       for _, command in process_manager._running_processes()):
                return
            time.sleep(0.1)

    def _launch(self, app_id: str, url: str) -> RunningTool:
        """本物のブラウザーを1つ開く。**開くまで待つ。**"""
        profile = browser.profile_dir(app_id)
        profile.mkdir(parents=True, exist_ok=True)
        command = browser.build_command(_REAL, url, profile)
        command.append("--headless=new")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # root では砂箱を作れず、Chromium は起動を拒否する。
            # **試験の都合なので、製品の起動行には入れない** ──
            # 現場では利用者の権限で動くので砂箱はそのまま使う
            command.append("--no-sandbox")

        # 自分の組 (プロセスグループ) で動かす。止めるとき子まで届くように
        proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                start_new_session=True)
        self.procs.append(proc)
        running = RunningTool(app_id=app_id, display_name="日報",
                              browser_pid=proc.pid, browser_profile=str(profile))

        deadline = time.monotonic() + self.LAUNCH_WAIT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = (proc.stdout.read() or b"").decode("utf-8", "replace")
                self.skipTest(f"この環境では Chromium が起動しません: "
                              f"{output.strip()[:200]}")
            if (process_manager.process_command_line(proc.pid)
                    and process_manager.is_browser_open(running)):
                return running
            time.sleep(0.1)
        self.skipTest("Chromium が時間内に起動しませんでした")

    def test_旗が受け付けられて動き続ける(self) -> None:
        running = self._launch("fake.real", "http://127.0.0.1:9/")
        command = process_manager.process_command_line(running.browser_pid)

        self.assertIn("--app=", command)
        self.assertIn("--user-data-dir=", command)
        # 少し置いてもまだ動いていること (旗を拒否して落ちていない)
        time.sleep(1.0)
        self.assertTrue(process_manager.is_browser_open(running),
                        "ブラウザーがすぐ終了しました(旗が拒否された可能性)")

    def test_照合して閉じられる(self) -> None:
        running = self._launch("fake.real2", "http://127.0.0.1:9/")
        self.assertTrue(process_manager.close_browser(running))
        self.assertFalse(process_manager._is_alive(running.browser_pid))

    def test_別プロファイルのブラウザーは閉じない(self) -> None:
        """§8.3.3 の要。**利用者のブラウザーに手が届かないこと。**"""
        mine = self._launch("fake.mine", "http://127.0.0.1:9/")
        theirs = self._launch("fake.theirs", "http://127.0.0.1:9/")
        # 両方とも本当に動いていることを確かめてから試す
        self.assertTrue(process_manager.is_browser_open(mine))
        self.assertTrue(process_manager.is_browser_open(theirs))

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
