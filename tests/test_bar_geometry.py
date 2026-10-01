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


class AnchorTests(unittest.TestCase):
    """状態に合わせた置き場所 (要件定義書 §5.1 / §5.2)。"""

    ARGS = dict(width=980, height=56, screen_width=1920, screen_height=1080,
                bottom_margin=48, edge_margin=16)

    def test_中央(self) -> None:
        """何も選んでいないとき。ランチャーが主役なので真ん中に出す。"""
        x, y = geometry.anchor_position("center", **self.ARGS)
        self.assertEqual(x, (1920 - 980) // 2)
        self.assertEqual(y, (1080 - 56) // 2)

    def test_左下(self) -> None:
        """ツールを選んだあと。業務画面が主役なので隅へ寄る。"""
        x, y = geometry.anchor_position("bottom_left", **self.ARGS)
        self.assertEqual(x, 16)
        self.assertEqual(y, 1080 - 56 - 48)

    def test_右下(self) -> None:
        x, y = geometry.anchor_position("bottom_right", **self.ARGS)
        self.assertEqual(x, 1920 - 980 - 16)
        self.assertEqual(y, 1080 - 56 - 48)

    def test_どの置き場所でも画面内に収まる(self) -> None:
        for anchor in geometry.ANCHORS:
            for screen in (FHD, NOTEBOOK):
                with self.subTest(anchor=anchor, screen=screen):
                    p = geometry.compute(content_width=980, **screen, **BAR,
                                         anchor=anchor)
                    self.assertGreaterEqual(p.x, 0)
                    self.assertGreaterEqual(p.y, 0)
                    self.assertLessEqual(p.x + p.width, screen["screen_width"])
                    self.assertLessEqual(p.y + p.height, screen["screen_height"])

    def test_知らない呼び名は既定に倒す(self) -> None:
        """設定ファイルに打ち間違いがあっても起動する。"""
        self.assertEqual(geometry.normalize_anchor("まんなか"), "bottom_center")
        self.assertEqual(geometry.normalize_anchor(""), "bottom_center")
        for anchor in geometry.ANCHORS:
            self.assertEqual(geometry.normalize_anchor(anchor), anchor)

    def test_起動時と選択後で位置が変わる(self) -> None:
        """要件: 起動時は画面中央、ツール選択後は左下。"""
        idle = geometry.compute(content_width=980, **FHD, **BAR,
                                anchor="center")
        active = geometry.compute(content_width=980, **FHD, **BAR,
                                  anchor="bottom_left")
        self.assertEqual((idle.x, idle.y), (470, 512))
        self.assertEqual((active.x, active.y), (16, 976))
        self.assertNotEqual((idle.x, idle.y), (active.x, active.y))
        # 幅は変わらない。移動で大きさまで変わると落ち着かない
        self.assertEqual(idle.width, active.width)


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

    def test_手で置いた位置は自動の置き場所より強い(self) -> None:
        """**動かすたびに戻される**ことがないように。"""
        for anchor in geometry.ANCHORS:
            with self.subTest(anchor):
                p = geometry.compute(content_width=980, **FHD, **BAR,
                                     anchor=anchor, saved=(300, 400))
                self.assertEqual((p.x, p.y), (300, 400))

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


class FitButtonsTests(unittest.TestCase):
    """入りきらないツールを［▼］へ回す (要件定義書 §14)。

    バーは折り返さず、窓も広がらない。**黙って切れると、押せない
    ツールがあることに利用者は気づけない。**
    """

    # 1920×1080 でツールのボタンに残るおおよその幅
    BUDGET = 1300
    OVERFLOW = 44

    def fit(self, widths, **kwargs):
        return geometry.fit_buttons(widths, budget=self.BUDGET,
                                    overflow_width=self.OVERFLOW, **kwargs)

    def test_4個なら全部出る(self) -> None:
        visible, hidden = self.fit([100] * 4)
        self.assertEqual(visible, [0, 1, 2, 3])
        self.assertEqual(hidden, [])

    def test_入りきらなければ後ろを回す(self) -> None:
        visible, hidden = self.fit([100] * 20)
        self.assertTrue(hidden, "あふれているのに回していません")
        self.assertEqual(visible + hidden, sorted(visible + hidden))
        self.assertEqual(len(visible) + len(hidden), 20, "ツールが消えました")

    def test_出すぶんは予算に収まる(self) -> None:
        """**ここが狂うと、結局ボタンが切れる。**"""
        for count in range(1, 30):
            widths = [100] * count
            with self.subTest(count=count):
                visible, hidden = self.fit(widths)
                used = sum(widths[i] for i in visible)
                room = self.BUDGET - (self.OVERFLOW if hidden else 0)
                self.assertLessEqual(used, room,
                                     f"{count}個で {used}px 使っています")

    def test_並び順は変えない(self) -> None:
        """設定の order_no どおりに並べる。"""
        visible, hidden = self.fit([100] * 20)
        self.assertEqual(visible, sorted(visible))
        self.assertEqual(hidden, sorted(hidden))

    def test_使用中のツールは必ず出す(self) -> None:
        """**隠れると、どれが動いているか見えなくなる。**"""
        for target in (0, 5, 12, 19):
            with self.subTest(target=target):
                visible, hidden = self.fit([100] * 20, must_show=target)
                self.assertIn(target, visible)
                self.assertNotIn(target, hidden)
                used = sum(100 for _ in visible)
                self.assertLessEqual(used, self.BUDGET - self.OVERFLOW)

    def test_使用中が幅広くても収まる(self) -> None:
        widths = [100] * 19 + [400]
        visible, hidden = self.fit(widths, must_show=19)
        self.assertIn(19, visible)
        self.assertLessEqual(sum(widths[i] for i in visible),
                             self.BUDGET - self.OVERFLOW)

    def test_1つも入らないときも落ちない(self) -> None:
        visible, hidden = geometry.fit_buttons(
            [900], budget=500, overflow_width=44)
        self.assertEqual(visible, [])
        self.assertEqual(hidden, [0])

    def test_ツールが無いとき(self) -> None:
        self.assertEqual(geometry.fit_buttons([], budget=100,
                                              overflow_width=44), ([], []))

    def test_予算が0でも落ちない(self) -> None:
        visible, hidden = geometry.fit_buttons([100, 100], budget=0,
                                               overflow_width=44)
        self.assertEqual(visible, [])
        self.assertEqual(hidden, [0, 1])

    def test_ちょうど収まるときは回さない(self) -> None:
        """**［▼］を出すために誰かを追い出す、が起きないこと。**"""
        widths = [100] * 13          # 1300 = 予算ちょうど
        visible, hidden = self.fit(widths)
        self.assertEqual(hidden, [])
        self.assertEqual(len(visible), 13)

    def test_1つ超えたら回る(self) -> None:
        visible, hidden = self.fit([100] * 14)
        self.assertTrue(hidden)
        # ［▼］のぶんを取り置くので、出せるのは12個
        self.assertEqual(len(visible), 12)

    def test_幅の上限(self) -> None:
        self.assertEqual(geometry.max_width(1920), 1880)
        self.assertEqual(geometry.max_width(1366), 1326)
        # 極端に狭い画面でも読める幅は確保する
        self.assertEqual(geometry.max_width(300), geometry.MIN_WIDTH)


if __name__ == "__main__":
    unittest.main()


class UserMoveTests(unittest.TestCase):
    """自分で動かしたこだまを、利用者のドラッグと取り違えない。

    以前は時間だけで見分けていたため、Windows で遅れて届いた知らせを
    「利用者が動かした」と覚え、以後バーが中央から動かなくなっていた。
    """

    def test_geometryの文字から位置を取る(self) -> None:
        self.assertEqual(geometry.parse_geometry_xy("600x56+100+200"), (100, 200))
        self.assertEqual(geometry.parse_geometry_xy("+-8+100"), (-8, 100))
        self.assertEqual(geometry.parse_geometry_xy("600x56-10-20"), (-10, -20))
        self.assertIsNone(geometry.parse_geometry_xy("600x56"))
        self.assertIsNone(geometry.parse_geometry_xy(""))

    def test_置いた場所のままなら利用者の移動ではない(self) -> None:
        self.assertFalse(geometry.moved_by_user((400, 500), (400, 500)))
        # Windows は枠のぶん数pxずらして報告することがある
        self.assertFalse(geometry.moved_by_user((400, 500), (408, 492)))

    def test_はっきり動いていれば利用者の移動(self) -> None:
        self.assertTrue(geometry.moved_by_user((400, 500), (700, 500)))
        self.assertTrue(geometry.moved_by_user((400, 500), (400, 200)))

    def test_位置が分からなければ覚えない(self) -> None:
        self.assertFalse(geometry.moved_by_user((400, 500), None))


class ActivePositionTests(unittest.TestCase):
    """ツールを起動したあとの置き場所は、上下×左中右の6通りから選べる。"""

    def place(self, anchor: str) -> tuple[int, int]:
        return geometry.anchor_position(
            anchor, width=600, height=56, screen_width=1920, screen_height=1080,
            bottom_margin=48, edge_margin=16)

    def test_6通りが選べる(self) -> None:
        self.assertEqual(set(geometry.ACTIVE_ANCHORS), {
            "top_left", "top_center", "top_right",
            "bottom_left", "bottom_center", "bottom_right"})
        self.assertEqual(geometry.ACTIVE_ANCHORS["top_right"], "右上")

    def test_上の3つは画面の上に寄る(self) -> None:
        self.assertEqual(self.place("top_left"), (16, 16))
        self.assertEqual(self.place("top_center"), (660, 16))
        self.assertEqual(self.place("top_right"), (1304, 16))

    def test_下の3つはタスクバーを避ける(self) -> None:
        self.assertEqual(self.place("bottom_left"), (16, 976))
        self.assertEqual(self.place("bottom_right"), (1304, 976))


class TextTests(unittest.TestCase):
    """状態の文が枠に収まらないとき、途中で切れて読めなくしない。"""

    def test_収まれば切らない(self) -> None:
        from launcher.ui.texts import fit_text
        self.assertEqual(fit_text("動作中：日報", 300, lambda t: 12 * len(t)),
                         "動作中：日報")

    def test_収まらなければ後ろを省いて印を付ける(self) -> None:
        from launcher.ui.texts import fit_text
        long = "カレンダーを終了しました（画面は手で閉じてください）"
        fitted = fit_text(long, 150, lambda t: 12 * len(t))
        self.assertTrue(fitted.endswith("…"))
        self.assertLessEqual(12 * len(fitted), 150)
        self.assertTrue(long.startswith(fitted[:-1]))

    def test_閉じるときは止めずに閉じたらどうなるかまで書く(self) -> None:
        from launcher.ui.texts import close_question
        text = close_question("日報、看板")
        self.assertIn("日報、看板", text)
        self.assertIn("自分で終了", text)
        self.assertIn("引き継ぎ", text)
