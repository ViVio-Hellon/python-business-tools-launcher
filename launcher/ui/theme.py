"""ランチャーバーの見た目。

色と余白をここへ集める。業務ツール側の画面と張り合わないよう、
**目立たせるのは状態表示だけ**にしてある (要件定義書 §5.2
「業務ツールの操作をできるだけ邪魔しない」)。
"""
from __future__ import annotations

BG = "#1f2630"                 # バーの地色。暗くして業務画面と分ける
FG = "#e8edf3"
MUTED = "#9aa7b5"

BUTTON_BG = "#2c3644"
BUTTON_ACTIVE = "#3a4757"
BUTTON_CURRENT = "#2f7d5f"     # いま使っているツール
BUTTON_UNSET = "#4a3b2a"       # start.bat が未設定のツール

# 状態の色 (要件定義書 §5.2「起動中・停止中・切り替え中・エラー」)
STATE_COLORS = {
    "idle": MUTED,
    "starting": "#d79b00",
    "running": "#4caf82",
    "stopping": "#d79b00",
    "error": "#e06c6c",
}

FONT = ("Yu Gothic UI", 10)
FONT_BOLD = ("Yu Gothic UI", 10, "bold")
FONT_SMALL = ("Yu Gothic UI", 9)

PAD = 6
