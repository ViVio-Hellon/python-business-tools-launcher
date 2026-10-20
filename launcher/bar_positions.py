"""バーの置き場所の名前 (［設定］で選ぶ・配布先フォルダに入る)

設定 (`tool_registry`) と画面 (`ui.geometry`・`ui.settings_dialog`) の両方が使う。
**画面の部品に置かない** ── 以前は `ui.geometry` にあり、設定の中核が画面の
部品に依存していた。tkinter は読み込まない。
"""
from __future__ import annotations

# ［設定］で選べる置き場所 (と画面の呼び名)。**ランチャーを起動したとき**と
# **ツールを使っているとき**の2つを、どちらもこの7通りから選ぶ
POSITION_NAMES = {
    "center": "画面中央",
    "top_left": "左上", "top_center": "中央上", "top_right": "右上",
    "bottom_left": "左下", "bottom_center": "中央下", "bottom_right": "右下",
}
# 以前の名前 (ツールを使っているときの置き場所)。中身は同じ7通り
ACTIVE_ANCHORS = POSITION_NAMES
DEFAULT_IDLE_ANCHOR = "center"
DEFAULT_ACTIVE_ANCHOR = "bottom_left"
