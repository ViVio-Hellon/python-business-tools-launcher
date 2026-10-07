"""常駐ランチャーバー (要件定義書 §5 / §6)

画面下部に置く細長い窓。業務ツールより大きな画面を占有しない。

    [● 日報] [● 看板] [カレンダー] [総合]   ● 動作中：日報、看板   [設定] [終了]

ツールは**同時に使える**。動いているツールのボタンは緑で ● が付く。
起動直後は画面中央に出て、ツールを起動すると［設定］で選んだ場所へ寄る。

【スレッドの約束】
`ToolManager` は起動・停止を別スレッドで行い、そこから状態を通知して
くる。**tkinter を別スレッドから触ってはいけない**ので、通知はいったん
キューへ入れ、`after()` で回している画面側のループが取り出して描く。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Optional

from .. import app_config, fileprobe, logging_utils, tool_registry, trace
from ..logging_utils import get_logger
from . import geometry, password, theme
from .geometry import POSITION_KEY
from .texts import (CLOSE_CANCEL, CLOSE_DEFAULT, CLOSE_STOP, close_choice,
                    close_question, fit_text, force_question)
from .progress_window import ProgressWindow
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
        # 画面の処理で拾われなかった例外を、障害記録にする。既定では
        # 標準エラーへ出るだけで、pythonw では**誰にも見えない**
        self.root.report_callback_exception = self._on_ui_error
        # 起動・切り替えの進み具合は別の窓に出す。バーの1行だけでは、
        # ツールが立ち上がるまでの数十秒が「何も起きていない」ように見える
        self.progress = ProgressWindow(self.root)
        # **タスクバーと Alt+Tab に版を出す。** バーのバッジは画面を
        # 見ている人にしか届かないが、タイトルは窓の一覧にも出るので、
        # 「どれが動いているか」を離れた場所からも確かめられる
        self.root.title(f"{app_config.display_name()} "
                        f"{app_config.version_label()}")
        self.buttons: dict[str, tk.Button] = {}
        # 設定済みかどうかは、ボタンを作るときに控えておく。
        # 塗り直しは状態が届くたびに走るので、そのつど設定DBを
        # 読みに行かせない
        self._configured: dict[str, bool] = {}
        # 表示名。あふれたぶんを［▼］のメニューへ出すときに要る
        self._names: dict[str, str] = {}
        # 並び順。あふれ判定は番号で行うので、対応を持っておく
        self._order: list[str] = []
        self._last_detail = ""
        # ［詳細］から開ける障害記録
        self._last_incident = ""
        # 最後に選ばれたツール。［▼］に隠さない
        self._focus_id = ""
        # 状態の全文 (バーには「…」で切って出すことがある)
        self._status_full = ""
        # 利用者がツールを選んだか。**選ぶまでは画面中央に居続ける** ──
        # 起動したとき、すでに動いているツールを引き継いでいても
        self._engaged = False
        # 生存監視が回っている最中か (重ねて走らせない)
        self._polling = False
        # 閉じる処理の最中か。**× を続けて押しても2回走らせない**
        self._closing = False
        self._save_handle = None
        # 自分で動かしている最中か。**利用者のドラッグと区別する印**
        self._programmatic = False
        self._programmatic_handle = None
        # 最後に自分で置いた場所。届いた「動いた」が自分のこだまか、
        # 利用者のドラッグかを、**場所で**見分ける
        self._expected_xy = None
        self._forget_untrusted_position()
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
        # 起動の下ごしらえで決まった状態を出す (引き継いだツール・確かめられ
        # ない起動ファイルの案内)。**この窓ができる前に出た知らせ**なので、
        # 待っていても届かない
        self._render(self.manager.status)
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
        # 入りきらないツールの受け皿。バーは折り返さないので、
        # あふれたぶんはここへ入れる (数が増えても細いままにする)
        self.overflow_button = self._tool_button("▼", self.show_overflow)
        self.overflow_button.configure(padx=10)
        self.overflow_menu = tk.Menu(self.root, tearoff=0,
                                     bg=theme.BUTTON_BG, fg=theme.FG,
                                     activebackground=theme.BUTTON_ACTIVE,
                                     activeforeground=theme.FG, bd=0)
        self._hidden: list[str] = []
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
            bg=theme.BG, fg=theme.MUTED, activebackground=theme.UTIL_BG,
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
        # **幅を決めておく。** 決めないと、長い案内で文が途中で切れたり、
        # 右の操作が押し出されたりする。収まらない文は「…」で切り、
        # 全文はマウスを載せたときと［詳細］で出す
        status = tk.Frame(frame, bg=theme.BG, width=theme.STATUS_WIDTH,
                          height=28)
        status.pack(side="left", padx=(12, 0))
        status.pack_propagate(False)
        self.lamp = tk.Canvas(status, width=12, height=12, bg=theme.BG,
                              highlightthickness=0)
        self.lamp.pack(side="left")
        self._lamp_dot = self.lamp.create_oval(2, 2, 11, 11,
                                               fill=theme.STATE_COLORS["idle"],
                                               outline="")
        self.status_label = tk.Label(status, text="起動していません",
                                     bg=theme.BG, fg=theme.FG,
                                     font=theme.FONT_BOLD, anchor="w")
        self.status_label.pack(side="left", padx=(6, 0), fill="x", expand=True)
        self._status_font = tkfont.Font(font=theme.FONT_BOLD)
        self.status_label.bind("<Enter>", self._show_full_status)
        self.status_label.bind("<Leave>", self._hide_full_status)
        self.status_label.bind("<Button-1>", lambda _e: self.show_detail())
        self._tip: Optional[tk.Toplevel] = None

    def _build_tool_buttons(self) -> None:
        """設定にあるツールぶんのボタンを作り直す。

        設定画面で並びや表示名が変わったあとにも呼ぶ。
        """
        for child in self.tool_frame.winfo_children():
            if child is not self.overflow_button:
                child.destroy()
        self.buttons.clear()
        self._configured.clear()
        self._names.clear()
        self._order.clear()
        self._hidden.clear()
        self.overflow_button.pack_forget()

        tools = tool_registry.all_tools()
        if not tools:
            # 出荷時はツールを1つも持たない。何をすればよいかを出す
            tk.Label(self.tool_frame,
                     text="ツールがありません —［設定］→［＋ ツールを追加］で登録",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT).pack(side="left")
            return

        # 起動ファイルは**まとめて同時に**確かめる (全体で数秒まで)。古い
        # 置き場所がつながらない共有フォルダーでも、バーが固まらない
        files = fileprobe.probe_many(t.start_command for t in tools)
        for tool in tools:
            button = self._tool_button(
                tool.display_name,
                lambda app_id=tool.app_id: self.on_select(app_id))
            button.pack(side="left", padx=(0, 4))
            self.buttons[tool.app_id] = button
            self._names[tool.app_id] = tool.display_name
            self._order.append(tool.app_id)
            found = files.get(tool.start_command.strip().strip('"'))
            # 確かめられないものは「未設定」(茶) にしない。**設定はある** ──
            # 押せば、確かめられない理由を出す
            self._configured[tool.app_id] = bool(found) and (found.found
                                                              or found.unknown)
        self._apply_overflow()
        self._paint_buttons(self.manager.status)

    # --------------------------------------------------------------
    # 入りきらないツール
    # --------------------------------------------------------------
    def _apply_overflow(self) -> None:
        """入りきらないツールを［▼］へ回す。

        バーは折り返さず、窓も広がらない。**黙って切れると、押せない
        ツールがあることに気づけない。**
        """
        if not self._order:
            return

        # いったん全部出して、それぞれの幅を測る
        for app_id in self._order:
            self.buttons[app_id].pack(side="left", padx=(0, 4))
        self.overflow_button.pack_forget()
        self.root.update_idletasks()

        budget = self._tool_budget()
        widths = [self.buttons[app_id].winfo_reqwidth() + 4
                  for app_id in self._order]
        must_show = (self._order.index(self._focus_id)
                     if self._focus_id in self._order else None)
        visible, hidden = geometry.fit_buttons(
            widths, budget=budget,
            overflow_width=self.overflow_button.winfo_reqwidth() + 4,
            must_show=must_show)

        self._hidden = [self._order[index] for index in hidden]
        for index, app_id in enumerate(self._order):
            if index in visible:
                self.buttons[app_id].pack(side="left", padx=(0, 4))
            else:
                self.buttons[app_id].pack_forget()
        if self._hidden:
            self.overflow_button.pack(side="left", padx=(0, 4))
            log.info("%d 個のツールを［▼］へ回しました: %s",
                     len(self._hidden),
                     "、".join(self._names[a] for a in self._hidden))

    def _tool_budget(self) -> int:
        """ツールのボタンに使える幅。

        窓全体の上限から、ツール以外(状態表示と右側の操作)が使うぶんを
        引いた残り。
        """
        total = self.root.winfo_reqwidth()
        tools = self.tool_frame.winfo_reqwidth()
        limit = geometry.max_width(self.root.winfo_screenwidth())
        return max(0, limit - max(0, total - tools))

    def show_overflow(self) -> None:
        """［▼］のメニューを出す。"""
        if not self._hidden:
            return
        self.overflow_menu.delete(0, "end")
        running = set(self.manager.status.running_ids)
        for app_id in self._hidden:
            label = self._names.get(app_id, app_id)
            if app_id in running:
                label = f"● {label}（動作中）"
            elif not self._configured.get(app_id):
                label = f"{label}（未設定）"
            self.overflow_menu.add_command(
                label=label,
                command=lambda target=app_id: self.on_select(target))
        try:
            self.overflow_menu.tk_popup(
                self.overflow_button.winfo_rootx(),
                self.overflow_button.winfo_rooty()
                + self.overflow_button.winfo_height())
        finally:
            self.overflow_menu.grab_release()

    def _tool_button(self, text: str, command) -> tk.Button:
        """ツールのボタン。**押すものだと一目で分かる**よう、地より明るく太く。"""
        return tk.Button(self.tool_frame, text=text, command=command,
                         bg=theme.TOOL_BG, fg=theme.FG,
                         activebackground=theme.TOOL_ACTIVE,
                         activeforeground=theme.FG,
                         relief="flat", bd=0, padx=14, pady=5,
                         font=theme.FONT_TOOL, cursor="hand2")

    def _small_button(self, parent: tk.Widget, text: str, command) -> tk.Button:
        """設定・終了などの操作。ツールのボタンより控えめにする。"""
        return tk.Button(parent, text=text, command=command,
                         bg=theme.UTIL_BG, fg=theme.UTIL_FG,
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
        active = self._engaged and bool(
            status.running_ids or status.starting_ids or status.stopping_ids)
        if active:
            return tool_registry.active_bar_position()
        return geometry.normalize_anchor(
            str(app_config.ui_setting("position_idle")), fallback="center")

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
        start = (geometry.parse_geometry_xy(self.root.geometry())
                 or (self.root.winfo_x(), self.root.winfo_y()))
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
        placed = geometry.parse_geometry_xy(text)
        if placed is not None:
            self._expected_xy = placed
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
        """手で置かれた場所を覚え、以後の自動移動をやめる。

        **自分で置いた場所から動いていなければ覚えない。** 時間で見分ける
        だけだと、遅れて届いた知らせ (Windows では窓が出た瞬間などに
        起きる) を「利用者が動かした」と取り違え、以後ずっと中央に
        居座ることになる。
        """
        self._save_handle = None
        actual = geometry.parse_geometry_xy(self.root.geometry())
        if not geometry.moved_by_user(self._expected_xy, actual):
            return
        try:
            tool_registry.set_pc_setting(
                POSITION_KEY, geometry.format_saved(*actual))
        except Exception:                     # noqa: BLE001 - 覚えられなくても続ける
            log.warning("位置を保存できませんでした", exc_info=True)
            return
        if not self._manual:
            log.info("手で置かれたので、自動の移動をやめます: %s", actual)
        self._manual = True
        self._expected_xy = actual

    def _forget_untrusted_position(self) -> None:
        """以前の覚え方で覚えた位置を、1度だけ忘れる。

        以前は、自分で動かしたぶんを手で置いたものと取り違えて覚える
        ことがあった。その位置が残っていると、直したあとも自動で寄らない。
        """
        try:
            if tool_registry.get_pc_setting(geometry.POSITION_RULE_KEY) \
                    == geometry.POSITION_RULE:
                return
            if tool_registry.get_pc_setting(POSITION_KEY):
                tool_registry.clear_pc_setting(POSITION_KEY)
                log.info("以前の版で覚えたバーの位置を忘れます (自動に戻します)")
            tool_registry.set_pc_setting(geometry.POSITION_RULE_KEY,
                                         geometry.POSITION_RULE)
        except Exception:                     # noqa: BLE001 - 位置で起動を止めない
            log.warning("バーの位置の覚え方を確かめられませんでした", exc_info=True)

    # --------------------------------------------------------------
    # 操作
    # --------------------------------------------------------------
    def on_select(self, app_id: str) -> None:
        """ツールのボタンが押された (要件定義書 §6)。

        **ほかのツールは止めない。** 動いていなければ起こし、動いて
        いれば画面を前に出す。
        """
        self._engaged = True
        self._focus_id = app_id
        self.manager.select(app_id)

    def on_stop_tool(self) -> None:
        """［ツール停止］。動いているツールを止める。ランチャーは残る。

        1つだけなら確かめて止める。2つ以上なら、どれを止めるか選んで
        もらう (すべて止める、も選べる)。
        """
        status = self.manager.status
        running = self.manager.running
        starting = [a for a in status.starting_ids if a not in running]
        if len(running) == 1 and not starting:
            only = next(iter(running.values()))
            if self.manager.stop_refused(only.app_id):
                self._force_stop(only.app_id, only.display_name)
                return
            if messagebox.askyesno(
                    app_config.display_name(),
                    f"{only.display_name} を終了しますか?\n\n"
                    "ほかのツールやランチャーはそのまま使えます。",
                    parent=self.root):
                self.manager.stop(only.app_id)
            return

        menu = tk.Menu(self.root, tearoff=0, bg=theme.UTIL_BG, fg=theme.FG,
                       activebackground=theme.TOOL_ACTIVE,
                       activeforeground=theme.FG, font=theme.FONT)
        for app_id, item in running.items():
            name = item.display_name or app_id
            if self.manager.stop_refused(app_id):
                # 止めようとして断られた (保存の確認・実行中の処理)
                menu.add_command(
                    label=f"{name} を強制終了する",
                    command=lambda target=app_id, label=name:
                        self._force_stop(target, label))
                continue
            menu.add_command(label=f"{name} を止める",
                             command=lambda target=app_id: self.manager.stop(target))
        for app_id in starting:
            name = self._names.get(app_id, app_id)
            menu.add_command(label=f"{name} の起動をやめる",
                             command=lambda target=app_id: self.manager.stop(target))
        if len(running) >= 2:
            menu.add_separator()
            menu.add_command(label="すべて止める", command=self._stop_all)
        try:
            menu.tk_popup(self.stop_button.winfo_rootx(),
                          self.stop_button.winfo_rooty()
                          + self.stop_button.winfo_height())
        finally:
            menu.grab_release()

    def _force_stop(self, app_id: str, name: str) -> None:
        """断られたツールを強制終了する。**確かめてから。**"""
        if messagebox.askyesno(
                app_config.display_name(),
                f"{name} は終了の確認を出しているか、実行中の処理があります。\n\n"
                "強制終了しますか?\n(保存していない内容や、途中の処理は失われます)",
                icon="warning", default="no", parent=self.root):
            trace.event("強制終了を選んだ", trace.WARNING,
                        tool=self.manager.running.get(app_id))
            self.manager.stop(app_id, force=True)

    def _stop_all(self) -> None:
        names = "、".join(r.display_name for r in self.manager.running.values())
        if messagebox.askyesno(app_config.display_name(),
                               f"{names} をすべて終了しますか?",
                               parent=self.root):
            self.manager.stop_all()

    def show_version(self) -> None:
        """バージョン情報 (どの版が入っていて、どの版が動いているか)。"""
        VersionDialog(self.root)

    def open_settings(self) -> None:
        """設定画面 (要件定義書 §13)。

        **管理者パスワードを確かめてから開く。** ライン作業者が起動
        ファイルをうっかり変えてしまうのを防ぐ。
        """
        if not password.unlock(self.root):
            return
        dialog = SettingsDialog(self.root)
        if dialog.saved:
            # ログの出力先が変わっていれば、書き先を移す
            logging_utils.reconfigure()
            self._build_tool_buttons()
            # ツールが増えたぶん、窓を広げないとボタンが切れる
            self._resize_to_content()
            # 設定画面で「自動に戻す」や、起動後の位置が変わったかもしれない
            self._manual = self._saved_position() is not None
            if not self._manual:
                self._anchor = ""             # 新しい決まりで寄せ直す
                self._follow_state(self.manager.status)

    def show_detail(self) -> None:
        if not self._last_detail:
            return
        if not self._last_incident:
            messagebox.showinfo(app_config.display_name(), self._last_detail,
                                parent=self.root)
            return
        # 障害記録がある。**その場で開ける**ようにする ── 場所を書き写して
        # エクスプローラで探す手間があると、記録は読まれない
        if messagebox.askyesno(
                app_config.display_name(),
                f"{self._last_detail}\n\n"
                "障害記録 (なぜなぜ分析の下書き) を開きますか?",
                parent=self.root):
            if not trace.open_path(self._last_incident):
                messagebox.showinfo(app_config.display_name(),
                                    f"開けませんでした。\n{self._last_incident}",
                                    parent=self.root)

    def _on_ui_error(self, exc_type, exc, tb) -> None:
        """画面の処理で想定外の例外が起きた。**記録して、画面は動かし続ける。**"""
        log.error("画面の処理で想定外の失敗", exc_info=(exc_type, exc, tb))
        if exc is None:
            return
        if exc.__traceback__ is None:
            exc = exc.with_traceback(tb)
        path = trace.unexpected("ランチャーの画面", exc)
        if not path:
            return                            # 同じ失敗はもう記録してある
        try:
            self._set_status_text("error", "ランチャーで想定外の失敗がありました")
            self._last_detail = (f"{exc_type.__name__}: {exc}\n"
                                 f"障害記録: {path}")
            self._last_incident = path
            self.detail_button.configure(text="詳細 ！", bg=theme.ATTENTION_BG,
                                         fg=theme.FG)
            self.detail_button.pack(side="right", padx=(4, 0))
        except Exception:                     # noqa: BLE001 - 知らせで重ねて落ちない
            pass

    def on_close(self) -> None:
        """「終了」またはウィンドウの × (Alt+F4・タスクバーの「閉じる」も)。

        **どれも同じ確かめを通る** (`WM_DELETE_WINDOW` もここへ結んである)。
        業務ツールまで止めるかを尋ねる。ブラウザーを閉じることと
        バックエンドを止めることは別 (要件定義書 §11) なので、ランチャー
        だけ終わらせたい場面がある。止めずに閉じたツールは:

        * そのまま使い続けられる (画面も残る)
        * 画面を閉じれば、そのツールは自分で終わる
        * 次にランチャーを起動したとき引き継ぐ (二重に起動しない)

        **× は「終了」ボタンと違い、設定画面などを開いているあいだも押せる**
        (Windows ではその窓だけが前を塞ぎ、バーの × は生きている)。そのまま
        閉じると、開いている画面の下でランチャーが消える。開いている画面を
        前に出して、閉じない。
        """
        if self._closing:
            return
        modal = self._modal_window()
        if modal:
            log.info("設定画面などを開いているので閉じません: %s", modal)
            self._raise_modal(modal)
            return
        self._closing = True
        try:
            self._close()
        finally:
            self._closing = False

    def _modal_window(self) -> str:
        """前を塞いでいる (入力を独り占めしている) 窓の名前。無ければ空。"""
        try:
            names = [str(item) for item in
                     self.root.tk.splitlist(self.root.tk.call("grab", "current"))]
        except tk.TclError:
            return ""
        names = [name for name in names if name and name != "."]
        return names[0] if names else ""

    def _raise_modal(self, name: str) -> None:
        try:
            self.root.tk.call("raise", name)
            self.root.tk.call("focus", "-force", name)
            self.root.bell()
        except tk.TclError:
            pass

    def _close(self) -> None:
        running = self.manager.running
        starting = [a for a in self.manager.starting_ids if a not in running]
        names = "、".join(r.display_name or a for a, r in running.items())
        starting_names = "、".join(self._names.get(a, a) for a in starting)
        choice = None
        if running or starting:
            answer = messagebox.askyesnocancel(
                app_config.display_name(),
                close_question(names, starting_names),
                # **既定のボタンを「はい」(全部止める) にしない**
                default=CLOSE_DEFAULT, parent=self.root)
            choice = close_choice(answer)
            if choice == CLOSE_CANCEL:
                return
        stop_tools = choice == CLOSE_STOP

        log.info("ランチャーを終了します (ツールも停止=%s)", stop_tools)
        trace.event("ランチャー終了", trace.INFO,
                    cause=("ツールも止める" if stop_tools else
                           "ツールは動かしたまま" if running or starting else ""),
                    detail="、".join(x for x in (names, starting_names) if x))
        if stop_tools:
            self._set_status_text("stopping", "ツールを終了しています...")
            self.root.update_idletasks()

        if not self.manager.shutdown(stop_tools=stop_tools):
            # 止められなかった。ほとんどは実行中の処理があるとき。
            # **黙って閉じない** ── 止めたつもりで残るのがいちばん困る
            detail = self.manager.status.detail or "終了できませんでした。"
            if not messagebox.askyesno(
                    app_config.display_name(), force_question(detail),
                    # 中断は取り返しがつかない。**既定は「いいえ」**
                    icon="warning", default="no", parent=self.root):
                trace.event("ランチャー終了", trace.CANCELLED,
                            cause="実行中の処理を中断しないことを選んだ")
                self._render(self.manager.status)
                return
            trace.event("強制終了を選んだ", trace.WARNING,
                        cause="ランチャーを閉じるとき、実行中の処理を中断")
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
                # 進み具合の窓には**1つずつ**渡す。最後の1つだけだと、
                # 途中の段 (前の画面を閉じる、など) が抜ける
                self.progress.update(status)
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
        self._last_incident = status.incident
        if status.detail:
            # 読んでほしい案内がある。**目立たせる**
            self.detail_button.configure(text="詳細 ！", bg=theme.ATTENTION_BG,
                                         fg=theme.FG)
            self.detail_button.pack(side="right", padx=(4, 0))
        else:
            self.detail_button.pack_forget()

        if status.focus_id:
            self._focus_id = status.focus_id
        self._paint_buttons(status)

        if status.running_ids or status.starting_ids:
            self.stop_button.pack(side="right", padx=(4, 0))
        else:
            self.stop_button.pack_forget()

        self._follow_state(status)

    def _set_status_text(self, state: str, message: str) -> None:
        """状態の文を出す。**枠に収まらなければ「…」で切る。**"""
        self._status_full = message
        width = max(40, theme.STATUS_WIDTH - 24)
        self.status_label.configure(
            text=fit_text(message, width, self._status_font.measure))
        self.lamp.itemconfigure(
            self._lamp_dot,
            fill=theme.STATE_COLORS.get(state, theme.MUTED))

    def _show_full_status(self, _event=None) -> None:
        """切れている文の全文を、マウスを載せたあいだだけ出す。"""
        if self.status_label.cget("text") == self._status_full or self._tip:
            return
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=self._status_full, bg=theme.UTIL_BG, fg=theme.FG,
                 font=theme.FONT, padx=8, pady=4, justify="left",
                 wraplength=480).pack()
        tip.update_idletasks()
        x = self.status_label.winfo_rootx()
        y = self.status_label.winfo_rooty() - tip.winfo_reqheight() - 6
        if y < 0:
            y = self.status_label.winfo_rooty() + self.status_label.winfo_height() + 6
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide_full_status(self, _event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _paint_buttons(self, status) -> None:
        """動いているツールのボタンを目立たせる (要件定義書 §5.2)。

        動いている → 緑と ●、起動・停止の最中 → 橙と …、未設定 → 茶。
        **色だけに頼らず印も付ける** (色の見分けにくい人、遠目にも分かる)。
        """
        running = set(status.running_ids)
        busy = set(status.starting_ids) | set(status.stopping_ids)
        hidden_running = [a for a in running | busy if a in self._hidden]
        if hidden_running:
            # 動いているツールが［▼］の中に隠れている。**どれが動いて
            # いるか見えなくなる**ので、出し直す
            self._focus_id = hidden_running[0]
            self._apply_overflow()
        for app_id, button in self.buttons.items():
            name = self._names.get(app_id, app_id)
            if app_id in busy:
                button.configure(text=f"… {name}", bg=theme.TOOL_BUSY,
                                 fg=theme.FG, activebackground=theme.TOOL_BUSY)
            elif app_id in running:
                button.configure(text=f"● {name}", bg=theme.TOOL_RUNNING,
                                 fg=theme.FG,
                                 activebackground=theme.TOOL_RUNNING_ACTIVE)
            elif not self._configured.get(app_id):
                # 押しても起動しないことを、押す前に見せる
                button.configure(text=name, bg=theme.TOOL_UNSET,
                                 fg=theme.TOOL_UNSET_FG,
                                 activebackground=theme.TOOL_UNSET)
            else:
                button.configure(text=name, bg=theme.TOOL_BG, fg=theme.FG,
                                 activebackground=theme.TOOL_ACTIVE)

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
        # **画面のスレッドでは回さない。** ツールが重い処理をしていると、
        # 応答を待つあいだバーが固まる (ツールが増えるほど長くなる)。
        # 結果は状態の知らせとしてキュー経由で届く
        if not self._polling:
            self._polling = True
            threading.Thread(target=self._poll_worker, name="poll",
                             daemon=True).start()
        self.root.after(self._poll_interval_ms(), self._poll_health)

    def _poll_worker(self) -> None:
        try:
            self.manager.poll_health()
        except Exception as exc:              # noqa: BLE001 - 監視で画面を止めない
            log.exception("生存監視で失敗しました")
            trace.unexpected("生存監視", exc)
        finally:
            self._polling = False

    # --------------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()


def run(manager) -> None:
    """バーを出して常駐する。"""
    LauncherBar(manager).run()
