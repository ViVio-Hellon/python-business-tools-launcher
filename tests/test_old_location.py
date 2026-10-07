"""古い置き場所の起動ファイルと、残ったロックで、ランチャーが止まらないか

ツールを別の場所へ移したあと、設定や配布先フォルダに**古い置き場所**が
残っていることがある。Windows では、その置き場所によって「ファイルが
あるか」の問い合わせそのものが失敗したり、長く待たされたりする:

    アクセス権を外されたフォルダー   PermissionError (Path.is_file() は例外を出す)
    つながらない共有フォルダー        OSError、または応答まで数十秒

この試験では `os.stat` をその振る舞いに差し替えて確かめる。以前は:

* 例外のとき … 起動の下ごしらえ (`boot.run`) やバーのボタン作りで落ち、
  **ランチャーが起動しなかった**
* 遅いとき   … 設定を読むたびに問い合わせ直し、起動に 36 秒 (1回3秒のとき)

ロック (`launcher.lock`) の Windows 固有の残り方も確かめる。
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

from _isolation import LocalAreaTestCase  # noqa: E402

import boot  # noqa: E402
import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from launcher import distribution, fileprobe, tool_registry  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402

OLD = os.path.join(os.sep, "old-share", "業務ツール")
_real_stat = os.stat


class OldLocation:
    """`os.stat` を、古い置き場所だけ Windows の失敗の仕方に差し替える。"""

    def __init__(self, kind: str, delay: float = 0.0) -> None:
        self.kind = kind
        self.delay = delay
        self.calls = 0

    def __call__(self, path, *args, **kwargs):
        text = os.fspath(path)
        if isinstance(text, str) and text.startswith(OLD):
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            if self.kind == "denied":
                raise PermissionError(13, "アクセスが拒否されました", text)
            if self.kind == "network":
                err = OSError(22, "指定されたネットワーク名は利用できません", text)
                err.winerror = 64
                raise err
            raise FileNotFoundError(2, "見つかりません", text)
        return _real_stat(path, *args, **kwargs)


class OldLocationTestCase(LocalAreaTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(launch_guard.remove_lock)

    def old_share(self, kind: str, delay: float = 0.0) -> OldLocation:
        fake = OldLocation(kind, delay)
        patcher = mock.patch("os.stat", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def distribute_old_path(self) -> None:
        """配布先フォルダに、古い置き場所の起動ファイルが書かれている。"""
        distribution.folder().mkdir(parents=True, exist_ok=True)
        (distribution.folder() / "settings.json").write_text(json.dumps({
            "format": 1,
            "tools": [{"app_id": "nlm.daily", "display_name": "日報", "port": 8733,
                       "start_command": OLD + "/日報/start.bat"}]},
            ensure_ascii=False), encoding="utf-8")

    def register_old_path(self) -> None:
        """この端末の設定に、古い置き場所の起動ファイルが残っている。"""
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.daily", display_name="日報", port=8733,
            start_command=OLD + "/日報/start.bat"))


class StartupTests(OldLocationTestCase):
    """ランチャーの起動が止まらない。"""

    def test_配布先フォルダの古い置き場所に入れなくても起動する(self) -> None:
        self.distribute_old_path()
        for kind in ("denied", "network"):
            with self.subTest(kind=kind):
                fileprobe.forget()
                self.old_share(kind)
                result = boot.run()
                self.assertTrue(result.started)
                launch_guard.remove_lock()

    def test_設定の古い置き場所に入れなくても起動してバーで知らせる(self) -> None:
        self.register_old_path()
        self.old_share("denied")
        result = boot.run()
        self.assertTrue(result.started)
        detail = result.manager.status.detail
        self.assertIn("起動ファイルを確かめられません", detail)
        self.assertIn("日報", detail)
        self.assertIn("アクセスが拒否されました", detail)

    def test_つながらない共有フォルダーで起動を待たせない(self) -> None:
        """1回の問い合わせに 2 秒かかる置き場所 (本物は数十秒のこともある)。"""
        self.distribute_old_path()
        self.register_old_path()
        fake = self.old_share("missing", delay=2.0)
        with mock.patch.object(fileprobe, "CHECK_TIMEOUT_SEC", 0.3):
            began = time.monotonic()
            result = boot.run()
            elapsed = time.monotonic() - began
        self.assertTrue(result.started)
        self.assertLess(elapsed, 1.5, "古い置き場所を何度も問い合わせています")
        self.assertLessEqual(fake.calls, 2)

    def test_設定を読むたびに古い置き場所へ問い合わせない(self) -> None:
        """埋まっている行のために、配布先フォルダの置き場所を見にいかない。"""
        start = self.work_root / "日報" / "start.bat"
        start.parent.mkdir(parents=True)
        start.write_text("@echo off\n", encoding="utf-8")
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.daily", display_name="日報", port=8733,
            start_command=str(start)))
        self.distribute_old_path()
        fake = self.old_share("missing", delay=0.0)
        for _ in range(5):
            tool_registry.get("nlm.daily")
            tool_registry.all_tools()
        self.assertEqual(fake.calls, 0)


class ProbeTests(OldLocationTestCase):
    """起動ファイルの確かめ方 (`fileprobe`)。"""

    def test_例外を出さず確かめられないと答える(self) -> None:
        self.old_share("denied")
        found = fileprobe.probe(OLD + "/日報/start.bat")
        self.assertTrue(found.unknown)
        self.assertIn("アクセスが拒否されました", found.reason)
        self.register_old_path()
        tool = tool_registry.get("nlm.daily")
        self.assertFalse(tool.is_configured)          # 以前は例外
        self.assertIn("確かめられません",
                      tool_registry.validate_start_command(tool.start_command))
        args, reason = tool_registry.recommend_start_args(tool.start_command)
        self.assertEqual(args, "")
        self.assertIn("確かめられない", reason)

    def test_ネットワークの失敗も同じ(self) -> None:
        self.old_share("network")
        found = fileprobe.probe(OLD + "/日報/start.bat")
        self.assertTrue(found.unknown)
        self.assertIn("WinError 64", found.reason)

    def test_遅い置き場所は待ちすぎず遅れた答えを覚える(self) -> None:
        fake = self.old_share("missing", delay=0.6)
        path = OLD + "/日報/start.bat"
        began = time.monotonic()
        first = fileprobe.probe(path, timeout=0.2)
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(first.unknown)
        time.sleep(0.6)                               # 遅れて答えが届く
        second = fileprobe.probe(path, timeout=0.2)
        self.assertEqual(second.state, fileprobe.MISSING)
        self.assertEqual(fake.calls, 1, "覚えた答えを使わずに問い合わせ直しました")

    def test_すぐ答える場所はあとから置いたファイルにすぐ気づく(self) -> None:
        path = self.work_root / "新しい" / "start.bat"
        self.assertEqual(fileprobe.probe(path).state, fileprobe.MISSING)
        path.parent.mkdir()
        path.write_text("@echo off\n", encoding="utf-8")
        self.assertTrue(fileprobe.probe(path).found)

    def test_形の正しくない名前は無いものとして扱う(self) -> None:
        self.assertEqual(fileprobe.probe("C:/業務\0ツール/start.bat").state,
                         fileprobe.MISSING)
        err = OSError(22, "ファイル名、ディレクトリ名、またはボリューム ラベルの構文が"
                          "間違っています")
        err.winerror = 123
        with mock.patch("os.stat", side_effect=err):
            self.assertEqual(fileprobe.probe("C:/a?b/start.bat").state,
                             fileprobe.MISSING)

    def test_止めるときも古い置き場所で落ちない(self) -> None:
        """stop.bat は起動ファイルの隣。そこが入れない場所でも次の手へ回る。"""
        self.old_share("denied")
        running = RunningTool(app_id="nlm.daily",
                              start_command=OLD + "/日報/start.bat")
        self.assertIsNone(process_manager.resolve_stop_bat(running))

    def test_配布先フォルダの道を文字の上だけで畳む(self) -> None:
        """`Path.resolve()` はファイルを見にいくので使わない。"""
        fake = self.old_share("denied")
        text = OLD + "/日報/../日報/start.bat"
        self.assertEqual(tool_registry.resolve_config_path(text),
                         os.path.normpath(OLD + "/日報/start.bat"))
        self.assertEqual(fake.calls, 0)


class LockTests(OldLocationTestCase):
    """残ったロックで起動が止まらない (Windows で起きやすい形)。"""

    def other_process(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        return proc

    def write_lock(self, pid: int, started_at: float) -> None:
        launch_guard.lock_path().parent.mkdir(parents=True, exist_ok=True)
        launch_guard.lock_path().write_text(json.dumps({
            "app_id": "nlm.business-tools-launcher", "pid": pid,
            "started_at": started_at}), encoding="utf-8")

    def test_番号を使い回された残りのロックは片付ける(self) -> None:
        """電源断で残ったロック。PIDはいま別のプロセスが使っている。

        **コマンドラインが取れない端末** (wmic が無く PowerShell も禁止) でも、
        そのプロセスがロックのあとで起動したことで見分ける。以前はここで
        「確かめられませんでした」となり、ずっと起動できなかった。
        """
        proc = self.other_process()
        time.sleep(0.2)
        self.write_lock(proc.pid, started_at=time.time() - 3600)
        with mock.patch.object(process_manager, "process_command_line",
                               return_value=""):
            result = launch_guard.acquire()
        self.assertTrue(result.should_start, result.reason)

    def test_ロックを書いた本人が動いていれば断る(self) -> None:
        proc = self.other_process()
        time.sleep(0.2)
        self.write_lock(proc.pid, started_at=time.time() + 5)
        with mock.patch.object(process_manager, "process_command_line",
                               return_value=""):
            result = launch_guard.acquire()
        self.assertFalse(result.should_start)
        self.assertIn("すでに起動しています", result.reason)

    def test_消せないロックでもずっと起動できなくはならない(self) -> None:
        """Windows では読み取り専用・ほかが開いているファイルは消せない。"""
        launch_guard.lock_path().parent.mkdir(parents=True, exist_ok=True)
        launch_guard.lock_path().write_text("壊れた中身", encoding="utf-8")
        with mock.patch.object(Path, "unlink",
                               side_effect=PermissionError(13, "アクセスが拒否されました")):
            result = launch_guard.acquire()
        self.assertTrue(result.should_start)
        self.assertIn("消せませんでした", result.warning)
        self.assertNotIn("ほかのランチャー", result.reason)


if __name__ == "__main__":
    unittest.main()
