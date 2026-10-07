"""起動・停止の進み具合を出す窓

ツールが立ち上がるまでの数十秒、バーの1行だけでは「何も起きていない」
ように見える。画面の中央に、**いまどの段にいて、何が済んだか**を出す。

    ┌──────────────────────────────────────┐
    │ 看板を起動しています                   │
    │  ✓ 看板の起動ファイルを実行する        │
    │  ▶ 看板の準備ができるのを待つ           │
    │      アプリを準備中                     │
    │  ・ 看板の画面を開く                    │
    │  [■■■□□□□□□□□□ (動き続ける) ]         │
    │  12秒 (最大90秒まで待ちます)    [隠す]  │
    └──────────────────────────────────────┘

* 忙しくなって少し経ってから出す (すぐ終わる操作でちらつかせない)
* 起動が終わる・失敗すると自分で閉じる (失敗の理由はバーの［詳細］)
* ［隠す］を押したら、その操作のあいだは出し直さない。ただし、起動の最中に
  **そのツールのボタンがもう一度押されたら**出し直して前に出す (`reveal`)

中身は `startup_progress` が組み立てる。ここは描くだけ。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from ..startup_progress import ACTIVE, DONE, ProgressTracker, ProgressView
from . import theme

# 忙しくなってから窓を出すまで (ミリ秒)。すぐ終わる操作で
# 一瞬だけ窓が出て消える、を避ける
SHOW_DELAY_MS = 400

# 経過秒を進める間隔 (ミリ秒)。終了を待つあいだは通知が来ないので、
# こちらで時計を回す
TICK_MS = 500

_MARKS = {DONE: ("✓", "#4caf82"), ACTIVE: ("▶", "#d79b00")}
_PENDING_MARK = ("・", theme.MUTED)


class ProgressWindow:
    """バーから状態を受け取り、必要なあいだだけ窓を出す。"""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.tracker = ProgressTracker()
        self.top: Optional[tk.Toplevel] = None
        self._view: Optional[ProgressView] = None
        self._show_job = None
        self._tick_job = None
        self._dismissed = False

    # --------------------------------------------------------------
    def update(self, status) -> None:
        """状態の通知を1つずつ受け取る (まとめて最後だけ、ではなく)。"""
        view = self.tracker.update(status)
        if view is None:
            self._dismissed = False
            self._close()
            return
        self._view = view
        if self._dismissed:
            return
        if self.top is not None:
            self._draw(view)
        elif self._show_job is None:
            self._show_job = self.root.after(SHOW_DELAY_MS, self._open)

    # --------------------------------------------------------------
    def _open(self) -> None:
        self._show_job = None
        if self._view is None or self._dismissed or self.top is not None:
            return
        top = tk.Toplevel(self.root)
        self.top = top
        top.title("起動しています")
        top.configure(bg=theme.BG)
        top.resizable(False, False)
        # バーは最前面にいる。この窓も負けて後ろに隠れないように
        top.attributes("-topmost", True)
        top.protocol("WM_DELETE_WINDOW", self.dismiss)

        self.title_label = tk.Label(top, bg=theme.BG, fg=theme.FG,
                                    font=theme.FONT_BOLD, anchor="w")
        self.title_label.pack(fill="x", padx=18, pady=(16, 8))
        self.steps_frame = tk.Frame(top, bg=theme.BG)
        self.steps_frame.pack(fill="x", padx=18)

        # 中身が分からない待ち (ツールの準備) なので、割合ではなく
        # **動き続ける**棒にする。止まっていないことが伝わればよい
        self.bar = ttk.Progressbar(top, mode="indeterminate", length=360)
        self.bar.pack(fill="x", padx=18, pady=(12, 4))
        self.bar.start(15)

        bottom = tk.Frame(top, bg=theme.BG)
        bottom.pack(fill="x", padx=18, pady=(0, 14))
        self.elapsed_label = tk.Label(bottom, bg=theme.BG, fg=theme.MUTED,
                                      font=theme.FONT_SMALL, anchor="w")
        self.elapsed_label.pack(side="left")
        tk.Button(bottom, text="隠す", command=self.dismiss,
                  bg=theme.BUTTON_BG, fg=theme.MUTED, relief="flat", bd=0,
                  padx=12, pady=3, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="right")

        self._draw(self._view)
        self._center()
        self._tick_job = self.root.after(TICK_MS, self._tick)

    def _draw(self, view: ProgressView) -> None:
        if self.top is None:
            return
        self.title_label.configure(text=view.title)
        for child in self.steps_frame.winfo_children():
            child.destroy()
        for step in view.steps:
            mark, color = _MARKS.get(step.state, _PENDING_MARK)
            row = tk.Frame(self.steps_frame, bg=theme.BG)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=mark, width=2, bg=theme.BG, fg=color,
                     font=theme.FONT_BOLD).pack(side="left")
            tk.Label(row, text=step.label, bg=theme.BG,
                     fg=theme.FG if step.state != "pending" else theme.MUTED,
                     font=theme.FONT_BOLD if step.state == ACTIVE else theme.FONT,
                     anchor="w").pack(side="left")
            if step.state == ACTIVE and view.note:
                tk.Label(self.steps_frame, text=f"      {view.note}",
                         bg=theme.BG, fg="#d79b00", font=theme.FONT_SMALL,
                         anchor="w").pack(fill="x")
        self.elapsed_label.configure(text=view.elapsed_text)

    def _tick(self) -> None:
        self._tick_job = None
        if self.top is None:
            return
        view = self.tracker.view()
        if view is not None:
            self._view = view
            self.elapsed_label.configure(text=view.elapsed_text)
        self._tick_job = self.root.after(TICK_MS, self._tick)

    def _center(self) -> None:
        """画面の中央やや上。バー (画面の下) とも、開く画面とも重ねない。"""
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        x = max(0, (self.top.winfo_screenwidth() - width) // 2)
        y = max(0, (self.top.winfo_screenheight() - height) // 3)
        # **位置だけ決めて、大きさは中身に任せる。** 段の下に準備の段階
        # (「アプリを準備中」) が増えると背が伸びる。大きさまで決めると、
        # 下の経過秒と［隠す］が切れる
        self.top.geometry(f"+{x}+{y}")

    # --------------------------------------------------------------
    def reveal(self, app_id: str) -> bool:
        """起動の最中に、そのツールのボタンがもう一度押された。

        ［隠す］で隠していても**出し直して前に出す** (押したのに何も
        起きないように見せない)。そのツールの進み具合を出していなければ
        何もしない。出したら真。
        """
        if not app_id or self.tracker.watching != app_id or self._view is None:
            return False
        self._dismissed = False
        if self.top is None:
            if self._show_job is not None:
                try:
                    self.root.after_cancel(self._show_job)
                except tk.TclError:
                    pass
                self._show_job = None
            self._open()
        if self.top is None:
            return False
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:
            return False
        return True

    def dismiss(self) -> None:
        """［隠す］。起動は続ける。この操作のあいだは出し直さない。"""
        self._dismissed = True
        self._close()

    def _close(self) -> None:
        for job in (self._show_job, self._tick_job):
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self._show_job = self._tick_job = None
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None
