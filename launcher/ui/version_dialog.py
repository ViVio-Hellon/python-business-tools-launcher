"""バージョン情報 (どの版が入っていて、どの版が動いているか)

バーの版バッジを押すと出る。**現場で「入れ替えたのに直らない」を
調べるための画面**なので、版だけでなく置き場所まで出す ── 別のフォルダーを
起動していたのか、古いプロセスが残っているのかで、直し方が違う。
"""
from __future__ import annotations

import tkinter as tk

from .. import app_config, version_info
from ..logging_utils import get_logger
from . import theme

log = get_logger("ui.version")

# 状態ごとの色。バーのランプと同じ意味で使う
STATE_COLORS = {
    "動作中": theme.STATE_COLORS["running"],
    "版ちがい": theme.STATE_COLORS["error"],
    "停止中": theme.MUTED,
    "未設定": theme.STATE_COLORS["starting"],
}


class VersionDialog:
    """モーダルのバージョン情報。閉じるまで戻らない。"""

    def __init__(self, parent: tk.Misc) -> None:
        self.top = tk.Toplevel(parent)
        self.top.title(f"{app_config.display_name()} "
                       f"{app_config.version_label()} - バージョン情報")
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.top.resizable(False, False)

        self._build()
        self._center_on(parent)
        self.top.grab_set()
        self.top.bind("<Escape>", lambda _event: self.top.destroy())
        self.top.wait_window()

    # --------------------------------------------------------------
    def _build(self) -> None:
        info = version_info.launcher()

        tk.Label(self.top, text=app_config.display_name(), bg=theme.BG,
                 fg=theme.FG, font=("Yu Gothic UI", 13, "bold"),
                 anchor="w").pack(fill="x", padx=18, pady=(16, 0))
        tk.Label(self.top, text=info.version, bg=theme.BG,
                 fg=theme.BUTTON_CURRENT, font=("Yu Gothic UI", 11, "bold"),
                 anchor="w").pack(fill="x", padx=18)

        detail = tk.Frame(self.top, bg=theme.BG)
        detail.pack(fill="x", padx=18, pady=(8, 0))
        self._field(detail, "アプリ本体", info.app_root)
        self._field(detail, "Python", info.python)
        self._field(detail, "作業領域", info.local_root)
        if info.problem:
            self._field(detail, "版の問題", info.problem,
                        color=theme.STATE_COLORS["error"])

        tk.Label(self.top, text="業務ツール", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD,
                 anchor="w").pack(fill="x", padx=18, pady=(16, 4))
        self._build_tools()
        self._build_buttons()

    def _field(self, parent: tk.Widget, label: str, value: str,
               color: str = theme.MUTED) -> None:
        row = tk.Frame(parent, bg=theme.BG)
        row.pack(fill="x")
        tk.Label(row, text=f"{label}:", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL, width=10,
                 anchor="w").pack(side="left")
        tk.Label(row, text=value, bg=theme.BG, fg=color,
                 font=theme.FONT_SMALL, anchor="w",
                 justify="left", wraplength=430).pack(side="left")

    def _build_tools(self) -> None:
        box = tk.Frame(self.top, bg=theme.BG)
        box.pack(fill="x", padx=18)

        items = version_info.tools()
        if not items:
            tk.Label(box, text="登録されているツールがありません", bg=theme.BG,
                     fg=theme.MUTED, font=theme.FONT_SMALL,
                     anchor="w").pack(fill="x")
            return

        for item in items:
            row = tk.Frame(box, bg=theme.BG)
            row.pack(fill="x", pady=(0, 2))
            tk.Label(row, text=item.display_name, bg=theme.BG, fg=theme.FG,
                     font=theme.FONT, width=10,
                     anchor="w").pack(side="left")
            tk.Label(row, text=item.state, bg=theme.BG,
                     fg=STATE_COLORS.get(item.state, theme.MUTED),
                     font=theme.FONT_SMALL, width=8,
                     anchor="w").pack(side="left")
            tk.Label(row, text=item.version_text, bg=theme.BG, fg=theme.FG,
                     font=theme.FONT_SMALL, anchor="w").pack(side="left")

            if item.app_root:
                tk.Label(box, text=f"    {item.app_root}", bg=theme.BG,
                         fg=theme.MUTED, font=theme.FONT_SMALL, anchor="w",
                         justify="left", wraplength=430).pack(fill="x")
            if item.mismatched:
                # **これが出たら、入れ替えたのに古いほうが動いている。**
                # 直し方まで書く
                tk.Label(box,
                         text=("    ※古いプロセスが動いたままです。"
                               "stop.bat で止めてから起動し直してください"),
                         bg=theme.BG, fg=theme.STATE_COLORS["error"],
                         font=theme.FONT_SMALL, anchor="w", justify="left",
                         wraplength=430).pack(fill="x")

    def _build_buttons(self) -> None:
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=18, pady=(16, 16))
        tk.Button(frame, text="閉じる", command=self.top.destroy,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=20, pady=6, font=theme.FONT,
                  cursor="hand2").pack(side="right")
        tk.Button(frame, text="コピー", command=self.copy,
                  bg=theme.BUTTON_BG, fg=theme.MUTED, relief="flat", bd=0,
                  padx=14, pady=6, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="right", padx=(0, 8))

    def copy(self) -> None:
        """中身を丸ごとクリップボードへ。

        不具合を知らせるときに**そのまま貼れる**ようにする。画面を見て
        書き写させると、肝心の版やパスが抜ける。
        """
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(version_info.describe())
            log.info("バージョン情報をコピーしました")
        except tk.TclError as exc:            # noqa: BLE001
            log.warning("コピーできませんでした: %s", exc)

    def _center_on(self, parent: tk.Misc) -> None:
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = max(20, parent.winfo_rooty() - height - 20)
        self.top.geometry(f"+{max(0, x)}+{y}")
