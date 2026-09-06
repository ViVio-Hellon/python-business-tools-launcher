"""ランチャーバーの幅と座標 (要件定義書 §5.1)

**tkinter を読み込まない。** 置き場所の計算だけをここに切り出してあるので、
画面の無い環境でも試験できる。画面まわりで唯一、間違えると実機でしか
気づけない部分なので、計算そのものは確かめられる形にしておく。
"""
from __future__ import annotations

from typing import NamedTuple, Optional

# これより狭いと、状態表示の文字が入らない
MIN_WIDTH = 520
# 画面の端にぴったり付けない。左右に残す余白
SCREEN_MARGIN = 40


class Placement(NamedTuple):
    """`geometry()` に渡す値。"""

    width: int
    height: int
    x: int
    y: int

    def as_geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def compute(*, content_width: int, screen_width: int, screen_height: int,
            bar_height: int, bottom_margin: int,
            saved: Optional[tuple[int, int]] = None) -> Placement:
    """バーの幅と座標を決める。

    `content_width` は**すべての操作が出ている状態**の必要幅を渡すこと。
    畳んだ状態で測ると、「ツール停止」や「詳細」が出たときに右端が切れる
    (窓の幅は変えられない)。

    `saved` があればその座標を使う。ただし画面の外に出ているときは
    捨てて既定の位置に戻す ── 画面構成が変わった端末 (二画面を外した、
    解像度を変えた) で、**バーが見えない場所に出たまま戻せなくなる**のを
    避けるため。
    """
    width = max(content_width, MIN_WIDTH)
    width = min(width, max(MIN_WIDTH, screen_width - SCREEN_MARGIN))
    height = max(1, bar_height)

    default_x = max(0, (screen_width - width) // 2)
    default_y = max(0, screen_height - height - max(0, bottom_margin))

    if saved is not None and is_on_screen(saved, width, height,
                                          screen_width, screen_height):
        return Placement(width, height, saved[0], saved[1])
    return Placement(width, height, default_x, default_y)


# 画面内と認めるのに必要な、見えている量 (ピクセル)。
# 端に少しかかっている程度なら、利用者が意図して寄せたものとして残す
VISIBLE_MARGIN = 80


def is_on_screen(position: tuple[int, int], width: int, height: int,
                 screen_width: int, screen_height: int) -> bool:
    """その座標に置いたバーが、掴める程度に見えているか。"""
    x, y = position
    if x + width < VISIBLE_MARGIN or x > screen_width - VISIBLE_MARGIN:
        return False
    if y + height < 0 or y > screen_height - height // 2:
        return False
    return True


def parse_saved(text: str) -> Optional[tuple[int, int]]:
    """保存した座標を読む。壊れていれば `None`。"""
    parts = (text or "").split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def format_saved(x: int, y: int) -> str:
    return f"{x},{y}"
