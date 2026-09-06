"""ランチャーバーの幅と座標 (要件定義書 §5.1)

画面まわりで唯一、**間違えると実機でしか気づけない**部分なので、
計算だけを切り出して確かめる。ここが狂うと、バーが画面の外に出たり、
右端のボタンが切れたりする。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.ui import geometry  # noqa: E402

# よくある端末
FHD = dict(screen_width=1920, screen_height=1080)
NOTEBOOK = dict(screen_width=1366, screen_height=768)
BAR = dict(bar_height=56, bottom_margin=48)


class DefaultPlacementTests(unittest.TestCase):
    """既定の置き場所。"""

    def test_画面下部の中央に置く(self) -> None:
        p = geometry.compute(content_width=980, **FHD, **BAR)
        self.assertEqual(p.width, 980)
        self.assertEqual(p.height, 56)
        self.assertEqual(p.x, (1920 - 980) // 2)
        # 下端から バーの高さ + 余白 だけ上げる (タスクバーを避ける)
        self.assertEqual(p.y, 1080 - 56 - 48)
        self.assertEqual(p.as_geometry(), "980x56+470+976")

    def test_狭すぎる内容でも読める幅を取る(self) -> None:
        p = geometry.compute(content_width=200, **FHD, **BAR)
        self.assertEqual(p.width, geometry.MIN_WIDTH)

    def test_画面をはみ出さない(self) -> None:
        """ツールが増えても、画面より広い窓は作らない。"""
        p = geometry.compute(content_width=3000, **NOTEBOOK, **BAR)
        self.assertLessEqual(p.width, 1366)
        self.assertGreaterEqual(p.x, 0)
        self.assertLessEqual(p.x + p.width, 1366)

    def test_下端に収まる(self) -> None:
        for screen in (FHD, NOTEBOOK):
            with self.subTest(screen):
                p = geometry.compute(content_width=980, **screen, **BAR)
                self.assertGreaterEqual(p.y, 0)
                self.assertLessEqual(p.y + p.height, screen["screen_height"])

    def test_余白を0にすれば下端に付く(self) -> None:
        p = geometry.compute(content_width=980, screen_width=1920,
                             screen_height=1080, bar_height=56,
                             bottom_margin=0)
        self.assertEqual(p.y, 1080 - 56)


class SavedPositionTests(unittest.TestCase):
    """利用者が動かした位置を覚える。"""

    def test_覚えた位置を使う(self) -> None:
        p = geometry.compute(content_width=980, **FHD, **BAR, saved=(100, 200))
        self.assertEqual((p.x, p.y), (100, 200))

    def test_画面の外に出ていたら既定へ戻す(self) -> None:
        """二画面を外した端末で、**見えない場所に出たまま戻せなくなる**のを防ぐ。"""
        for saved in ((4000, 200), (-2000, 200), (100, 5000), (100, -900)):
            with self.subTest(saved):
                p = geometry.compute(content_width=980, **FHD, **BAR,
                                     saved=saved)
                self.assertEqual((p.x, p.y), (470, 976),
                                 f"{saved} を使ってしまいました")

    def test_端に少し寄せた程度なら残す(self) -> None:
        """利用者が意図して寄せたものを、勝手に中央へ戻さない。"""
        p = geometry.compute(content_width=980, **FHD, **BAR, saved=(0, 976))
        self.assertEqual((p.x, p.y), (0, 976))

    def test_壊れた値は無視する(self) -> None:
        for text in ("", "abc", "100", "100,abc", "1,2,3", None):
            with self.subTest(text):
                self.assertIsNone(geometry.parse_saved(text))

    def test_書いて読み直せる(self) -> None:
        self.assertEqual(geometry.parse_saved(geometry.format_saved(12, 34)),
                         (12, 34))


class OnScreenTests(unittest.TestCase):

    def test_見えていれば真(self) -> None:
        self.assertTrue(geometry.is_on_screen((470, 976), 980, 56, 1920, 1080))

    def test_完全に外なら偽(self) -> None:
        self.assertFalse(geometry.is_on_screen((-1500, 976), 980, 56, 1920, 1080))
        self.assertFalse(geometry.is_on_screen((1900, 976), 980, 56, 1920, 1080))


if __name__ == "__main__":
    unittest.main()
