"""neon-coil-zero (NEON COIL ZERO。pygame のゲーム) を登録できる (1.8.3)

* Web サーバー・`config/app.json` を持たず、**自分の窓 (pygame) を出す**
* 直下の main.py は `game.bootstrap` を呼ぶだけ。pygame の import は `game/` の中。
  requirements.txt が pygame (3.14 は pygame-ce) を求める
* `start.bat` は本体 (`python main.py %*`) が終わるまで待つ。`--no-browser` は知らない
  (渡すと argparse が断って起動しない)
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _isolation import LocalAreaTestCase  # noqa: E402

from launcher import tool_registry  # noqa: E402
from launcher.tool_registry import UI_APP, Tool  # noqa: E402

REQUIREMENTS = """# 実行に必要（import 名はどちらも pygame）
pygame==2.6.1; python_version < "3.14"
pygame-ce==2.5.8; python_version >= "3.14"

pyinstaller==6.16.0
"""


def make_game(base: Path, name: str = "neon-coil-zero-", *,
              requirements: str = REQUIREMENTS) -> Path:
    root = base / name
    (root / "game").mkdir(parents=True)
    (root / "main.py").write_text(
        'from game.bootstrap import main\n\nif __name__ == "__main__":\n'
        '    raise SystemExit(main())\n', encoding="utf-8")
    (root / "game" / "app.py").write_text("import pygame\n", encoding="utf-8")
    (root / "README.md").write_text("# NEON COIL ZERO\n\n見下ろし型 2D アクション\n",
                                    encoding="utf-8")
    if requirements:
        (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    start = root / "start.bat"
    start.write_text('@echo off\r\ncd /d "%~dp0"\r\n%PY% main.py %*\r\n', encoding="utf-8")
    return start


class NeonCoilZeroTests(LocalAreaTestCase):

    def test_窓のツールと見分けて名前とアプリIDを決める(self) -> None:
        start = make_game(self.work_root)
        found = tool_registry.probe_tool_folder(str(start))
        self.assertEqual(found["ui_mode"], UI_APP)
        self.assertEqual(found["display_name"], "NEON COIL ZERO")
        self.assertEqual(found["app_id"], "app.neon-coil-zero")
        self.assertNotIn("port", found)

    def test_起動引数は空で登録できる(self) -> None:
        start = make_game(self.work_root)
        args, reason = tool_registry.recommend_start_args(str(start))
        self.assertEqual(args, "", "--no-browser を渡すとゲームが起動しません")
        self.assertIn("窓", reason)
        tool = Tool(app_id="app.neon-coil-zero", display_name="NEON COIL ZERO",
                    start_command=str(start), ui_mode=UI_APP)
        self.assertEqual(tool.ui_problem(), "")
        self.assertTrue(tool.is_app)

    def test_pygame_ce_だけでも見分ける(self) -> None:
        start = make_game(self.work_root, requirements="pygame-ce>=2.5\n")
        self.assertEqual(tool_registry.probe_tool_folder(str(start))["ui_mode"], UI_APP)

    def test_窓の部品が無ければ見分けない(self) -> None:
        start = make_game(self.work_root, requirements="requests\n")
        (start.parent / "game" / "app.py").write_text("print('x')\n", encoding="utf-8")
        self.assertEqual(tool_registry.probe_tool_folder(str(start)), {})


if __name__ == "__main__":
    unittest.main()
