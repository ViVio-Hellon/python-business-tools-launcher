"""現場で使ってみての報告で直したこと (1.7.4)

* 画面を閉じてツールが終わっても、しばらく「動いています」と出たままだった。
  終わったのが確かなら (確認口が「動いていない」と答えた・ポートもプロセスも
  無い)、次の見回り 1 回で外す。終わったときに長い説明を出さない
* CoilCalculator を Start.vbs で登録しているのに exe で開いた。確認口は
  デスクトップ版でもブラウザー版でも「使える」と答えるので、先に exe が開いて
  いればその窓が前に出る。**何が動いていたのか**をバーに出す
* バーの位置が設定どおりに動かない (手で触れると、その場所に固定されていた)。
  「ランチャーを起動したとき」「ツールを使っているとき」を両方選べるようにし、
  手で動かした位置は覚えない
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_app_manager import ManagerTestCase  # noqa: E402
from test_tool_entries import _script, make_entry_tool  # noqa: E402

from app_manager import State, Status  # noqa: E402
from launcher import distribution, tool_entries, tool_registry  # noqa: E402
from launcher.ui import geometry  # noqa: E402


class EntryToolTests(ManagerTestCase):

    def setUp(self) -> None:
        super().setUp()
        self.root: Path | None = None

    def tearDown(self) -> None:
        if self.root is not None and (self.root / "pid.txt").exists():
            (self.root / "stop.txt").write_text("")
            deadline = time.monotonic() + 3
            while (self.root / "pid.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        super().tearDown()

    def test_すでに動いていたら何が動いているかを出す(self) -> None:
        start = make_entry_tool(self.work_root, "CoilCalculator")
        self.root = start.parent
        # デスクトップ版が先に開いている。起動ファイルは呼ばれてはいけない
        _script(start, "touch started_by_launcher.txt\nexit 0\n")
        _script(self.root / tool_entries.CHECK_FILE,
                'echo "CoilCalculator.exe (デスクトップ版) が動いています"\nexit 0\n')
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.coil-calculator", display_name="VC長さ", port=8741,
            start_command=str(start)))
        self.start(tool_registry.get("nlm.coil-calculator"))
        self.assertFalse((self.root / "started_by_launcher.txt").exists())
        status = self.manager.status
        self.assertEqual(status.state, State.RUNNING)
        self.assertEqual(status.message, "VC長さはすでに動いていました: "
                                         "CoilCalculator.exe (デスクトップ版) が動いています")

    def test_確認口が動いていないと答えたら次の見回りで外す(self) -> None:
        start = make_entry_tool(self.work_root, "coil", ready_after=0.2)
        self.root = start.parent
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.coil", display_name="VC長さ", start_command=str(start)))
        self.start(tool_registry.get("nlm.coil"))
        self.assertIn("nlm.coil", self.manager.running)
        # 利用者が画面を閉じ、ツールが自分で終わった
        (self.root / "stop.txt").write_text("")
        deadline = time.monotonic() + 3
        while (self.root / "pid.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.manager.poll_health()
        self.assertNotIn("nlm.coil", self.manager.running)
        status = self.manager.status
        self.assertEqual(status.message, "VC長さは終了しました")
        self.assertEqual(status.detail, "")


class PositionSettingTests(ManagerTestCase):

    def test_起動したときの位置も選べて配布先にも入る(self) -> None:
        self.assertEqual(tool_registry.idle_bar_position(), "center")
        tool_registry.set_idle_bar_position("top_right")
        tool_registry.set_active_bar_position("center")
        self.assertEqual((tool_registry.idle_bar_position(),
                          tool_registry.active_bar_position()), ("top_right", "center"))
        tool_registry.export_distribution()
        self.assertEqual(distribution.bar_position_idle(), "top_right")
        self.assertEqual(distribution.bar_position_active(), "center")
        with self.assertRaises(ValueError):
            tool_registry.set_idle_bar_position("まんなか")


def _have_tk() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001
        return False


@unittest.skipUnless(_have_tk(), "tkinter と画面が要る")
class BarPositionTests(ManagerTestCase):

    def setUp(self) -> None:
        from _isolation import release_tk

        self.addCleanup(release_tk, self)
        super().setUp()
        from unittest import mock

        from launcher.ui import bar as bar_module

        self.bar_module = bar_module
        mock.patch.object(self.manager, "select").start()
        self.addCleanup(mock.patch.stopall)

    def make_bar(self):
        bar = self.bar_module.LauncherBar(self.manager)
        self.addCleanup(self._destroy, bar)
        self.settle(bar)
        return bar

    @staticmethod
    def _destroy(bar) -> None:
        try:
            bar.root.destroy()
        except Exception:                     # noqa: BLE001
            pass

    @staticmethod
    def settle(bar) -> None:
        deadline = time.monotonic() + 0.6     # 滑らせて動かすぶん待つ
        while time.monotonic() < deadline:
            bar.root.update()
            time.sleep(0.02)

    @staticmethod
    def xy(bar) -> tuple[int, int]:
        return geometry.parse_geometry_xy(bar.root.geometry())

    def expected(self, bar, anchor: str) -> tuple[int, int]:
        return bar._placement(anchor=anchor)[2:]

    def running(self, app_id: str = "nlm.coil") -> Status:
        return Status(state=State.RUNNING, app_id=app_id, display_name="VC長さ",
                      running_ids=(app_id,))

    def test_起動したときは設定の位置に出る(self) -> None:
        tool_registry.set_idle_bar_position("top_right")
        bar = self.make_bar()
        self.assertEqual(bar._anchor, "top_right")
        self.assertEqual(self.xy(bar), self.expected(bar, "top_right"))

    def test_以前の版が覚えた手で置いた位置は使わない(self) -> None:
        tool_registry.set_pc_setting(geometry.POSITION_KEY, "5,5")
        bar = self.make_bar()
        self.assertEqual(tool_registry.get_pc_setting(geometry.POSITION_KEY), "")
        self.assertEqual(self.xy(bar), self.expected(bar, "center"))

    def test_手で動かしても固定せずツールを押せば設定の位置へ寄る(self) -> None:
        tool_registry.set_active_bar_position("top_center")
        bar = self.make_bar()
        bar.on_select("nlm.coil")
        bar._render(self.running())
        self.settle(bar)
        self.assertEqual(self.xy(bar), self.expected(bar, "top_center"))

        # 利用者がドラッグした。同じ状態のあいだはそのまま (見回りで戻さない)
        bar.root.geometry("+300+300")
        self.settle(bar)
        bar._render(self.running())
        self.settle(bar)
        self.assertEqual(self.xy(bar), (300, 300))
        self.assertEqual(tool_registry.get_pc_setting(geometry.POSITION_KEY), "")

        # ツールのボタンを押すと、設定の位置へ寄せ直す
        bar.on_select("nlm.coil")
        bar._render(self.running())
        self.settle(bar)
        self.assertEqual(self.xy(bar), self.expected(bar, "top_center"))

        # 使っているツールが無くなれば、起動したときの位置へ
        bar._render(Status(state=State.IDLE))
        self.settle(bar)
        self.assertEqual(self.xy(bar), self.expected(bar, "center"))


if __name__ == "__main__":
    unittest.main()
