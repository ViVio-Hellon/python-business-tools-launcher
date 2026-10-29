"""ランチャー設定画面 (要件定義書 §13 / §14)

**必須要件**。端末ごとにリポジトリの置き場所が違うので、起動ファイルの
場所をここで変えられるようにする。`start.bat`・`Start.vbs`・exe (Tauri の
アプリ、Python から作った exe) のどれも指定できる。コードを書き換えずに済ませることが
目的 (要件定義書 §13.3)。

1.8.0 で画面を作り直した (案A「見出し＋一覧と詳細」と案B「PC × ツールの表」を
合わせたもの)。**ツールの一覧は1つだけ**。ツール管理マスタを使っていれば、
その一覧がそのままマスタ (全 PC 共通) になる。

    ┌────────────┬──────────────────────────────────────────────┐
    │ ツール      │ ┌ツールの一覧─────┐ ┌選んだツール──────────────┐ │
    │ PCごとの表示│ │ 日報        ある │ │ 表示名 [日報]  nlm.nippou │ │
    │ この端末    │ │ 看板        ある │ │ 起動ファイル [....] [参照]│ │
    │ 記録・配布  │ │ 点検表      無い │ │ ポート [8733] 起動引数 [ ]│ │
    │ パスワード  │ └──────────────┘ └──────────────────────┘ │
    │            │ [＋ ツールを追加] [↑] [↓]                        │
    ├────────────┴──────────────────────────────────────────────┤
    │ ツール管理マスタ: \\\\server\\共有 (管理番号 3 の行)   [保存] [キャンセル] │
    └───────────────────────────────────────────────────────────┘

［PCごとの表示］は、縦に PC (ログインID・PC名)、横にツールを並べた表。出す
ツールにチェックを付ける。**PC の行はツールより先に足せる**。［保存］は1つで、
全部の見出しの内容をまとめて書く。
"""
from __future__ import annotations

import os
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from .. import (app_config, distribution, fileprobe, tool_entries,
               tool_master, tool_registry, trace)
from ..logging_utils import get_logger
from ..tool_master import Master, MasterError, MasterPc, MasterTool
from ..tool_registry import STOP_METHODS, UI_LABELS, Tool
from . import password, theme

log = get_logger("ui.settings")

# 窓の大きさ。**1366×768 の画面 (ライン PC) に収める** (タスクバーと窓の枠のぶんを残す)
WINDOW_WIDTH = 1340
WINDOW_HEIGHT = 650
# 画面の高さのうち、窓の外 (タイトルバー・タスクバー) に残すぶん
SCREEN_MARGIN = 110
NAV_WIDTH = 160
LIST_WIDTH = 330

# 左の見出し (キー, 呼び名)
SECTIONS = (("tools", "ツール"),
            ("pcs", "PCごとの表示"),
            ("local", "この端末"),
            ("records", "記録・配布"),
            ("password", "管理者パスワード"))

# PC × ツールの表 (案B の見た目)。ツールの列の幅 (px)・見出しと行の高さ・色
MATRIX_COLUMN_MIN = 54
MATRIX_COLUMN_MAX = 72
MATRIX_HEADER = 104
MATRIX_ROW = 36
MATRIX_HEAD_BG = "#222a35"
MATRIX_MINE_BG = "#243041"          # この端末の行
MATRIX_LINE = "#2c3644"             # 行の区切り
MATRIX_ABSENT = "#e0857c"           # この端末に無い
MATRIX_SMALL_FONT = ("Yu Gothic UI", 8)
MATRIX_BOLD_FONT = ("Yu Gothic UI", 9, "bold")
# ツールの頭文字の色 (案B と同じ並び。足りなければ繰り返す)
CHIP_COLORS = ("#4a7fd6", "#c97b22", "#3f9b62", "#c25a43", "#7a62c4", "#2f8fa3",
               "#a0703a", "#5b74b8", "#8a8f3a", "#b05c8a", "#4f8a6a", "#7d6a5a")
CHECK_ON = "#2e9e5e"
CHECK_ON_DIM = "#2f5e48"
CHECK_OFF = "#f2f5f8"
CHECK_OFF_DIM = "#6f7b88"

# 起動ファイルを選ぶダイアログの種類
ENTRY_FILETYPES = [("起動ファイル", "*.bat *.vbs *.exe"),
                   ("バッチファイル", "*.bat"),
                   ("VBScript", "*.vbs"),
                   ("アプリ (exe)", "*.exe"),
                   ("すべてのファイル", "*.*")]


