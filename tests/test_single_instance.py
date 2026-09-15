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


if __name__ == "__main__":
    unittest.main()
