"""ツール側が用意する入口 (ランチャー連携 要件定義 §3・§10)

ツールが起動ファイルの隣に `launcher_check.bat` (起動確認) と
`launcher_stop.bat` (終了) を置いていれば、ランチャーは自分の推測 (起動
確認の URL・ポート探し・窓の検出・停止方法の自動選択) より**優先して**
それを使う。

試験のツールは Start.vbs と同じく、起動ファイルが本体を別に起こして
**すぐ終わる**。設定のポートはわざと間違えておく ── それでも入口だけで
起動・確認・終了できることを確かめる。
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State, ToolManager  # noqa: E402
from launcher import desktop, tool_entries, tool_registry, trace  # noqa: E402

TOOL_PY = """\
import os, sys, time, pathlib
here = pathlib.Path(__file__).resolve().parent
(here / "pid.txt").write_text(str(os.getpid()))
(here / "started.txt").write_text("")
time.sleep(float(sys.argv[1]))
(here / "ready.txt").write_text("")
while not (here / "stop.txt").exists():
    time.sleep(0.1)
for name in ("ready.txt", "started.txt", "pid.txt", "stop.txt"):
    try:
        (here / name).unlink()
    except FileNotFoundError:
        pass
"""


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\ncd \"$(dirname \"$0\")\"\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def make_entry_tool(base: Path, name: str, *, ready_after: float = 0.5,
                    check: bool = True, stop: bool = True) -> Path:
    """入口を持つツールのフォルダー。起動ファイル (start.bat) の道を返す。"""
    root = base / name
    root.mkdir(parents=True)
    (root / "tool.py").write_text(TOOL_PY, encoding="utf-8")
    start = _script(root / "start.bat",
                    f'"{sys.executable}" tool.py {ready_after} '
                    "</dev/null >/dev/null 2>&1 &\nexit 0\n")
    if check:
        _script(root / tool_entries.CHECK_FILE,
                '[ -f ready.txt ] && { echo "使えます"; exit 0; }\n'
                '[ -f started.txt ] && { echo "データを読み込み中"; exit 2; }\n'
                "exit 1\n")
    if stop:
        _script(root / tool_entries.STOP_FILE, "touch stop.txt\nexit 0\n")
    return start


class EntryTestCase(LocalAreaTestCase):

    def stop_leftovers(self, root: Path) -> None:
        (root / "stop.txt").write_text("")
        deadline = time.monotonic() + 5
        while (root / "pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


class FindTests(EntryTestCase):

    def test_決まった名前の入口を見つける(self) -> None:
        start = make_entry_tool(self.work_root, "日報")
        entries = tool_entries.for_start(str(start))
        self.assertTrue(entries.has_check)
        self.assertEqual(Path(entries.check).name, "launcher_check.bat")
        self.assertEqual(Path(entries.stop).name, "launcher_stop.bat")
        self.assertIn("ツールの入口: 起動確認 launcher_check.bat・終了 launcher_stop.bat",
                      entries.describe())

    def test_入口が無ければ空(self) -> None:
        start = make_entry_tool(self.work_root, "看板", check=False, stop=False)
        entries = tool_entries.for_start(str(start))
        self.assertFalse(entries.any)
        self.assertEqual(entries.describe(), "")

    def test_launcher_jsonで名前とURLを決められる(self) -> None:
        start = make_entry_tool(self.work_root, "総合", check=False, stop=False)
        _script(start.parent / "my_stop.bat", "exit 0\n")
        (start.parent / "launcher.json").write_text(json.dumps({
            "check_url": "http://127.0.0.1:8700/api/health", "stop": "my_stop.bat"}),
            encoding="utf-8")
        entries = tool_entries.for_start(str(start))
        self.assertEqual(entries.check_url, "http://127.0.0.1:8700/api/health")
        self.assertEqual(Path(entries.stop).name, "my_stop.bat")
        self.assertEqual(entries.problem, "")

    def test_launcher_jsonの誤りは知らせて決まった名前に戻る(self) -> None:
        start = make_entry_tool(self.work_root, "日報")
        (start.parent / "launcher.json").write_text(json.dumps({
            "check_url": "http://example.com/health", "stop": "無い.bat"}),
            encoding="utf-8")
        entries = tool_entries.for_start(str(start))
        self.assertIn("127.0.0.1", entries.problem)
        self.assertIn("無い.bat", entries.problem)
        self.assertEqual(Path(entries.check).name, "launcher_check.bat")
        (start.parent / "launcher.json").write_text("{ 壊れた", encoding="utf-8")
        tool_entries.forget()
        self.assertIn("launcher.json を読めません", tool_entries.for_start(str(start)).problem)


class CheckTests(EntryTestCase):

    def test_終了コードで答える(self) -> None:
        start = make_entry_tool(self.work_root, "日報", ready_after=30)
        root = start.parent
        entries = tool_entries.for_start(str(start))
        self.assertEqual(tool_entries.check(entries).state, tool_entries.STOPPED)
        (root / "started.txt").write_text("")
        answer = tool_entries.check(entries)
        self.assertEqual((answer.state, answer.note), (tool_entries.STARTING, "データを読み込み中"))
        (root / "ready.txt").write_text("")
        self.assertTrue(tool_entries.check(entries).ready)

    def test_時間切れは分からないと答える(self) -> None:
        root = self.work_root / "遅い"
        root.mkdir()
        _script(root / tool_entries.CHECK_FILE, "sleep 5\nexit 0\n")
        entries = tool_entries.for_start(str(root / "start.bat"))
        answer = tool_entries.check(entries, timeout=0.3)
        self.assertEqual(answer.state, tool_entries.UNKNOWN)
        self.assertFalse(answer.alive)

    def test_URLの入口(self) -> None:
        import subprocess
        from _fake_tool_support import make_tool_dir

        port = free_port()
        root = make_tool_dir(self.work_root, app_id="fake.url", port=port,
                             ready_after=0.8)
        entries = tool_entries.Entries(folder=str(root),
                                       check_url=f"http://127.0.0.1:{port}/api/health")
        self.assertEqual(tool_entries.check(entries).state, tool_entries.STOPPED)
        proc = subprocess.Popen([str(root / "start.bat"), "--no-browser"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait, 5)
        # start.bat の下の本体ごと止める (start.bat だけ止めると本体が残る)
        self.addCleanup(lambda: [process_manager._terminate(p, force=True)
                                 for p in desktop.process_tree({proc.pid})])
        self.assertTrue(tool_entries.wait_until(entries, want_alive=True, timeout=10))
        deadline = time.monotonic() + 10
        while not tool_entries.check(entries).ready and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(tool_entries.check(entries).ready)


class ManagerEntryTests(ManagerTestCase):
    """ランチャーが入口を優先して使う。"""

    def setUp(self) -> None:
        super().setUp()
        self.roots: list[Path] = []

    def tearDown(self) -> None:
        # 一時フォルダーが消える (`super().tearDown`) **前に**、本体を止める
        for root in self.roots:
            self.stop_leftovers(root)
        super().tearDown()

    def register_entry_tool(self, app_id: str, name: str, **options):
        start = make_entry_tool(self.work_root, app_id, **options)
        self.roots.append(start.parent)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name,
            port=free_port(),                 # わざと間違えたポート (誰も待ち受けない)
            start_command=str(start)))
        return tool_registry.get(app_id)

    def stop_leftovers(self, root: Path) -> None:
        pid_file = root / "pid.txt"
        if not pid_file.exists():
            return
        pid = int(pid_file.read_text() or 0)
        (root / "stop.txt").write_text("")
        deadline = time.monotonic() + 2
        while pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid_file.exists() and pid:
            # 準備中で眠っている (stop.txt を見ない)。試験の後始末として止める
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def test_入口で起動を確かめる(self) -> None:
        tool = self.register_entry_tool("nlm.daily", "日報", ready_after=1.0)
        began = time.monotonic()
        self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING, status.detail)
        self.assertLess(time.monotonic() - began, 10)
        self.assertGreaterEqual(time.monotonic() - began, 1.0, "準備中 (2) なのに起動完了にしました")
        stages = [s.stage for s in self.statuses if s.stage]
        self.assertIn("データを読み込み中", stages, "入口の出力を準備の段階に出していません")
        self.assertTrue(self.manager.running["nlm.daily"].confirmed)

    def test_動いていれば押しても2つ目を起こさない(self) -> None:
        tool = self.register_entry_tool("nlm.daily", "日報", ready_after=0.3)
        self.start(tool)
        pid = (Path(tool.start_command).parent / "pid.txt").read_text()
        self.manager._select_blocking(tool)
        self.assertEqual((Path(tool.start_command).parent / "pid.txt").read_text(), pid)
        # ランチャーを起動し直しても、入口で見つけて引き継ぐ
        again = ToolManager()
        adopted = again.adopt_running()
        self.assertIn("nlm.daily", [r.app_id for r in adopted])

    def test_終了の入口で止めて入口で確かめる(self) -> None:
        tool = self.register_entry_tool("nlm.daily", "日報", ready_after=0.3)
        self.start(tool)
        root = Path(tool.start_command).parent
        with mock.patch.object(process_manager, "_stop_by_pid",
                               side_effect=AssertionError("PID で止めました")):
            self.assertTrue(self.manager._stop_blocking("nlm.daily"))
        self.assertFalse((root / "pid.txt").exists(), "ツールが動いたままです")
        self.assertNotIn("nlm.daily", self.manager.running)

    def test_終了の入口が断ったら知らせて強制終了を選ばせる(self) -> None:
        tool = self.register_entry_tool("nlm.busy", "日報", ready_after=0.3)
        root = Path(tool.start_command).parent
        _script(root / tool_entries.STOP_FILE,
                'echo "取り込みの途中です"\nexit 1\n')
        tool_entries.forget()
        self.start(tool)
        self.assertFalse(self.manager._stop_blocking("nlm.busy"))
        self.assertTrue(self.manager.stop_refused("nlm.busy"))
        self.assertIn("取り込みの途中です", self.manager.status.detail)
        self.assertTrue((root / "pid.txt").exists(), "断ったのに止めました")
        # 利用者が強制終了を選んだときだけ、ランチャーの止め方で続ける
        # (ここでは最後の手 = PID で止められたことにする)
        stopped = process_manager.StopResult(app_id="nlm.busy", display_name="日報",
                                             stopped=True, method="pid")
        with mock.patch.object(process_manager, "_stop_by_pid",
                               return_value=stopped) as by_pid, \
                mock.patch.object(process_manager, "_stop_by_api", return_value=None):
            self.assertTrue(self.manager._stop_blocking("nlm.busy", force=True))
        self.assertTrue(by_pid.called)

    def test_入口が動いていないと言えば思わず止まったと気づく(self) -> None:
        tool = self.register_entry_tool("nlm.daily", "日報", ready_after=0.3)
        self.start(tool)
        self.stop_leftovers(Path(tool.start_command).parent)
        for _ in range(5):
            self.manager.poll_health()
        self.assertNotIn("nlm.daily", self.manager.running)

    def test_使えると言わなければ起動失敗として記録し片付けない(self) -> None:
        ui = tool_registry.app_config.load()["ui"]
        with mock.patch.dict(ui, {"start_timeout_seconds": 1.5}):
            tool = self.register_entry_tool("nlm.slow", "看板", ready_after=30)
            self.start(tool)
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("起動確認の入口 (launcher_check.bat)", status.detail)
        text, _ = trace.read_record(status.incident)
        self.assertIn("準備中 (2)", text)
        self.assertTrue((Path(tool.start_command).parent / "pid.txt").exists(),
                        "ランチャーがツールを止めました (止め方はツールが決める)")

    def test_入口があればポートが無くても起動できる(self) -> None:
        """ポートを知っているのはツール自身。入口があればランチャーは要らない。"""
        start = make_entry_tool(self.work_root, "nlm.noport", ready_after=0.3)
        self.roots.append(start.parent)
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.noport", display_name="資材ツール", start_command=str(start)))
        tool = tool_registry.get("nlm.noport")
        self.assertEqual(tool.ui_problem(), "")
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING, self.manager.status.detail)

    def test_入口が無いツールはこれまでどおり(self) -> None:
        tool = self.register("fake.legacy", "カレンダー")
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(self.opened, [tool.home_url])

    def test_診断に入口を出す(self) -> None:
        self.register_entry_tool("nlm.daily", "日報")
        text = tool_registry.describe()
        self.assertIn("確認: ツールの入口", text)
        self.assertIn("終了: ツールの入口", text)


if __name__ == "__main__":
    unittest.main()
