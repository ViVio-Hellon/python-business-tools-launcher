"""起動・切替・停止の通し試験 (要件定義書 §7 / §8 / §9 / §20)

偽の業務ツールを本物と同じ形 (`start.bat` + `/api/health` +
`/api/shutdown`) で用意し、ランチャーが決められた順序どおりに
動くことを確かめる。**ここが要件定義書 §20「完成条件」の中身**。

画面 (tkinter) は使わない。`ToolManager` は画面を読み込まないので、
画面の無い環境でも通せる。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _fake_tool_support import free_port, make_app_dir, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

import app_manager  # noqa: E402
import process_manager  # noqa: E402
from app_manager import State, ToolManager  # noqa: E402
from launcher import (browser, desktop, health, runtime_state,  # noqa: E402
                      tool_registry, trace)

_FAKE_BROWSER = Path(__file__).resolve().parent / "_fake_browser.py"


class ManagerTestCase(LocalAreaTestCase):
    """偽ツールを登録した `ToolManager` を用意する。"""

    def setUp(self) -> None:
        super().setUp()
        tool_registry.initialize()
        self.statuses: list = []
        self.manager = ToolManager(on_status=self.statuses.append)

        # 偽のブラウザーを使う。**本物と同じように起こして閉じる** ──
        # 開いたことにするだけの差し替えでは、要件定義書 §8.3 の
        # 「切り替えのとき画面を閉じる」が確かめられない
        self.browser_log = self.work_root / "opened.txt"
        os.environ["FAKE_BROWSER_LOG"] = str(self.browser_log)
        self.addCleanup(os.environ.pop, "FAKE_BROWSER_LOG", None)
        patcher = mock.patch.object(browser, "find_browser",
                                    return_value=("fake", str(_FAKE_BROWSER)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._stop_everything)

    @property
    def opened(self) -> list:
        """これまでに開かれた画面のURL。"""
        if not self.browser_log.exists():
            return []
        return [line for line in
                self.browser_log.read_text(encoding="utf-8").splitlines() if line]

    def _stop_everything(self) -> None:
        records = dict(runtime_state.read_all())
        records.update(self.manager.running)
        for running in records.values():
            process_manager.stop(running, force=True, timeout=5)
        # `start.bat` の受け皿も引き取る。残すと、試験のたびに
        # 引き取られないプロセスが増えていく
        for app_id in list(self.manager._processes):
            self.manager._reap_process(app_id)
        # 残った画面も片付ける。**本番では残ってよい** ── ツールを
        # 動かしたままランチャーだけ閉じたときは、画面も残るのが正しい
        # (要件定義書 §11)。片付けるのは試験の都合
        for pid in browser.managed_pids():
            process_manager._terminate(pid, force=True)
            browser.forget(pid)

    def register(self, app_id: str, display_name: str, **kwargs) -> object:
        """偽ツールを1つ作って設定DBへ登録する。"""
        port = kwargs.pop("port", None) or free_port()
        root = make_tool_dir(self.work_root, app_id=app_id, port=port,
                             display_name=display_name, **kwargs)
        tool = tool_registry.Tool(
            app_id=app_id, display_name=display_name, port=port,
            start_command=str(root / "start.bat"),
            # 偽ツールは `--no-browser` を受け取って無視する
            start_args="--no-browser")
        tool_registry.save(tool)
        return tool_registry.get(app_id)

    def start(self, tool) -> None:
        """起動を待ち合わせる (試験では別スレッドにしない)。"""
        before = len(self.opened)
        self.manager._select_blocking(tool)
        running = self.manager.running.get(tool.app_id)
        if running is not None and running.browser_pid and len(self.opened) <= before:
            # 画面を起こしたところまでは同期で確かめられるが、
            # **記録を書くのは向こうのプロセス**なので、そこは待つ
            self.wait_opened(before + 1)

    def wait_opened(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.opened) < count and time.monotonic() < deadline:
            time.sleep(0.05)


class StartTests(ManagerTestCase):
    """要件定義書 §7 起動処理。"""

    def test_起動完了を確認してからブラウザーを開く(self) -> None:
        # 準備に2秒かかるツール。`start.bat` を実行しただけで
        # 起動完了と判断していれば、ブラウザーは早く開いてしまう
        tool = self.register("fake.slow", "日報", ready_after=1.5)
        began = time.monotonic()
        self.start(tool)
        elapsed = time.monotonic() - began

        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(self.opened, [tool.home_url])
        self.assertGreaterEqual(
            elapsed, 1.5,
            "準備が終わる前にブラウザーを開いています (要件定義書 §7.2)")

    def test_起動中は状態を表示する(self) -> None:
        tool = self.register("fake.stage", "日報", ready_after=1.0)
        self.start(tool)
        starting = [s for s in self.statuses if s.state is State.STARTING]
        self.assertTrue(starting, "起動中の表示がありません (要件定義書 §5.2)")
        self.assertIn("日報", starting[0].message)

    def test_起動後の記録にPIDとポートが入る(self) -> None:
        tool = self.register("fake.record", "日報")
        self.start(tool)

        running = runtime_state.read_all().get("fake.record")
        self.assertIsNotNone(running)
        self.assertEqual(running.port, tool.port)
        # **ツール本体のPID**が入っていること。`start.bat` を起こした
        # プロセスのPIDでは止められない (要件定義書 §10)
        self.assertGreater(running.pid, 0)
        self.assertNotEqual(running.pid, running.launch_pid)

    def test_startbatが未設定なら理由を出す(self) -> None:
        tool_registry.save(tool_registry.Tool(
            app_id="fake.unset", display_name="カレンダー", port=free_port()))
        self.start(tool_registry.get("fake.unset"))

        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("設定", status.detail)
        self.assertEqual(self.opened, [], "失敗したのにブラウザーを開いています")

    def test_startbatが失敗したら戻り値を出す(self) -> None:
        tool = self.register("fake.broken", "看板", exit_code=3)
        self.start(tool)

        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("戻り値 3", status.detail)
        # 時間切れを待たずに気づくこと。90秒待っていたら遅すぎる
        self.assertLess(status.elapsed, 30)

    def test_起動に失敗してもランチャーは使える(self) -> None:
        """要件定義書 §20「異常終了時にランチャーが継続して利用できる」。"""
        broken = self.register("fake.ng", "看板", exit_code=1)
        self.start(broken)
        self.assertEqual(self.manager.status.state, State.ERROR)

        good = self.register("fake.ok", "日報")
        self.start(good)
        self.assertEqual(self.manager.status.state, State.RUNNING)


class SameToolTests(ManagerTestCase):
    """要件定義書 §9 同じツールを選択した場合。"""

    def test_同じツールを押しても二重起動しない(self) -> None:
        tool = self.register("fake.same", "日報")
        self.start(tool)
        first = self.manager.running[tool.app_id]

        with mock.patch.object(browser, "bring_to_front",
                               return_value=True) as front:
            self.start(tool)
        second = self.manager.running[tool.app_id]

        self.assertEqual(first.pid, second.pid, "二重に起動しています")
        # 画面が出ているのにもう1枚開かない。**前に出すだけ**
        self.assertEqual(self.opened, [tool.home_url],
                         "画面を二重に開いています")
        front.assert_called_once_with(first.browser_pid)


class ConcurrentTests(ManagerTestCase):
    """ツールを**同時に**使う。

    以前は切り替えるたびに前のツールを止めていた。ツールが自分の画面を
    ふだんのブラウザーに開く形だと、その画面が残り、どれも「バックエンドに
    接続できません」になっていた。
    """

    def alive(self, tool) -> bool:
        return health.is_tool(health.probe(tool.health_url), tool.app_id)

    def test_2つ目を起動しても1つ目は止めない(self) -> None:
        a = self.register("fake.a", "日報")
        b = self.register("fake.b", "看板")
        self.start(a)
        self.start(b)

        self.assertTrue(self.alive(a), "2つ目の起動で1つ目を止めています")
        self.assertTrue(self.alive(b))
        self.assertEqual(set(self.manager.running), {"fake.a", "fake.b"})
        self.assertEqual(set(runtime_state.read_all()), {"fake.a", "fake.b"})
        # 画面は2つとも出ている
        self.assertEqual(self.opened, [a.home_url, b.home_url])

    def test_動いているツールがバーに分かる(self) -> None:
        a = self.register("fake.r1", "日報")
        b = self.register("fake.r2", "看板")
        self.start(a)
        self.start(b)
        status = self.manager.status
        self.assertEqual(set(status.running_ids), {"fake.r1", "fake.r2"})
        self.assertEqual(status.focus_id, "fake.r2")
        self.assertEqual(status.message, "動作中：日報、看板")

    def test_終わった知らせではもう最中に数えない(self) -> None:
        """動いているのにボタンが「…」(最中) のまま残っていた。"""
        a = self.register("fake.done", "日報")
        self.start(a)
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertNotIn("fake.done", status.starting_ids)
        self.assertIn("fake.done", status.running_ids)

        self.manager._stop_blocking("fake.done")
        status = self.manager.status
        self.assertNotIn("fake.done", status.stopping_ids)
        self.assertNotIn("fake.done", status.running_ids)

    def test_4つ同時に動かせる(self) -> None:
        tools = [self.register(f"fake.c{i}", name) for i, name in
                 enumerate(("日報", "カレンダー", "看板", "総合"))]
        for tool in tools:
            self.start(tool)
            self.assertEqual(self.manager.status.state, State.RUNNING,
                             f"{tool.display_name} で止まりました")
        self.assertTrue(all(self.alive(t) for t in tools))

    def test_1つ止めてもほかは動き続ける(self) -> None:
        """報告のあった不具合: 1つ閉じると全部「接続できません」になる。"""
        a = self.register("fake.k1", "日報")
        b = self.register("fake.k2", "看板")
        c = self.register("fake.k3", "カレンダー")
        for tool in (a, b, c):
            self.start(tool)

        self.assertTrue(self.manager._stop_blocking("fake.k2"))
        self.assertFalse(self.alive(b))
        self.assertTrue(self.alive(a), "ほかのツールまで止まりました")
        self.assertTrue(self.alive(c), "ほかのツールまで止まりました")
        self.assertEqual(set(self.manager.running), {"fake.k1", "fake.k3"})
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertIn("看板を終了しました", self.manager.status.message)

    def test_画面を閉じて終わったツールだけ外れる(self) -> None:
        """画面を閉じると、そのツールは自分で終わる。ほかには触らない。"""
        from launcher import app_config

        a = self.register("fake.e1", "日報")
        b = self.register("fake.e2", "看板")
        self.start(a)
        self.start(b)
        a_running = self.manager.running["fake.e1"]

        # 利用者が日報の画面を閉じ、日報は「誰も見ていない」ので終わった
        process_manager._terminate(a_running.browser_pid, force=True)
        process_manager._wait_pid_gone(a_running.browser_pid, 5)
        self.manager.poll_health()
        process_manager._stop_by_api(a_running, force=True, timeout=10)

        limit = int(app_config.ui_setting("health_failures_before_dead"))
        for _ in range(limit):
            self.manager.poll_health()

        self.assertEqual(set(self.manager.running), {"fake.e2"})
        self.assertTrue(self.alive(b))
        status = self.manager.status
        # 画面を閉じて終わったのはふつうのこと。エラーにしない
        self.assertNotEqual(status.state, State.ERROR)
        self.assertIn("日報は終了しました", status.message)

    def test_すべて止める(self) -> None:
        a = self.register("fake.all1", "日報")
        b = self.register("fake.all2", "看板")
        self.start(a)
        self.start(b)
        self.assertTrue(self.manager._stop_all_blocking())
        self.assertEqual(self.manager.running, {})
        self.assertEqual(runtime_state.read_all(), {})
        self.assertEqual(self.manager.status.state, State.IDLE)


class StopTests(ManagerTestCase):
    """要件定義書 §10 プロセス管理 / 安全な終了。"""

    def test_正常終了できる(self) -> None:
        tool = self.register("fake.stop", "日報")
        self.start(tool)
        self.assertTrue(self.manager._stop_blocking(tool.app_id))

        self.assertEqual(self.manager.status.state, State.IDLE)
        self.assertEqual(runtime_state.read_all(), {})
        self.assertIsNone(self.manager.current)

    def test_実行中の処理があれば止めずに知らせる(self) -> None:
        """基盤仕様書 2.8「実行中の終了確認」。"""
        tool = self.register("fake.busy", "看板", busy=True)
        self.start(tool)
        stopped = self.manager._stop_blocking(tool.app_id)

        self.assertFalse(stopped)
        self.assertIn("実行中の処理", self.manager.status.detail)
        # まだ動いていること。黙って落としていない
        self.assertTrue(process_manager.is_running(
            runtime_state.read_all()[tool.app_id]))

    def test_強制指定なら実行中でも止める(self) -> None:
        tool = self.register("fake.busy2", "看板", busy=True)
        self.start(tool)
        self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        self.assertEqual(runtime_state.read_all(), {})

    def test_stopbatが無くても停止APIで止まる(self) -> None:
        tool = self.register("fake.nobat", "日報", with_stop_bat=False)
        self.start(tool)
        running = runtime_state.read_all()[tool.app_id]
        self.assertIsNone(process_manager.resolve_stop_bat(running))

        result = process_manager.stop(running, timeout=10)
        self.assertTrue(result.stopped, result.message)
        self.assertEqual(result.method, "shutdown-api")

    def test_起動中なら起動をやめられる(self) -> None:
        """遅いツールを押してしまった。待たずにやめられる。"""
        import threading

        tool = self.register("fake.cancel", "日報", ready_after=30)
        worker = threading.Thread(target=self.manager._select_blocking,
                                  args=(tool,))
        worker.start()
        deadline = time.monotonic() + 10
        while not self.manager.is_busy(tool.app_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.manager.stop(tool.app_id)
        worker.join(30)

        self.assertFalse(worker.is_alive(), "起動待ちが終わりません")
        self.assertNotIn(tool.app_id, self.manager.running)
        self.assertIn("起動をやめました", self.manager.status.message)
        self.assertEqual(self.opened, [])


class BrowserTests(ManagerTestCase):
    """要件定義書 §8.3 ブラウザー画面の管理。"""

    def test_専用ウィンドウとして開く(self) -> None:
        """§8.3.1 ランチャーが管理できる形で開く。"""
        tool = self.register("fake.win", "日報")
        self.start(tool)

        running = runtime_state.read()
        self.assertTrue(running.browser_managed,
                        "閉じられない形で開いています")
        self.assertGreater(running.browser_pid, 0)
        # 専用プロファイルはツールごとに分かれている
        self.assertEqual(Path(running.browser_profile),
                         browser.profile_dir("fake.win"))
        self.assertTrue(process_manager.is_browser_open(running))

    def test_止めると画面が閉じる(self) -> None:
        """§8.3.2 ツールを止めるとき、そのツールの画面を閉じる。"""
        a = self.register("fake.w1", "日報")
        b = self.register("fake.w2", "看板")
        self.start(a)
        self.start(b)
        a_running = self.manager.running["fake.w1"]
        b_running = self.manager.running["fake.w2"]

        self.manager._stop_blocking("fake.w1")

        self.assertFalse(process_manager._is_alive(a_running.browser_pid),
                         "止めた日報の画面が残っています")
        # ほかのツールの画面には触らない
        self.assertTrue(process_manager.is_browser_open(b_running))

    def test_画面を閉じてからバックエンドを止める(self) -> None:
        """§8.3.2 の処理順序。逆だと利用者に接続エラーが見える。"""
        a = self.register("fake.order1", "日報")
        self.start(a)
        running = self.manager.running["fake.order1"]

        order: list[str] = []
        real_close = process_manager.close_browser
        real_stop_api = process_manager._stop_by_api
        real_stop_bat = process_manager._stop_by_bat

        def spy_close(r):
            order.append("画面を閉じる")
            return real_close(r)

        def spy_api(r, **kw):
            order.append("バックエンドを止める")
            return real_stop_api(r, **kw)

        def spy_bat(r, **kw):
            order.append("バックエンドを止める")
            return real_stop_bat(r, **kw)

        with mock.patch.object(process_manager, "close_browser", spy_close), \
             mock.patch.object(process_manager, "_stop_by_api", spy_api), \
             mock.patch.object(process_manager, "_stop_by_bat", spy_bat):
            self.manager._stop_blocking("fake.order1")

        self.assertEqual(order[:2], ["画面を閉じる", "バックエンドを止める"],
                         f"順序が違います: {order}")
        self.assertFalse(process_manager._is_alive(running.browser_pid))

    def test_無関係なブラウザーは閉じない(self) -> None:
        """§8.3.3 利用者が別に開いているブラウザーには触らない。

        **この試験がいちばん大事。** 専用プロファイルの道を持たない
        プロセスは、PIDが記録に入っていても閉じない。
        """
        import subprocess as sp

        tool = self.register("fake.other", "日報")
        self.start(tool)
        running = self.manager.running[tool.app_id]

        # 利用者がふだん使っているブラウザーのつもり
        other = sp.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                          "--user-data-dir=/home/利用者/AppData/Google/Chrome"],
                         stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        self.addCleanup(self._kill_quietly, other)
        for _ in range(50):
            if process_manager.process_command_line(other.pid):
                break
            time.sleep(0.02)

        # そのPIDを掴んでいる状態を作る
        running.browser_pid = other.pid
        closed = process_manager.close_browser(running)

        self.assertFalse(closed, "無関係なブラウザーを閉じたと報告しています")
        self.assertIsNone(other.poll(), "無関係なブラウザーを閉じました")

    @staticmethod
    def _kill_quietly(proc) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def test_画面だけ手で閉じてもバックエンドは動く(self) -> None:
        """§8.3.4 画面とバックエンドを別々に把握する。"""
        tool = self.register("fake.manual", "日報")
        self.start(tool)
        running = self.manager.running[tool.app_id]
        self.manager.poll_health()              # 画面が出ていることを見ておく

        # 利用者が画面だけ手で閉じた
        process_manager._terminate(running.browser_pid, force=True)
        process_manager._wait_pid_gone(running.browser_pid, 5)

        # バックエンドは動いている。ランチャーはその状態を把握する
        self.assertTrue(self.manager.poll_health())
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertTrue(status.responding, "バックエンドを落ちた扱いにしています")
        self.assertFalse(status.browser_open)
        self.assertTrue(status.browser_managed)
        self.assertIn("まもなく終了します", status.message)

    def test_同じボタンで画面だけ開き直せる(self) -> None:
        tool = self.register("fake.reopen", "日報")
        self.start(tool)
        first = runtime_state.read_all()[tool.app_id]
        first_window = first.browser_pid

        process_manager._terminate(first.browser_pid, force=True)
        process_manager._wait_pid_gone(first.browser_pid, 5)
        self.manager.poll_health()

        self.start(tool)
        second = runtime_state.read_all()[tool.app_id]

        # バックエンドは起動し直していない
        self.assertEqual(first.pid, second.pid, "バックエンドを起動し直しました")
        # 画面は開き直されている
        self.assertNotEqual(first_window, second.browser_pid)
        self.assertTrue(process_manager.is_browser_open(second))
        self.assertEqual(self.opened, [tool.home_url, tool.home_url])

    def test_閉じられない形なら記録に残る(self) -> None:
        """Chromium系が無い端末。**黙って閉じたことにしない。**"""
        tool = self.register("fake.default", "日報")
        with mock.patch.object(browser, "find_browser", return_value=("", "")), \
             mock.patch.object(browser.webbrowser, "open") as opened:
            self.start(tool)

        opened.assert_called_once_with(tool.home_url)
        running = runtime_state.read_all()[tool.app_id]
        self.assertFalse(running.browser_managed)
        self.assertEqual(running.browser_pid, 0)
        # 手で閉じてもらう必要があることを画面に出す
        self.assertIn("手で閉じて", self.manager.status.detail)


class ShutdownTests(ManagerTestCase):
    """ランチャーを閉じるとき (要件定義書 §11)。"""

    def test_ツールを残したまま閉じられる(self) -> None:
        """ブラウザーを閉じることとバックエンドを止めることは別。"""
        a = self.register("fake.keep1", "日報")
        b = self.register("fake.keep2", "看板")
        self.start(a)
        self.start(b)
        records = runtime_state.read_all()

        self.assertTrue(self.manager.shutdown(stop_tools=False))
        # ツールはまだ動いている
        for running in records.values():
            self.assertTrue(process_manager.is_running(running))
        # 記録も残る。次にランチャーを開いたとき引き継げる
        self.assertEqual(set(runtime_state.read_all()), {"fake.keep1", "fake.keep2"})

    def starting_in_background(self, tool) -> threading.Thread:
        """起動を始め、`start.bat` を実行したところまで待つ。"""
        worker = threading.Thread(target=self.manager._select_blocking,
                                  args=(tool,), daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while tool.app_id not in self.manager._processes \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.manager.starting_ids, [tool.app_id])
        self.addCleanup(worker.join, 10)
        return worker

    def test_いいえなら起動の最中のツールもやめさせない(self) -> None:
        """以前は「動かしたまま」を選んでも、起動中のツールは起動をやめていた。"""
        tool = self.register("fake.midway", "看板", ready_after=1.0)
        worker = self.starting_in_background(tool)

        self.assertTrue(self.manager.shutdown(stop_tools=False))
        worker.join(10)
        self.assertIn(tool.app_id, self.manager.running, "起動をやめさせました")
        running = self.manager.running[tool.app_id]
        self.assertTrue(process_manager.is_running(running))
        # 後始末: ツールを止めてから、手放した start.bat を引き取る
        process_manager.stop(running, force=True, timeout=5)
        for proc in list(app_manager._DETACHED):
            app_manager._DETACHED.remove(proc)
            proc.wait(timeout=10)

    def test_はいなら起動の最中のツールは片付けてから閉じる(self) -> None:
        tool = self.register("fake.midway2", "看板", ready_after=30)
        self.starting_in_background(tool)

        self.assertTrue(self.manager.shutdown(stop_tools=True))
        self.assertEqual(self.manager.starting_ids, [])
        self.assertFalse(health.is_tool(health.probe(tool.health_url), tool.app_id))

    def test_ツールごと閉じられる(self) -> None:
        a = self.register("fake.both1", "日報")
        b = self.register("fake.both2", "看板")
        self.start(a)
        self.start(b)

        self.assertTrue(self.manager.shutdown(stop_tools=True))
        self.assertEqual(runtime_state.read_all(), {})

    def test_止まらなければ偽を返す(self) -> None:
        """**黙って閉じない。** 止めたつもりで残るのがいちばん困る。"""
        tool = self.register("fake.stuck", "看板", busy=True)
        self.start(tool)
        running = runtime_state.read_all()[tool.app_id]

        self.assertFalse(self.manager.shutdown(stop_tools=True))
        self.assertTrue(process_manager.is_running(running),
                        "止まっていないのに記録だけ消えています")
        self.assertIn("実行中の処理", self.manager.status.detail)

        # 中断してよいと言われたら止める
        self.assertTrue(self.manager.shutdown(stop_tools=True, force=True))
        self.assertEqual(runtime_state.read_all(), {})


class MonitorTests(ManagerTestCase):
    """基盤仕様書 2.9 ブラウザーとバックエンドの状態監視。"""

    def test_画面が出ているのに落ちたら知らせる(self) -> None:
        from launcher import app_config

        tool = self.register("fake.die", "日報")
        self.start(tool)
        self.assertTrue(self.manager.poll_health())

        # ツールだけを落とす (画面は出たまま。思わぬ停止)
        running = self.manager.running[tool.app_id]
        process_manager._stop_by_api(running, force=True, timeout=10)

        # **1回では断じない。** スリープ復帰や重い処理中に、動いている
        # ツールを落ちた扱いにしないため (tests/test_resilience.py)
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        for _ in range(limit - 1):
            self.manager.poll_health()
            self.assertIn(tool.app_id, self.manager.running)

        self.assertFalse(self.manager.poll_health())
        self.assertEqual(self.manager.status.state, State.ERROR)
        self.assertIn("終了しました", self.manager.status.message)

    def test_ランチャー外で動いているツールをすべて引き継ぐ(self) -> None:
        """要件定義書 §9。記録が無くても二重起動させない。"""
        a = self.register("fake.adopt1", "日報")
        b = self.register("fake.adopt2", "看板")
        self.start(a)
        self.start(b)
        # ランチャーを再起動したことにする (記録も失った)
        runtime_state.clear()
        fresh = ToolManager()

        adopted = fresh.adopt_running()
        self.assertEqual({r.app_id for r in adopted}, {"fake.adopt1", "fake.adopt2"})
        self.assertEqual(fresh.status.state, State.RUNNING)
        self.assertEqual(set(fresh.status.running_ids),
                         {"fake.adopt1", "fake.adopt2"})

    def test_以前の版の記録も引き継ぐ(self) -> None:
        """同時に1つだけの頃の `current.json` が残っていても読む。"""
        import json
        from dataclasses import asdict

        tool = self.register("fake.legacy", "日報")
        self.start(tool)
        running = self.manager.running[tool.app_id]
        runtime_state.clear()
        legacy = runtime_state.state_path().with_name("current.json")
        legacy.write_text(json.dumps(asdict(running)), encoding="utf-8")

        fresh = ToolManager()
        adopted = fresh.adopt_running()
        self.assertEqual([r.app_id for r in adopted], ["fake.legacy"])
        # 画面の手がかりも引き継いでいる (止めるとき画面を閉じられる)
        self.assertEqual(adopted[0].browser_pid, running.browser_pid)
        self.assertFalse(legacy.exists(), "古い記録が残っています")


class LeftoverScreenTests(ManagerTestCase):
    """止めたあとに残る画面は、残ると伝える。"""

    def register_self_opening(self, app_id: str, name: str):
        """ツールが自分で画面を開く形 (起動引数が届かない・空)。"""
        tool = self.register(app_id, name)
        tool_registry.save(replace(tool, start_args=""))
        return tool_registry.get(app_id)

    def test_ランチャーが閉じられない画面は手で閉じてと出す(self) -> None:
        tool = self.register_self_opening("fake.self", "日報")
        self.start(tool)
        self.assertFalse(self.manager.running[tool.app_id].browser_managed)

        self.manager._stop_blocking(tool.app_id)
        status = self.manager.status
        self.assertEqual(status.state, State.IDLE)
        self.assertIn("手で閉じて", status.message)
        self.assertIn("タブを手で閉じて", status.detail)

    def test_ランチャーが閉じた画面なら何も足さない(self) -> None:
        tool = self.register("fake.managed", "日報")
        self.start(tool)
        self.assertTrue(self.manager.running[tool.app_id].browser_managed)

        self.manager._stop_blocking(tool.app_id)
        self.assertEqual(self.manager.status.message, "日報を終了しました")
        self.assertEqual(self.manager.status.detail, "")


class RecordTests(ManagerTestCase):
    """後追いの記録。**起動しなかった理由を、あとから順に追えるか。**"""

    def events(self, kind: str = "") -> list[dict]:
        rows = trace.read_events()
        return [r for r in rows if not kind or r["種類"] == kind]

    def incident_text(self) -> str:
        path = self.manager.status.incident
        self.assertTrue(path, "障害記録がありません")
        return Path(path).read_text(encoding="utf-8-sig")

    def test_起動できたら版とPIDとかかった秒を残す(self) -> None:
        tool = self.register("fake.rec", "日報", ready_after=0.5)
        self.start(tool)

        started = self.events("起動開始")
        done = self.events("起動完了")
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["結果"], "成功")
        self.assertEqual(done[0]["ツール版"], "1.0.0")
        self.assertEqual(done[0]["PID"], str(self.manager.running[tool.app_id].pid))
        self.assertGreaterEqual(float(done[0]["経過秒"]), 0.5)
        # ボタンを押してから起動できるまでが、同じ操作IDで追える
        self.assertEqual(started[0]["操作ID"], done[0]["操作ID"])
        self.assertEqual(self.manager.status.incident, "")

    def test_起動ファイルが落ちたらなぜなぜを書く(self) -> None:
        tool = self.register("fake.crash", "看板", exit_code=3)
        Path(tool.start_command).write_text(
            "#!/bin/sh\necho 'Traceback (most recent call last):'\n"
            "echo \"ModuleNotFoundError: No module named 'foo'\"\nexit 3\n",
            encoding="utf-8")
        self.start(tool)

        text = self.incident_text()
        self.assertIn("なぜ2  ツールが立ち上がる前に、起動ファイルが終了した (戻り値 3)",
                      text)
        self.assertIn("なぜ3  ツールの出力にエラーが出ている: "
                      "ModuleNotFoundError: No module named 'foo'", text)
        # ツールの出力の写し。ツールのログを探しに行かなくても読める
        self.assertIn("| Traceback (most recent call last):", text)
        # 画面の［詳細］からも場所が分かる
        self.assertIn(self.manager.status.incident, self.manager.status.detail)

        failed = self.events("起動失敗")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["原因"], "start.bat が戻り値 3 で終了した")
        self.assertEqual(failed[0]["障害記録"], self.manager.status.incident)
        self.assertEqual(failed[0]["操作ID"], self.events("起動開始")[0]["操作ID"])

    def test_ポートを別のアプリが使っていたらそう書く(self) -> None:
        other = self.register("fake.squatter", "看板")
        self.start(other)
        victim = self.register("fake.victim", "日報", port=other.port)
        self.start(victim)

        self.assertEqual(self.manager.status.state, State.ERROR)
        text = self.incident_text()
        self.assertIn(f"ポート {other.port} では、別のアプリ (アプリID fake.squatter)",
                      text)
        self.assertIn("別のアプリ (fake.squatter)", self.manager.status.detail)

    def test_待ち受けていなければそう書く(self) -> None:
        """BAT は動いたまま、ツールが立ち上がらない (時間切れ)。"""
        tool = self.register("fake.hang", "日報")
        Path(tool.start_command).write_text("#!/bin/sh\nsleep 30\n",
                                            encoding="utf-8")
        spawned = []
        real_spawn = self.manager._spawn

        def spawn(*args, **kwargs):
            proc = real_spawn(*args, **kwargs)
            spawned.append(proc)
            return proc

        def reap() -> None:
            # 時間切れのあとも BAT は動いたまま (ランチャーは手放す)。
            # 試験の後始末として止める
            for proc in spawned:
                proc.kill()
                proc.wait(timeout=5)
        self.addCleanup(reap)

        ui = app_manager.app_config.load()["ui"]
        with mock.patch.dict(ui, {"start_timeout_seconds": 1}), \
                mock.patch.object(self.manager, "_spawn", spawn):
            self.start(tool)

        text = self.incident_text()
        self.assertIn(f"ポート {tool.port} で待ち受けていない", text)
        self.assertIn("起動ファイルの状態 : 動いたまま", text)
        self.assertEqual(self.events("起動失敗")[0]["原因"],
                         f"ポート {tool.port} で待ち受けていない")

    def test_思わぬ停止は落ちたのか固まったのかを書く(self) -> None:
        from launcher import app_config

        tool = self.register("fake.lost", "日報")
        self.start(tool)
        running = self.manager.running[tool.app_id]
        process_manager._stop_by_api(running, force=True, timeout=10)
        for _ in range(int(app_config.ui_setting("health_failures_before_dead"))):
            self.manager.poll_health()

        text = self.incident_text()
        self.assertIn(f"ツールのプロセス (PID {running.pid}) が無くなっている", text)
        lost = self.events("思わぬ停止")
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["結果"], "失敗")

    def test_止められなかったら記録する(self) -> None:
        tool = self.register("fake.stuck", "日報")
        self.start(tool)
        result = process_manager.StopResult(app_id=tool.app_id, stopped=False,
                                            method="pid",
                                            message="終了を確認できませんでした")
        with mock.patch.object(process_manager, "stop", return_value=result):
            self.assertFalse(self.manager._stop_blocking(tool.app_id))

        text = self.incident_text()
        self.assertIn("なぜ1  終了を確認できなかった (終了を確認できませんでした)", text)
        self.assertIn("ツールはまだ起動確認に応答している", text)
        kinds = [r["種類"] for r in self.events()]
        self.assertIn("停止要求", kinds)
        self.assertIn("停止失敗", kinds)

    def test_止めたら止め方を残す(self) -> None:
        tool = self.register("fake.bye", "日報")
        self.start(tool)
        self.manager._stop_blocking(tool.app_id)
        done = self.events("停止完了")
        self.assertEqual(len(done), 1)
        self.assertIn("止め方", done[0]["詳細"])

    def test_想定外の例外も記録して画面に場所を出す(self) -> None:
        tool = self.register("fake.boom", "日報")
        with mock.patch.object(self.manager, "_select_blocking",
                               side_effect=RuntimeError("試しの失敗")):
            self.manager._run_select(tool)

        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        text = self.incident_text()
        self.assertIn("RuntimeError: 試しの失敗", text)
        self.assertIn("■ 想定外の例外", text)
        self.assertEqual(self.events("想定外の例外")[0]["障害記録"], status.incident)

    def test_設定が無ければ理由だけ残す(self) -> None:
        """原因がはっきりしているものは、障害記録までは要らない。"""
        tool_registry.save(tool_registry.Tool(
            app_id="fake.blank", display_name="カレンダー", port=free_port()))
        self.start(tool_registry.get("fake.blank"))
        row = self.events("起動できない")[0]
        self.assertIn("設定されていない", row["原因"])
        self.assertEqual(self.manager.status.incident, "")
        self.assertEqual(trace.recent_incidents(), [])


@unittest.skipIf(os.name == "nt", "偽の exe (名前だけ .exe のスクリプト) は Windows 以外で動かす")
class AppWindowTests(ManagerTestCase):
    """自分の窓を出すアプリ (Tauri などの exe)。"""

    def setUp(self) -> None:
        super().setUp()
        # 待ち時間を縮める (確認を出して終わらないアプリを待つところ)
        for name, value in (("APP_CLOSE_WAIT_SEC", 1.0),
                            ("APP_CLOSE_WAIT_FORCE_SEC", 0.5),
                            ("TERMINATE_WAIT_SEC", 1.0)):
            patcher = mock.patch.object(process_manager, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.spawned = []
        real = self.manager._spawn

        def spawn(*args, **kwargs):
            proc = real(*args, **kwargs)
            self.spawned.append(proc)
            return proc
        patcher = mock.patch.object(self.manager, "_spawn", spawn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register_app(self, app_id: str, name: str, *, port: int = 0,
                     ui_mode: str = "", start_args: str = "", **options):
        exe = make_app_dir(self.work_root, app_id=app_id, port=port or None,
                           **options)
        tool_registry.save(tool_registry.Tool(
            app_id=app_id, display_name=name, port=port, start_command=str(exe),
            ui_mode=ui_mode, start_args=start_args))
        return tool_registry.get(app_id)

    def wait_exit(self, pid: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while process_manager.is_pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        # 子なら引き取る (引き取るまでは終わっていても残って見える)
        for proc in self.spawned:
            if proc.pid == pid:
                proc.wait(timeout=timeout)

    def test_exeを起動してもブラウザーは開かない(self) -> None:
        tool = self.register_app("fake.tauri", "日報App")
        self.assertTrue(tool.watches_window)
        self.start(tool)

        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertEqual(status.ui_mode, "app")
        self.assertEqual(self.opened, [], "アプリなのにブラウザーを開いています")
        running = self.manager.running[tool.app_id]
        self.assertTrue(running.is_app)
        # **exe をそのまま実行した** (cmd.exe を挟まない) ので、起こした
        # プロセスがアプリそのもの
        self.assertEqual(running.pid, self.spawned[0].pid)
        self.assertEqual(runtime_state.read_all()[tool.app_id].ui_mode, "app")
        self.assertIn("起動完了", [r["種類"] for r in trace.read_events()])

    def test_動いているアプリを押しても2つ目を起こさず前に出す(self) -> None:
        tool = self.register_app("fake.front", "看板App")
        self.start(tool)
        with mock.patch.object(desktop, "bring_to_front",
                               return_value=True) as front:
            self.manager._select_blocking(tool)
        self.assertEqual(len(self.spawned), 1)
        front.assert_called_once_with(self.manager.running[tool.app_id].window_pid)

    def test_止めると窓を閉じてもらう(self) -> None:
        tool = self.register_app("fake.close", "日報App")
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid

        self.assertTrue(self.manager._stop_blocking(tool.app_id))
        self.wait_exit(pid)
        self.assertFalse(process_manager.is_pid_alive(pid))
        self.assertEqual(self.manager.status.message, "日報Appを終了しました")
        self.assertEqual(self.manager.status.detail, "")
        done = [r for r in trace.read_events() if r["種類"] == "停止完了"]
        self.assertIn("close-window", done[0]["詳細"])

    def test_保存の確認を出していれば落とさずに強制終了を選ばせる(self) -> None:
        tool = self.register_app("fake.ask", "日報App", ask_on_close=True)
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid

        self.assertFalse(self.manager._stop_blocking(tool.app_id))
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertIn("終了の確認", status.detail)
        self.assertIn("強制終了", status.detail)
        self.assertTrue(process_manager.is_pid_alive(pid), "確認中のアプリを落としました")
        self.assertTrue(self.manager.stop_refused(tool.app_id))

        self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        self.wait_exit(pid)
        self.assertFalse(process_manager.is_pid_alive(pid))
        self.assertFalse(self.manager.stop_refused(tool.app_id))

    def test_利用者が窓を閉じたら閉じたと出す(self) -> None:
        tool = self.register_app("fake.user", "日報App")
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid
        os.kill(pid, 15)                          # ×ボタンの代わり
        self.wait_exit(pid)

        self.manager.poll_health()                # 1回で気づく
        status = self.manager.status
        self.assertEqual(status.state, State.IDLE)
        self.assertEqual(status.message, "日報Appを閉じました")
        self.assertEqual(status.incident, "")
        self.assertNotIn(tool.app_id, self.manager.running)
        self.assertIn("アプリを閉じた", [r["種類"] for r in trace.read_events()])

    def test_落ちたら戻り値つきで異常終了と出す(self) -> None:
        tool = self.register_app("fake.crash", "日報App", crash_after=1.5)
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid
        self.wait_exit(pid)

        self.manager.poll_health()
        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("異常終了", status.message)
        text = Path(status.incident).read_text(encoding="utf-8-sig")
        self.assertIn("戻り値 101 (Rust の panic", text)
        self.assertIn("panicked at", text)

    def test_窓を出す前に落ちたら障害記録を残す(self) -> None:
        tool = self.register_app("fake.early", "日報App", exit_code=3)
        self.start(tool)

        status = self.manager.status
        self.assertEqual(status.state, State.ERROR)
        self.assertIn("FakeApp.exe が窓を出す前に終了しました (戻り値 3)", status.detail)
        text = Path(status.incident).read_text(encoding="utf-8-sig")
        self.assertIn("窓が出る前に、FakeApp.exe が終了した", text)
        self.assertIn("アプリの出力にエラーが出ている: Error: WebView2", text)
        failed = [r for r in trace.read_events() if r["種類"] == "起動失敗"]
        self.assertEqual(len(failed), 1)

    def test_ランチャーの外で動いているアプリは起こさず前に出す(self) -> None:
        tool = self.register_app("fake.outside", "日報App")
        outside = subprocess.Popen([tool.start_command])
        self.addCleanup(outside.wait, 5)
        self.addCleanup(outside.terminate)
        deadline = time.monotonic() + 5
        while not desktop.find_by_exe(tool.start_command) \
                and time.monotonic() < deadline:
            time.sleep(0.05)

        with mock.patch.object(desktop, "bring_to_front", return_value=True):
            self.manager._select_blocking(tool)
        self.assertEqual(self.spawned, [], "外で動いているのに2つ目を起こしました")
        self.assertEqual(self.manager.running[tool.app_id].pid, outside.pid)

    def test_ランチャーを起動し直しても引き継ぐ(self) -> None:
        tool = self.register_app("fake.adopt", "日報App")
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid

        adopted = ToolManager().adopt_running()
        self.assertEqual([r.pid for r in adopted], [pid])
        # 記録を失っても、exe の道でプロセスを探して見つける
        runtime_state.clear()
        adopted = ToolManager().adopt_running()
        self.assertEqual([r.pid for r in adopted], [pid])
        self.assertTrue(adopted[0].is_app)

    def test_Webサーバーを持つアプリは応答を待つがブラウザーは開かない(self) -> None:
        port = free_port()
        tool = self.register_app("fake.sidecar", "Tauri+Python", port=port,
                                 ui_mode="app", ready_after=0.6)
        self.assertFalse(tool.watches_window)
        began = time.monotonic()
        self.start(tool)
        self.assertGreaterEqual(time.monotonic() - began, 0.6)
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(self.opened, [])
        running = self.manager.running[tool.app_id]
        self.assertEqual(running.launch_pid, self.spawned[0].pid)
        self.assertTrue(running.is_app)

        self.assertTrue(self.manager._stop_blocking(tool.app_id))
        self.wait_exit(running.launch_pid)
        self.assertFalse(health.is_port_accepting(port))

    def test_サーバーのexeはブラウザーで開く(self) -> None:
        """Python から作った Web サーバーの exe (ポートあり)。"""
        port = free_port()
        tool = self.register_app("fake.server-exe", "日報", port=port,
                                 start_args="--no-browser")
        self.assertEqual(tool.resolved_ui_mode, "browser")
        self.start(tool)
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(self.opened, [tool.home_url])

    def test_ランチャーを閉じてもexeのツールは落とさない(self) -> None:
        tool = self.register_app("fake.keep", "日報App")
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid
        self.assertTrue(self.manager.shutdown(stop_tools=False))
        time.sleep(0.3)
        self.assertTrue(process_manager.is_pid_alive(pid),
                        "動かしたままにしたはずのアプリが消えました")
        # 後始末 (手放したプロセスも引き取る)
        process_manager.stop(runtime_state.read_all()[tool.app_id], force=True)
        for proc in list(app_manager._DETACHED):
            app_manager._DETACHED.remove(proc)
            proc.wait(timeout=5)

    def test_Webサーバーを持たないアプリにbatは使えない(self) -> None:
        tool = self.register("fake.batapp", "日報")
        tool_registry.save(replace(tool, port=0, ui_mode="app"))
        self.start(tool_registry.get("fake.batapp"))
        self.assertEqual(self.manager.status.state, State.ERROR)
        self.assertIn("exe を直接指定", self.manager.status.detail)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main()
