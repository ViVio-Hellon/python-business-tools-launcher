"""［詳細］で障害記録を読めるか (ランチャーの中で読む)

現場で、［詳細］の「障害記録を開きますか?」に「はい」を押しても
**「指定されたパスが見つかりません」**になった (既定の置き場所でも)。
Microsoft Store 版の Python は AppData に書いたファイルを、その Python に
だけ見える場所へ振り替える。ランチャーには見えるファイルが、メモ帳や
エクスプローラからは見えない。

この試験では、振り替えを**シンボリックリンク**で真似る (ランチャーに見える
場所 → 実体の場所)。確かめること:

* 記録は**ランチャー自身が読んで**画面に写す (メモ帳に頼らない)
* 案内・診断に出す場所は**実体の場所** (ほかのアプリから探せる場所)
* メモ帳で開けないときは、実体の場所と「この画面で読める」ことを出す
* 記録の書き方が古い Python (3.9) でも動く
"""
from __future__ import annotations

import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import ROOT, LocalAreaTestCase, release_tk  # noqa: E402

import app_manager  # noqa: E402
from launcher import trace  # noqa: E402


def _can_show_windows() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:                         # noqa: BLE001 - tkinter が無い・画面が無い
        return False


HAVE_TK = _can_show_windows()


class RedirectedTestCase(LocalAreaTestCase):
    """記録を書いて、Store 版 Python の振り替えを真似る。"""

    def write_incident(self) -> str:
        path = trace.incident("統合ツールを起動できなかった",
                              whys=["ポート 8713 が待ち受けていない"],
                              hints=["［設定］のポートを確かめる"])
        self.assertTrue(path)
        return path

    def redirected(self) -> tuple[Path, Path]:
        """(ランチャーに見える場所, 実体の場所)。見える場所はリンク。"""
        real = Path(self._tmp.name) / "Packages" / "PythonSoftwareFoundation" / "LocalCache"
        real.mkdir(parents=True)
        seen = Path(self._tmp.name) / "AppData"
        try:
            seen.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"シンボリックリンクを作れません: {exc}")
        return seen, real


class ReadRecordTests(RedirectedTestCase):

    def test_書いた記録をそのまま読める(self) -> None:
        path = self.write_incident()
        text, problem = trace.read_record(path)
        self.assertEqual(problem, "")
        self.assertIn("統合ツールを起動できなかった", text)
        self.assertIn("ポート 8713 が待ち受けていない", text)
        self.assertFalse(text.startswith("\ufeff"), "BOM が残っています")
        self.assertNotIn("\r", text)

    def test_無い記録は理由を返して落ちない(self) -> None:
        text, problem = trace.read_record(self.work_root / "無い.txt")
        self.assertEqual(text, "")
        self.assertTrue(problem)

    def test_長い記録は途中まで(self) -> None:
        path = self.work_root / "長い.txt"
        path.write_bytes(("あ" * 50).encode("utf-8-sig"))
        with mock.patch.object(trace, "READ_LIMIT", 10):
            text, _problem = trace.read_record(path)
        self.assertTrue(text.startswith("あ" * 10))
        self.assertIn("途中まで", text)

    def test_振り替えられた場所でも読める(self) -> None:
        seen, real = self.redirected()
        record = seen / "incidents" / "記録.txt"
        record.parent.mkdir()
        record.write_bytes("障害記録\r\nなぜ1\r\n".encode("utf-8-sig"))
        text, problem = trace.read_record(record)
        self.assertEqual((text, problem), ("障害記録\nなぜ1\n", ""))
        self.assertTrue(trace.moved_by_python(record))
        self.assertEqual(trace.real_path(record),
                         os.path.realpath(real / "incidents" / "記録.txt"))


