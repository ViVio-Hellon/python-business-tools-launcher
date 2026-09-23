"""管理者パスワードの入力欄

流れ (いつ尋ねて、何回まで許すか) は `admin_lock` が持つ。ここは
尋ね方と知らせ方を tkinter で用意するだけ。

`simpledialog.askstring` は使わない。

* 閉じたあと、**呼び出し元のモーダルが外れる** (設定画面の後ろのバーが
  押せるようになり、設定画面を2枚開けてしまう)
* Python 3.9 では呼び出し元の 50px 下に出る。バーは画面の下にあるので、
  **入力欄が画面の外に出る**
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from typing import Optional

from .. import admin_lock
from . import theme


class _Prompt:
    """伏せ字の1行入力。閉じるまで戻らない。"""

    def __init__(self, parent: tk.Misc, title: str, message: str) -> None:
        self.value: Optional[str] = None
        # 呼び出し元がモーダルなら、閉じたあとで戻す
        previous = parent.grab_current()

        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(bg=theme.BG)
        self.top.resizable(False, False)
        self.top.transient(parent)
        # バーは最前面にいる。負けて後ろに隠れないように
        self.top.attributes("-topmost", True)

        tk.Label(self.top, text=message, bg=theme.BG, fg=theme.FG,
                 font=theme.FONT, justify="left", anchor="w",
                 wraplength=380).pack(fill="x", padx=16, pady=(14, 6))
        # 横から画面を見られても分からないよう、伏せ字にする
        self.var = tk.StringVar()
        entry = tk.Entry(self.top, textvariable=self.var, show="*",
                         width=30, font=theme.FONT)
        entry.pack(fill="x", padx=16)

        buttons = tk.Frame(self.top, bg=theme.BG)
        buttons.pack(fill="x", padx=16, pady=12)
        tk.Button(buttons, text="キャンセル", command=self.cancel,
                  bg=theme.BUTTON_BG, fg=theme.MUTED, relief="flat", bd=0,
                  padx=14, pady=5, font=theme.FONT,
                  cursor="hand2").pack(side="right")
        tk.Button(buttons, text="OK", command=self.ok,
                  bg=theme.BUTTON_CURRENT, fg=theme.FG, relief="flat", bd=0,
                  padx=20, pady=5, font=theme.FONT_BOLD,
                  cursor="hand2").pack(side="right", padx=(0, 8))

        self.top.bind("<Return>", lambda _e: self.ok())
        self.top.bind("<Escape>", lambda _e: self.cancel())
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)

        self._center_on_screen()
        entry.focus_set()
        self.top.grab_set()
        self.top.wait_window()

        if previous is not None:
            try:
                previous.grab_set()
            except tk.TclError:
                pass                          # 呼び出し元がもう無い

    def _center_on_screen(self) -> None:
        """画面の中央やや上。バーの位置 (画面の下) に引きずられない。"""
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        x = max(0, (self.top.winfo_screenwidth() - width) // 2)
        y = max(0, (self.top.winfo_screenheight() - height) // 3)
        self.top.geometry(f"+{x}+{y}")

    def ok(self) -> None:
        self.value = self.var.get()
        self.top.destroy()

    def cancel(self) -> None:
        self.value = None
        self.top.destroy()


def _asker(parent: tk.Misc):
    def ask(title: str, message: str) -> Optional[str]:
        return _Prompt(parent, title, message).value
    return ask


def _teller(parent: tk.Misc):
    def tell(kind: str, title: str, message: str) -> None:
        if kind == "error":
            messagebox.showerror(title, message, parent=parent)
        else:
            messagebox.showinfo(title, message, parent=parent)
    return tell


def unlock(parent: tk.Misc) -> bool:
    """［設定］を開いてよいか。パスワードがまだ無ければ決めてもらう。"""
    return admin_lock.unlock(_asker(parent), _teller(parent))


def change(parent: tk.Misc) -> bool:
    """パスワードを変える。［設定］を開けている人だけが呼べる。"""
    return admin_lock.choose(_asker(parent), _teller(parent))
