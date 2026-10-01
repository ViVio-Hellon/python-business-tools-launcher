"""起動・切り替えの進み具合 (別の窓に出す中身)"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_app_manager import ManagerTestCase  # noqa: E402

import app_manager  # noqa: E402
from app_manager import State, Status  # noqa: E402
from launcher import startup_progress as sp  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _status(state, phase, name, **kw) -> Status:
    # 試験では表示名をそのままアプリIDにする (ツールの見分けに使う)
    return Status(state=state, phase=phase, display_name=name, app_id=name,
                  message=kw.pop("message", ""), **kw)


def _marks(view) -> list[tuple[str, str]]:
    return [(s.state, s.label) for s in view.steps]


class TrackerTests(unittest.TestCase):

    def setUp(self) -> None:
        self.clock = _Clock()
        self.tracker = sp.ProgressTracker(clock=self.clock)

    def test_起動だけなら3段(self) -> None:
        view = self.tracker.update(_status(State.STARTING, sp.SPAWN, "日報"))
        self.assertEqual(view.title, "日報を起動しています")
        self.assertEqual(_marks(view), [
            (sp.ACTIVE, "日報の起動ファイルを実行する"),
            (sp.PENDING, "日報の準備ができるのを待つ"),
            (sp.PENDING, "日報の画面を開く")])

    def test_待っているあいだは段階と上限を出す(self) -> None:
        self.tracker.update(_status(State.STARTING, sp.SPAWN, "日報", timeout=90))
        self.clock.now += 12.4
        view = self.tracker.update(_status(
            State.STARTING, sp.WAIT, "日報", stage="アプリを準備中", timeout=90))
        self.assertEqual([s.state for s in view.steps],
                         [sp.DONE, sp.ACTIVE, sp.PENDING])
        self.assertEqual(view.note, "アプリを準備中")
        self.assertEqual(view.elapsed_text, "12秒 (最大90秒まで待ちます)")

    def test_止めるときは画面を閉じる段から(self) -> None:
        self.tracker.update(_status(State.STOPPING, sp.CLOSE_BROWSER, "日報"))
        view = self.tracker.update(_status(State.STOPPING, sp.STOP_TOOL, "日報"))
        self.assertEqual(view.title, "日報を終了しています")
        self.assertEqual(_marks(view), [
            (sp.DONE, "日報の画面を閉じる"),
            (sp.ACTIVE, "日報を終了する")])

    def test_止めるだけ(self) -> None:
        view = self.tracker.update(_status(State.STOPPING, sp.STOP_TOOL, "日報"))
        self.assertEqual(view.title, "日報を終了しています")
        self.assertEqual(len(view.steps), 1)

    def test_終われば消す_次の操作は数え直す(self) -> None:
        self.tracker.update(_status(State.STARTING, sp.SPAWN, "日報"))
        self.clock.now += 30
        self.assertIsNone(self.tracker.update(
            _status(State.RUNNING, "", "日報")))
        view = self.tracker.update(_status(State.STARTING, sp.SPAWN, "看板"))
        self.assertEqual(view.elapsed_text, "0秒")
        self.assertEqual(view.title, "看板を起動しています")

    def test_失敗しても消す(self) -> None:
        self.tracker.update(_status(State.STARTING, sp.WAIT, "日報"))
        self.assertIsNone(self.tracker.update(_status(State.ERROR, "", "日報")))

    def test_ほかのツールの知らせは混ぜない(self) -> None:
        """ツールは同時に動く。見ている起動が終わるまで、窓は行き来しない。"""
        self.tracker.update(_status(State.STARTING, sp.WAIT, "日報"))
        view = self.tracker.update(_status(State.STARTING, sp.SPAWN, "看板"))
        self.assertEqual(view.title, "日報を起動しています")
        # 看板が先に終わっても、日報の窓は閉じない
        self.assertIsNotNone(self.tracker.update(
            _status(State.RUNNING, "", "看板")))
        # 日報が終われば閉じる。次の知らせから看板を見る
        self.assertIsNone(self.tracker.update(_status(State.RUNNING, "", "日報")))
        view = self.tracker.update(_status(State.STARTING, sp.WAIT, "看板"))
        self.assertEqual(view.title, "看板を起動しています")

    def test_経過は通知が来なくても進む(self) -> None:
        """終了を待つあいだは通知が来ない。窓の時計で進める。"""
        self.tracker.update(_status(State.STOPPING, sp.STOP_TOOL, "日報"))
        self.clock.now += 7
        self.assertEqual(self.tracker.view().elapsed_text, "7秒")

    def test_段の名前はapp_managerと同じ(self) -> None:
        self.assertEqual(
            (sp.CLOSE_BROWSER, sp.STOP_TOOL, sp.SPAWN, sp.WAIT, sp.OPEN_BROWSER),
            (app_manager.PHASE_CLOSE_BROWSER, app_manager.PHASE_STOP_TOOL,
             app_manager.PHASE_SPAWN, app_manager.PHASE_WAIT,
             app_manager.PHASE_OPEN_BROWSER))


class RealStartStopTests(ManagerTestCase):
    """本物の起動と停止で、段が順に届くこと。"""

    def phases(self, statuses) -> list[str]:
        found: list[str] = []
        for status in statuses:
            if status.busy and (not found or found[-1] != status.phase):
                found.append(status.phase)
        return found

    def test_起動の段が順に届く(self) -> None:
        nippou = self.register("fake.nippou", "日報")
        kanban = self.register("fake.kanban", "看板", ready_after=1.0)
        self.start(nippou)
        self.statuses.clear()

        self.start(kanban)
        # ほかのツールが動いていても、止める段は無い (同時に使える)
        self.assertEqual(self.phases(self.statuses), [
            app_manager.PHASE_SPAWN, app_manager.PHASE_WAIT,
            app_manager.PHASE_OPEN_BROWSER])

        tracker = sp.ProgressTracker()
        views = [tracker.update(s) for s in self.statuses]
        last_busy = [v for v in views if v is not None][-1]
        self.assertEqual(last_busy.title, "看板を起動しています")
        self.assertEqual([s.state for s in last_busy.steps],
                         [sp.DONE, sp.DONE, sp.ACTIVE])
        self.assertIsNone(views[-1])            # 起動し終われば消す
        self.assertEqual(self.statuses[-1].state, State.RUNNING)

    def test_停止の段が順に届く(self) -> None:
        nippou = self.register("fake.nippou", "日報")
        self.start(nippou)
        self.statuses.clear()
        self.manager._stop_blocking(nippou.app_id)
        self.assertEqual(self.phases(self.statuses), [
            app_manager.PHASE_CLOSE_BROWSER, app_manager.PHASE_STOP_TOOL])

    def test_止めて閉じた画面の手がかりを手放す(self) -> None:
        """止め方の結果に「画面を閉じた」が引き継がれず、閉じた画面の
        手がかりが残り続けていた。"""
        from launcher import browser
        nippou = self.register("fake.nippou", "日報")
        self.start(nippou)
        old_pid = self.manager.current.browser_pid
        self.assertIn(old_pid, browser.managed_pids())

        self.manager._stop_blocking(nippou.app_id)
        self.assertNotIn(old_pid, browser.managed_pids())


if __name__ == "__main__":
    unittest.main()
