"""ツールの開発側からの報告で直したこと (1.7.1)

* all-tools・python-web-tools・CoilCalculator・coil-packing-tools とも、exe 版と
  ブラウザー版が同じ config/app.json を読み、**exe 版はそのポートで待ち受けない**。
  exe を選んでもポートを入れない。入っていれば保存時に知らせる
* 起動確認に答えないアプリの窓のツールを止めると、窓を閉じずに **PID で
  止めにいっていた** (python-web-tools)
* 終了の入口 (stop.bat・launcher_stop.bat) の**戻り値を見ずに 30秒待って**
  いた。1 (処理中) が返ったら、すぐ「強制終了しますか」を出し、選ばれたら
  `--force` で呼び直す (coil-packing-tools)
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_tool_support import free_port, make_app_dir  # noqa: E402
from test_app_manager import ManagerTestCase  # noqa: E402
from test_tool_entries import _script, make_entry_tool  # noqa: E402

import process_manager  # noqa: E402
from app_manager import State, Status  # noqa: E402
from launcher import desktop, tool_entries, tool_registry  # noqa: E402


class AppStopTests(ManagerTestCase):

    def test_起動確認に答えないアプリの窓は窓を閉じて止める(self) -> None:
        """設定のポートがブラウザー版のもので、exe は待ち受けない。"""
        exe = make_app_dir(self.work_root, app_id="nlm.packaging-tool", no_health=True,
                           port=free_port())
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.packaging-tool", display_name="梱包資材総合ツール",
            port=free_port(), start_command=str(exe), ui_mode="app"))
        tool = tool_registry.get("nlm.packaging-tool")
        # 窓の無い環境では、起動の上限まで待ってから「動いている」とみなす。短くする
        ui = tool_registry.app_config.load()["ui"]
        with mock.patch.dict(ui, {"start_timeout_seconds": 2}):
            self.start(tool)
        self.assertFalse(self.manager.running[tool.app_id].confirmed)
        with mock.patch.object(process_manager, "_stop_by_pid",
                               side_effect=AssertionError("PID で止めにいきました")), \
                mock.patch.object(process_manager, "_handle_unresponsive",
                                  side_effect=AssertionError("応答しないものとして片付けました")):
            self.assertTrue(self.manager._stop_blocking(tool.app_id))
        self.assertEqual(desktop.processes_in_folder(str(exe.parent)), [])


class ForceAppStopTests(ManagerTestCase):

    def test_窓に閉じるよう頼んだあとの強制終了はstopbatでなくPIDで止める(self) -> None:
        """デスクトップ版の stop.bat は窓を前に出すために exe を起動する。窓が
        閉じかけのときに呼ぶと、新しく起動してしまう (coil-packing-tools・all-tools)。"""
        exe = make_app_dir(self.work_root, app_id="nlm.coil-packing", ask_on_close=True)
        marker = exe.parent / "stop_bat_called.txt"
        stop_bat = exe.parent / "stop.bat"
        stop_bat.write_text(f"#!/bin/sh\necho \"$@\" >> '{marker}'\nexit 0\n",
                            encoding="utf-8")
        os.chmod(stop_bat, 0o755)
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.coil-packing", display_name="コイル梱包ツール",
            start_command=str(exe)))
        tool = tool_registry.get("nlm.coil-packing")
        self.assertTrue(tool.is_app)
        self.start(tool)
        pid = self.manager.running[tool.app_id].pid
        # 「保存しますか」を出して止まらない → 断り
        self.assertFalse(self.manager._stop_blocking(tool.app_id))
        # 強制終了: 窓に頼み直したあと、stop.bat --force ではなく PID で止める
        with mock.patch.object(process_manager, "_stop_by_pid",
                               wraps=process_manager._stop_by_pid) as by_pid:
            self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        self.assertTrue(by_pid.called)
        self.assertFalse(marker.exists(), "窓が閉じかけなのに stop.bat を呼びました")
        deadline = time.monotonic() + 5
        while process_manager.is_pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(process_manager.is_pid_alive(pid))


class StopExitCodeTests(ManagerTestCase):

    def test_stopbatが断ったら待たずに理由を出しforceで呼び直す(self) -> None:
        tool = self.register("fake.busy", "看板", busy=True)
        self.start(tool)
        calls = []
        real_run = process_manager.subprocess.run

        def run(command, *args, **kwargs):
            calls.append(list(command))
            return real_run(command, *args, **kwargs)

        with mock.patch.object(process_manager.subprocess, "run", side_effect=run):
            began = time.monotonic()
            self.assertFalse(self.manager._stop_blocking(tool.app_id))
            self.assertLess(time.monotonic() - began, 8, "断られたのに待っています")
            status = self.manager.status
            self.assertTrue(status.refused)
            self.assertIn("実行中の処理があります: 取り込み", status.detail)
            self.assertTrue(self.manager._stop_blocking(tool.app_id, force=True))
        stop_calls = [c for c in calls if c and c[0].endswith("stop.bat")]
        self.assertEqual(len(stop_calls), 2)
        self.assertNotIn("--force", stop_calls[0])
        self.assertIn("--force", stop_calls[1])


class EntryForceTests(ManagerTestCase):

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

    def test_終了口が処理中と答えたらforceで呼び直す(self) -> None:
        start = make_entry_tool(self.work_root, "coil", ready_after=0.2)
        self.root = start.parent
        # 1 = 処理中。--force のときだけ止める (coil-packing-tools の作り)
        _script(self.root / tool_entries.STOP_FILE,
                'echo "$@" >> args.txt\n'
                'if [ "$1" = "--force" ]; then touch stop.txt; exit 0; fi\n'
                'echo "取り込みの途中です"\nexit 1\n')
        tool_registry.save(tool_registry.Tool(
            app_id="nlm.coil", display_name="コイル梱包", start_command=str(start)))
        tool = tool_registry.get("nlm.coil")
        self.start(tool)
        self.assertFalse(self.manager._stop_blocking("nlm.coil"))
        self.assertIn("取り込みの途中です", self.manager.status.detail)
        self.assertTrue(self.manager._stop_blocking("nlm.coil", force=True))
        self.assertEqual((self.root / "args.txt").read_text().split(), ["--force"])


def _have_tk() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001
        return False


HAVE_TK = _have_tk()


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class BarRefusedTests(ManagerTestCase):
    """断られたら、もう一度［ツール停止］を押させずに、すぐ聞く。"""

    def setUp(self) -> None:
        from _isolation import release_tk

        self.addCleanup(release_tk, self)
        super().setUp()
        from launcher.ui import bar as bar_module

        self.bar_module = bar_module
        self.ask = mock.patch.object(bar_module.messagebox, "askyesno",
                                     return_value=True).start()
        self.stop = mock.patch.object(self.manager, "stop").start()
        self.addCleanup(mock.patch.stopall)
        self.bar = bar_module.LauncherBar(self.manager)
        self.addCleanup(self._destroy)

    def _destroy(self) -> None:
        try:
            self.bar.root.destroy()
        except Exception:                     # noqa: BLE001
            pass

    def refused(self, app_id: str = "nlm.coil") -> Status:
        return Status(state=State.RUNNING, app_id=app_id, display_name="コイル梱包",
                      detail="コイル梱包が終了を断りました: 取り込みの途中です\n"
                             "終了するときは［ツール停止］から「強制終了」を選んでください",
                      refused=True, running_ids=(app_id,))

    def test_止めようとしたツールに断られたらすぐ強制終了を聞く(self) -> None:
        self.bar._request_stop("nlm.coil")
        self.stop.assert_called_once_with("nlm.coil")
        self.bar._render(self.refused())
        self.bar.root.update()
        self.ask.assert_called_once()
        self.assertIn("取り込みの途中です", self.ask.call_args.args[1])
        self.assertEqual(self.ask.call_args.kwargs.get("default"), "no")
        self.stop.assert_called_with("nlm.coil", force=True)

    def test_ランチャーを閉じるときの断りでは聞かない(self) -> None:
        self.bar._render(self.refused())
        self.bar.root.update()
        self.ask.assert_not_called()


@unittest.skipUnless(HAVE_TK, "設定画面の部品が tkinter を読み込む")
class SettingsWarningTests(unittest.TestCase):

    def test_exeの行のポートを知らせる(self) -> None:
        from launcher.ui.settings_dialog import _exe_port_warning, _port_note

        exe = tool_registry.Tool(app_id="a", display_name="統合ツール", port=8700,
                                 start_command=r"C:\tools\統合ツール.exe")
        self.assertIn("ポート 8700", _exe_port_warning(exe))
        for quiet in (tool_registry.Tool(app_id="b", display_name="b", port=0,
                                         start_command=r"C:\a.exe"),
                      tool_registry.Tool(app_id="c", display_name="c", port=8700,
                                         start_command=r"C:\Start.vbs")):
            self.assertEqual(_exe_port_warning(quiet), "")
        self.assertIn("8713 はブラウザー版", _port_note({"browser_port": 8713}))
        self.assertEqual(_port_note({}), "")


if __name__ == "__main__":
    unittest.main()
