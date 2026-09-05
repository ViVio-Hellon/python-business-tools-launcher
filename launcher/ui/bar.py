"""常駐ランチャーバー (要件定義書 §5 / §6)

画面下部に置く細長い窓。業務ツールより大きな画面を占有しない。

    [日報] [カレンダー] [看板] [総合]   ● 現在：日報      [設定] [終了]

ツールを切り替えてもこの窓は残る (要件定義書 §5.2)。

【スレッドの約束】
`ToolManager` は起動・停止を別スレッドで行い、そこから状態を通知して
くる。**tkinter を別スレッドから触ってはいけない**ので、通知はいったん
キューへ入れ、`after()` で回している画面側のループが取り出して描く。
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import messagebox
from typing import Optional

from .. import app_config, tool_registry
from ..logging_utils import get_logger
from . import theme
from .settings_dialog import SettingsDialog

log = get_logger("ui.bar")

# 画面側のループが状態を取りに行く間隔 (ミリ秒)。
# 起動中の経過表示がなめらかに見える程度でよい
DRAIN_MS = 120


class LauncherBar:
    """常駐する小型GUI本体。"""

    def __init__(self, manager) -> None:
        self.manager = manager
        self.queue: queue.Queue = queue.Queue()
        # 別スレッドからの通知はキューへ置くだけにする。
        # **tkinter を別スレッドから触ってはいけない**
        manager.set_status_callback(self.queue.put)

        self.root = tk.Tk()
        self.root.title(app_config.display_name())
        self.buttons: dict[str, tk.Button] = {}
        # 設定済みかどうかは、ボタンを作るときに控えておく。
        # 塗り直しは状態が届くたびに走るので、そのつど設定DBを
        # 読みに行かせない
        self._configured: dict[str, bool] = {}
        self._last_detail = ""
        self._current_app_id = ""

        self._build()
        self._place_at_bottom()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(DRAIN_MS, self._drain)
        self.root.after(self._poll_interval_ms(), self._poll_health)

    # --------------------------------------------------------------
    # 組み立て
    # --------------------------------------------------------------
    def _build(self) -> None:
        self.root.configure(bg=theme.BG)
        # 常に手前に置く。業務画面の裏に回ると、切り替えのたびに
        # 探すことになる (要件定義書 §5.1「常駐する小型GUI」)
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)

        frame = tk.Frame(self.root, bg=theme.BG)
        frame.pack(fill="both", expand=True, padx=theme.PAD, pady=theme.PAD)

        # --- ツールのボタン (要件定義書 §6) ---
        self.tool_frame = tk.Frame(frame, bg=theme.BG)
        self.tool_frame.pack(side="left")
        self._build_tool_buttons()

        # --- 右側の操作 ---
        right = tk.Frame(frame, bg=theme.BG)
        right.pack(side="right")
        self._small_button(right, "終了", self.on_close).pack(side="right",
                                                              padx=(4, 0))
        self._small_button(right, "設定", self.open_settings).pack(side="right",
                                                                   padx=(4, 0))
        # ツールだけ止めてランチャーは残す (要件定義書 §4「ツール停止」)。
        # 動いているときだけ見せる
        self.stop_button = self._small_button(right, "ツール停止",
                                              self.on_stop_tool)
        self.detail_button = self._small_button(right, "詳細", self.show_detail)
        # 出せる詳細があるときだけ見せる。ふだんは畳んでおく

        # --- 状態表示 (要件定義書 §5.2) ---
        status = tk.Frame(frame, bg=theme.BG)
        status.pack(side="left", padx=(12, 0))
        self.lamp = tk.Canvas(status, width=12, height=12, bg=theme.BG,
                              highlightthickness=0)
        self.lamp.pack(side="left")
        self._lamp_dot = self.lamp.create_oval(2, 2, 11, 11,
                                               fill=theme.STATE_COLORS["idle"],
                                               outline="")
        self.status_label = tk.Label(status, text="起動していません",
                                     bg=theme.BG, fg=theme.FG,
                                     font=theme.FONT_BOLD, anchor="w")
        self.status_label.pack(side="left", padx=(6, 0))

    def _build_tool_buttons(self) -> None:
        """設定にあるツールぶんのボタンを作り直す。

        設定画面で並びや表示名が変わったあとにも呼ぶ。
        """
        for child in self.tool_frame.winfo_children():
            child.destroy()
        self.buttons.clear()
        self._configured.clear()

        tools = tool_registry.all_tools()
        if not tools:
            tk.Label(self.tool_frame, text="ツールが登録されていません",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT).pack(side="left")
            return

        for tool in tools:
            button = tk.Button(
                self.tool_frame, text=tool.display_name,
                command=lambda app_id=tool.app_id: self.on_select(app_id),
                bg=theme.BUTTON_BG, fg=theme.FG,
                activebackground=theme.BUTTON_ACTIVE, activeforeground=theme.FG,
                relief="flat", bd=0, padx=14, pady=6, font=theme.FONT,
                cursor="hand2")
            button.pack(side="left", padx=(0, 4))
            self.buttons[tool.app_id] = button
            self._configured[tool.app_id] = tool.is_configured
            if not tool.is_configured:
                # 押しても起動しないことを、押す前に見せる
                button.configure(bg=theme.BUTTON_UNSET)
        self._paint_buttons(self._current_app_id)

    def _small_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=theme.BUTTON_BG, fg=theme.MUTED,
                         activebackground=theme.BUTTON_ACTIVE,
                         activeforeground=theme.FG,
                         relief="flat", bd=0, padx=10, pady=5,
                         font=theme.FONT_SMALL, cursor="hand2")

    def _place_at_bottom(self) -> None:
        """画面下部に細長く置く (要件定義書 §5.1)。

        幅は画面いっぱいにしない。**業務画面を隠さない**ことが目的なので、
        必要なぶんだけ取り、下端から少し上げてタスクバーを避ける。
        """
        self.root.update_idletasks()
        height = int(app_config.ui_setting("bar_height"))
        margin = int(app_config.ui_setting("bottom_margin"))

        width = max(self.root.winfo_reqwidth(), 520)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - 40)

        x = (screen_w - width) // 2
        y = max(0, screen_h - height - margin)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    # --------------------------------------------------------------
    # 操作
    # --------------------------------------------------------------
    def on_select(self, app_id: str) -> None:
        """ツールのボタンが押された (要件定義書 §6)。"""
        if self.manager.status.busy:
            # 起動・停止の途中。押し直しは受け付けるが、何が起きているかは
            # 出しておく。無反応に見えるのがいちばん困る
            log.info("処理中に %s が押されました", app_id)
        self.manager.select(app_id)

    def on_stop_tool(self) -> None:
        """動いているツールだけを止める。ランチャーは残る。"""
        running = self.manager.current
        if running is None:
            return
        if not messagebox.askyesno(
                app_config.display_name(),
                f"{running.display_name} を終了しますか?",
                parent=self.root):
            return
        self.manager.stop_current()

    def open_settings(self) -> None:
        """設定画面 (要件定義書 §13)。"""
        dialog = SettingsDialog(self.root)
        if dialog.saved:
            self._build_tool_buttons()

    def show_detail(self) -> None:
        if not self._last_detail:
            return
        messagebox.showinfo(app_config.display_name(), self._last_detail,
                            parent=self.root)

    def on_close(self) -> None:
        """「終了」またはウィンドウを閉じたとき。

        **業務ツールまで止めるかを尋ねる。** ブラウザーを閉じることと
        バックエンドを止めることは別 (要件定義書 §11) なので、ランチャー
        だけ終わらせたい場面がある。
        """
        running = self.manager.current
        stop_tools = False
        if running is not None:
            answer = messagebox.askyesnocancel(
                app_config.display_name(),
                f"{running.display_name} が動いています。\n\n"
                "「はい」  … ツールも終了してランチャーを閉じる\n"
                "「いいえ」… ツールは動かしたままランチャーだけ閉じる\n"
                "「キャンセル」… 閉じない",
                parent=self.root)
            if answer is None:
                return
            stop_tools = bool(answer)

        log.info("ランチャーを終了します (ツールも停止=%s)", stop_tools)
        if stop_tools:
            self._set_status_text("stopping", "終了しています...")
            self.root.update_idletasks()

        if not self.manager.shutdown(stop_tools=stop_tools):
            # 止められなかった。ほとんどは実行中の処理があるとき。
            # **黙って閉じない** ── 止めたつもりで残るのがいちばん困る
            detail = self.manager.status.detail or "終了できませんでした。"
            if not messagebox.askyesno(
                    app_config.display_name(),
                    f"{detail}\n\n中断して終了しますか?\n"
                    "「いいえ」を選ぶと、ツールを動かしたままにします。",
                    parent=self.root):
                self._render(self.manager.status)
                return
            if not self.manager.shutdown(stop_tools=True, force=True):
                messagebox.showerror(
                    app_config.display_name(),
                    f"{self.manager.status.detail}\n\n"
                    f"stop.bat --force を実行してください。",
                    parent=self.root)
                self._render(self.manager.status)
                return
        self.root.destroy()

    # --------------------------------------------------------------
    # 画面の更新
    # --------------------------------------------------------------
    def _drain(self) -> None:
        """別スレッドから届いた状態を描く。"""
        status = None
        try:
            while True:
                status = self.queue.get_nowait()
        except queue.Empty:
            pass
        if status is not None:
            self._render(status)
        self.root.after(DRAIN_MS, self._drain)

    def _render(self, status) -> None:
        message = status.message
        if status.state.value == "starting" and status.elapsed >= 1:
            # 経過を出す。「進んでいる」ことが分かればよいので秒だけ
            # (基盤仕様書 2.2)
            message = f"{message} ({status.elapsed:.0f}秒)"
        self._set_status_text(status.state.value, message)

        self._last_detail = status.detail
        if status.detail:
            self.detail_button.pack(side="right", padx=(4, 0))
        else:
            self.detail_button.pack_forget()

        self._current_app_id = (status.app_id
                                if status.state.value == "running" else "")
        self._paint_buttons(self._current_app_id)

        if self.manager.current is not None:
            self.stop_button.pack(side="right", padx=(4, 0))
        else:
            self.stop_button.pack_forget()

    def _set_status_text(self, state: str, message: str) -> None:
        self.status_label.configure(text=message)
        self.lamp.itemconfigure(
            self._lamp_dot,
            fill=theme.STATE_COLORS.get(state, theme.MUTED))

    def _paint_buttons(self, current_app_id: str) -> None:
        """いま使っているツールのボタンを目立たせる (要件定義書 §5.2)。"""
        for app_id, button in self.buttons.items():
            if app_id == current_app_id:
                button.configure(bg=theme.BUTTON_CURRENT)
                continue
            button.configure(bg=theme.BUTTON_BG if self._configured.get(app_id)
                             else theme.BUTTON_UNSET)

    # --------------------------------------------------------------
    # 生存監視 (基盤仕様書 2.9)
    # --------------------------------------------------------------
    def _poll_interval_ms(self) -> int:
        return int(app_config.ui_setting("health_poll_seconds")) * 1000

    def _poll_health(self) -> None:
        """ツールがまだ応答しているか、定期的に確かめる。

        **ブラウザーを閉じたこととツールが落ちたことは別**。ここが見て
        いるのはツール側だけで、ブラウザーの有無は問わない
        (要件定義書 §11)。
        """
        try:
            self.manager.poll_health()
        except Exception:                     # noqa: BLE001 - 監視で画面を止めない
            log.exception("生存監視で失敗しました")
        self.root.after(self._poll_interval_ms(), self._poll_health)

    # --------------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()


def run(manager) -> None:
    """バーを出して常駐する。"""
    LauncherBar(manager).run()
