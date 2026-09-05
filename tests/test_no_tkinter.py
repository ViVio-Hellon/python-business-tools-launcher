"""起動制御が画面から独立していること (基盤仕様書 2.5)

起動・停止の判断を tkinter に結びつけると、画面の無い環境
(`stop.bat` からの停止、CIでの試験、tkinter の入っていない端末) で
何も動かせなくなる。

そこで**画面より下の層は tkinter を読み込まない**ことを試験で固定する。
`launcher/ui/` だけが tkinter を知ってよい。
"""
from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 画面が無くても動かなければならないもの
HEADLESS_MODULES = ("process_manager", "launch_guard", "app_manager",
                    "launcher.tool_registry", "launcher.health",
                    "launcher.app_config", "launcher.runtime_state")


class HeadlessImportTests(unittest.TestCase):

    def test_起動制御はtkinterを読み込まない(self) -> None:
        """別プロセスで確かめる。同じプロセスでは他の試験の影響を受ける。"""
        script = (
            "import sys\n"
            + "".join(f"import {name}\n" for name in HEADLESS_MODULES)
            + "print('tkinter' in sys.modules)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0,
                         f"読み込めませんでした:\n{completed.stderr}")
        self.assertEqual(
            completed.stdout.strip(), "False",
            "起動制御が tkinter を読み込んでいます (基盤仕様書 2.5)")

    def test_停止はtkinter無しで動く(self) -> None:
        """`stop.bat` の中身が画面なしで動くこと。"""
        completed = subprocess.run(
            [sys.executable, "process_manager.py", "--status"], cwd=ROOT,
            capture_output=True, text=True, timeout=60)
        self.assertEqual(completed.returncode, 0, completed.stderr)


class LayeringTests(unittest.TestCase):
    """どのファイルが tkinter を知ってよいか。"""

    # 画面の組み立ては `launcher/ui/` が持つ。`launcher.py` だけは
    # 例外で、**起動前の確認と失敗の通知**にダイアログを使う ──
    # `Start.vbs` はコンソールを出さないので、そこで伝えないと
    # 「何も起きない」ようにしか見えない
    ALLOWED = {"launcher.py"}

    def test_tkinterはui配下だけ(self) -> None:
        """import 文だけを見る。

        本文の検索では、説明文に出てくる「tkinter」まで拾ってしまい、
        **注釈を書くほど試験が落ちる**ことになる。構文木から実際の
        import を取り出して確かめる。
        """
        offenders = []
        targets = list(ROOT.glob("*.py")) + list((ROOT / "launcher").glob("*.py"))
        for path in targets:
            name = (path.name if path.parent == ROOT
                    else f"launcher/{path.name}")
            if path.name in self.ALLOWED:
                continue
            if _imports_tkinter(path):
                offenders.append(name)
        self.assertEqual(offenders, [],
                         "画面の外で tkinter を import しています")


def _imports_tkinter(path: Path) -> bool:
    """そのファイルが tkinter を import しているか (関数の中も含む)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "tkinter" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "tkinter":
                return True
    return False


if __name__ == "__main__":
    unittest.main()
