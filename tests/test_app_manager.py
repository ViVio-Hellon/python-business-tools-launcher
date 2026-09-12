"""起動・切替・停止の通し試験 (要件定義書 §7 / §8 / §9 / §20)

偽の業務ツールを本物と同じ形 (`start.bat` + `/api/health` +
`/api/shutdown`) で用意し、ランチャーが決められた順序どおりに
動くことを確かめる。**ここが要件定義書 §20「完成条件」の中身**。

画面 (tkinter) は使わない。`ToolManager` は画面を読み込まないので、
画面の無い環境でも通せる。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _fake_tool_support import free_port, make_tool_dir  # noqa: E402
from _isolation import LocalAreaTestCase  # noqa: E402

import app_manager  # noqa: E402
import process_manager  # noqa: E402
from app_manager import State, ToolManager  # noqa: E402
from launcher import browser, health, runtime_state, tool_registry  # noqa: E402

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
        running = self.manager.current or runtime_state.read()
        if running is not None:
            process_manager.stop(running, force=True, timeout=5)
        # `start.bat` の受け皿も引き取る。残すと、試験のたびに
        # 引き取られないプロセスが増えていく
        self.manager._reap_process()
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
        self.manager._select_blocking(tool, self.manager._generation)
        running = self.manager.current
        if running is not None and running.browser_pid:
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

        running = runtime_state.read()
        self.assertIsNotNone(running)
        self.assertEqual(running.app_id, "fake.record")
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
        first = runtime_state.read()

        self.start(tool)
        second = runtime_state.read()

        self.assertEqual(first.pid, second.pid, "二重に起動しています")
        # 画面が出ているのにもう1枚開かない。同じツールの窓が2つ並ぶ
        # ほうが分かりにくい (要件定義書 §8.3.1)
        self.assertEqual(self.opened, [tool.home_url],
                         "画面を二重に開いています")


class SwitchTests(ManagerTestCase):
    """要件定義書 §8 ツール切り替え。"""

    def test_AからBへ切り替える(self) -> None:
        a = self.register("fake.a", "日報")
        b = self.register("fake.b", "看板")

        self.start(a)
        a_running = runtime_state.read()
        self.assertTrue(process_manager.is_running(a_running))

        self.start(b)

        # Aは止まっていること (要件定義書 §8.1「日報を停止」)
        self.assertFalse(health.is_tool(health.probe(a.health_url), a.app_id),
                         "切り替え後も前のツールが動いています")
        # Bが動いていること
        self.assertEqual(self.manager.status.state, State.RUNNING)
        self.assertEqual(runtime_state.read().app_id, "fake.b")
        self.assertIn("看板", self.manager.status.message)

    def test_切り替え中の表示が順に出る(self) -> None:
        a = self.register("fake.s1", "日報")
        b = self.register("fake.s2", "看板")
        self.start(a)
        self.statuses.clear()
        self.start(b)

        states = [s.state for s in self.statuses]
        self.assertIn(State.STOPPING, states)
        self.assertIn(State.STARTING, states)
        self.assertLess(states.index(State.STOPPING), states.index(State.STARTING),
                        "終了より先に起動しています (要件定義書 §8.1)")
        stopping = next(s for s in self.statuses if s.state is State.STOPPING)
        self.assertIn("日報", stopping.message)

    def test_連続して切り替えられる(self) -> None:
        """要件定義書 §20「A→B→C→Dのように連続して切り替えられる」。"""
        tools = [self.register(f"fake.c{i}", name) for i, name in
                 enumerate(("日報", "カレンダー", "看板", "総合"))]
        for tool in tools:
            self.start(tool)
            self.assertEqual(self.manager.status.state, State.RUNNING,
                             f"{tool.display_name} で止まりました")
            self.assertEqual(runtime_state.read().app_id, tool.app_id)

        # 最後の1つだけが動いていること
        alive = [t.app_id for t in tools
                 if health.is_tool(health.probe(t.health_url), t.app_id)]
        self.assertEqual(alive, [tools[-1].app_id])


class StopTests(ManagerTestCase):
    """要件定義書 §10 プロセス管理 / 安全な終了。"""

    def test_正常終了できる(self) -> None:
        tool = self.register("fake.stop", "日報")
        self.start(tool)
        self.manager._stop_current(self.manager._generation)

        self.assertEqual(self.manager.status.state, State.IDLE)
        self.assertIsNone(runtime_state.read())
        self.assertIsNone(self.manager.current)

    def test_実行中の処理があれば止めずに知らせる(self) -> None:
        """基盤仕様書 2.8「実行中の終了確認」。"""
        tool = self.register("fake.busy", "看板", busy=True)
        self.start(tool)
        stopped = self.manager._stop_current(self.manager._generation)

        self.assertFalse(stopped)
        self.assertIn("実行中の処理", self.manager.status.detail)
        # まだ動いていること。黙って落としていない
        self.assertTrue(process_manager.is_running(runtime_state.read()))

    def test_強制指定なら実行中でも止める(self) -> None:
        tool = self.register("fake.busy2", "看板", busy=True)
        self.start(tool)
        stopped = self.manager._stop_current(self.manager._generation, force=True)

        self.assertTrue(stopped)
        self.assertIsNone(runtime_state.read())

    def test_stopbatが無くても停止APIで止まる(self) -> None:
        tool = self.register("fake.nobat", "日報", with_stop_bat=False)
        self.start(tool)
        running = runtime_state.read()
        self.assertIsNone(process_manager.resolve_stop_bat(running))

        result = process_manager.stop(running, timeout=10)
        self.assertTrue(result.stopped, result.message)
        self.assertEqual(result.method, "shutdown-api")


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

    def test_切り替えで前の画面が閉じる(self) -> None:
        """§8.3.2 切り替え時に現在のツールの画面を閉じる。"""
        a = self.register("fake.w1", "日報")
        b = self.register("fake.w2", "看板")

        self.start(a)
        a_running = runtime_state.read()
        a_browser_pid = a_running.browser_pid
        self.assertTrue(process_manager.is_browser_open(a_running))

        self.start(b)

        self.assertFalse(process_manager._is_alive(a_browser_pid),
                         "切り替え後も日報の画面が残っています")
        b_running = runtime_state.read()
        self.assertEqual(b_running.app_id, "fake.w2")
        self.assertTrue(process_manager.is_browser_open(b_running))
        self.assertEqual(self.opened, [a.home_url, b.home_url])

    def test_画面を閉じてからバックエンドを止める(self) -> None:
        """§8.3.2 の処理順序。逆だと利用者に接続エラーが見える。"""
        a = self.register("fake.order1", "日報")
        b = self.register("fake.order2", "看板")
        self.start(a)
        running = runtime_state.read()

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
            self.start(b)

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
        running = runtime_state.read()

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
        running = runtime_state.read()

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

    def test_同じボタンで画面だけ開き直せる(self) -> None:
        tool = self.register("fake.reopen", "日報")
        self.start(tool)
        first = runtime_state.read()

        process_manager._terminate(first.browser_pid, force=True)
        process_manager._wait_pid_gone(first.browser_pid, 5)
        self.manager.poll_health()

        self.start(tool)
        second = runtime_state.read()

        # バックエンドは起動し直していない
        self.assertEqual(first.pid, second.pid, "バックエンドを起動し直しました")
        # 画面は開き直されている
        self.assertNotEqual(first.browser_pid, second.browser_pid)
        self.assertTrue(process_manager.is_browser_open(second))
        self.assertEqual(self.opened, [tool.home_url, tool.home_url])

    def test_閉じられない形なら記録に残る(self) -> None:
        """Chromium系が無い端末。**黙って閉じたことにしない。**"""
        tool = self.register("fake.default", "日報")
        with mock.patch.object(browser, "find_browser", return_value=("", "")), \
             mock.patch.object(browser.webbrowser, "open") as opened:
            self.start(tool)

        opened.assert_called_once_with(tool.home_url)
        running = runtime_state.read()
        self.assertFalse(running.browser_managed)
        self.assertEqual(running.browser_pid, 0)
        # 手で閉じてもらう必要があることを画面に出す
        self.assertIn("手で閉じて", self.manager.status.detail)


class ShutdownTests(ManagerTestCase):
    """ランチャーを閉じるとき (要件定義書 §11)。"""

    def test_ツールを残したまま閉じられる(self) -> None:
        """ブラウザーを閉じることとバックエンドを止めることは別。"""
        tool = self.register("fake.keep", "日報")
        self.start(tool)
        running = runtime_state.read()

        self.assertTrue(self.manager.shutdown(stop_tools=False))
        # ツールはまだ動いている
        self.assertTrue(process_manager.is_running(running))
        # 記録も残る。次にランチャーを開いたとき引き継げる
        self.assertIsNotNone(runtime_state.read())

    def test_ツールごと閉じられる(self) -> None:
        tool = self.register("fake.both", "日報")
        self.start(tool)

        self.assertTrue(self.manager.shutdown(stop_tools=True))
        self.assertIsNone(runtime_state.read())

    def test_止まらなければ偽を返す(self) -> None:
        """**黙って閉じない。** 止めたつもりで残るのがいちばん困る。"""
        tool = self.register("fake.stuck", "看板", busy=True)
        self.start(tool)
        running = runtime_state.read()

        self.assertFalse(self.manager.shutdown(stop_tools=True))
        self.assertTrue(process_manager.is_running(running),
                        "止まっていないのに記録だけ消えています")
        self.assertIn("実行中の処理", self.manager.status.detail)

        # 中断してよいと言われたら止める
        self.assertTrue(self.manager.shutdown(stop_tools=True, force=True))
        self.assertIsNone(runtime_state.read())


class MonitorTests(ManagerTestCase):
    """基盤仕様書 2.9 ブラウザーとバックエンドの状態監視。"""

    def test_ツールが落ちたら気づく(self) -> None:
        from launcher import app_config

        tool = self.register("fake.die", "日報")
        self.start(tool)
        self.assertTrue(self.manager.poll_health())

        # ツールだけを落とす (ランチャーは知らない)
        process_manager.stop(runtime_state.read(), force=True, timeout=10)

        # **1回では断じない。** スリープ復帰や重い処理中に、動いている
        # ツールを落ちた扱いにしないため (tests/test_resilience.py)
        limit = int(app_config.ui_setting("health_failures_before_dead"))
        for _ in range(limit - 1):
            self.assertTrue(self.manager.poll_health())
            self.assertEqual(self.manager.status.state, State.RUNNING)

        self.assertFalse(self.manager.poll_health())
        self.assertEqual(self.manager.status.state, State.ERROR)
        self.assertIn("終了しました", self.manager.status.message)

    def test_ランチャー外で動いているツールを引き継ぐ(self) -> None:
        """要件定義書 §9。記録が無くても二重起動させない。"""
        tool = self.register("fake.adopt", "日報")
        self.start(tool)
        # ランチャーを再起動したことにする
        fresh = ToolManager()
        runtime_state.clear()

        adopted = fresh.adopt_running()
        self.assertIsNotNone(adopted, "動いているツールを見つけられていません")
        self.assertEqual(adopted.app_id, "fake.adopt")
        self.assertEqual(fresh.status.state, State.RUNNING)


if __name__ == "__main__":
    unittest.main()