class SettingsDialog:
    """モーダルの設定画面。閉じるまで戻らない。"""

    def __init__(self, parent: tk.Misc, *, section: str = "tools") -> None:
        self.saved = False
        # ツール管理マスタを反映して、この端末のツールが変わった (バーを描き直す)
        self.tools_changed = False
        self.rows: list[_ToolRow] = []
        self.pc_rows: list[_PcRow] = []
        self.deleted_pcs: list[int] = []
        self.selected: Optional[_ToolRow] = None
        self.master: Optional[Master] = None
        self.master_problem = ""
        self._known_tools: list[int] = []
        self._list_job = None
        self._jobs: set[str] = set()
        self._wheel_target: Optional[tk.Canvas] = None

        self.top = tk.Toplevel(parent)
        self.top.title(f"{app_config.display_name()} "
                       f"{app_config.version_label()} - 設定")
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.top.resizable(True, True)
        # 閉じたあとに、待たせていた描き直しを動かさない
        self.top.bind("<Destroy>", self._on_destroy, add="+")

        self._master_before = tool_registry.get_pc_setting(tool_registry.TOOL_MASTER_KEY)
        self.master_var = tk.StringVar(value=self._master_before)
        self._load_master()
        self._build()
        self.show_section(section if section in dict(SECTIONS) else "tools")
        self._center_on(parent)

        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.top.wait_window()

    # ==============================================================
    # 枠組み: 左の見出し・中身・下の［保存］
    # ==============================================================
    def _build(self) -> None:
        # ［保存］［キャンセル］は**いつも窓の下に見えている**。先に下へ置く
        self._build_footer()

        self.nav = tk.Frame(self.top, bg=theme.UTIL_BG, width=NAV_WIDTH)
        self.nav.pack(side="left", fill="y")
        self.nav.pack_propagate(False)
        tk.Label(self.nav, text="設定", bg=theme.UTIL_BG, fg=theme.FG,
                 font=theme.FONT_TOOL, anchor="w").pack(fill="x", padx=14, pady=(14, 10))
        self.nav_buttons: dict[str, tk.Button] = {}
        for key, title in SECTIONS:
            button = tk.Button(self.nav, text=title, anchor="w",
                               command=lambda k=key: self.show_section(k),
                               bg=theme.UTIL_BG, fg=theme.UTIL_FG, relief="flat", bd=0,
                               activebackground=theme.BUTTON_ACTIVE,
                               activeforeground=theme.FG, padx=14, pady=8,
                               font=theme.FONT, cursor="hand2")
            button.pack(fill="x")
            self.nav_buttons[key] = button

        self.content = tk.Frame(self.top, bg=theme.BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.sections: dict[str, tk.Frame] = {
            key: tk.Frame(self.content, bg=theme.BG) for key, _ in SECTIONS}
        self.current_section = ""
        self._build_tools_section(self.sections["tools"])
        self._build_pcs_section(self.sections["pcs"])
        self._build_local_section(self.sections["local"])
        self._build_records_section(self.sections["records"])
        self._build_password_section(self.sections["password"])
        self._update_mode()

    def _later(self, func, ms: int = 0) -> None:
        """少し後で動かす。閉じたら取り消す。"""
        def run() -> None:
            self._jobs.discard(job)
            func()
        job = self.top.after(ms, run) if ms else self.top.after_idle(run)
        self._jobs.add(job)

    def _on_destroy(self, event) -> None:
        if event.widget is not self.top:
            return
        for job in list(self._jobs):
            try:
                self.top.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        for row in self.rows:
            row.cancel_jobs()

    def show_section(self, key: str) -> None:
        """左の見出しを押した。中身を入れ替える (入力した値は残る)。"""
        if self.current_section:
            self.sections[self.current_section].pack_forget()
        self.current_section = key
        if key == "pcs":
            self._render_matrix()
        elif key == "tools":
            self._refresh_list()
        self.sections[key].pack(fill="both", expand=True, padx=16, pady=(12, 0))
        for name, button in self.nav_buttons.items():
            current = name == key
            button.configure(bg=theme.BUTTON_ACTIVE if current else theme.UTIL_BG,
                             fg=theme.FG if current else theme.UTIL_FG,
                             font=theme.FONT_BOLD if current else theme.FONT)

    def _section_title(self, parent: tk.Frame, title: str, text: str) -> tk.Label:
        tk.Label(parent, text=title, bg=theme.BG, fg=theme.FG, font=theme.FONT_TOOL,
                 anchor="w").pack(fill="x")
        label = tk.Label(parent, text=text, bg=theme.BG, fg=theme.MUTED,
                         font=theme.FONT_SMALL, anchor="w", justify="left",
                         wraplength=900)
        label.pack(fill="x", pady=(2, 8))
        return label

    def _build_footer(self) -> None:
        frame = tk.Frame(self.top, bg=theme.UTIL_BG)
        frame.pack(side="bottom", fill="x")
        tk.Button(frame, text="キャンセル", command=self.cancel,
                  bg=theme.BUTTON_BG, fg=theme.MUTED, relief="flat", bd=0,
                  padx=16, pady=6, font=theme.FONT,
                  cursor="hand2").pack(side="right", padx=(0, 16), pady=10)
        tk.Button(frame, text="保存", command=self.save,
                  bg=theme.BUTTON_CURRENT, fg=theme.FG, relief="flat", bd=0,
                  padx=26, pady=6, font=theme.FONT_BOLD,
                  cursor="hand2").pack(side="right", padx=(0, 8), pady=10)
        # いまのツール管理マスタ・この端末の行 (どの見出しを開いていても見える)
        self.status = tk.Label(frame, text="", bg=theme.UTIL_BG, fg=theme.MUTED,
                               font=theme.FONT_SMALL, anchor="w", justify="left",
                               wraplength=780)
        self.status.pack(side="left", fill="x", expand=True, padx=16)

    def _center_on(self, parent: tk.Misc) -> None:
        """画面の中ほどに出す。**窓全体 (［保存］まで) が画面に収まる**大きさにする。"""
        self.top.update_idletasks()
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        width = min(WINDOW_WIDTH, screen_w - 20)
        height = min(WINDOW_HEIGHT, screen_h - SCREEN_MARGIN)
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height - SCREEN_MARGIN // 2) // 2)
        self.top.minsize(min(900, width), min(520, height))
        self.top.geometry(f"{width}x{height}+{x}+{y}")

    def _bind_wheel(self, canvas: tk.Canvas) -> None:
        """ホイールで巻く。その巻物の上にいるあいだだけ (ほかの窓のホイールを奪わない)。"""
        def on_wheel(event) -> None:
            target = self._wheel_target
            if target is not None and target.winfo_exists():
                step = -1 * (event.delta // 120 or (1 if event.delta > 0 else -1))
                target.yview_scroll(step, "units")

        def enter(_event) -> None:
            self._wheel_target = canvas
            self.top.bind_all("<MouseWheel>", on_wheel)

        def leave(_event) -> None:
            self._wheel_target = None
            self.top.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)

    def _scrolled(self, parent: tk.Widget, *, width: int = 0,
                  horizontal: bool = False, bg: str = theme.BG) -> tuple[tk.Frame, tk.Canvas]:
        """縦 (と横) に巻ける枠。(中に置く Frame, Canvas) を返す。"""
        outer = tk.Frame(parent, bg=bg)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        if width:
            canvas.configure(width=width)
        inner = tk.Frame(canvas, bg=bg)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        if horizontal:
            hbar = ttk.Scrollbar(outer, orient="horizontal", command=canvas.xview)
            canvas.configure(xscrollcommand=hbar.set)
            hbar.pack(side="bottom", fill="x")
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def on_inner(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas(event) -> None:
            if not horizontal:
                canvas.itemconfigure(window, width=event.width)

        inner.bind("<Configure>", on_inner)
        canvas.bind("<Configure>", on_canvas)
        self._bind_wheel(canvas)
        outer.canvas = canvas                 # type: ignore[attr-defined]
        return inner, outer

    @staticmethod
    def _action_button(parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                         padx=14, pady=5, font=theme.FONT_SMALL,
                         activebackground=theme.BUTTON_ACTIVE, activeforeground=theme.FG,
                         disabledforeground=theme.MUTED, cursor="hand2")

    # ==============================================================
    # ツール管理マスタを読む
    # ==============================================================
    def _master_path(self) -> Optional[Path]:
        text = self.master_var.get().strip() or distribution.tool_master().strip()
        return tool_master.path_for(text) if text else None

    @property
    def shared(self) -> bool:
        """ツールの一覧をツール管理マスタに書くか (マスタを読めているとき)。"""
        return self.master is not None

    def _load_master(self) -> None:
        """マスタを読む (場所が決まっていれば)。読めなければ理由を覚えておく。"""
        self.master, self.master_problem = None, ""
        path = self._master_path()
        if path is None:
            return
        try:
            self.master = tool_master.load(path)
        except MasterError as exc:
            self.master_problem = str(exc)
            log.warning("ツール管理マスタを読めません: %s", exc)

    def _update_mode(self) -> None:
        """マスタを使うか使わないかで変わる案内・欄を出し直す。"""
        for row in self.rows:
            row.set_shared(self.shared)
        if self.shared:
            self.tools_intro.configure(
                text="全PCで同じツールの一覧です (ツール管理マスタに書きます)。"
                     "どのPCに出すかは［PCごとの表示］で決めます。"
                     "一覧で選ぶと右に詳しい設定が出ます。")
        elif self.master_problem:
            self.tools_intro.configure(
                text=f"ツール管理マスタを読めません ({self.master_problem})。"
                     "この端末の設定だけを直せます (マスタには書きません。"
                     "マスタに届くようになると、マスタの内容に戻ります)。")
        else:
            self.tools_intro.configure(
                text="この端末のバーに出すツールです。一覧で選ぶと右に詳しい設定が出ます。"
                     "PCごとに出すツールを決めるときは［PCごとの表示］へ。")
        self._update_status()
        self._build_master_panel()

    def _update_status(self) -> None:
        path = self._master_path()
        login, pc_name = tool_master.this_pc()
        if self.shared and path is not None:
            row = self._this_pc_row()
            where = (f"この端末 ({login} / {pc_name}) は管理番号 {row.pc_no or '(新しい行)'} の行"
                     if row is not None else
                     f"この端末 ({login} / {pc_name}) の行はありません (マスタのツールを全部出します)")
            text = f"ツール管理マスタ: {path}\n{where}"
        elif self.master_problem:
            text = f"ツール管理マスタを読めません: {self.master_problem}"
        else:
            text = "ツール管理マスタは使っていません (この端末の設定だけで並べます)"
        self.status.configure(text=text, fg=theme.STATE_COLORS["error"]
                              if self.master_problem else theme.MUTED)

    # ==============================================================
    # 見出し「ツール」: 一覧と詳細
    # ==============================================================
    def _build_tools_section(self, frame: tk.Frame) -> None:
        self.tools_intro = self._section_title(frame, "ツール", "")
        body = tk.Frame(frame, bg=theme.BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=theme.BG, width=LIST_WIDTH)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        buttons = tk.Frame(left, bg=theme.BG)
        buttons.pack(side="bottom", fill="x", pady=(8, 10))
        self._action_button(buttons, "＋ ツールを追加", self.add_tool).pack(side="left")
        self._action_button(buttons, "↑", lambda: self.move_selected(-1)).pack(
            side="left", padx=(8, 0))
        self._action_button(buttons, "↓", lambda: self.move_selected(1)).pack(
            side="left", padx=(4, 0))
        self.list_inner, list_outer = self._scrolled(left, bg=theme.UTIL_BG)
        list_outer.pack(fill="both", expand=True)

        # 右: 選んだツールの詳しい設定 (1つだけ出す)
        self.detail = tk.Frame(body, bg=theme.BG)
        self.detail.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.detail_empty = tk.Label(
            self.detail, text="まだツールがありません。\n"
                              "左下の［＋ ツールを追加］で、各ツールの start.bat (または "
                              "Start.vbs・exe) を選んでください。\n"
                              "アプリID・表示名・ポートは、そのツールの config/app.json "
                              "から読み取ります。",
            bg=theme.BG, fg=theme.MUTED, font=theme.FONT, justify="left", anchor="nw")
        # マスタを使うとき: そのツールを出す PC
        self.detail_pcs = tk.Label(self.detail, text="", bg=theme.BG, fg=theme.MUTED,
                                   font=theme.FONT_SMALL, anchor="w", justify="left",
                                   wraplength=560)
        self._fill_rows()

    def _fill_rows(self) -> None:
        """ツールの行を (作り直して) 並べる。マスタを読めていれば、マスタのツール。"""
        for row in self.rows:
            row.box.destroy()
        self.rows = []
        self.selected = None
        local = tool_registry.all_tools(include_disabled=True)
        # 開いたときの設定。保存したとき**何が変わったか**を記録に残す
        self._before_tools = list(local)
        if self.master is None:
            for tool in local:
                self._add_row(tool)
        else:
            self._merge_master(self.master, local)
        self._load_pcs()
        if self.rows:
            self.select(self.rows[0])
        else:
            self._show_detail(None)

    def _merge_master(self, master: Master, local: list[Tool]) -> None:
        """マスタのツールを行にする。行がもうあれば (同じツール) その行を使う。

        マスタに無い、この端末だけのツールも並べる (保存するとマスタに入る)。
        """
        for row in self.rows:
            row.master_no, row.master_note = 0, ""
        for master_tool in master.tools:
            row = next((r for r in self.rows if tool_master.local_match(
                master_tool, [r.tool]) is not None and not r.master_no), None)
            if row is None:
                row = self._add_row(tool_master.as_tool(master_tool, local))
            row.master_no, row.master_note = master_tool.tool_no, master_tool.note
        order = {master_tool.tool_no: i for i, master_tool in enumerate(master.tools)}
        self.rows.sort(key=lambda r: order.get(r.master_no, len(order)))
        self._known_tools = [t.tool_no for t in master.tools]

    def _add_row(self, tool: Tool, *, is_new: bool = False) -> "_ToolRow":
        row = _ToolRow(self.detail, tool, is_new=is_new, shared=self.shared)
        row.box.pack_forget()
        for var in (row.name_var, row.path_var, row.delete_var, row.enabled_var):
            var.trace_add("write", lambda *_: self._schedule_list())
        self.rows.append(row)
        return row

    def select(self, row: Optional["_ToolRow"]) -> None:
        self.selected = row
        self._show_detail(row)
        self._refresh_list()

    def _show_detail(self, row: Optional["_ToolRow"]) -> None:
        for other in self.rows:
            if other is not row:
                other.box.pack_forget()
        self.detail_empty.pack_forget()
        self.detail_pcs.pack_forget()
        if row is None:
            self.detail_empty.pack(fill="both", expand=True, pady=(4, 0))
            return
        row.box.pack(fill="x", anchor="n")
        if self.shared:
            self.detail_pcs.configure(text=self._pcs_text(row))
            self.detail_pcs.pack(fill="x", pady=(10, 0))

    def _pcs_text(self, row: "_ToolRow") -> str:
        names = [pc.title() for pc in self.pc_rows if not pc.deleted and pc.has(row)]
        if not self.pc_rows:
            return ("出すPC: PC の行がまだ無いので、このツールがある PC 全部に出ます。"
                    "PCごとに決めるときは［PCごとの表示］で。")
        text = "出すPC: " + ("、".join(names) if names else "(どの行にもありません)")
        return text + "\n(行の無い PC には、その PC にあるツールを全部出します。変えるときは［PCごとの表示］で)"

    def _schedule_list(self) -> None:
        if self._list_job is not None:
            return
        self._list_job = True
        self._later(self._refresh_list, 150)

    def _refresh_list(self) -> None:
        """左の一覧を描き直す (名前・この端末にあるか・消す印)。"""
        self._list_job = None
        if not self.top.winfo_exists():
            return
        for child in self.list_inner.winfo_children():
            child.destroy()
        for row in self.rows:
            current = row is self.selected
            bg = theme.TOOL_ACTIVE if current else theme.UTIL_BG
            item = tk.Frame(self.list_inner, bg=bg, cursor="hand2")
            item.pack(fill="x", pady=(0, 1))
            status, color = row.status()
            top = tk.Frame(item, bg=bg)
            top.pack(fill="x", padx=10, pady=(6, 0))
            name = tk.Label(top, text=row.label(), bg=bg,
                            fg=theme.MUTED if row.marked_for_delete() else theme.FG,
                            font=theme.FONT_BOLD, anchor="w")
            name.pack(side="left")
            badge = tk.Label(top, text=status, bg=bg, fg=color, font=theme.FONT_SMALL)
            badge.pack(side="right")
            sub = tk.Label(item, text=row.subtitle(), bg=bg, fg=theme.MUTED,
                           font=theme.FONT_SMALL, anchor="w")
            sub.pack(fill="x", padx=10, pady=(0, 6))
            for widget in (item, top, name, badge, sub):
                widget.bind("<Button-1>", lambda _e, r=row: self.select(r))
        if self.selected is not None and self.shared:
            self.detail_pcs.configure(text=self._pcs_text(self.selected))

    def move_selected(self, step: int) -> None:
        """選んだツールを1つ上 (`-1`)・下 (`+1`) へ。バーの並びになる。"""
        row = self.selected
        if row is None:
            return
        index = self.rows.index(row)
        other = index + step
        if 0 <= other < len(self.rows):
            self.rows[index], self.rows[other] = self.rows[other], self.rows[index]
            self._refresh_list()

    def add_tool(self) -> None:
        """起動ファイルを選んでもらい、その素性から新しい行を作る。

        **アプリIDは手で写させない。** 相手の `config/app.json` から
        読み取る ── 1文字違うだけで起動確認が永久に通らず、画面には
        「応答がありません」としか出ないので原因にたどり着きにくい。
        読めなければ空のまま出すので、手で入れてもらう。
        """
        chosen = filedialog.askopenfilename(
            title="追加するツールの起動ファイル (start.bat / Start.vbs / exe) を"
                  "選んでください",
            filetypes=ENTRY_FILETYPES, parent=self.top)
        if not chosen:
            return

        found = tool_registry.probe_tool_folder(chosen)
        # 起動引数は**こちらで決める**。利用者には分からないことが多い
        args, reason = tool_registry.recommend_start_args(chosen)
        tool = Tool(app_id=found.get("app_id", ""),
                    display_name=found.get("display_name", ""),
                    port=int(found.get("port", 0)),
                    order_no=tool_registry.next_order_no(),
                    start_command=chosen,
                    start_args=args,
                    ui_mode=found.get("ui_mode", ""))
        if found.get("kind") == "tauri":
            reason = "Tauri のアプリと見分けました。" + reason
        reason = "\n".join(text for text in (reason, _port_note(found)) if text)
        row = self._add_row(tool, is_new=True)
        if self.shared:
            # デスクトップの中なら %DESKTOP% で書く (ログインIDが違う PC でも見つかる)
            row.path_var.set(tool_master.portable(chosen))
            # **足したツールは、この端末のバーに出す。** この端末の行があれば
            # そこにチェックを入れる (入れないと「足したのに出ない」になる)
            mine = self._this_pc_row()
            if mine is not None:
                mine.set(row, True)
                reason = "\n".join(t for t in (
                    reason, f"この端末の行 (管理番号 {mine.pc_no or '新しい行'}) に入れました。"
                            "ほかの PC に出すときは［PCごとの表示］で") if t)
        row.show_note(reason)
        self.select(row)
        if not found:
            messagebox.showinfo(
                "設定",
                "そのフォルダーから設定を読み取れませんでした。\n"
                "アプリIDと表示名を入力してください。\n\n"
                "アプリIDは、そのツールの config/app.json の app_id と"
                "同じ値にしてください。",
                parent=self.top)

    # ==============================================================
    # 見出し「PCごとの表示」: PC × ツールの表
    # ==============================================================
    def _build_pcs_section(self, frame: tk.Frame) -> None:
        """［PCごとの表示］。案B の「PC × ツール」の表 (1.8.4 で案の見た目に寄せた)。"""
        head = tk.Frame(frame, bg=theme.BG)
        head.pack(fill="x", pady=(0, 8))
        tk.Label(head, text="PC × ツール", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_TOOL).pack(side="left")
        tk.Label(head, text="チェックを付けたツールが、その PC のバーに出ます。"
                            "ツール名を押すと詳しい設定",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL).pack(
            side="left", padx=(10, 0))
        self.pcs_buttons = tk.Frame(head, bg=theme.BG)
        self.pcs_buttons.pack(side="right")
        add_tool = self._action_button(self.pcs_buttons, "＋ ツールを追加",
                                       self.add_tool_from_matrix)
        add_tool.configure(bg=theme.BUTTON_CURRENT, activebackground=theme.TOOL_RUNNING_ACTIVE)
        add_tool.pack(side="right")
        self._action_button(self.pcs_buttons, "＋ PCを追加", self.add_pc).pack(
            side="right", padx=(0, 8))
        self._action_button(self.pcs_buttons, "＋ この端末", self.add_this_pc).pack(
            side="right", padx=(0, 8))

        # マスタを使っていないときの案内と場所の欄 (使っているときは表の下に1行)
        self.master_panel = tk.Frame(frame, bg=theme.BG)
        self.matrix_area = tk.Frame(frame, bg=theme.BG)
        self.matrix_area.pack(fill="both", expand=True)

        # 表の下: 凡例とマスタの場所
        self.matrix_foot = tk.Frame(self.matrix_area, bg=theme.BG)
        self.matrix_foot.pack(side="bottom", fill="x", pady=(8, 8))
        self.matrix_legend = tk.Label(self.matrix_foot, text="", bg=theme.BG, fg=theme.MUTED,
                                      font=theme.FONT_SMALL, anchor="w", justify="left")
        self.matrix_legend.pack(fill="x")
        self.master_line = tk.Frame(self.matrix_foot, bg=theme.BG)
        self.master_line.pack(fill="x", pady=(4, 0))

        # 表: 見出し (上に固定) と行 (縦に巻く)。横は両方いっしょに巻く
        table = tk.Frame(self.matrix_area, bg=MATRIX_LINE, bd=0)
        table.pack(fill="both", expand=True)
        self.matrix_hbar = ttk.Scrollbar(table, orient="horizontal",
                                         command=self._matrix_xview)
        self.matrix_hbar.pack(side="bottom", fill="x")
        self.matrix_vbar = ttk.Scrollbar(table, orient="vertical")
        self.matrix_vbar.pack(side="right", fill="y")
        self.matrix_head_canvas = tk.Canvas(table, bg=MATRIX_HEAD_BG, highlightthickness=0,
                                            height=10)
        self.matrix_head_canvas.pack(side="top", fill="x", padx=(1, 0), pady=(1, 0))
        self.matrix_body_canvas = tk.Canvas(table, bg=theme.BG, highlightthickness=0)
        self.matrix_body_canvas.pack(side="top", fill="both", expand=True, padx=(1, 0))
        self.matrix_head = tk.Frame(self.matrix_head_canvas, bg=MATRIX_HEAD_BG)
        self.matrix_inner = tk.Frame(self.matrix_body_canvas, bg=theme.BG)
        self.matrix_head_canvas.create_window((0, 0), window=self.matrix_head, anchor="nw")
        self.matrix_body_canvas.create_window((0, 0), window=self.matrix_inner, anchor="nw")
        self.matrix_body_canvas.configure(xscrollcommand=self.matrix_hbar.set,
                                          yscrollcommand=self.matrix_vbar.set)
        self.matrix_vbar.configure(command=self.matrix_body_canvas.yview)

        def fit(_event=None) -> None:
            head, body = self.matrix_head_canvas, self.matrix_body_canvas
            head.configure(height=self.matrix_head.winfo_reqheight(),
                           scrollregion=(0, 0, self.matrix_head.winfo_reqwidth(),
                                         self.matrix_head.winfo_reqheight()))
            body.configure(scrollregion=(0, 0, self.matrix_inner.winfo_reqwidth(),
                                         self.matrix_inner.winfo_reqheight()))

        self.matrix_head.bind("<Configure>", fit)
        self.matrix_inner.bind("<Configure>", fit)
        self._bind_wheel(self.matrix_body_canvas)
        # 外から見る名前 (巻物の枠)
        self.matrix_outer = table
        table.canvas = self.matrix_body_canvas      # type: ignore[attr-defined]

    def _matrix_xview(self, *args) -> None:
        self.matrix_head_canvas.xview(*args)
        self.matrix_body_canvas.xview(*args)

    def _build_master_panel(self) -> None:
        """ツール管理マスタの場所。使っていなければ大きく案内、使っていれば表の下に1行。"""
        panel = self.master_panel
        for child in panel.winfo_children():
            child.destroy()
        for child in self.master_line.winfo_children():
            child.destroy()
        shared = distribution.tool_master()
        blank = (f"配布先フォルダの指定 ({shared})" if shared else "使いません")
        if self.shared:
            panel.pack_forget()
            self.pcs_buttons.pack(side="right")
            self.matrix_area.pack(fill="both", expand=True)
            tk.Label(self.master_line, text="ツール管理マスタ", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL).pack(side="left")
            tk.Entry(self.master_line, textvariable=self.master_var, width=60,
                     font=theme.FONT_SMALL, highlightthickness=0).pack(side="left",
                                                                     padx=(8, 0))
            self._action_button(self.master_line, "参照", self.browse_master).pack(
                side="left", padx=(6, 0))
            self._action_button(self.master_line, "読み直す", self.use_master).pack(
                side="left", padx=(6, 0))
            tk.Label(self.master_line, text=f"全 PC で共有 ・ 空欄のとき: {blank}",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL).pack(
                side="left", padx=(10, 0))
            return
        self.matrix_area.pack_forget()
        self.pcs_buttons.pack_forget()
        panel.pack(fill="x", pady=(0, 8))
        line = tk.Frame(panel, bg=theme.BG)
        line.pack(fill="x")
        tk.Label(line, text="ツール管理マスタの場所", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Entry(line, textvariable=self.master_var, width=46, font=theme.FONT,
                 highlightthickness=0).pack(side="left", padx=(10, 0))
        self._action_button(line, "参照", self.browse_master).pack(side="left", padx=(6, 0))
        self._action_button(line, "この場所を使う", self.use_master).pack(side="left",
                                                                         padx=(6, 0))
        text = ("PCごとに出すツールを決めるには、全 PC が読める場所 (共有フォルダー) を"
                "入れて［この場所を使う］を押してください。その中に "
                f"{tool_master.FILE_NAME} を作ります (あれば読みます)。"
                "いまの［ツール］の一覧は、保存するとマスタに入ります。"
                f"\n空欄のとき: {blank}")
        if self.master_problem:
            text = f"読めません: {self.master_problem}\n" + text
        tk.Label(panel, text=text, bg=theme.BG,
                 fg=theme.STATE_COLORS["error"] if self.master_problem else theme.MUTED,
                 font=theme.FONT_SMALL, anchor="w", justify="left",
                 wraplength=920).pack(fill="x", pady=(4, 0))

    def add_tool_from_matrix(self) -> None:
        """表の上の［＋ ツールを追加］。足したら表に列が増える (見出しは移らない)。"""
        self.add_tool()
        if self.current_section == "pcs":
            self._render_matrix()

    def browse_master(self) -> None:
        current = self.master_var.get().strip()
        start = tool_master.path_for(current).parent if current else Path.home()
        chosen = filedialog.askdirectory(
            parent=self.top, title="ツール管理マスタを置くフォルダー",
            initialdir=str(start if os.path.isdir(start) else Path.home()))
        if chosen:
            self.master_var.set(str(Path(chosen)))

    def use_master(self) -> None:
        """［この場所を使う］［読み直す］。マスタを読み、一覧と表を作り直す。

        いまの画面の値 (保存していない変更) は残す。マスタにしか無いツールを
        足し、PC の行をマスタから読む。**保存するまで、この端末の設定は変えない。**
        """
        path = self._master_path()
        if path is None:
            self.master, self.master_problem = None, ""
            self.pc_rows, self.deleted_pcs = [], []
            self._known_tools = []
            for row in self.rows:
                row.master_no = 0
            self._update_mode()
            self._render_matrix()
            return
        try:
            master = tool_master.load(path)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ",
                                 f"ツール管理マスタを読めませんでした。\n{exc}", parent=self.top)
            return
        self.master, self.master_problem = master, ""
        self._merge_master(master, tool_registry.all_tools(include_disabled=True))
        self.deleted_pcs = []
        self._load_pcs()
        self._update_mode()
        self._render_matrix()
        if self.rows and self.selected is None:
            self.select(self.rows[0])

    def _load_pcs(self) -> None:
        self.pc_rows = []
        if self.master is None:
            return
        by_no = {row.master_no: row for row in self.rows if row.master_no}
        for pc in self.master.pcs:
            self.pc_rows.append(_PcRow(pc, [by_no[n] for n in pc.tool_nos if n in by_no]))

    def _this_pc_row(self) -> Optional["_PcRow"]:
        """この端末にあたる行 (画面の値で探す。保存していない行も)。"""
        rows = [pc for pc in self.pc_rows if not pc.deleted]
        found = Master(path=Path("."), pcs=tuple(
            replace(pc.value(), pc_no=i) for i, pc in enumerate(rows))).row_for(
            *tool_master.this_pc())
        return rows[found.pc_no] if found is not None else None

    def add_this_pc(self) -> None:
        """この端末の行を足す。いまバーに出ているツールにチェックを入れておく。"""
        login, pc_name = tool_master.this_pc()
        for pc in self.pc_rows:
            if not pc.deleted and pc.login_var.get().strip().casefold() == login.casefold() \
                    and pc.pc_var.get().strip().casefold() == pc_name.casefold():
                messagebox.showinfo("PCごとの表示", "この端末の行はもうあります。",
                                    parent=self.top)
                return
        row = _PcRow(MasterPc(0, login_id=login, pc_name=pc_name),
                     [r for r in self.rows if not r.marked_for_delete() and r.here()])
        self.pc_rows.append(row)
        self._render_matrix()

    def add_pc(self) -> None:
        """空の PC の行を足す (ツールより先でもよい)。"""
        self.pc_rows.append(_PcRow(MasterPc(0), []))
        self._render_matrix(focus_last=True)

    def remove_pc(self, pc: "_PcRow") -> None:
        pc.deleted = True
        if pc.pc_no:
            self.deleted_pcs.append(pc.pc_no)
        self._render_matrix()

    def _matrix_widths(self, count: int) -> tuple[list[int], int, list[int]]:
        """列の幅 (px)。左の固定の列・ツール1列・右の列 (有効・消す)。

        ツールの列は窓の幅に合わせる (案B は 66px)。狭くなりすぎるなら横に巻く。
        """
        fixed = [40, 122, 140, 80]
        tail = [46, 40]
        width = self.top.winfo_width()
        if width < 200:                      # まだ描いていない
            width = min(WINDOW_WIDTH, self.top.winfo_screenwidth() - 20)
        room = width - NAV_WIDTH - 32 - 22 - sum(fixed) - sum(tail)
        each = room // max(1, count)
        return fixed, max(MATRIX_COLUMN_MIN, min(MATRIX_COLUMN_MAX, each)), tail

    def _cell(self, parent: tk.Widget, row: int, column: int, width: int, height: int,
              bg: str) -> tk.Frame:
        cell = tk.Frame(parent, bg=bg, width=width, height=height)
        cell.grid(row=row, column=column, sticky="nsew", pady=(0, 1))
        cell.pack_propagate(False)
        return cell

    def _render_matrix(self, *, focus_last: bool = False) -> None:
        """PC × ツールの表を描き直す (チェックなどの値は _PcRow が持つ)。"""
        for holder in (self.matrix_head, self.matrix_inner):
            for child in holder.winfo_children():
                child.destroy()
        if not self.shared:
            return
        tools = [r for r in self.rows if not r.marked_for_delete()]
        fixed, each, tail = self._matrix_widths(len(tools))
        widths = fixed + [each] * len(tools) + tail

        # --- 見出し (上に固定) ---
        head_bg = MATRIX_HEAD_BG
        titles = ["No", "ログインID", "PC名", "備考"]
        for column, title in enumerate(titles):
            cell = self._cell(self.matrix_head, 0, column, widths[column], MATRIX_HEADER,
                              head_bg)
            tk.Label(cell, text=title, bg=head_bg, fg=theme.MUTED, font=theme.FONT_SMALL,
                     anchor="w").pack(side="bottom", fill="x", padx=6, pady=(0, 8))
        for index, row in enumerate(tools):
            column = len(fixed) + index
            cell = self._cell(self.matrix_head, 0, column, each, MATRIX_HEADER, head_bg)
            self._tool_head(cell, row, index, each)
        for offset, title in enumerate(("有効", "")):
            column = len(fixed) + len(tools) + offset
            cell = self._cell(self.matrix_head, 0, column, tail[offset], MATRIX_HEADER,
                              head_bg)
            tk.Label(cell, text=title, bg=head_bg, fg=theme.MUTED, font=theme.FONT_SMALL).pack(
                side="bottom", pady=(0, 8))

        # --- 行。この端末の行をいちばん上に ---
        mine = self._this_pc_row()
        self._last_mine = mine
        visible = [pc for pc in self.pc_rows if not pc.deleted]
        if mine is not None:
            visible.remove(mine)
            visible.insert(0, mine)
        here = {id(r): r.here() for r in tools}
        for line, pc in enumerate(visible):
            bg = MATRIX_MINE_BG if pc is mine else theme.BG
            cells = [self._cell(self.matrix_inner, line, c, w, MATRIX_ROW, bg)
                     for c, w in enumerate(widths)]
            number = f"{pc.pc_no}" if pc.pc_no else "新"
            tk.Label(cells[0], text=number + (" ★" if pc is mine else ""), bg=bg,
                     fg=theme.FG if pc is mine else theme.MUTED,
                     font=theme.FONT_SMALL, anchor="w").pack(fill="both", expand=True,
                                                             padx=(8, 0))
            for cell, var, font, color in ((cells[1], pc.login_var, MATRIX_BOLD_FONT, theme.FG),
                                           (cells[2], pc.pc_var, MATRIX_BOLD_FONT, theme.FG),
                                           (cells[3], pc.note_var, theme.FONT_SMALL,
                                            theme.MUTED)):
                entry = _flat_entry(cell, var, bg=bg, fg=color, font=font)
                entry.bind("<FocusOut>", lambda _e: self._after_pc_edit())
                entry.bind("<Return>", lambda _e: self._after_pc_edit())
            for index, row in enumerate(tools):
                _Check(cells[len(fixed) + index], pc.var(row), bg=bg,
                       dim=not here[id(row)], command=self._refresh_list).place(
                    relx=0.5, rely=0.5, anchor="center")
            _Check(cells[-2], pc.enabled_var, bg=bg, command=self._after_pc_edit).place(
                relx=0.5, rely=0.5, anchor="center")
            remove = tk.Label(cells[-1], text="×", bg=bg, fg=theme.MUTED, font=theme.FONT_BOLD,
                              cursor="hand2")
            remove.place(relx=0.5, rely=0.5, anchor="center")
            remove.bind("<Button-1>", lambda _e, p=pc: self.remove_pc(p))
            remove.bind("<Enter>", lambda _e, w=remove: w.configure(
                fg=theme.STATE_COLORS["error"]))
            remove.bind("<Leave>", lambda _e, w=remove: w.configure(fg=theme.MUTED))

        # 表の終わり: PC の行を足す (ツールより先でもよい)
        add = tk.Button(self.matrix_inner, text="＋ PCを追加（ツールは後で選べます）",
                        command=self.add_pc, bg=theme.BG, fg=theme.MUTED, relief="flat",
                        bd=0, padx=12, pady=6, font=theme.FONT_SMALL, cursor="hand2",
                        activebackground=theme.BUTTON_BG, activeforeground=theme.FG,
                        highlightthickness=1, highlightbackground=theme.TOOL_ACTIVE)
        add.grid(row=len(visible), column=0, columnspan=len(widths), sticky="w",
                 padx=10, pady=10)
        if not visible:
            tk.Label(self.matrix_inner,
                     text="PC の行はまだありません。行が無いあいだは、どの PC にも、"
                          "その PC にあるツールを全部出します。",
                     bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL, anchor="w").grid(
                row=len(visible) + 1, column=0, columnspan=len(widths), sticky="w", padx=10)

        login, pc_name = tool_master.this_pc()
        if mine is not None:
            legend = (f"★ この端末（{login} / {pc_name}）の行はいちばん上 ・ "
                      "列の下の「なし」= この端末に起動ファイルが無いツール（この端末では出ません）")
        else:
            legend = (f"この端末（{login} / {pc_name}）にあたる行はありません（この端末には、"
                      "ある ツールを全部出します）・ 薄い色の列は、この端末に起動ファイルが無いツール")
        self.matrix_legend.configure(text=legend)
        self._update_status()
        if focus_last and visible:
            # 足したばかりの行 (いちばん下) のログインIDに入れるようにする
            entries = [w for w in self.matrix_inner.grid_slaves(row=len(visible) - 1, column=1)]
            for cell in entries:
                for child in cell.winfo_children():
                    child.focus_set()
            self._later(lambda: self.matrix_body_canvas.yview_moveto(1.0))

    def _tool_head(self, cell: tk.Frame, row: "_ToolRow", index: int, width: int) -> None:
        """ツールの列の見出し: 色の付いた頭文字・名前・この端末にあるか。押すと詳しい設定。"""
        here = row.here()
        bg = MATRIX_HEAD_BG
        mark = tk.Label(cell, text="✓ ある" if here else "なし", bg=bg,
                        fg=theme.STATE_COLORS["running"] if here else MATRIX_ABSENT,
                        font=MATRIX_SMALL_FONT)
        mark.pack(side="bottom", pady=(0, 6))
        name = tk.Label(cell, text=_short_name(row.label()), bg=bg,
                        fg=theme.FG if here else theme.MUTED, font=MATRIX_SMALL_FONT,
                        wraplength=width - 6, justify="center")
        name.pack(side="bottom", pady=(0, 2))
        chip = tk.Label(cell, text=_initial(row.label()), bg=_chip_color(index, here),
                        fg="#ffffff", font=theme.FONT_BOLD, width=2)
        chip.pack(side="bottom", pady=(0, 3))
        for widget in (cell, mark, name, chip):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", lambda _e, r=row: self._open_tool(r))

    def _open_tool(self, row: "_ToolRow") -> None:
        """表の見出しを押した: ［ツール］へ移り、そのツールの詳しい設定を出す。"""
        self.show_section("tools")
        self.select(row)

    def _after_pc_edit(self) -> None:
        """ログインID・PC名を直した。★ (この端末の行) を付け直す。"""
        mine = self._this_pc_row()
        if mine is not getattr(self, "_last_mine", None):
            self._last_mine = mine
            self._later(self._render_matrix)
        else:
            self._update_status()

    # ==============================================================
    # 見出し「この端末」
    # ==============================================================
    def _build_local_section(self, frame: tk.Frame) -> None:
        login, pc_name = tool_master.this_pc()
        self._section_title(frame, "この端末",
                            f"ログインID {login or '?'} / PC名 {pc_name or '?'} の設定です"
                            " (ほかの PC には配りません)。")
        self._build_pc_mode(frame)
        self._build_bar_position(frame)

    def _build_pc_mode(self, parent: tk.Frame) -> None:
        """このPCのモード (要件定義書 §15)。

        値を預かるだけで、中身は解釈しない。どのモードがあるかは
        各業務ツール側の要件なので、自由に入力できる形にしてある。
        """
        frame = tk.Frame(parent, bg=theme.BG)
        frame.pack(fill="x", pady=(6, 0))
        tk.Label(frame, text="このPCのモード", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        self.mode_var = tk.StringVar(value=tool_registry.pc_mode())
        tk.Entry(frame, textvariable=self.mode_var, width=18,
                 font=theme.FONT).pack(side="left", padx=(10, 0))
        tk.Label(frame, text="(例: 中板 / 小板。空でも動きます)",
                 bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left", padx=(8, 0))

    def _build_bar_position(self, parent: tk.Frame) -> None:
        """バーの置き場所 (要件定義書 §5.1)。

        **ランチャーを起動したとき**と**ツールを使っているとき**を、それぞれ
        7通り (画面中央・上下 × 左中右) から選ぶ。手で動かしても覚えない
        (次に状態が変わると、ここで選んだ場所へ戻る)。
        """
        # 呼び名は tkinter に触らない `geometry` が持つ。`bar` から取ると、
        # `bar` → `settings_dialog` → `bar` の輪ができる
        from .geometry import POSITION_NAMES

        self._anchor_names = dict(POSITION_NAMES)
        names = list(self._anchor_names.values())
        tk.Label(parent, text="バーの位置", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD, anchor="w").pack(fill="x", pady=(16, 0))
        self.idle_position_var = tk.StringVar(
            value=self._anchor_names[tool_registry.idle_bar_position()])
        self.active_position_var = tk.StringVar(
            value=self._anchor_names[tool_registry.active_bar_position()])
        for text, var in (("ランチャーを起動したとき", self.idle_position_var),
                          ("ツールを使っているとき", self.active_position_var)):
            line = tk.Frame(parent, bg=theme.BG)
            line.pack(fill="x", pady=(4, 0))
            tk.Label(line, text=text, bg=theme.BG, fg=theme.FG, width=22, anchor="w",
                     font=theme.FONT_SMALL).pack(side="left", padx=(12, 4))
            ttk.Combobox(line, textvariable=var, width=10, values=names,
                         state="readonly", font=theme.FONT).pack(side="left")
        tk.Label(parent, text="(手で動かした位置は、次にツールを起動・停止すると"
                              "ここで選んだ場所に戻ります)",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", padx=12, pady=(2, 0))
        self._build_select_over(parent)

    def _build_select_over(self, parent: tk.Frame) -> None:
        """ツールが多いとき、バーをボタンの列ではなく「選ぶ ▼」と［開く］にする (1.8.4)。"""
        tk.Label(parent, text="ツールが多いときのバー", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD, anchor="w").pack(fill="x", pady=(16, 0))
        line = tk.Frame(parent, bg=theme.BG)
        line.pack(fill="x", pady=(4, 0))
        tk.Label(line, text="ツールが", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_SMALL).pack(side="left", padx=(12, 4))
        self.select_over_var = tk.StringVar(value=str(tool_registry.bar_select_over()))
        tk.Spinbox(line, from_=0, to=50, width=4, textvariable=self.select_over_var,
                   font=theme.FONT).pack(side="left")
        tk.Label(line, text="個を超えたら、ボタンを並べずに「選ぶ ▼」と［開く］にする",
                 bg=theme.BG, fg=theme.FG, font=theme.FONT_SMALL).pack(side="left",
                                                                    padx=(4, 0))
        tk.Label(parent, text="(0 にすると、いつもボタンを並べます。選んだだけでは起動せず、"
                              "［開く］で起動します)",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", padx=12, pady=(2, 0))

    # ==============================================================
    # 見出し「記録・配布」
    # ==============================================================
    def _build_records_section(self, frame: tk.Frame) -> None:
        self._section_title(frame, "記録・配布",
                            "ログの書き先と、ほかの PC へ配るための配布先フォルダです。")
        self._build_log_dir(frame)
        self._build_distribution(frame)

    def _build_log_dir(self, parent: tk.Frame) -> None:
        """ログの出力先 (後追い・なぜなぜ分析の記録)。

        共有フォルダーを指定すると、全端末の記録が1か所に集まる (端末名の
        入った名前で書く)。**書けない場所でもランチャーは止めない**
        ── 書けるようになるまで端末の中に書く。
        """
        self._log_dir_before = tool_registry.get_pc_setting(tool_registry.LOG_DIR_KEY)
        self.log_dir_var = tk.StringVar(value=self._log_dir_before)

        frame = tk.Frame(parent, bg=theme.BG)
        frame.pack(fill="x", pady=(6, 0))
        tk.Label(frame, text="ログの出力先", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Entry(frame, textvariable=self.log_dir_var, width=48,
                 font=theme.FONT).pack(side="left", padx=(10, 0))
        for text, command in (("参照", self.browse_log_dir),
                              ("既定に戻す", lambda: self.log_dir_var.set("")),
                              ("開く", self.open_log_dir)):
            self._action_button(frame, text, command).pack(side="left", padx=(6, 0))

        shared = distribution.log_dir()
        blank = (f"配布先フォルダの指定 ({shared})" if shared
                 else f"この端末の中 ({trace.local_dir()})")
        for text in (f"空欄のとき: {blank}",
                     f"指定した場所に「{trace.TOOL_ID}」のフォルダーを作り、"
                     f"{trace.TOOL_ID}_{trace.pc_tag()}_日付.log のように、"
                     f"{trace.TOOL_ID}_ で始まり端末名の入った名前で書きます"
                     " (共有フォルダーなら全端末の記録が1か所に集まります)"):
            tk.Label(parent, text=text, bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL, anchor="w", justify="left",
                     wraplength=900).pack(fill="x", pady=(4, 0))
        dest = trace.destination()
        tk.Label(parent, text=f"いまの書き先: {dest.describe()}",
                 bg=theme.BG,
                 fg=theme.STATE_COLORS["error"] if dest.problem else theme.MUTED,
                 font=theme.FONT_SMALL, anchor="w", justify="left",
                 wraplength=900).pack(fill="x", pady=(2, 0))

    def browse_log_dir(self) -> None:
        current = self.log_dir_var.get().strip()
        start = trace.expand_dir(current) if current else trace.local_dir()
        chosen = filedialog.askdirectory(
            parent=self.top, title="ログの出力先",
            initialdir=str(start if os.path.isdir(start) else Path.home()))
        if chosen:
            self.log_dir_var.set(str(Path(chosen)))

    def open_log_dir(self) -> None:
        path = trace.destination().path
        if not trace.open_path(path):
            messagebox.showinfo("ログの出力先", f"開けませんでした。\n{path}",
                                parent=self.top)

    def _check_log_dir(self) -> bool:
        """ログの出力先に書けるか。書けなくても、利用者が良ければ保存する。"""
        text = self.log_dir_var.get().strip()
        if not text or text == self._log_dir_before.strip():
            return True
        target = trace.folder_for(text)
        problem = trace.check_writable(target)
        if not problem:
            return True
        return messagebox.askyesno(
            "ログの出力先",
            f"指定した場所に書けません。\n{target}\n({problem})\n\n"
            "書けるようになるまで、この端末の中に書きます:\n"
            f"{trace.local_dir()}\n\nこのまま保存しますか?", parent=self.top)

    def _build_distribution(self, parent: tk.Frame) -> None:
        """配布先フォルダ (`distribution/`)。

        1台を整えてから作り、ランチャーのフォルダーごと配る。配った先
        では起動時に読み込まれる (**その端末にすでにある設定が優先**)。
        **手で JSON を書かせない**ための入口。
        """
        frame = tk.Frame(parent, bg=theme.BG)
        frame.pack(fill="x", pady=(22, 0))
        tk.Label(frame, text="配布先フォルダ", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Label(frame, text=distribution.state_text(), bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(side="left", padx=(10, 0))
        # **どこにできるか**をいつも出す (ランチャーの起動ファイル Start.vbs と同じ場所)
        where = tk.Frame(parent, bg=theme.BG)
        where.pack(fill="x", pady=(4, 0))
        tk.Label(where, text=f"場所: {distribution.folder()}", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_SMALL, anchor="w").pack(side="left")
        self._action_button(where, "開く", self.open_distribution).pack(side="left",
                                                                        padx=(8, 0))
        tk.Label(parent, text="ランチャーの起動ファイル (Start.vbs) と同じ場所に"
                              f"「{distribution.FOLDER_NAME}」フォルダーを作ります。"
                              "ランチャーのフォルダーごと配ってください。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", pady=(2, 0))

        actions = tk.Frame(parent, bg=theme.BG)
        actions.pack(fill="x", pady=(6, 0))
        self._action_button(actions, "配布先フォルダを作る",
                            self.export_distribution).pack(side="left")
        reload = self._action_button(actions, "配布先フォルダの内容で置き換える",
                                     self.reload_distribution)
        reload.pack(side="left", padx=(8, 0))
        if not distribution.tools():
            reload.configure(state="disabled")

        # 配った先でもツールがランチャーと同じ並びに置かれるなら、相対
        # パスにしておくとドライブ名やフォルダー名が違っても動く
        self.relative_var = tk.BooleanVar(value=True)
        tk.Checkbutton(parent,
                       text="起動ファイルはランチャーのフォルダーからの相対パスで"
                            "書く (配る先でも同じ並びに置く場合)",
                       variable=self.relative_var,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0, highlightthickness=0,
                       anchor="w").pack(fill="x", pady=(6, 0))
        tk.Label(parent, text="ツール管理マスタの場所も配布先フォルダに書きます"
                              " (配った先の PC も同じマスタを読みます)。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", pady=(4, 0))

    def open_distribution(self) -> None:
        """配布先フォルダを開く (まだ無ければ、ランチャーのフォルダーを開く)。"""
        target = distribution.folder()
        if not target.is_dir():
            target = target.parent
        if not trace.open_path(target):
            messagebox.showinfo("配布先フォルダ", f"開けませんでした。\n{target}",
                                parent=self.top)

    def export_distribution(self) -> None:
        """いまの画面を保存してから、配布先フォルダを作る。

        **画面の内容と書き出す内容をずらさない。** 保存していない変更が
        あるまま作ると、「直したのに配った先に入らない」になる。
        """
        if not messagebox.askyesno(
                "配布先フォルダ",
                "いまの画面の内容を保存してから、配布先フォルダを作ります。\n\n"
                f"作る場所: {distribution.folder()}\n\n"
                "ランチャーのフォルダーごと配ると、配った先で起動したときに\n"
                "読み込まれます。すでに使っている端末では、その端末の設定が\n"
                "優先されます (まだ無いツールと、空欄の起動ファイルだけ入ります)。\n\n"
                "よろしいですか?", parent=self.top):
            return
        if not self._commit():
            return
        self.saved = True
        try:
            tool_registry.export_distribution(
                relative=bool(self.relative_var.get()))
        except OSError as exc:
            log.warning("配布先フォルダを作れません: %s", exc)
            trace.event("配布先フォルダを作る", trace.FAILED, cause=str(exc),
                        detail=str(distribution.folder()))
            messagebox.showerror(
                "配布先フォルダ",
                f"設定は保存しましたが、配布先フォルダを作れませんでした。\n{exc}\n\n"
                "ランチャーのフォルダーに書き込めるか確かめてください。",
                parent=self.top)
            self.top.destroy()
            return

        trace.event("配布先フォルダを作る", trace.OK,
                    detail=f"{distribution.folder()} "
                           f"({len(distribution.tools())}件)")
        message = (f"配布先フォルダを作りました。\n{distribution.folder()}\n\n"
                   "ランチャーのフォルダーごと配ってください。\n"
                   "配った先では、起動したときに読み込まれます。")
        fixed = distribution.absolute_entries()
        if self.relative_var.get() and fixed:
            message += ("\n\n次のツールは別のドライブにあるため、"
                        "場所をそのまま書きました。\n配る先でも同じ場所に"
                        "置いてください:\n  " + "\n  ".join(fixed))
        if not distribution.has_password():
            message += ("\n\n管理者パスワードは既定のままです。"
                        "変えるときは［管理者パスワード］の見出しで。")
        messagebox.showinfo("配布先フォルダ", message, parent=self.top)
        self.top.destroy()

    def reload_distribution(self) -> None:
        """この端末の設定を、配布先フォルダの内容で置き換える。

        ふだんはその端末の設定が優先なので、配布先フォルダを作り直しても
        すでに使っている端末は変わらない。変えたい端末でだけ押す。
        """
        names = [item.get("display_name") or item["app_id"]
                 for item in distribution.tools()]
        if not messagebox.askyesno(
                "配布先フォルダ",
                "この端末の設定を、配布先フォルダの内容で置き換えます。\n\n"
                "  " + "、".join(names) + "\n\n"
                "この端末で直した値は上書きされます。この画面で直して\n"
                "まだ保存していない内容も使いません。\n"
                "(配布先フォルダに無いツールと、この端末に無い起動ファイルは\n"
                " そのままにします)\n\n"
                "よろしいですか?", parent=self.top):
            return
        # 置き換える前に控えを取る。戻したくなったときのため
        tool_registry.backup()
        replaced = tool_registry.reload_from_distribution()
        self.saved = True
        trace.event("配布先フォルダで置き換え", trace.OK,
                    detail="、".join(replaced) or "(なし)")
        messagebox.showinfo(
            "配布先フォルダ",
            f"{len(replaced)}件のツールを、配布先フォルダの内容で置き換えました。",
            parent=self.top)
        self.top.destroy()

    # ==============================================================
    # 見出し「管理者パスワード」
    # ==============================================================
    def _build_password_section(self, frame: tk.Frame) -> None:
        """管理者パスワード (1.7.14 から既定のパスワードのまま使える)。"""
        self._section_title(frame, "管理者パスワード",
                            "［設定］を開くときに聞くパスワードです。配布先フォルダに"
                            "書くので、配った PC でも同じになります。")
        line = tk.Frame(frame, bg=theme.BG)
        line.pack(fill="x", pady=(6, 0))
        tk.Label(line, text="いま", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        self.password_state = tk.Label(line, text=_password_state(), bg=theme.BG,
                                       fg=theme.MUTED, font=theme.FONT)
        self.password_state.pack(side="left", padx=(10, 0))
        self._action_button(line, "管理者パスワードを変える",
                            self.change_password).pack(side="left", padx=(16, 0))

    def change_password(self) -> None:
        if password.change(self.top):
            self.password_state.configure(text=_password_state())

    # ==============================================================
    # 保存
    # ==============================================================
    def save(self) -> None:
        if self._commit():
            self.saved = True
            self.top.destroy()

    def _store_master_path(self) -> None:
        text = self.master_var.get().strip()
        if text:
            tool_registry.set_pc_setting(tool_registry.TOOL_MASTER_KEY, text)
        else:
            tool_registry.clear_pc_setting(tool_registry.TOOL_MASTER_KEY)
        trace.event("設定変更", trace.OK,
                    detail=f"ツール管理マスタ「{self._master_before or '(なし)'}」→"
                           f"「{text or '(なし)'}」")
        self._master_before = text

    def _pc_values(self, entries: list[tuple["_ToolRow", Tool]]) -> list[MasterPc]:
        """表の PC の行を、マスタに書く形にする (新しいツールは負の番号)。"""
        refs = {id(row): (row.master_no or -(index + 1))
                for index, (row, _tool) in enumerate(entries)}
        values = []
        for pc in self.pc_rows:
            if pc.deleted:
                continue
            numbers = tuple(refs[id(row)] for row, _tool in entries if pc.has(row))
            values.append(replace(pc.value(), tool_nos=numbers))
        return values

    def _commit(self) -> bool:
        """保存時チェック (要件定義書 §13.2)。保存できたら真。

        **1つでも駄目なら何も保存しない。** 半分だけ書き換わった状態は、
        あとから見て何が起きたのか分からなくなる。
        """
        problems: list[str] = []
        entries: list[tuple[_ToolRow, Tool]] = []
        removed: list[str] = []
        deleted_tools: list[int] = []
        seen: set[str] = set()
        shared = self.shared

        for row in self.rows:
            label = row.label()
            if row.marked_for_delete():
                if row.master_no:
                    deleted_tools.append(row.master_no)
                if not row.is_new and tool_registry.get(row.tool.app_id) is not None:
                    removed.append(row.tool.app_id)
                continue

            tool, problem = row.collect()
            if problem:
                problems.append(f"{label}: {problem}")
                continue
            if tool.app_id in seen:
                problems.append(f"{label}: アプリID {tool.app_id} が重複しています")
                continue
            seen.add(tool.app_id)
            entries.append((row, tool))

        pcs: list[MasterPc] = []
        if shared:
            pcs = self._pc_values(entries)
            problem = tool_master.check_pcs(pcs)
            if problem:
                problems.append(f"PCごとの表示: {problem}")
        if problems:
            messagebox.showerror("設定を保存できません",
                                 "\n".join(problems), parent=self.top)
            return False
        if not self._check_log_dir():
            return False

        # 確かめ (この端末で出すツールだけ。ほかの PC 用のツールで聞かない)
        local = [replace(tool, start_command=row.entry_path()) for row, tool in entries]
        if shared:
            mine = self._this_pc_row()
            local = [replace(t, enabled=True) for (row, _), t in zip(entries, local)
                     if row.here() and (mine is None or mine.has(row))]
        warnings = [_delivery_warning(tool) for tool in local]
        warnings = [text for text in warnings if text] + _shared_port_warnings(local)
        warnings += [text for text in (_exe_port_warning(tool) for tool in local) if text]
        if shared:
            warnings += self._pc_warnings(pcs)
        if warnings and not messagebox.askyesno(
                "設定",
                "\n\n".join(warnings) + "\n\nこのまま保存しますか?",
                parent=self.top):
            return False

        if shared and deleted_tools:
            if not messagebox.askyesno(
                    "設定", f"{len(deleted_tools)}件のツールを、ツール管理マスタから消します。"
                            "よろしいですか?\n(全 PC のバーから消えます。"
                            "ツール本体は消えません。登録だけです)",
                    parent=self.top):
                return False
        elif removed and not messagebox.askyesno(
                "設定", f"{len(removed)}件のツールの登録を消します。よろしいですか?\n"
                        "(ツール本体は消えません。登録だけです)",
                parent=self.top):
            return False

        # 書き換える前に控えを取る。4つ分のパスを入れ直すのは手間なので
        tool_registry.backup()
        if shared:
            path = self._master_path()
            try:
                tool_master.write_catalog(
                    path, [tool_master.from_tool(tool, tool_no=row.master_no,
                                                 note=row.master_note)
                           for row, tool in entries],
                    deleted_tools=deleted_tools, pcs=pcs, deleted_pcs=self.deleted_pcs,
                    known_tools=self._known_tools)
            except (MasterError, ValueError) as exc:
                messagebox.showerror("設定を保存できません",
                                     f"ツール管理マスタに書けませんでした。\n{exc}",
                                     parent=self.top)
                return False
            for app_id in removed:
                tool_registry.delete_tool(app_id)
        else:
            for app_id in removed:
                tool_registry.delete_tool(app_id)
            tool_registry.save_all(replace(tool, order_no=(index + 1) * 10)
                                   for index, (_row, tool) in enumerate(entries))
        tool_entries.forget()                 # 起動ファイルが変われば入口も変わる
        if self.master_var.get().strip() != self._master_before.strip():
            self._store_master_path()
        # ツール管理マスタのとおりに、この端末のツールをそろえる
        result = tool_master.sync()
        if result.changed:
            self.tools_changed = True

        changes = tool_registry.describe_changes(
            self._before_tools, tool_registry.all_tools(include_disabled=True))
        if shared:
            changes.append(f"ツール管理マスタに保存 (ツール {len(entries)}件・"
                           f"PC の行 {len(pcs)}件"
                           + (f"・消したツール {len(deleted_tools)}件" if deleted_tools else "")
                           + (f"・消した行 {len(self.deleted_pcs)}件" if self.deleted_pcs
                              else "") + ")")
        changes += self._store_local_settings()
        log.info("設定を保存しました (%d件 / 削除 %d件)", len(entries),
                 len(deleted_tools) if shared else len(removed))
        # 「昨日まで動いていたのに」を追うとき、**いつ何を変えたか**が要る
        if changes:
            trace.event("設定変更", trace.OK, detail="\n".join(changes))
        return True

    def _pc_warnings(self, pcs: list[MasterPc]) -> list[str]:
        """PC の行で、思っていたのと違う結果になりそうなもの。"""
        texts = []
        for pc in pcs:
            if not pc.login_id.strip() and not pc.pc_name.strip():
                texts.append(f"PCごとの表示: ログインIDも PC名も空欄の行 "
                             f"({'管理番号 ' + str(pc.pc_no) if pc.pc_no else '新しい行'}) "
                             "は、ほかの行に当てはまらない全部の PC に使います。")
        mine = self._this_pc_row()
        if mine is not None and not mine.deleted and mine.enabled_var.get() \
                and not any(mine.has(r) for r in self.rows if not r.marked_for_delete()):
            texts.append("PCごとの表示: この端末の行にチェックが1つもありません。"
                         "この端末のバーにツールが出なくなります。")
        return texts

    def _store_local_settings(self) -> list[str]:
        """この端末だけの設定 (モード・バーの位置・ログの出力先)。変えたものを返す。"""
        changes = []
        mode_before = tool_registry.pc_mode()
        tool_registry.set_pc_mode(self.mode_var.get().strip())
        keys = {name: key for key, name in self._anchor_names.items()}
        for label, var, current, store in (
                ("ランチャーを起動したときのバーの位置", self.idle_position_var,
                 tool_registry.idle_bar_position, tool_registry.set_idle_bar_position),
                ("ツールを使っているときのバーの位置", self.active_position_var,
                 tool_registry.active_bar_position,
                 tool_registry.set_active_bar_position)):
            chosen = keys.get(var.get())
            if chosen and chosen != current():
                changes.append(f"{label}「{var.get()}」")
                store(chosen)
        if tool_registry.pc_mode() != mode_before:
            changes.append(f"このPCのモード「{mode_before or '(空)'}」→"
                           f"「{tool_registry.pc_mode() or '(空)'}」")
        try:
            select_over = max(0, int(self.select_over_var.get().strip() or "0"))
        except ValueError:
            select_over = tool_registry.bar_select_over()
        if select_over != tool_registry.bar_select_over():
            changes.append(f"バーを「選ぶ ▼」にするツールの数「{select_over} 個を超えたら」")
            tool_registry.set_bar_select_over(select_over)
        log_dir = self.log_dir_var.get().strip()
        if log_dir != self._log_dir_before.strip():
            changes.append(f"ログの出力先「{self._log_dir_before or '(既定)'}」→"
                           f"「{log_dir or '(既定)'}」")
            trace.set_log_dir(log_dir)
        return changes

    def cancel(self) -> None:
        self.top.destroy()


class _Check(tk.Canvas):
    """大きめのチェック (案B の表)。tk の Checkbutton は小さく、地の色に埋もれる。

    `dim` は、この端末に起動ファイルが無いツールの列 (薄く出す)。
    """

    SIZE = 20

    def __init__(self, parent: tk.Widget, var: tk.BooleanVar, *, bg: str,
                 dim: bool = False, command=None) -> None:
        super().__init__(parent, width=self.SIZE, height=self.SIZE, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.var, self.dim, self.command = var, dim, command
        self._trace = var.trace_add("write", lambda *_: self._draw())
        self.bind("<Button-1>", self.toggle)
        self.bind("<Destroy>", self._forget)
        self._draw()

    def toggle(self, _event=None) -> None:
        self.var.set(not self.var.get())
        if self.command is not None:
            self.command()

    def _forget(self, _event=None) -> None:
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def _draw(self) -> None:
        try:
            self.delete("all")
        except tk.TclError:
            return
        size = self.SIZE - 2
        on = bool(self.var.get())
        if on:
            fill = CHECK_ON_DIM if self.dim else CHECK_ON
            outline = fill
        else:
            fill = CHECK_OFF_DIM if self.dim else CHECK_OFF
            outline = "#8d99a6"
        r = 4
        points = (1 + r, 1, size - r, 1, size, 1, size, 1 + r, size, size - r, size, size,
                  size - r, size, 1 + r, size, 1, size, 1, size - r, 1, 1 + r, 1, 1)
        self.create_polygon(points, smooth=True, fill=fill, outline=outline)
        if on:
            self.create_line(5, 10, 8.5, 13.5, 15, 6, fill="#ffffff", width=2.4,
                             capstyle="round", joinstyle="round")


def _flat_entry(parent: tk.Widget, var: tk.StringVar, *, bg: str, fg: str,
                font) -> tk.Entry:
    """文字に見える入力欄 (案B の表のセル)。押すと直せる。入力中だけ枠が出る。"""
    entry = tk.Entry(parent, textvariable=var, bg=bg, fg=fg, font=font, relief="flat", bd=0,
                     insertbackground=theme.FG, highlightthickness=1,
                     highlightbackground=bg, highlightcolor=theme.TOOL_ACTIVE,
                     disabledbackground=bg)
    entry.pack(fill="both", expand=True, padx=(4, 4), pady=6)
    return entry


def _initial(name: str) -> str:
    """頭文字 (表の見出しの色の付いた四角)。"""
    text = (name or "?").strip() or "?"
    return text[0].upper()


def _short_name(name: str, limit: int = 16) -> str:
    text = (name or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _chip_color(index: int, here: bool) -> str:
    return CHIP_COLORS[index % len(CHIP_COLORS)] if here else "#4a535e"


class _PcRow:
    """PC × ツールの表の1行 (値だけ。画面の部品は表を描くたびに作る)。"""

    def __init__(self, pc: MasterPc, tools: list["_ToolRow"]) -> None:
        self.pc_no = pc.pc_no
        self.deleted = False
        self.login_var = tk.StringVar(value=pc.login_id)
        self.pc_var = tk.StringVar(value=pc.pc_name)
        self.note_var = tk.StringVar(value=pc.note)
        self.enabled_var = tk.BooleanVar(value=pc.enabled)
        self._checks: dict[int, tk.BooleanVar] = {}
        for row in tools:
            self.var(row).set(True)

    def var(self, row: "_ToolRow") -> tk.BooleanVar:
        if id(row) not in self._checks:
            self._checks[id(row)] = tk.BooleanVar(value=False)
        return self._checks[id(row)]

    def has(self, row: "_ToolRow") -> bool:
        return id(row) in self._checks and bool(self._checks[id(row)].get())

    def set(self, row: "_ToolRow", value: bool) -> None:
        self.var(row).set(value)

    def title(self) -> str:
        who = " / ".join(t for t in (self.login_var.get().strip(),
                                     self.pc_var.get().strip()) if t)
        number = f"{self.pc_no}" if self.pc_no else "新しい行"
        return f"{number} ({who or 'どのPCでも'})"

    def value(self) -> MasterPc:
        return MasterPc(pc_no=self.pc_no, login_id=self.login_var.get().strip(),
                        pc_name=self.pc_var.get().strip(),
                        enabled=bool(self.enabled_var.get()),
                        note=self.note_var.get().strip())


class _ToolRow:
    """1つのツールぶんの入力欄 (［ツール］の右に出す詳しい設定)。

    `shared` のとき (ツール管理マスタを使う) は、起動ファイルに `;` 区切りで
    いくつも書ける (PC によって置き場所が違うとき)。［使う］は出さない
    (出すかどうかは［PCごとの表示］で決める)。
    """

    def __init__(self, parent: tk.Widget, tool: Tool, *,
                 is_new: bool = False, shared: bool = False) -> None:
        self.tool = tool
        self.is_new = is_new
        self.shared = shared
        # ツール管理マスタのツール番号 (0 はまだマスタに無い) と備考
        self.master_no = 0
        self.master_note = ""

        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="x", pady=(0, 12), padx=6)
        self.box = box

        # --- 名前・アプリID・使う・消す ---
        title = tk.Frame(box, bg=theme.BG)
        title.pack(fill="x")
        _label(title, "表示名")
        self.name_var = tk.StringVar(value=tool.display_name)
        tk.Entry(title, textvariable=self.name_var, width=20,
                 font=theme.FONT_BOLD).pack(side="left")

        # **アプリIDは既存の行では変えられない。** `/api/health` の照合と
        # 実行中の記録がこの値で結びついているので、途中で変えると
        # 動いているツールを見失う。新しい行だけ入力できる
        self.app_id_var = tk.StringVar(value=tool.app_id)
        if is_new:
            tk.Label(title, text="  アプリID", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL).pack(side="left")
            tk.Entry(title, textvariable=self.app_id_var, width=22,
                     font=theme.FONT_SMALL).pack(side="left", padx=(4, 0))
        else:
            tk.Label(title, text=f"  アプリID {tool.app_id}", bg=theme.BG,
                     fg=theme.MUTED, font=theme.FONT_SMALL).pack(side="left")

        flags = tk.Frame(box, bg=theme.BG)
        flags.pack(fill="x", pady=(6, 0))
        # **［使う］と［削除］は同時に選べない。** 両方にチェックが入ると、
        # どちらになるのか分からない。［削除］を選んだら［使う］は外して
        # 押せなくし、［削除］を外したら元に戻す
        self.enabled_var = tk.BooleanVar(value=tool.enabled)
        self._enabled_before_delete = tool.enabled
        self.enabled_check = tk.Checkbutton(
            flags, text="この端末のバーに出す", variable=self.enabled_var,
            bg=theme.BG, fg=theme.FG, selectcolor=theme.BUTTON_BG,
            activebackground=theme.BG, activeforeground=theme.FG,
            disabledforeground="#5c6672",
            font=theme.FONT_SMALL, bd=0, highlightthickness=0)
        self.delete_var = tk.BooleanVar(value=False)
        self.delete_check = tk.Checkbutton(
            flags, text="保存するときにこのツールを消す", variable=self.delete_var,
            command=self._on_delete_toggled,
            bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
            activebackground=theme.BG, activeforeground=theme.FG,
            font=theme.FONT_SMALL, bd=0, highlightthickness=0)
        self.delete_check.pack(side="right")

        # --- 起動ファイルのパス (要件定義書 §13.1) ---
        tk.Label(box, text="起動ファイル (start.bat / Start.vbs / exe)", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_SMALL, anchor="w").pack(fill="x",
                                                                         pady=(10, 0))
        path_row = tk.Frame(box, bg=theme.BG)
        path_row.pack(fill="x", pady=(2, 0))
        self.path_var = tk.StringVar(value=tool.start_command)
        tk.Entry(path_row, textvariable=self.path_var, font=theme.FONT,
                 width=50).pack(side="left", fill="x", expand=True)
        tk.Button(path_row, text="参照", command=self.browse,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=12, pady=3, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="left", padx=(6, 0))
        self.path_help = tk.Label(
            box, text="デスクトップの中は %DESKTOP%、ユーザーのフォルダーの中は %USERPROFILE% と"
                      "書きます (［参照］で選ぶと自動でそう書きます)。ログインIDが違う PC でも、"
                      "その PC のデスクトップを探します。置き場所がほかにも違うときは、; で区切って"
                      "いくつも書けます (その PC にある最初のものを使います)",
            bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL, anchor="w",
            justify="left", wraplength=560)
        self.path_place = tk.Label(box, text="", bg=theme.BG, fg=theme.MUTED,
                                   font=theme.FONT_SMALL, anchor="w", justify="left",
                                   wraplength=560)
        self.path_place.pack(fill="x", pady=(2, 0))

        # --- 細かい設定 (要件定義書 §14) ---
        detail = tk.Frame(box, bg=theme.BG)
        detail.pack(fill="x", pady=(10, 0))
        self.port_var = tk.StringVar(value=str(tool.port or ""))
        self.args_var = tk.StringVar(value=tool.start_args)
        self.stop_var = tk.StringVar(value=tool.stop_method)
        # 画面の出し方。「自動」は exe でポートが無ければアプリの窓
        self._ui_keys = {label: key for key, label in UI_LABELS.items()}
        self.ui_var = tk.StringVar(value=UI_LABELS.get(tool.ui_mode, "自動"))

        _label(detail, "ポート")
        tk.Entry(detail, textvariable=self.port_var, width=7,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 16))
        _label(detail, "起動引数")
        tk.Entry(detail, textvariable=self.args_var, width=24,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 2))
        # 何を入れればよいか分からないときは、起動ファイルとツールの中身を
        # 見て決める (`recommend_start_args`)
        tk.Button(detail, text="自動", command=self.recommend_args,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=8, pady=1, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="left")
        detail2 = tk.Frame(box, bg=theme.BG)
        detail2.pack(fill="x", pady=(6, 0))
        _label(detail2, "停止方法")
        ttk.Combobox(detail2, textvariable=self.stop_var, width=12,
                     values=list(STOP_METHODS), state="readonly",
                     font=theme.FONT_SMALL).pack(side="left", padx=(0, 16))
        # ブラウザーで開くか、アプリが自分の窓を出すか (Tauri などの exe)
        _label(detail2, "画面")
        ttk.Combobox(detail2, textvariable=self.ui_var, width=10,
                     values=list(UI_LABELS.values()), state="readonly",
                     font=theme.FONT_SMALL).pack(side="left")

        # 起動引数を決めた理由など、1行の案内。ふだんは出さない
        self.note = tk.Label(box, text="", bg=theme.BG, fg=theme.MUTED,
                             font=theme.FONT_SMALL, anchor="w", justify="left",
                             wraplength=560)
        # **どう設定するのがよいか**のヒント。起動ファイル・ポート・停止方法・
        # 画面を変えるたびに出し直す (ツールの入口・config/app.json の
        # ポート・隣の stop.bat / Start.vbs・ほかの行との重なりから)
        self.hint_label = tk.Label(box, text="", bg=theme.BG,
                                   fg=theme.STATE_COLORS["starting"],
                                   font=theme.FONT_SMALL, anchor="w", justify="left",
                                   wraplength=560)
        # 折り返しは**行の幅に合わせる** (決め打ちの幅だと、窓が狭いとき右が切れた)
        for label in (self.note, self.hint_label, self.path_help, self.path_place):
            label.bind("<Configure>", lambda e, w=label: w.configure(
                wraplength=max(200, e.width - 8)))
        self._probe_cache: tuple[str, dict] = ("", {})
        self._hint_job = None
        for var in (self.path_var, self.port_var, self.stop_var, self.ui_var,
                    self.args_var):
            var.trace_add("write", lambda *_: self._schedule_hints())
        self.set_shared(shared)
        self.refresh_hints()

    def set_shared(self, shared: bool) -> None:
        """ツール管理マスタを使うか。使うなら［この端末のバーに出す］は出さない。"""
        self.shared = shared
        self.enabled_check.pack_forget()
        self.path_help.pack_forget()
        if shared:
            self.path_help.pack(fill="x", before=self.path_place)
        else:
            self.enabled_check.pack(side="left")
        self._update_place()

    def _update_place(self) -> None:
        text, color = self.place()
        self.path_place.configure(text=text, fg=color)

    def candidates(self) -> list[str]:
        if not self.shared:
            text = self.path_var.get().strip().strip('"')
            return [text] if text else []
        return MasterTool(0, start_command=self.path_var.get()).candidates()

    def entry_path(self) -> str:
        """この端末で使う起動ファイル (候補のうち、この端末にある最初のもの)。"""
        candidates = self.candidates()
        for candidate in candidates:
            if fileprobe.is_file(candidate):
                return candidate
        return candidates[0] if candidates else ""

    def here(self) -> bool:
        """起動ファイルがこの端末にあるか。"""
        return any(fileprobe.is_file(c) for c in self.candidates())

    def place(self) -> tuple[str, str]:
        """起動ファイルがこの端末にあるかの1行と色。"""
        candidates = self.candidates()
        if not candidates:
            return "起動ファイルが決まっていません", theme.STATE_COLORS["starting"]
        if self.here():
            found = self.entry_path()
            extra = (f" ({found})" if len(candidates) > 1 or "%" in self.path_var.get()
                     else "")
            return f"この端末にあります{extra}", theme.STATE_COLORS["running"]
        if self.shared:
            return ("この端末にはありません (ほかの PC 用ならこのままでかまいません。"
                    "この端末では出しません)"), theme.MUTED
        return "この端末にはありません", theme.STATE_COLORS["error"]

    def status(self) -> tuple[str, str]:
        """一覧に出す短い状態と色。"""
        if self.marked_for_delete():
            return "消す", theme.STATE_COLORS["error"]
        if self.is_new:
            return "新しい", theme.STATE_COLORS["starting"]
        if not self.candidates():
            return "未設定", theme.STATE_COLORS["starting"]
        if not self.here():
            return "この端末に無い", theme.MUTED
        if not self.shared and not self.enabled_var.get():
            return "出さない", theme.MUTED
        return "ある", theme.STATE_COLORS["running"]

    def subtitle(self) -> str:
        parts = [self.app_id_var.get().strip() or "(アプリID なし)"]
        if self.shared and not self.master_no and not self.is_new:
            parts.append("この端末だけ (保存するとマスタに入ります)")
        entry = self.entry_path()
        if entry:
            parts.append(Path(entry.replace("\\", "/")).name)
        return " ・ ".join(parts)

    def _schedule_hints(self) -> None:
        """打っているあいだは出し直さない (打ち終わってから)。"""
        if self._hint_job is not None:
            try:
                self.hint_label.after_cancel(self._hint_job)
            except tk.TclError:
                pass
        self._hint_job = self.hint_label.after(400, self.refresh_hints)

    def cancel_jobs(self) -> None:
        if self._hint_job is not None:
            try:
                self.hint_label.after_cancel(self._hint_job)
            except tk.TclError:
                pass
            self._hint_job = None

    def draft(self) -> Tool:
        """いま入っている値の Tool (確かめずに。ヒント用)。"""
        raw_port = self.port_var.get().strip()
        try:
            port = int(raw_port) if raw_port else 0
        except ValueError:
            port = 0
        return replace(self.tool,
                       start_command=self.entry_path(),
                       start_args=self.args_var.get().strip(), port=port,
                       stop_method=self.stop_var.get().strip() or "auto",
                       ui_mode=self._ui_keys.get(self.ui_var.get(), ""))

    def refresh_hints(self) -> None:
        self._hint_job = None
        self._update_place()
        tool = self.draft()
        path = tool.start_command
        if self._probe_cache[0] != path:
            try:
                found = tool_registry.probe_tool_folder(path) if path else {}
            except Exception:                 # noqa: BLE001 - ヒントで止めない
                found = {}
            self._probe_cache = (path, found)
        try:
            hints = tool_registry.setting_hints(tool, probed=self._probe_cache[1])
        except Exception:                     # noqa: BLE001 - ヒントで止めない
            log.warning("設定のヒントを作れませんでした", exc_info=True)
            hints = []
        self.hints = hints
        if hints:
            self.hint_label.configure(text="\n".join(_hint_line(h) for h in hints))
            self.hint_label.pack(fill="x", pady=(8, 0))
        else:
            self.hint_label.pack_forget()

    def show_note(self, text: str) -> None:
        if text:
            self.note.configure(text=text)
            self.note.pack(fill="x", pady=(8, 0), before=self.hint_label
                           if self.hint_label.winfo_manager() else None)
        else:
            self.note.pack_forget()

    def recommend_args(self) -> None:
        """［自動］。いまの起動ファイルから起動引数を決め直す。"""
        path = self.entry_path()
        args, reason = tool_registry.recommend_start_args(path)
        if fileprobe.is_file(path):
            self.args_var.set(args)
        self.show_note(reason)

    def _on_delete_toggled(self) -> None:
        if self.delete_var.get():
            self._enabled_before_delete = bool(self.enabled_var.get())
            self.enabled_var.set(False)
            self.enabled_check.configure(state="disabled")
        else:
            self.enabled_check.configure(state="normal")
            self.enabled_var.set(self._enabled_before_delete)

    def browse(self) -> None:
        """ファイル選択ダイアログからBATを選ぶ (要件定義書 §13.1)。"""
        current = self.entry_path()
        initial = str(Path(current).parent) if current else ""
        chosen = filedialog.askopenfilename(
            title=f"{self.tool.display_name} の起動ファイルを選んでください",
            initialdir=initial or None, filetypes=ENTRY_FILETYPES)
        if chosen:
            # マスタに書くときは、デスクトップの中なら %DESKTOP% で書く
            self.path_var.set(tool_master.portable(chosen) if self.shared else chosen)
            note = ""
            found = tool_registry.probe_tool_folder(chosen)
            if self.ui_var.get() == "自動" and found.get("ui_mode"):
                # exe・自分の窓を出す Python のツール (start.bat) は「アプリの窓」
                self.ui_var.set(UI_LABELS[found["ui_mode"]])
            if chosen.lower().endswith(".exe"):
                # ブラウザー版のポートが残っていれば空にする (exe は待ち受けない)
                if found.get("browser_port") and \
                        self.port_var.get().strip() == str(found["browser_port"]):
                    self.port_var.set("")
                note = _port_note(found)
            # 起動ファイルが変われば、渡せる引数も変わる
            self.recommend_args()
            if note:
                self.show_note("\n".join(t for t in (self.note.cget("text"), note) if t))

    def label(self) -> str:
        """問題を知らせるときの呼び名。"""
        return (self.name_var.get().strip() or self.app_id_var.get().strip()
                or "(名前のない行)")

    def marked_for_delete(self) -> bool:
        return bool(self.delete_var.get())

    def collect(self) -> tuple[Tool, str]:
        """入力を確かめて、新しい設定を組み立てる。

        ツール管理マスタを使うとき (`shared`) は、起動ファイルは書いたまま
        (`;` 区切りの候補のまま) 返す。この端末に無くてもよい (形だけ確かめる)。
        """
        app_id = self.app_id_var.get().strip()
        problem = tool_registry.validate_app_id(app_id)
        if problem:
            return self.tool, problem
        if self.is_new and not self.shared and tool_registry.get(app_id) is not None:
            return self.tool, f"アプリID {app_id} はすでに登録されています"

        name = self.name_var.get().strip()
        if not name:
            return self.tool, "表示名を入力してください"

        path = self.path_var.get().strip().strip('"')
        if self.shared:
            problem = tool_master.check_start(path)
        else:
            problem = tool_registry.validate_start_command(path)
        if problem:
            return self.tool, problem

        raw_port = self.port_var.get().strip()
        try:
            port = int(raw_port) if raw_port else 0
        except ValueError:
            return self.tool, "ポート番号は数字で入力してください"
        problem = tool_registry.validate_port(port)
        if problem:
            return self.tool, problem

        tool = replace(self.tool,
                       app_id=app_id,
                       display_name=name,
                       start_command=path,
                       start_args=self.args_var.get().strip(),
                       port=port,
                       stop_method=self.stop_var.get().strip() or "auto",
                       enabled=bool(self.enabled_var.get()),
                       ui_mode=self._ui_keys.get(self.ui_var.get(), ""))
        # 画面の出し方と起動ファイル・ポートが噛み合わなければ、押してから
        # 待たせる前に、ここで言う
        problem = replace(tool, start_command=self.entry_path()).ui_problem()
        if problem:
            return self.tool, problem
        return tool, ""


def _password_state() -> str:
    return "(変えてあります)" if distribution.has_password() else "(既定のまま)"


def _hint_line(text: str) -> str:
    """ヒント1つを画面の1行に。**半角の空白で折り返させない。**

    Tk は空白の位置で折り返すので、「ヒント: 」や「 (」のところで改行されて
    「ヒント:」だけの行ができていた。全角にして、文字の途中で折り返させる。
    """
    return "ヒント：" + (text.replace(" (", "（").replace(") ", "）")
                        .replace("(", "（").replace(")", "）"))


def _port_note(found: dict) -> str:
    """exe の行のポートを空にした理由 (ブラウザー版のポートだった)。"""
    port = found.get("browser_port")
    if not port:
        return ""
    return f"ポートは空にしました ({port} はブラウザー版の番号で、exe 版では使いません)"


def _exe_port_warning(tool: Tool) -> str:
    """exe の行にポートがある。デスクトップ版なら空にすべき (現場の4ツールの報告)。"""
    if not tool.enabled or tool.entry_kind != "exe" or tool.port <= 0:
        return ""
    return (f"{tool.display_name or tool.app_id}: exe の行にポート {tool.port} があります。"
            "exe (デスクトップ版) がそのポートで待ち受けないなら空にしてください。"
            "そのままだと、起動確認を待ち続けて「応答しません」と知らせます。")


def _shared_port_warnings(tools: list[Tool]) -> list[str]:
    """同じポートを使うツールが2つ以上あれば、その知らせ。

    同じツールの Python 版 (Start.vbs) と exe 版 (Tauri) を両方登録すると
    起きやすい。**同時には動かせない** (あとから押したほうは、先に動いて
    いるほうに断られる)。保存は止めない ── 片方ずつ使うなら困らない。

    **どれも自分で次の空きポートへ移るツールなら知らせない** (1.7.10)。
    Display-file-list と excel-format-converter (8753) のように、別のツールが
    たまたま同じ番号から始まるだけで、同時に使える。
    """
    by_port: dict[int, list[Tool]] = {}
    for tool in tools:
        if tool.enabled and tool.port > 0:
            by_port.setdefault(tool.port, []).append(tool)
    return [f"{'、'.join(t.display_name or t.app_id for t in group)} が同じポート {port} "
            "を使います。同時には動かせません (あとから押したほうは起動できません)。"
            for port, group in sorted(by_port.items())
            if len(group) > 1 and not all(tool_registry.port_retry(t) > 0 for t in group)]


def _delivery_warning(tool: Tool) -> str:
    """起動引数が届かない組み合わせを知らせる文。問題なければ空文字。

    `.vbs` は引数を転送するとは限らない (4ツールの `Start.vbs` は
    転送しない)。黙って保存すると「`--no-browser` を付けたのにタブが
    開く」ことになり、原因にたどり着きにくい。
    """
    if not tool.start_args.strip() or tool.forwards_args:
        return ""
    name = Path(tool.start_command).name
    return (f"{tool.display_name}: 起動引数「{tool.start_args}」は "
            f"{name} が転送しないため届きません。\n"
            "  画面はツール側がふだんのブラウザーに開き、"
            "止めるときランチャーからは閉じられません。")


def _label(parent: tk.Widget, text: str) -> None:
    tk.Label(parent, text=text, bg=theme.BG, fg=theme.MUTED,
             font=theme.FONT_SMALL).pack(side="left", padx=(0, 4))
