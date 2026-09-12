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
from . import geometry, theme
from .geometry import POSITION_KEY
from .settings_dialog import SettingsDialog
from .version_dialog import VersionDialog

log = get_logger("ui.bar")

# 画面側のループが状態を取りに行く間隔 (ミリ秒)。
# 起動中の経過表示がなめらかに見える程度でよい
DRAIN_MS = 120

# 動かしたあと、位置を書くまでの待ち (ミリ秒)。
# ドラッグ中は `<Configure>` が何十回も飛ぶので、止まってから書く
MOVE_SAVE_MS = 600

# 自分で動かしたあと、`<Configure>` を無視し続ける長さ (ミリ秒)。
# `geometry()` の通知は少し遅れて届くので、余韻をもって戻す
PROGRAMMATIC_TAIL_MS = 250

# 移動の1こまの長さ (ミリ秒)。60fps 相当
FRAME_MS = 16



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
        self._save_handle = None
        # 自分で動かしている最中か。**利用者のドラッグと区別する印**
        self._programmatic = False
        self._programmatic_handle = None
        # 利用者が手で置いたか。置いていれば自動の移動をやめる ──
        # **利用者が決めた場所がいちばん強い**
        self._manual = self._saved_position() is not None
        self._anchor = ""

        self._build()
        self._place()
        # 置いたあとで見張り始める。置いた瞬間の `<Configure>` を
        # 「利用者が動かした」と取り違えない
        self.root.bind("<Configure>", self._on_configure)

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
        # 版のバッジ。**押すとバージョン情報が出る** ── 「どれが入って
        # いるか」を調べたい人がいちばん最初に見る場所に置く。
        # ボタンを1つ増やさずに済むよう、バッジ自体を入口にする
        version = tk.Button(
            right, text=app_config.version_label(), command=self.show_version,
            bg=theme.BG, fg=theme.MUTED, activebackground=theme.BUTTON_BG,
            activeforeground=theme.FG, relief="flat", bd=0,
            padx=8, pady=5, font=theme.FONT_SMALL, cursor="hand2")
        version.pack(side="right", padx=(4, 0))
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

    def _content_width(self) -> int:
        """**すべての操作が出ている状態**での必要幅。

        「ツール停止」と「詳細」はふだん畳んでいるが、畳んだ幅で窓を
        決めてはいけない。窓は `resizable(False, False)` で広がらないので、
        あとで出てきたぶんが**右端で切れる**。ツールが動き出すたびに
        起きるので、最初から場所を取っておく。
        """
        shown = [b for b in (self.stop_button, self.detail_button)
                 if not b.winfo_ismapped()]
        for button in shown:
            button.pack(side="right", padx=(4, 0))
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        for button in shown:
            button.pack_forget()
        self.root.update_idletasks()
        return width

    def _placement(self, *, anchor: str, saved=None):
        return geometry.compute(
            content_width=self._content_width(),
            screen_width=self.root.winfo_screenwidth(),
            screen_height=self.root.winfo_screenheight(),
            bar_height=int(app_config.ui_setting("bar_height")),
            bottom_margin=int(app_config.ui_setting("bottom_margin")),
            edge_margin=int(app_config.ui_setting("edge_margin")),
            anchor=anchor, saved=saved)

    def _anchor_for(self, status) -> str:
        """いまの状態に合う置き場所。

        何も選んでいないあいだは**ランチャーが主役**なので画面中央 ──
        起動直後に探さずに見つかる。ツールを選んだら**業務画面が主役**に
        なるので隅へ寄る (要件定義書 §5.2「業務ツールの操作をできるだけ
        邪魔しない」)。
        """
        active = (self.manager.current is not None
                  or status.state.value in ("starting", "stopping"))
        key = "position_active" if active else "position_idle"
        return geometry.normalize_anchor(str(app_config.ui_setting(key)))

    def _place(self) -> None:
        """最初の置き場所を決める (要件定義書 §5.1)。

        幅は画面いっぱいにしない。**業務画面を隠さない**ことが目的なので、
        必要なぶんだけ取る。利用者が動かしてあれば、その位置を使う。

        すでに動いているツールを引き継いだ状態で始まることがあるので、
        中央と決め打ちにせず、いまの状態から決める。
        """
        self._anchor = self._anchor_for(self.manager.status)
        placement = self._placement(anchor=self._anchor,
                                    saved=self._saved_position())
        self._apply_geometry(placement.as_geometry())
        log.info("バーを置きました: %s (%s%s)", placement.as_geometry(),
                 self._anchor, " / 手動" if self._manual else "")

    def _resize_to_content(self) -> None:
        """ボタンが増減したあと、幅だけ取り直す。

        設定画面で5個目のツールを足したときに呼ぶ。**位置は動かさない** ──
        利用者が置いた場所から勝手に飛ぶと、探し直すことになる。
        """
        placement = self._placement(
            anchor=self._anchor or "bottom_center",
            saved=(self.root.winfo_x(), self.root.winfo_y()))
        self._apply_geometry(placement.as_geometry())

    # --------------------------------------------------------------
    # 状態に合わせて寄る
    # --------------------------------------------------------------
    def _follow_state(self, status) -> None:
        """状態が変わったら置き場所も合わせる。

        **利用者が手で置いていたら動かさない。** 自動の移動が利用者の
        置き場所を上書きすると、動かすたびに戻されることになる。
        """
        if self._manual:
            return
        anchor = self._anchor_for(status)
        if anchor == self._anchor:
            return
        log.info("バーを %s へ移します", anchor)
        self._anchor = anchor
        self._move_to(self._placement(anchor=anchor))

    def _move_to(self, placement) -> None:
        """新しい置き場所へ移す。滑らせて、どこへ行ったか分かるようにする。"""
        start = (self.root.winfo_x(), self.root.winfo_y())
        target = (placement.x, placement.y)
        # 幅は先に合わせる。動かしながら幅も変えると途中の形が崩れて見える
        self._apply_geometry(
            f"{placement.width}x{placement.height}+{start[0]}+{start[1]}")

        duration = int(app_config.ui_setting("move_animation_ms"))
        if duration <= 0 or start == target:
            self._apply_geometry(placement.as_geometry())
            return
        self._slide(start, target, placement, max(1, duration // FRAME_MS))

    def _slide(self, start, target, placement, steps: int, index: int = 1) -> None:
        """1こまずつ動かす。"""
        ratio = min(1.0, index / steps)
        # 終わりに向かってゆるめる。等速だと機械的に見える
        eased = 1 - (1 - ratio) ** 3
        x = round(start[0] + (target[0] - start[0]) * eased)
        y = round(start[1] + (target[1] - start[1]) * eased)
        self._apply_geometry(f"{placement.width}x{placement.height}+{x}+{y}")
        if index < steps:
            self.root.after(FRAME_MS, self._slide, start, target, placement,
                            steps, index + 1)

    def _apply_geometry(self, text: str) -> None:
        """窓の位置と大きさを変える。**自分で動かしたぶんは覚えない。**

        `geometry()` を呼ぶと `<Configure>` が飛ぶ。区別しないと、自動で
        寄せた位置を「利用者が動かした」と取り違え、以後の自動移動が
        止まってしまう。
        """
        self._programmatic = True
        if self._programmatic_handle is not None:
            self.root.after_cancel(self._programmatic_handle)
        self.root.geometry(text)
        self._programmatic_handle = self.root.after(
            PROGRAMMATIC_TAIL_MS, self._clear_programmatic)

    def _clear_programmatic(self) -> None:
        self._programmatic_handle = None
        self._programmatic = False

    # --------------------------------------------------------------
    # 動かした位置を覚える
    # --------------------------------------------------------------
    def _saved_position(self):
        try:
            return geometry.parse_saved(tool_registry.get_pc_setting(POSITION_KEY))
        except Exception:                     # noqa: BLE001 - 位置で起動を止めない
            log.warning("保存した位置を読めませんでした", exc_info=True)
            return None

    def _on_configure(self, event) -> None:
        """利用者に動かされたら、少し待ってから覚える。"""
        if event.widget is not self.root:
            return                            # 中の部品の変化は関係ない
        if self._programmatic:
            return                            # 自分で動かしたぶんは覚えない
        if self._save_handle is not None:
            self.root.after_cancel(self._save_handle)
        self._save_handle = self.root.after(MOVE_SAVE_MS, self._save_position)

    def _save_position(self) -> None:
        """手で置かれた場所を覚え、以後の自動移動をやめる。"""
        self._save_handle = None
        try:
            tool_registry.set_pc_setting(
                POSITION_KEY,
                geometry.format_saved(self.root.winfo_x(), self.root.winfo_y()))
        except Exception:                     # noqa: BLE001 - 覚えられなくても続ける
            log.warning("位置を保存できませんでした", exc_info=True)
            return
        if not self._manual:
            log.info("手で置かれたので、自動の移動をやめます")
        self._manual = True

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

    def show_version(self) -> None:
        """バージョン情報 (どの版が入っていて、どの版が動いているか)。"""
        VersionDialog(self.root)

    def open_settings(self) -> None:
        """設定画面 (要件定義書 §13)。"""
        dialog = SettingsDialog(self.root)
        if dialog.saved:
            self._build_tool_buttons()
            # ツールが増えたぶん、窓を広げないとボタンが切れる
            self._resize_to_content()
            # 設定画面で「位置を既定に戻す」が押されているかもしれない
            self._manual = self._saved_position() is not None
            if not self._manual:
                self._anchor = ""             # 次の状態変化で寄せ直す
                self._follow_state(self.manager.status)

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
        elif (status.state.value == "running" and status.browser_managed
                and not status.browser_open):
            # **画面を閉じると、ツールはまもなく自分から終わる。**
            # 各ツールは「誰も見ていなければ終了する」見張りを持っていて、
            # 画面の心拍が途切れると数秒で落ちる。「画面だけ閉じた状態が
            # 続く」かのように見せると、実態と食い違う
            message = f"{message}（画面を閉じました・まもなく終了します）"
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

        self._follow_state(status)

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