class ShownPathTests(RedirectedTestCase):
    """案内・診断には、ほかのアプリから探せる場所を書く。"""

    def test_案内の記録の場所は実体の場所(self) -> None:
        seen, real = self.redirected()
        note = app_manager._records_note(str(seen / "記録.txt"))
        self.assertIn(os.path.realpath(real / "記録.txt"), note)
        self.assertNotIn(str(seen), note)

    def test_振り替えのない場所はそのまま(self) -> None:
        path = self.write_incident()
        self.assertFalse(trace.moved_by_python(path))
        self.assertIn(path, app_manager._records_note(path))

    def test_診断に実体の場所と注意を出す(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("launcher_main",
                                                      ROOT / "launcher.py")
        main = importlib.util.module_from_spec(spec)
        # 読み込むと、バイトコードの置き場所を決めにいく。試験の外へ残さない
        with mock.patch.object(sys, "pycache_prefix",
                               str(Path(self._tmp.name) / "pycache")):
            spec.loader.exec_module(main)

        with mock.patch.object(trace, "is_store_python", return_value=True):
            text = main._describe_records()
        self.assertIn("Microsoft Store 版の Python", text)
        with mock.patch.object(trace, "is_store_python", return_value=False):
            self.assertNotIn("Microsoft Store", main._describe_records())

    def test_Store版のPythonを見分ける(self) -> None:
        store = (r"C:\Users\a\AppData\Local\Microsoft\WindowsApps"
                 r"\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe")
        with mock.patch.object(sys, "executable", store):
            self.assertTrue(trace.is_store_python())
        with mock.patch.object(sys, "executable", r"C:\Python312\python.exe"), \
                mock.patch.object(sys, "base_prefix", r"C:\Python312"):
            self.assertFalse(trace.is_store_python())


class OpenPathTests(RedirectedTestCase):

    def test_無いファイルは開こうとしない(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            self.assertFalse(trace.open_path(self.work_root / "無い.txt"))
        popen.assert_not_called()

    def test_ほかのアプリには実体の場所を渡す(self) -> None:
        seen, real = self.redirected()
        record = seen / "記録.txt"
        record.write_text("x", encoding="utf-8")
        with mock.patch("subprocess.Popen") as popen:
            if os.name == "nt":                # pragma: no cover - Windows だけ
                with mock.patch("os.startfile") as startfile:
                    self.assertTrue(trace.open_path(record))
                args = startfile.call_args.args
            else:
                self.assertTrue(trace.open_path(record))
                args = popen.call_args.args[0]
        self.assertIn(os.path.realpath(real / "記録.txt"), args)


class WriteCompatibilityTests(unittest.TestCase):
    """`Path.write_text(newline=...)` は Python 3.10 から (3.9 では TypeError)。"""

    def test_write_textにnewlineを渡していない(self) -> None:
        found = []
        for path in sorted(ROOT.rglob("*.py")):
            if any(part.startswith(".") or part in ("dist", "build")
                   for part in path.relative_to(ROOT).parts):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "write_text"
                        and any(k.arg == "newline" for k in node.keywords)):
                    found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [], "Python 3.9 で落ちます。write_bytes で書いてください")


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class ViewerTests(RedirectedTestCase):

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        import tkinter

        from launcher.ui import record_viewer

        self.tk = tkinter
        self.module = record_viewer
        self.root = tkinter.Tk()
        self.addCleanup(self.root.destroy)
        self.info = mock.patch.object(record_viewer.messagebox, "showinfo").start()
        self.addCleanup(mock.patch.stopall)

    def open(self, detail: str, incident: str = ""):
        viewer = self.module.RecordViewer(self.root, detail, incident, wait=False)
        self.addCleanup(lambda: viewer.top.winfo_exists() and viewer.top.destroy())
        viewer.top.update()
        return viewer

    def test_記録をランチャーの中で読める(self) -> None:
        path = self.write_incident()
        viewer = self.open("92秒 待ちましたが応答がありませんでした。", path)
        shown = viewer.text.get("1.0", "end")
        self.assertIn("ポート 8713 が待ち受けていない", shown)
        self.assertEqual(viewer.path_entry.get(), trace.real_path(path))
        self.assertEqual(str(viewer.text.cget("state")), "disabled")

    def test_振り替えられていれば実体の場所と注意を出す(self) -> None:
        seen, real = self.redirected()
        record = seen / "記録.txt"
        record.write_bytes("なぜ1: ポートが違う\r\n".encode("utf-8-sig"))
        viewer = self.open("起動できませんでした", str(record))
        self.assertIn("なぜ1: ポートが違う", viewer.text.get("1.0", "end"))
        self.assertEqual(viewer.path_entry.get(), os.path.realpath(real / "記録.txt"))
        labels = [w.cget("text") for w in viewer.top.winfo_children()
                  if isinstance(w, self.tk.Label)]
        self.assertTrue(any("Microsoft Store" in t for t in labels))

    def test_コピーで案内と記録をそのまま貼れる(self) -> None:
        path = self.write_incident()
        viewer = self.open("起動できませんでした", path)
        viewer.copy()
        pasted = self.root.clipboard_get()
        self.assertIn("起動できませんでした", pasted)
        self.assertIn(f"障害記録: {trace.real_path(path)}", pasted)
        self.assertIn("ポート 8713 が待ち受けていない", pasted)

    def test_メモ帳で開けなければ場所とこの画面で読めることを出す(self) -> None:
        path = self.write_incident()
        viewer = self.open("起動できませんでした", path)
        with mock.patch.object(trace, "open_path", return_value=False):
            viewer.notepad_button.invoke()
        message = self.info.call_args.args[1]
        self.assertIn(trace.real_path(path), message)
        self.assertIn("この画面で読めます", message)

    def test_読めない記録でも案内は出る(self) -> None:
        viewer = self.open("起動できませんでした", str(self.work_root / "消えた.txt"))
        self.assertIn("障害記録を読めませんでした", viewer.text.get("1.0", "end"))

    def test_記録が無ければ案内だけ(self) -> None:
        viewer = self.open("ツールの出力を確かめてください")
        self.assertFalse(hasattr(viewer, "text"))
        self.assertFalse(hasattr(viewer, "notepad_button"))
        viewer.copy()
        self.assertEqual(self.root.clipboard_get(), "ツールの出力を確かめてください\n")


