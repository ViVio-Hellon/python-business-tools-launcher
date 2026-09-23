"""多重起動の防止 (要件定義書 §9 / 基盤仕様書 2.4)

**素早く2回押されたときに耐えられるか**を見る。`Start.vbs` は pythonw で
起動するので押しても数秒は何も出ない。ツールの起動にも時間がかかる。
どちらも「反応が無いからもう一度押す」が現実に起きる操作で、そこが
守れていなければ多重起動の防止は名ばかりになる。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

import app_manager  # noqa: E402
import launch_guard  # noqa: E402
import process_manager  # noqa: E402
from app_manager import State, ToolManager  # noqa: E402
from launcher import browser, runtime_state, tool_registry  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 同時に `acquire()` を呼ぶ子プロセス。**コマンドラインに本体の場所を
# 入れる** ── 本番の `pythonw "C:\\...\\launcher.py"` と同じ形にしないと、
# 照合が「別のプロセス」と答えて試験にならない
_CHILD = '''
import os, sys, time
root, local, start_at = sys.argv[1], sys.argv[2], float(sys.argv[3])
sys.path.insert(0, root)
os.environ["BUSINESS_TOOLS_LAUNCHER_LOCAL_DIR"] = local
from launcher import app_config
app_config.ensure_local_dirs()
import launch_guard
while time.time() < start_at:
    time.sleep(0.001)
print("YES" if launch_guard.acquire().should_start else "NO")
sys.stdout.flush()
time.sleep(1.5)
'''


class LauncherInstanceTests(LocalAreaTestCase):
    """ランチャー自身 (基盤仕様書 2.4)。"""

    def test_同時に起動しても1つだけ(self) -> None:
        """**Start.vbs を素早く2回**押した状況。

        調べてから書くと、その隙間に両方が通る。取れたら起動する形
        (`O_CREAT|O_EXCL`) でなければここは守れない。
        """
        start_at = time.time() + 1.0
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD, str(ROOT),
                 str(self.local_root), str(start_at)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for _ in range(2)]
        answers = [p.communicate(timeout=60)[0].strip() for p in procs]

        self.assertEqual(answers.count("YES"), 1,
                         f"起動できたランチャーの数が違います: {answers}")
        self.assertEqual(answers.count("NO"), 1)

    def test_書き込み途中のロックを壊れた扱いにしない(self) -> None:
        """**場所を押さえてから書くまでの一瞬**を、壊れたロックと誤らない。

        誤ると押さえたはずのロックが消え、もう1つが起動できてしまう。
        """
        path = launch_guard.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")   # 作った直後の姿

        # 途中から本物の中身が書かれる
        def finish() -> None:
            time.sleep(0.2)
            launch_guard.write_lock(launch_guard.build_lock_info())

        thread = threading.Thread(target=finish)
        thread.start()
        self.addCleanup(thread.join)

        info = launch_guard._read_lock_settled()
        self.assertIsNotNone(info, "書き終わるのを待てていません")
        self.assertTrue(path.exists(), "書き込み中のロックを消しました")

    def test_本当に壊れていれば片付ける(self) -> None:
        path = launch_guard.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("これはJSONではない", encoding="utf-8")

        result = launch_guard.acquire()
        self.assertTrue(result.should_start)
        self.assertIsNotNone(launch_guard.read_lock())

    def test_死んだロックなら起動できる(self) -> None:
        info = launch_guard.build_lock_info()
        info.pid = 999_999
        launch_guard.write_lock(info)

        self.assertTrue(launch_guard.acquire().should_start)
        self.assertEqual(launch_guard.read_lock().pid, os.getpid())


class ToolInstanceTests(LocalAreaTestCase):
    """業務ツール (要件定義書 §9「同じツールを選択した場合」)。"""

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.manager = ToolManager()
        self.spawns: list[str] = []
        self.addCleanup(self._cleanup)

        patcher = mock.patch.object(browser, "find_browser", return_value=("", ""))
        patcher.start()
        self.addCleanup(patcher.stop)
        opener = mock.patch.object(browser.webbrowser, "open")
        opener.start()
        self.addCleanup(opener.stop)

    def _cleanup(self) -> None:
        running = self.manager.current or runtime_state.read()
        if running is not None:
            process_manager.stop(running, force=True, timeout=5)
        self.manager._reap_process()

    def register(self, app_id: str, name: str, **kwargs):
        port = kwargs.pop("port", None) or free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port,
                             display_name=name, **kwargs)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name, port=port,
            start_command=str(root / "start.bat"), start_args="--no-browser"))
        return tool_registry.get(app_id)

    def count_spawns(self):
        real = self.manager._spawn

        def counting(tool):
            self.spawns.append(tool.app_id)
            return real(tool)

        return mock.patch.object(self.manager, "_spawn", counting)

    def test_素早く2回押してもstartbatは1回(self) -> None:
        """**2つ目はポートが空いていないので必ず失敗する。**

        守らないと、起動できているのに「起動できませんでした」と出る。
        """
        tool = self.register("fake.twice", "日報", ready_after=1.0)

        with self.count_spawns():
            threads = [threading.Thread(
                target=self.manager._select_blocking,
                args=(tool, self.manager._generation)) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(self.spawns), 1,
                         f"start.bat を {len(self.spawns)} 回実行しました")
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(runtime_state.read().app_id, "fake.twice")

    def test_3回押しても1回(self) -> None:
        tool = self.register("fake.thrice", "日報", ready_after=1.0)

        with self.count_spawns():
            threads = [threading.Thread(
                target=self.manager._select_blocking,
                args=(tool, self.manager._generation)) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(self.spawns), 1)

    def test_起動が終われば印は外れる(self) -> None:
        """次の操作が塞がれないこと。"""
        tool = self.register("fake.clear", "日報")
        self.manager._select_blocking(tool, self.manager._generation)

        self.assertIsNone(self.manager._starting)
        self.assertEqual(self.manager.status.state, State.RUNNING)

    def test_失敗しても印は外れる(self) -> None:
        tool = self.register("fake.fail", "看板", exit_code=1)
        self.manager._select_blocking(tool, self.manager._generation)

        self.assertIsNone(self.manager._starting,
                          "失敗したあと、次の起動が塞がれます")
        self.assertEqual(self.manager.status.state, State.ERROR)

    def test_動いているツールをもう一度押しても起こさない(self) -> None:
        tool = self.register("fake.again", "日報")
        with self.count_spawns():
            self.manager._select_blocking(tool, self.manager._generation)
            self.manager._select_blocking(tool, self.manager._generation)

        self.assertEqual(len(self.spawns), 1)


class ScreenTests(LocalAreaTestCase):
    """同じツールの画面を2枚にしない (要件定義書 §9)。

    プロセスが1つでも、**画面が2枚あればどちらにも入力できる**。
    業務ツールの作業状態はプロセスに1つしか無い(総合ツールの
    `selection_session` など)ので、2枚あると奪い合う。
    """

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.browser_log = self.work_root / "opened.txt"
        os.environ["FAKE_BROWSER_LOG"] = str(self.browser_log)
        self.addCleanup(os.environ.pop, "FAKE_BROWSER_LOG", None)

        fake = Path(__file__).resolve().parent / "_fake_browser.py"
        patcher = mock.patch.object(browser, "find_browser",
                                    return_value=("fake", str(fake)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cleanup)
        self.managers: list = []

    def _cleanup(self) -> None:
        for manager in self.managers:
            running = manager.current
            if running is not None:
                process_manager.stop(running, force=True, timeout=5)
            manager._reap_process()
        if runtime_state.read() is not None:
            process_manager.stop(runtime_state.read(), force=True, timeout=5)
        for pid in browser.managed_pids():
            process_manager._terminate(pid, force=True)
            browser.forget(pid)

    @property
    def opened(self) -> list:
        if not self.browser_log.exists():
            return []
        return [line for line in
                self.browser_log.read_text(encoding="utf-8").splitlines() if line]

    def make_manager(self) -> ToolManager:
        manager = ToolManager()
        self.managers.append(manager)
        return manager

    def register(self, app_id: str, name: str):
        port = free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port,
                             display_name=name)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name, port=port,
            start_command=str(root / "start.bat"), start_args="--no-browser"))
        return tool_registry.get(app_id)

    def wait_opened(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while len(self.opened) < count and time.time() < deadline:
            time.sleep(0.05)

    def test_手がかりを失っても2枚目を開かない(self) -> None:
        """**記録が消えても、すでに開いている画面を探し直す。**

        記録は消えることがある(ランチャー外で起動していた、生存監視が
        誤って消した)。そのままだと「画面が無い」と判断してもう1枚開き、
        2つの画面が同じ作業状態を奪い合う。
        """
        tool = self.register("fake.screen", "日報")
        first = self.make_manager()
        first._select_blocking(tool, first._generation)
        self.wait_opened(1)
        first_window = first.current.browser_pid
        self.assertTrue(process_manager._is_alive(first_window))

        # ランチャーを開き直し、記録を失った状況
        runtime_state.clear()
        second = self.make_manager()
        second.adopt_running()
        self.assertEqual(second.current.browser_pid, 0,
                         "この試験は手がかりが無い状態を前提にしています")

        # 利用者がもう一度ボタンを押す
        second._select_blocking(tool, second._generation)
        time.sleep(0.5)

        self.assertEqual(len(self.opened), 1,
                         f"画面が {len(self.opened)} 枚開きました")
        self.assertTrue(process_manager._is_alive(first_window),
                        "元の画面が閉じられています")

    def test_見つけた画面は閉じられる(self) -> None:
        """探し直した画面の手がかりを取り戻し、切り替えで閉じられること。"""
        tool = self.register("fake.recover", "日報")
        first = self.make_manager()
        first._select_blocking(tool, first._generation)
        self.wait_opened(1)
        first_window = first.current.browser_pid

        runtime_state.clear()
        second = self.make_manager()
        second.adopt_running()
        second._select_blocking(tool, second._generation)

        # 手がかりを取り戻している
        self.assertEqual(second.current.browser_pid, first_window)
        self.assertTrue(second.current.browser_managed)
        self.assertTrue(process_manager.is_browser_open(second.current))

        # だから閉じられる
        self.assertTrue(process_manager.close_browser(second.current))
        self.assertFalse(process_manager._is_alive(first_window))

    def test_本当に画面が無ければ開く(self) -> None:
        """探し直しが、必要な画面まで開かなくするのでは困る。"""
        tool = self.register("fake.reopen2", "日報")
        manager = self.make_manager()
        manager._select_blocking(tool, manager._generation)
        self.wait_opened(1)

        window = manager.current.browser_pid
        process_manager._terminate(window, force=True)
        process_manager._wait_pid_gone(window, 5)
        manager.poll_health()

        manager._select_blocking(tool, manager._generation)
        self.wait_opened(2)

        self.assertEqual(len(self.opened), 2, "画面を開き直せていません")
        self.assertNotEqual(manager.current.browser_pid, window)


class LockedDownPcTests(ScreenTests):
    """`wmic` も PowerShell も使えない端末。

    Windows 11 24H2 以降は `wmic` が既定で入っていない。工場の端末では
    PowerShell を一般利用者に禁じていることがある。両方使えないと、
    外からプロセスを調べる手段がすべて無くなる。

    ここで確かめるのは**できること**と**できないこと**の両方。
    できないことを「できる」と試験が言ってしまうと、現場で初めて気づく。
    """

    def setUp(self) -> None:
        super().setUp()
        # 外からプロセスを調べる手段を、どちらも塞ぐ
        for name, value in (("process_command_line", ""),):
            patcher = mock.patch.object(process_manager, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(process_manager, "_running_processes",
                                    side_effect=lambda: iter(()))
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- できること ------------------------------------------------
    def test_自分で開いた画面は閉じられる(self) -> None:
        """**いちばんよく通る道。** 開いたときの手がかりで閉じる。

        閉じられないと、切り替えのたびに画面が2枚になる。
        """
        a = self.register("fake.lock1", "日報")
        b = self.register("fake.lock2", "看板")
        manager = self.make_manager()

        manager._select_blocking(a, manager._generation)
        self.wait_opened(1)
        first = manager.current.browser_pid
        self.assertTrue(process_manager._is_alive(first))

        manager._select_blocking(b, manager._generation)

        self.assertFalse(process_manager._is_alive(first),
                         "照合できない端末で、切り替え時に画面を閉じられません")

    def test_手がかりの無い画面は閉じない(self) -> None:
        """**確かめられないものには触らない**、は変えない。"""
        import subprocess as sp

        other = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        self.addCleanup(lambda: (other.kill(), other.wait(timeout=5)))
        running = runtime_state.RunningTool(
            app_id="x", browser_pid=other.pid,
            browser_profile=str(self.work_root / "プロファイル"))

        self.assertFalse(process_manager.close_browser(running))
        self.assertIsNone(other.poll(), "照合できないプロセスを閉じました")

    def test_本当に画面が無ければ開く(self) -> None:
        super().test_本当に画面が無ければ開く()

    def test_診断で分かる(self) -> None:
        ok, how = process_manager.inspection_status()
        self.assertFalse(ok)
        self.assertIn("wmic", how)

    # --- できないこと (制限として固定する) ---------------------------
    def test_手がかりを失っても2枚目を開かない(self) -> None:
        """**この端末ではできない。** 前のランチャーが開いた画面は、
        外から調べる手段が無いので探し出せない。

        記録を失うこと自体がまれ (ランチャー外での起動、生存監視の誤判定)
        なので制限として受け入れ、`start_debug.bat --check` で知らせる。
        """
        self.skipTest("制限: 照合できない端末では前回の画面を探し出せない")

    def test_見つけた画面は閉じられる(self) -> None:
        self.skipTest("制限: 照合できない端末では前回の画面を探し出せない")


class MarkerSearchTests(LocalAreaTestCase):
    """印からプロセスを探す。"""

    def test_短い印では探さない(self) -> None:
        """短い印はどのプロセスにも当たる。**探さないほうが安全。**"""
        for marker in ("", "/", "C:\\", "/opt"):
            with self.subTest(marker):
                self.assertEqual(
                    process_manager.find_process_by_marker(marker), 0)

    def test_自分自身を見つけられる(self) -> None:
        import subprocess as sp

        marker = str(self.work_root / "目印になる長いフォルダー名")
        proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                         f"--user-data-dir={marker}"],
                        stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        self.addCleanup(lambda: (proc.kill(), proc.wait(timeout=5)))
        for _ in range(50):
            if process_manager.process_command_line(proc.pid):
                break
            time.sleep(0.02)

        self.assertEqual(process_manager.find_process_by_marker(marker),
                         proc.pid)

    def test_無ければ0(self) -> None:
        self.assertEqual(
            process_manager.find_process_by_marker(
                str(self.work_root / "どこにも無い長いフォルダー名")), 0)


if __name__ == "__main__":
    unittest.main()
