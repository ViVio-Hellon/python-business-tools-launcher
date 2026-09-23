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

# 置き場所の呼び名。
#
#   center        … 画面中央。**ランチャーが主役のとき** (まだ何も選んで
#                   いない起動直後)。探さずに見つかる位置に出す
#   bottom_left   … 左下。**業務画面が主役のとき**。隅へ寄って邪魔をしない
#   bottom_center … 画面下部の中央
#   bottom_right  … 右下
ANCHORS = ("center", "bottom_left", "bottom_center", "bottom_right")

# 手で動かした位置を覚えておく鍵 (PC別設定の入れ物を借りる)。
# **値があること自体が「手動」の印**。無ければ状態に合わせて自動で寄る
POSITION_KEY = "bar_position"


class Placement(NamedTuple):
    """`geometry()` に渡す値。"""

    width: int
    height: int
    x: int
    y: int

    def as_geometry(self) -> str:
        return f"{self.width}x{self.height}+{self.x}+{self.y}"


def max_width(screen_width: int) -> int:
    """バーが取れる幅の上限。"""
    return max(MIN_WIDTH, screen_width - SCREEN_MARGIN)


def fit_buttons(widths: list[int], *, budget: int, overflow_width: int,
                must_show: Optional[int] = None) -> tuple[list[int], list[int]]:
    """ツールのボタンを「出すぶん」と「あふれたぶん」に分ける。

    バーは折り返さないし、窓も広がらない (`resizable(False, False)`)。
    入りきらないボタンは**黙って切れて押せなくなる**ので、あふれたものは
    ［▼］のメニューへ回す。

    `must_show` は、必ず出しておきたいボタンの番号 (いま使っているツール)。
    **使っているツールが隠れると、どれが動いているのか見えなくなる。**

    戻り値はどちらも**元の並び順**の番号。並びは設定の `order_no` に
    従うので、ここで入れ替えない。
    """
    if not widths:
        return [], []
    if sum(widths) <= budget:
        return list(range(len(widths))), []

    # あふれる。［▼］のぶんを取り置いてから詰める
    room = budget - overflow_width
    visible: list[int] = []
    used = 0
    for index, width in enumerate(widths):
        if used + width > room:
            break
        visible.append(index)
        used += width

    if must_show is not None and 0 <= must_show < len(widths) \
            and must_show not in visible:
        # 使っているツールを入れるため、後ろから譲る
        needed = widths[must_show]
        while visible and used + needed > room:
            used -= widths[visible.pop()]
        if used + needed <= room:
            visible.append(must_show)
            visible.sort()

    hidden = [index for index in range(len(widths)) if index not in visible]
    return visible, hidden


def anchor_position(anchor: str, *, width: int, height: int,
                    screen_width: int, screen_height: int,
                    bottom_margin: int, edge_margin: int) -> tuple[int, int]:
    """呼び名から座標を出す。

    下寄せは `bottom_margin` だけ上げてタスクバーを避ける。左右寄せは
    `edge_margin` だけ内側に入れる ── 端にぴったり付けると、画面端の
    自動表示 (タスクバーやサイドバー) と重なることがある。
    """
    bottom_y = max(0, screen_height - height - max(0, bottom_margin))
    center_x = max(0, (screen_width - width) // 2)
    right_x = max(0, screen_width - width - max(0, edge_margin))

    if anchor == "center":
        return center_x, max(0, (screen_height - height) // 2)
    if anchor == "bottom_left":
        return max(0, edge_margin), bottom_y
    if anchor == "bottom_right":
        return right_x, bottom_y
    return center_x, bottom_y                 # bottom_center


def normalize_anchor(value: str, fallback: str = "bottom_center") -> str:
    """設定から来た呼び名を確かめる。知らない値なら既定に倒す。"""
    return value if value in ANCHORS else fallback


def compute(*, content_width: int, screen_width: int, screen_height: int,
            bar_height: int, bottom_margin: int, edge_margin: int = 16,
            anchor: str = "bottom_center",
            saved: Optional[tuple[int, int]] = None) -> Placement:
    """バーの幅と座標を決める。

    `content_width` は**すべての操作が出ている状態**の必要幅を渡すこと。
    畳んだ状態で測ると、「ツール停止」や「詳細」が出たときに右端が切れる
    (窓の幅は変えられない)。

    `anchor` は状態に応じた置き場所 (起動直後は中央、ツールを選んだあとは
    左下、など)。

    `saved` があればそちらを優先する ── **利用者が手で置いた場所が
    いちばん強い。** 自動の移動が利用者の置き場所を上書きすると、
    動かすたびに戻されることになる。ただし画面の外に出ているときは
    捨てて `anchor` に戻す ── 画面構成が変わった端末 (二画面を外した、
    解像度を変えた) で、**バーが見えない場所に出たまま戻せなくなる**のを
    避けるため。
    """
    width = max(content_width, MIN_WIDTH)
    width = min(width, max(MIN_WIDTH, screen_width - SCREEN_MARGIN))
    height = max(1, bar_height)

    if saved is not None and is_on_screen(saved, width, height,
                                          screen_width, screen_height):
        return Placement(width, height, saved[0], saved[1])

    x, y = anchor_position(normalize_anchor(anchor),
                           width=width, height=height,
                           screen_width=screen_width,
                           screen_height=screen_height,
                           bottom_margin=bottom_margin,
                           edge_margin=edge_margin)
    return Placement(width, height, x, y)


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
