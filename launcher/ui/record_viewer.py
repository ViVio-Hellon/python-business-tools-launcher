"""［詳細］の画面 (案内と障害記録を、ランチャーの中で読む)

以前は「障害記録を開きますか?」と聞いて、メモ帳 (関連づけ) で開いていた。
現場では**「はい」を押しても「指定されたパスが見つかりません」**になる端末が
あった。Microsoft Store 版の Python は AppData に書いたファイルを、その
Python にだけ見える場所へ振り替えるので、ランチャーには見えるファイルが
メモ帳やエクスプローラからは見えない (`trace.real_path`)。

そこで、記録は**ランチャー自身が読んで、この画面に写す。** どの端末でも
読めて、［コピー］でそのまま貼れる。メモ帳・エクスプローラで開くボタンは
添えるだけにし、開けなければ実体の場所を出す。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from .. import app_config, trace
from ..logging_utils import get_logger
from . import theme

log = get_logger("ui.record")

TITLE = "詳細"


class RecordViewer:
    """モーダルの［詳細］。閉じるまで戻らない (`wait=False` なら戻る)。"""

    def __init__(self, parent: tk.Misc, detail: str, incident: str = "", *,
                 wait: bool = True) -> None:
        self.detail = detail or ""
        self.incident = str(incident or "")
        # ほかのアプリ (メモ帳・エクスプローラ) から見た場所
        self.real = trace.real_path(self.incident) if self.incident else ""
        self.record, self.problem = (trace.read_record(self.incident)
                                     if self.incident else ("", ""))

        self.top = tk.Toplevel(parent)
        self.top.title(f"{app_config.display_name()} - {TITLE}")
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.top.minsize(420, 200)

        self._build()
        self._place_on(parent)
        self.top.bind("<Escape>", lambda _event: self.close())
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        try:
            self.top.grab_set()
        except tk.TclError:                   # 表示が間に合わない環境
            pass
        if wait:
            self.top.wait_window()

    # --------------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 16}
        tk.Label(self.top, text=self.detail, bg=theme.BG, fg=theme.FG,
                 font=theme.FONT, anchor="w", justify="left",
                 wraplength=720).pack(fill="x", pady=(14, 0), **pad)

        if self.incident:
            tk.Label(self.top, text="障害記録 (なぜなぜ分析の下書き)",
                     bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD,
                     anchor="w").pack(fill="x", pady=(12, 2), **pad)
            self._path_row("場所", self.real)
            if trace.moved_by_python(self.incident):
                # Store 版 Python の振り替え。**ランチャーに見える名前では、
                # メモ帳もエクスプローラも見つけられない**
                tk.Label(self.top,
                         text=("※ この Python (Microsoft Store 版) は、AppData に"
                               "書いたファイルを上の場所へ振り替えています。"
                               "エクスプローラで探すときは上の場所を使ってください。"),
                         bg=theme.BG, fg=theme.STATE_COLORS["starting"],
                         font=theme.FONT_SMALL, anchor="w", justify="left",
                         wraplength=720).pack(fill="x", **pad)
            self._build_record()

        self._build_buttons()

    def _path_row(self, label: str, value: str) -> None:
        row = tk.Frame(self.top, bg=theme.BG)
        row.pack(fill="x", padx=16)
        tk.Label(row, text=f"{label}:", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left")
        # 書き写さずに選んでコピーできるよう、読み取り専用の入力欄に出す
        var = tk.StringVar(value=value)
        entry = tk.Entry(row, textvariable=var, state="readonly",
                         readonlybackground=theme.BG, fg=theme.MUTED,
                         relief="flat", font=theme.FONT_SMALL)
        entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.path_entry = entry
        self._path_var = var                  # 消されないよう持っておく

    def _build_record(self) -> None:
        box = tk.Frame(self.top, bg=theme.BG)
        box.pack(fill="both", expand=True, padx=16, pady=(6, 0))
        scroll = tk.Scrollbar(box)
        scroll.pack(side="right", fill="y")
        self.text = tk.Text(box, width=88, height=22, wrap="none",
                            yscrollcommand=scroll.set, font=("MS Gothic", 9),
                            bg="#10161d", fg=theme.FG, insertbackground=theme.FG,
                            relief="flat")
        self.text.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.text.yview)
        if self.problem:
            body = (f"障害記録を読めませんでした: {self.problem}\n"
                    f"{self.incident}\n\n上の［詳細］の文は、そのまま読めます。")
        else:
            body = self.record
        self.text.insert("1.0", body)
        # 読むだけ。書き換えて保存させない (選んでコピーはできる)
        self.text.configure(state="disabled")

    def _build_buttons(self) -> None:
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(12, 14))
        self.close_button = self._button(frame, "閉じる", self.close,
                                         fg=theme.FG, padx=20)
        self.close_button.pack(side="right")
        self.copy_button = self._button(frame, "コピー", self.copy)
        self.copy_button.pack(side="right", padx=(0, 8))
        if self.incident:
            self.folder_button = self._button(frame, "フォルダーを開く",
                                              self.open_folder)
            self.folder_button.pack(side="left")
            self.notepad_button = self._button(frame, "メモ帳で開く",
                                               self.open_file)
            self.notepad_button.pack(side="left", padx=(8, 0))
        self.close_button.focus_set()

    def _button(self, parent: tk.Widget, text: str, command, *,
                fg: str = theme.MUTED, padx: int = 14) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=theme.BUTTON_BG, fg=fg,
                         activebackground=theme.BUTTON_ACTIVE,
                         activeforeground=theme.FG, relief="flat", bd=0,
                         padx=padx, pady=6, font=theme.FONT_SMALL,
                         cursor="hand2")

    # --------------------------------------------------------------
    def copy_text(self) -> str:
        """コピーする中身。案内・記録の場所・記録の本文。"""
        parts = [self.detail.strip()]
        if self.incident:
            parts.append(f"障害記録: {self.real}")
            if self.record:
                parts.append(self.record.strip())
        return "\n\n".join(p for p in parts if p) + "\n"

    def copy(self) -> None:
        """丸ごとクリップボードへ。**そのまま開発担当へ貼れる。**"""
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(self.copy_text())
            self.copy_button.configure(text="コピーしました")
            log.info("詳細をコピーしました")
        except tk.TclError as exc:
            log.warning("コピーできませんでした: %s", exc)

    def open_file(self) -> None:
        if not trace.open_path(self.incident):
            self._could_not_open("メモ帳で開けませんでした。")

    def open_folder(self) -> None:
        if not trace.reveal_path(self.incident):
            self._could_not_open("フォルダーを開けませんでした。")

    def _could_not_open(self, message: str) -> None:
        messagebox.showinfo(
            TITLE, f"{message}\n{self.real}\n\n"
            "中身はこの画面で読めます。［コピー］でそのまま貼れます。",
            parent=self.top)

    def close(self) -> None:
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()

    def _place_on(self, parent: tk.Misc) -> None:
        """バーの上 (画面の内側) に出す。バーは画面の端に居ることが多い。"""
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() - height - 20
        if y < 20:
            y = parent.winfo_rooty() + parent.winfo_height() + 20
        x = min(max(0, x), max(0, screen_w - width))
        y = min(max(0, y), max(0, screen_h - height - 40))
        self.top.geometry(f"+{x}+{y}")
