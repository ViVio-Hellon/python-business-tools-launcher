"""tkinter の部品を、裏のスレッドで片付けない (ランチャーごと落ちない)

現場で「ツールを起動中にランチャーが落ちる」「ボタンを連打・切り替えると
落ちる」。落ちたときの記録 (faulthandler):

    Windows fatal exception: code 0x80000003
    Current thread (most recent call first):
      Garbage-collecting
      ... desktop.processes_in_folder ← look_elsewhere ← _start_locked (裏の処理)

起動中の窓 (splash) は自分の Tk (Tcl の本体) を持ち、部品どうしが指し合って
いるので、閉じたあとも**ごみ集めを待つ**。裏のスレッドがたくさん物を作って
ごみ集めを始めると、そこで Tk が片付けられ、Tcl が「別のスレッドで消された」
(Tcl_AsyncDelete) として Python ごと落とす。

落ちると試験のプロセスごと消えるので、別のプロセスで確かめる。
"""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import ROOT  # noqa: E402


def _can_show_windows() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001 - tkinter が無い・画面が無い
        return False


HAVE_TK = _can_show_windows()


def _run(script: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          capture_output=True, text=True, timeout=60, env=env,
                          cwd=str(ROOT))


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class SplashTests(unittest.TestCase):

    def test_起動中の窓を閉じたあと裏のスレッドでごみ集めしても落ちない(self) -> None:
        result = _run("""
            import faulthandler, gc, threading
            faulthandler.enable()
            from launcher.ui import splash
            splash.run_with_splash(["a", "b"], lambda report: 42)
            worker = threading.Thread(target=gc.collect)   # ツールの起動を待つ処理の代わり
            worker.start(); worker.join()
            print("生きている")
        """)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("生きている", result.stdout)


def _have_tkinter() -> bool:
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


class BarGcTests(unittest.TestCase):

    @unittest.skipUnless(_have_tkinter(), "tkinter が要る")
    def test_バーが動いているあいだ自動のごみ集めを止める(self) -> None:
        from launcher.ui import bar as bar_module

        seen = {}

        class FakeBar:
            def __init__(self, manager) -> None:
                pass

            def run(self) -> None:
                seen["enabled"] = gc.isenabled()

        self.addCleanup(gc.enable)
        with mock.patch.object(bar_module, "LauncherBar", FakeBar):
            bar_module.run(manager=None)
        self.assertFalse(seen["enabled"], "バーが動いているのに自動のごみ集めが動きます")
        self.assertTrue(gc.isenabled(), "バーを閉じたら元に戻します")

    @unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
    def test_ごみ集めはバーのメインスレッドで続ける(self) -> None:
        result = _run("""
            import gc, os, tempfile, threading
            os.environ["BUSINESS_TOOLS_LAUNCHER_LOCAL_DIR"] = tempfile.mkdtemp()
            from launcher import app_config
            os.environ[app_config.LOCAL_DIR_ENV] = tempfile.mkdtemp()
            from launcher.ui import bar as bar_module
            from app_manager import ToolManager
            bar_module.GC_MS = 50
            calls = []
            real = gc.collect
            def collect(*a):
                calls.append(threading.current_thread() is threading.main_thread())
                return real(*a)
            gc.collect = collect
            original = bar_module.LauncherBar.run
            def run(self):
                self.root.after(400, self.root.destroy)
                original(self)
            bar_module.LauncherBar.run = run
            bar_module.run(ToolManager())
            print(len(calls) >= 3, all(calls))
        """)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.split()[-2:], ["True", "True"], result.stdout)


if __name__ == "__main__":
    unittest.main()
