"""ランチャー設定画面 (要件定義書 §13 / §14)

**必須要件**。端末ごとにリポジトリの置き場所が違うので、`start.bat` の
場所をここで変えられるようにする。コードを書き換えずに済ませることが
目的 (要件定義書 §13.3)。

    ┌──────────────────────────────────────────────┐
    │                 ランチャー設定                │
    ├──────────────────────────────────────────────┤
    │ 日報                                          │
    │ [ C:\\業務ツール\\日報\\start.bat     ] [参照] │
    │ ポート [8733] 起動引数 [--no-browser]         │
    │                                              │
    │                    [保存] [キャンセル]        │
    └──────────────────────────────────────────────┘
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import app_config, tool_registry
from ..logging_utils import get_logger
from ..tool_registry import STOP_METHODS, Tool
from . import theme

log = get_logger("ui.settings")

# ダイアログの高さの上限。ツールが増えても画面からはみ出さないよう、
# ここを超えたら中身を巻物にする
MAX_BODY_HEIGHT = 460


class SettingsDialog:
    """モーダルの設定画面。閉じるまで戻らない。"""

    def __init__(self, parent: tk.Misc) -> None:
        self.saved = False
        self.rows: list[_ToolRow] = []

        self.top = tk.Toplevel(parent)
        self.top.title(f"{app_config.display_name()} - 設定")
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.top.resizable(False, True)

        self._build()
        self._center_on(parent)

        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.top.wait_window()

    # --------------------------------------------------------------
    def _build(self) -> None:
        header = tk.Label(
            self.top, text="各ツールの start.bat の場所を指定してください",
            bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD, anchor="w")
        header.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(self.top,
                 text="PCごとに置き場所が違っていても、ここを変えるだけで動きます。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 10))

        body = self._scrollable_body()
        for tool in tool_registry.all_tools(include_disabled=True):
            self.rows.append(_ToolRow(body, tool))

        self._build_pc_mode()
        self._build_bar_position()
        self._build_buttons()

    def _scrollable_body(self) -> tk.Frame:
        """ツールが増えても画面に収まるよう、中身を巻物にする。"""
        container = tk.Frame(self.top, bg=theme.BG)
        container.pack(fill="both", expand=True, padx=10)

        canvas = tk.Canvas(container, bg=theme.BG, highlightthickness=0,
                           height=MAX_BODY_HEIGHT)
        scroll = ttk.Scrollbar(container, orient="vertical",
                               command=canvas.yview)
        inner = tk.Frame(canvas, bg=theme.BG)

        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)

        def on_configure(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())
            # 中身が収まるなら巻物の高さを縮める。4つしか無いのに
            # 空白の広い画面を出さない
            needed = min(inner.winfo_reqheight(), MAX_BODY_HEIGHT)
            canvas.configure(height=needed)

        inner.bind("<Configure>", on_configure)
        canvas.bind("<Configure>", on_configure)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return inner

    def _build_pc_mode(self) -> None:
        """このPCのモード (要件定義書 §15)。

        値を預かるだけで、中身は解釈しない。どのモードがあるかは
        各業務ツール側の要件なので、自由に入力できる形にしてある。
        """
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(frame, text="このPCのモード", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        self.mode_var = tk.StringVar(value=tool_registry.pc_mode())
        tk.Entry(frame, textvariable=self.mode_var, width=18,
                 font=theme.FONT).pack(side="left", padx=(10, 0))
        tk.Label(frame, text="(例: 中板 / 小板。空でも動きます)",
                 bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left", padx=(8, 0))

    def _build_bar_position(self) -> None:
        """バーの置き場所 (要件定義書 §5.1)。

        ふだんは状態に合わせて自動で寄る (何も選んでいなければ中央、
        ツールを選んだら左下)。**手で動かすとそちらが優先される**ので、
        自動に戻す道をここに用意する。
        """
        # 鍵は tkinter に触らない `geometry` が持つ。`bar` から取ると、
        # `bar` → `settings_dialog` → `bar` の輪ができる
        from .geometry import POSITION_KEY, parse_saved

        self._position_key = POSITION_KEY
        saved = parse_saved(tool_registry.get_pc_setting(POSITION_KEY))
        self.reset_position = tk.BooleanVar(value=False)

        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(frame, text="バーの位置", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        if saved is None:
            tk.Label(frame,
                     text="自動（何も選んでいなければ中央、選ぶと左下）",
                     bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL).pack(side="left", padx=(10, 0))
            return

        tk.Label(frame, text=f"手動（{saved[0]}, {saved[1]}）",
                 bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left", padx=(10, 0))
        tk.Checkbutton(frame, text="自動に戻す", variable=self.reset_position,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0,
                       highlightthickness=0).pack(side="left", padx=(10, 0))

    def _build_buttons(self) -> None:
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=14)
        tk.Button(frame, text="キャンセル", command=self.cancel,
                  bg=theme.BUTTON_BG, fg=theme.MUTED, relief="flat", bd=0,
                  padx=16, pady=6, font=theme.FONT,
                  cursor="hand2").pack(side="right")
        tk.Button(frame, text="保存", command=self.save,
                  bg=theme.BUTTON_CURRENT, fg=theme.FG, relief="flat", bd=0,
                  padx=22, pady=6, font=theme.FONT_BOLD,
                  cursor="hand2").pack(side="right", padx=(0, 8))

    def _center_on(self, parent: tk.Misc) -> None:
        self.top.update_idletasks()
        width = self.top.winfo_reqwidth()
        height = self.top.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = max(20, parent.winfo_rooty() - height - 20)
        self.top.geometry(f"+{max(0, x)}+{y}")

    # --------------------------------------------------------------
    def save(self) -> None:
        """保存時チェック (要件定義書 §13.2)。

        **1つでも駄目なら何も保存しない。** 半分だけ書き換わった状態は、
        あとから見て何が起きたのか分からなくなる。
        """
        problems: list[str] = []
        updated: list[Tool] = []
        for row in self.rows:
            tool, problem = row.collect()
            if problem:
                problems.append(f"{row.tool.display_name}: {problem}")
            else:
                updated.append(tool)

        if problems:
            messagebox.showerror("設定を保存できません",
                                 "\n".join(problems), parent=self.top)
            return

        # 書き換える前に控えを取る。4つ分のパスを入れ直すのは手間なので
        tool_registry.backup()
        tool_registry.save_all(updated)
        tool_registry.set_pc_mode(self.mode_var.get().strip())
        if self.reset_position.get():
            tool_registry.clear_pc_setting(self._position_key)
        log.info("設定を保存しました (%d件)", len(updated))
        self.saved = True
        self.top.destroy()

    def cancel(self) -> None:
        self.top.destroy()


class _ToolRow:
    """1つのツールぶんの入力欄。"""

    def __init__(self, parent: tk.Widget, tool: Tool) -> None:
        self.tool = tool

        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="x", pady=(0, 12), padx=6)

        title = tk.Frame(box, bg=theme.BG)
        title.pack(fill="x")
        tk.Label(title, text=tool.display_name, bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Label(title, text=f"  {tool.app_id}", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left")
        self.enabled_var = tk.BooleanVar(value=tool.enabled)
        tk.Checkbutton(title, text="使う", variable=self.enabled_var,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0,
                       highlightthickness=0).pack(side="right")

        # --- start.bat のパス (要件定義書 §13.1) ---
        path_row = tk.Frame(box, bg=theme.BG)
        path_row.pack(fill="x", pady=(4, 0))
        self.path_var = tk.StringVar(value=tool.start_command)
        tk.Entry(path_row, textvariable=self.path_var, font=theme.FONT,
                 width=54).pack(side="left", fill="x", expand=True)
        tk.Button(path_row, text="参照", command=self.browse,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=12, pady=3, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="left", padx=(6, 0))

        # --- 細かい設定 (要件定義書 §14) ---
        detail = tk.Frame(box, bg=theme.BG)
        detail.pack(fill="x", pady=(4, 0))
        self.port_var = tk.StringVar(value=str(tool.port or ""))
        self.args_var = tk.StringVar(value=tool.start_args)
        self.stop_var = tk.StringVar(value=tool.stop_method)

        _label(detail, "ポート")
        tk.Entry(detail, textvariable=self.port_var, width=7,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 12))
        _label(detail, "起動引数")
        tk.Entry(detail, textvariable=self.args_var, width=16,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 12))
        _label(detail, "停止方法")
        ttk.Combobox(detail, textvariable=self.stop_var, width=13,
                     values=list(STOP_METHODS), state="readonly",
                     font=theme.FONT_SMALL).pack(side="left")

    def browse(self) -> None:
        """ファイル選択ダイアログからBATを選ぶ (要件定義書 §13.1)。"""
        current = self.path_var.get().strip()
        initial = str(Path(current).parent) if current else ""
        chosen = filedialog.askopenfilename(
            title=f"{self.tool.display_name} の start.bat を選んでください",
            initialdir=initial or None,
            filetypes=[("バッチファイル", "*.bat"), ("すべてのファイル", "*.*")])
        if chosen:
            self.path_var.set(chosen)

    def collect(self) -> tuple[Tool, str]:
        """入力を確かめて、新しい設定を組み立てる。"""
        path = self.path_var.get().strip().strip('"')
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

        return replace(self.tool,
                       start_command=path,
                       start_args=self.args_var.get().strip(),
                       port=port,
                       stop_method=self.stop_var.get().strip() or "auto",
                       enabled=bool(self.enabled_var.get())), ""


def _label(parent: tk.Widget, text: str) -> None:
    tk.Label(parent, text=text, bg=theme.BG, fg=theme.MUTED,
             font=theme.FONT_SMALL).pack(side="left", padx=(0, 4))
