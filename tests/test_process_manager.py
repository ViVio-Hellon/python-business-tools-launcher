"""安全な停止と多重起動防止 (要件定義書 §10 / §21 / 基盤仕様書 2.4 / 2.8)

ここで確かめるのは1つ ── **対象外のプロセスを絶対に落とさないこと**。
止まらない不具合は利用者が気づいて手で直せるが、無関係なアプリを
落とすと気づかれないままデータが失われる。
"""
from __future__ import annotations

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from launcher.runtime_state import RunningTool  # noqa: E402


class _ChildProcessMixin:
    """止めてよいか試すための、無害な子プロセス。"""

    def spawn(self, marker: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", marker],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._kill, proc)
        # コマンドラインが `/proc` に見えるまで待つ
        for _ in range(50):
            if process_manager.process_command_line(proc.pid):
                break
            time.sleep(0.02)
        return proc

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


class VerifyTests(LocalAreaTestCase, _ChildProcessMixin):
    """要件定義書 §10「対象アプリを特定してから終了する」。"""

    def test_場所が一致すれば止めてよい(self) -> None:
        marker = str(self.work_root / "業務ツール" / "日報")
        proc = self.spawn(marker)
        running = RunningTool(app_id="x", pid=proc.pid, app_root=marker)
        self.assertTrue(process_manager.verify_process(proc.pid, running).ok)

    def test_場所が違えば止めない(self) -> None:
        proc = self.spawn(str(self.work_root / "業務ツール" / "日報"))
        running = RunningTool(app_id="x", pid=proc.pid,
                              app_root=str(self.work_root / "別のアプリ"))
        verdict = process_manager.verify_process(proc.pid, running)
        self.assertFalse(verdict.ok)

    def test_短すぎる印では照合しない(self) -> None:
        """`C:\\` のような値を許すと、どのプロセスにも一致してしまう。"""
        proc = self.spawn(str(self.work_root))
        for marker in ("C:\\", "/", "/opt", ""):
            with self.subTest(marker):
                running = RunningTool(app_id="x", pid=proc.pid, app_root=marker)
                self.assertFalse(
                    process_manager.verify_process(proc.pid, running).ok,
                    f"{marker!r} で照合が通ってしまいました")

    def test_照合できないプロセスは落とさない(self) -> None:
        """**この試験がいちばん大事。** 無関係なプロセスを守る。"""
        proc = self.spawn(str(self.work_root / "無関係なアプリ"))
        running = RunningTool(app_id="x", display_name="日報", pid=proc.pid,
                              app_root=str(self.work_root / "日報"))

        result = process_manager._stop_by_pid(running, force=True)

        self.assertFalse(result.stopped)
        self.assertIn("止めません", result.message)
        self.assertIsNone(proc.poll(), "無関係なプロセスを落としました")


class AliveTests(LocalAreaTestCase, _ChildProcessMixin):

    def test_生きているプロセスが分かる(self) -> None:
        proc = self.spawn(str(self.work_root))
        self.assertTrue(process_manager._is_alive(proc.pid))

    def test_終了済みは生きていない(self) -> None:
        """**引き取り待ち(ゾンビ)を「生きている」と答えないこと。**

        ランチャーは `start.bat` を自分の子として起こすので、止めた
        あとに必ずこの状態を通る。ここを取り違えると、止まったものを
        止まっていないと報告し続ける。
        """
        proc = self.spawn(str(self.work_root))
        proc.kill()
        time.sleep(0.3)
        self.assertFalse(process_manager._is_alive(proc.pid))

    def test_居ないPIDは生きていない(self) -> None:
        self.assertFalse(process_manager._is_alive(0))
        self.assertFalse(process_manager._is_alive(-1))


class StopBatTests(LocalAreaTestCase):
    """`stop.bat` の見つけ方。"""

    def test_startbatの隣を既定にする(self) -> None:
        root = self.work_root / "日報"
        root.mkdir(parents=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        (root / "stop.bat").write_text("@echo off\n", encoding="utf-8")

        running = RunningTool(app_id="x", start_command=str(root / "start.bat"))
        self.assertEqual(process_manager.resolve_stop_bat(running),
                         root / "stop.bat")

    def test_無ければNone(self) -> None:
        root = self.work_root / "看板"
        root.mkdir(parents=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        running = RunningTool(app_id="x", start_command=str(root / "start.bat"))
        self.assertIsNone(process_manager.resolve_stop_bat(running))

    def test_設定があればそちらを使う(self) -> None:
        root = self.work_root / "カレンダー"
        root.mkdir(parents=True)
        (root / "start.bat").write_text("@echo off\n", encoding="utf-8")
        custom = self.work_root / "とめる.bat"
        custom.write_text("@echo off\n", encoding="utf-8")

        running = RunningTool(app_id="x", start_command=str(root / "start.bat"),
                              stop_command=str(custom))
        self.assertEqual(process_manager.resolve_stop_bat(running), custom)


class TokenTests(LocalAreaTestCase):
    """相手の停止トークンを探す (best effort)。"""

    def test_ロックファイルから拾える(self) -> None:
        root = self.work_root / "日報"
        (root / "config").mkdir(parents=True)
        (root / "config" / "app.json").write_text(
            '{"local_dir_name": "TestNippou"}', encoding="utf-8")

        import os
        local = self.work_root / "appdata"
        (local / "TestNippou" / "runtime").mkdir(parents=True)
        (local / "TestNippou" / "runtime" / "field.lock").write_text(
            '{"port": 8733, "token": "abc123"}', encoding="utf-8")

        original = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = str(local)
        try:
            running = RunningTool(app_id="x", port=8733, app_root=str(root))
            self.assertEqual(process_manager.find_shutdown_token(running),
                             "abc123")
            # ポートが違えば拾わない
            other = RunningTool(app_id="x", port=9999, app_root=str(root))
            self.assertEqual(process_manager.find_shutdown_token(other), "")
        finally:
            if original is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = original

    def test_相手の設定が無くても失敗しない(self) -> None:
        running = RunningTool(app_id="x", port=1,
                              app_root=str(self.work_root / "ない"))
        self.assertEqual(process_manager.find_shutdown_token(running), "")


class LaunchGuardTests(LocalAreaTestCase, _ChildProcessMixin):
    """ランチャー自身の多重起動防止 (基盤仕様書 2.4)。"""

    def test_ロックが無ければ起動してよい(self) -> None:
        self.assertTrue(launch_guard.check_existing().should_start)

    def test_自分のロックは邪魔しない(self) -> None:
        launch_guard.write_lock()
        self.assertTrue(launch_guard.check_existing().should_start)

    def test_死んだロックは片付けて起動する(self) -> None:
        """異常終了で残ったロックのせいで二度と起動しない、を防ぐ。"""
        info = launch_guard.build_lock_info()
        info.pid = 999_999
        launch_guard.write_lock(info)

        result = launch_guard.check_existing()
        self.assertTrue(result.should_start)
        self.assertFalse(launch_guard.lock_path().exists())

    def test_別のプロセスがPIDを使っていても起動できる(self) -> None:
        """PIDは使い回される。無関係なプロセスで塞がれないこと。"""
        proc = self.spawn(str(self.work_root / "無関係"))
        info = launch_guard.build_lock_info()
        info.pid = proc.pid
        info.app_root = str(self.work_root / "別のランチャー")
        launch_guard.write_lock(info)

        result = launch_guard.check_existing()
        self.assertTrue(result.should_start, result.reason)

    def test_動いているランチャーがあれば起動しない(self) -> None:
        marker = str(self.work_root / "ランチャー本体")
        proc = self.spawn(marker)
        info = launch_guard.build_lock_info()
        info.pid = proc.pid
        info.app_root = marker
        launch_guard.write_lock(info)

        result = launch_guard.check_existing()
        self.assertFalse(result.should_start)
        self.assertIn("すでに起動しています", result.reason)


if __name__ == "__main__":
    unittest.main()
