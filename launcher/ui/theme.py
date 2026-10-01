"""ランチャーバーの見た目。

色と余白をここへ集める。業務ツール側の画面と張り合わないよう地は暗く
するが、**押すもの・動いているものははっきり見せる**:

    ツールのボタン   … 地より明るく、太字。押せるものだと分かる
    動いているツール … 緑。名前の前に ● (色が見分けにくい人にも分かる)
    起動・停止の最中 … 橙
    未設定           … 茶。押しても起動しないことを押す前に見せる
    設定・終了など   … 控えめ。ツールのボタンと取り違えない
"""
from __future__ import annotations

BG = "#1b222c"                 # バーの地色。暗くして業務画面と分ける
FG = "#f2f5f8"
# 補足の文字。暗い地でも読める明るさにする (以前は暗すぎて読みにくかった)
MUTED = "#b3bfcb"

# --- ツールのボタン ---
TOOL_BG = "#3b4757"
TOOL_ACTIVE = "#4b5a6d"
TOOL_RUNNING = "#23845a"       # 動いているツール
TOOL_RUNNING_ACTIVE = "#2c9c6b"
TOOL_BUSY = "#a87708"          # 起動・停止の最中
TOOL_UNSET = "#4a3b2a"         # start.bat が未設定のツール
TOOL_UNSET_FG = "#e0cdb2"

# --- 設定・終了などの操作 ---
UTIL_BG = "#262f3b"
UTIL_FG = "#d0d9e2"
ATTENTION_BG = "#8a5a00"       # ［詳細］読んでほしい案内がある

# 以前からの呼び名 (設定画面などが使う)
BUTTON_BG = "#2c3644"
BUTTON_ACTIVE = "#3a4757"
BUTTON_CURRENT = TOOL_RUNNING
BUTTON_UNSET = TOOL_UNSET

# 状態の色 (要件定義書 §5.2「起動中・停止中・エラー」)
STATE_COLORS = {
    "idle": MUTED,
    "starting": "#e0a400",
    "running": "#4fd18e",
    "stopping": "#e0a400",
    "error": "#ff7b72",
}

FONT = ("Yu Gothic UI", 10)
FONT_BOLD = ("Yu Gothic UI", 10, "bold")
FONT_SMALL = ("Yu Gothic UI", 9)
FONT_TOOL = ("Yu Gothic UI", 11, "bold")

PAD = 6

# 状態表示に取る幅 (px)。**ここに収まらない文は「…」で切り、全文は
# ［詳細］とマウスを載せたときに出す**。幅を決めずにおくと、長い案内で
# 右の操作が押し出されたり、文が途中で切れて読めなくなったりする
STATUS_WIDTH = 300
