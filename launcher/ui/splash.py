"""ランチャー自身の起動中に出す窓

    ┌─────────────────────────────────────┐
    │ 業務ツール統合ランチャー v1.1.0      │
    │ 起動しています… (3秒)                │
    │  ✓ 二重起動していないか確かめています  │
    │  ✓ 設定を読み込んでいます              │
    │  ▶ 動いているツールを探しています      │
    │  ・ バーを準備しています               │
    │  [■■□□□□□□□ (動き続ける) ]          │
    └─────────────────────────────────────┘

下ごしらえ (`boot.run`) は**別スレッド**で回し、この窓はメインスレッドで
動き続ける。同じスレッドで回すと、遅い段のあいだ窓が固まり、
「応答なし」に見える。

別スレッドは tkinter に触らない。段の番号をキューに入れるだけで、
窓が `after()` で取り出して描く。
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Sequence

from .. import app_config
from . import theme

POLL_MS = 60

_DONE = ("✓", "#4caf82")
_ACTIVE = ("▶", "#d79b00")
_PENDING = ("・", theme.MUTED)


class Splash:
    """起動中の窓。`run()` が下ごしらえの終わりまで窓を出し続ける。"""

    def __init__(self, steps: Sequence[str]) -> None:
        self.steps = list(steps)
        self.root = tk.Tk()
        root = self.root
        root.title(f"{app_config.display_name()} {app_config.version_label()}")
        root.configure(bg=theme.BG)
        root.resizable(False, False)
        root.attributes("-topmost", True)
        # 途中で閉じさせない。下ごしらえを中断すると、ロックだけ残る
        root.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(root, text=f"{app_config.display_name()}  "
                            f"{app_config.version_label()}",
                 bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD,
                 anchor="w").pack(fill="x", padx=18, pady=(16, 0))
        self.headline = tk.Label(root, text="起動しています…", bg=theme.BG,
                                 fg=theme.MUTED, font=theme.FONT_SMALL,
                                 anchor="w")
        self.headline.pack(fill="x", padx=18, pady=(0, 8))

        self.rows: list[tuple[tk.Label, tk.Label]] = []
        frame = tk.Frame(root, bg=theme.BG)
        frame.pack(fill="x", padx=18)
        for text in self.steps:
            row = tk.Frame(frame, bg=theme.BG)
            row.pack(fill="x", pady=1)
            mark = tk.Label(row, width=2, bg=theme.BG, font=theme.FONT_BOLD)
            mark.pack(side="left")
            label = tk.Label(row, text=text, bg=theme.BG, anchor="w",
                             font=theme.FONT)
            label.pack(side="left")
            self.rows.append((mark, label))

        # 何秒かかるかは端末しだいで分からない。割合ではなく、
        # **動き続ける**棒で「止まっていない」ことだけ伝える
        self.bar = ttk.Progressbar(root, mode="indeterminate", length=360)
        self.bar.pack(fill="x", padx=18, pady=(12, 16))
        self.bar.start(15)

        self._paint(-1)
        self._center()
        root.update()                 # 下ごしらえの前に、まず窓を見せる

    # --------------------------------------------------------------
    def run(self, work: Callable[[Callable[[int], None]], Any]) -> Any:
        """`work(report)` を別スレッドで回し、終わったら窓を閉じて結果を返す。

        `work` が投げた例外は、窓を閉じてからこちらで投げ直す。
        """
        steps: queue.Queue = queue.Queue()
        outcome: dict[str, Any] = {}
        started = time.monotonic()

        def worker() -> None:
            try:
                outcome["value"] = work(steps.put)
            except BaseException as exc:          # noqa: BLE001 - 投げ直す
                outcome["error"] = exc

        thread = threading.Thread(target=worker, name="boot", daemon=True)
        thread.start()

        def poll() -> None:
            try:
                while True:
                    self._paint(steps.get_nowait())
            except queue.Empty:
                pass
            elapsed = int(time.monotonic() - started)
            if elapsed >= 1:
                self.headline.configure(text=f"起動しています… ({elapsed}秒)")
            if thread.is_alive():
                self.root.after(POLL_MS, poll)
            else:
                self.root.quit()

        self.root.after(POLL_MS, poll)
        self.root.mainloop()
        self.close()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def close(self) -> None:
        try:
            self.bar.stop()
            self.root.destroy()
        except tk.TclError:
            pass

    # --------------------------------------------------------------
    def _paint(self, current: int) -> None:
        for index, (mark, label) in enumerate(self.rows):
            if index < current:
                symbol, color = _DONE
            elif index == current:
                symbol, color = _ACTIVE
            else:
                symbol, color = _PENDING
            mark.configure(text=symbol, fg=color)
            label.configure(
                fg=theme.FG if index <= current else theme.MUTED,
                font=theme.FONT_BOLD if index == current else theme.FONT)

    def _center(self) -> None:
        """画面の中央やや上。バーが最初に出る場所 (中央) の少し上。"""
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"+{x}+{y}")


def run_with_splash(steps: Sequence[str],
                    work: Callable[[Callable[[int], None]], Any]) -> Any:
    """起動中の窓を出しながら `work` を回す。窓を出せなければ、そのまま回す。"""
    try:
        splash = Splash(steps)
    except tk.TclError:
        return work(lambda step: None)
    return splash.run(work)