@unittest.skipUnless(HAVE_TK, "tkinter と画面が要る")
class BarDetailTests(RedirectedTestCase):
    """バーの［詳細］から、聞かずに記録の画面を出す。"""

    def setUp(self) -> None:
        self.addCleanup(release_tk, self)
        super().setUp()
        from app_manager import ToolManager
        from launcher.ui import bar as bar_module
        from launcher.ui import record_viewer

        self.bar_module = bar_module
        self.viewers = []

        def open_viewer(*args, **kwargs):
            viewer = record_viewer.RecordViewer(*args, wait=False, **kwargs)
            self.viewers.append(viewer)
            return viewer

        mock.patch.object(bar_module, "RecordViewer", open_viewer).start()
        self.ask = mock.patch.object(bar_module.messagebox, "askyesno").start()
        self.ask_close = mock.patch.object(bar_module.messagebox, "askyesnocancel",
                                           return_value=False).start()
        self.addCleanup(mock.patch.stopall)
        self.bar = bar_module.LauncherBar(ToolManager())
        self.addCleanup(self._destroy)

    def _destroy(self) -> None:
        try:
            self.bar.root.destroy()
        except Exception:                     # noqa: BLE001
            pass

    def test_詳細で記録の画面を出す(self) -> None:
        path = self.write_incident()
        self.bar._last_detail = "92秒 待ちましたが応答がありませんでした。"
        self.bar._last_incident = path
        self.bar.show_detail()
        self.ask.assert_not_called()
        self.assertEqual(len(self.viewers), 1)
        self.assertIn("ポート 8713", self.viewers[0].text.get("1.0", "end"))

    def test_記録の画面を開いているあいだは窓の閉じるボタンで閉じない(self) -> None:
        self.bar.manager._running["nlm.daily"] = __import__(
            "launcher.runtime_state", fromlist=["RunningTool"]).RunningTool(
                app_id="nlm.daily", display_name="日報")
        self.bar._last_detail = "案内"
        self.bar.show_detail()
        self.bar.root.update()
        handler = self.bar.root.tk.call("wm", "protocol", self.bar.root,
                                        "WM_DELETE_WINDOW")
        self.bar.root.tk.eval(handler)
        self.ask_close.assert_not_called()
        self.assertTrue(self.bar.root.winfo_exists())


if __name__ == "__main__":
    unittest.main()
