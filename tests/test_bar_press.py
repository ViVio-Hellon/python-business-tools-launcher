"""起動の最中にツールのボタンをもう一度押したとき (バー)

起動の最中に押しても2つ目は起こさない (`ToolManager`)。そのうえで、
**押したのに何も起きないように見せない。** 進み具合の窓を［隠す］で
隠していても出し直して前に出す。ツールの窓がもう出ていれば、そちらを
前に出すのは `ToolManager` (`test_app_manager`)。

画面 (tkinter) と表示先が要る。無い環境では飛ばす。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase, release_tk  # noqa: E402

from launcher.startup_progress import ProgressTracker  # noqa: E402


def _can_show_windows() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001 - tkinter が無い・画面が無い
        return False


HAVE_TK = _can_show_windows()


def _starting(app_id: str, name: str):
    from app_manager import PHASE_WAIT, State, Status

    return Status(state=State.STARTING, app_id=app_id, display_name=name,
                  message=f"{name}を起動しています...", phase=PHASE_WAIT,
                  elapsed=3, timeout=90, starting_ids=(app_id,))


class TrackerTests(unittest.TestCase):

    def test_見ているツールを答える(self) -> None:
        tracker = ProgressTracker()
        self.assertEqual(tracker.watching, "")
        tracker.update(_starting("nlm.daily", "日報"))
        self.assertEqual(tracker.watching, "nlm.daily")
        from app_manager import State, Status
        tracker.update(Status(state=State.RUNNING, app_id="nlm.daily"))
        self.assertEqual(tracker.watching, "")


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class PressWhileStartingTests(LocalAreaTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        from app_manager import ToolManager
        from launcher.ui import bar as bar_module

        self.manager = ToolManager()
        self.select = mock.patch.object(self.manager, "select").start()
        self.addCleanup(mock.patch.stopall)
        self.bar = bar_module.LauncherBar(self.manager)
        self.bar.root.update()
        self.addCleanup(self._destroy)

    def _destroy(self) -> None:
        try:
            self.bar.root.destroy()
        except Exception:                     # noqa: BLE001
            pass

    def begin_start(self, app_id: str = "nlm.daily", name: str = "日報") -> None:
        """起動の最中にする (進み具合の窓は［隠す］で隠した)。"""
        self.manager._starting.add(app_id)
        self.bar.progress.update(_starting(app_id, name))
        self.bar.progress.dismiss()
        self.bar.root.update()
        self.assertIsNone(self.bar.progress.top)

    def test_隠した進み具合の窓を出し直す(self) -> None:
        self.begin_start()
        self.bar.on_select("nlm.daily")
        self.bar.root.update()
        self.assertIsNotNone(self.bar.progress.top, "押しても何も出ません")
        self.assertIn("日報", self.bar.progress.title_label.cget("text"))
        self.select.assert_called_once_with("nlm.daily")

    def test_ほかのツールを押しても出し直さない(self) -> None:
        self.begin_start()
        self.bar.on_select("nlm.kanban")
        self.bar.root.update()
        self.assertIsNone(self.bar.progress.top)
        self.select.assert_called_once_with("nlm.kanban")

    def test_起動していなければ出さない(self) -> None:
        self.bar.on_select("nlm.daily")
        self.bar.root.update()
        self.assertIsNone(self.bar.progress.top)

    def test_出ている窓は前に出すだけで重ねない(self) -> None:
        self.manager._starting.add("nlm.daily")
        self.bar.progress.update(_starting("nlm.daily", "日報"))
        self.bar.progress._open()
        first = self.bar.progress.top
        self.bar.on_select("nlm.daily")
        self.bar.root.update()
        self.assertIs(self.bar.progress.top, first)


if __name__ == "__main__":
    unittest.main()
