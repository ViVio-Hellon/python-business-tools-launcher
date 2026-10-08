"""ランチャー設定画面 (要件定義書 §13 / §14)

**必須要件**。端末ごとにリポジトリの置き場所が違うので、起動ファイルの
場所をここで変えられるようにする。`start.bat`・`Start.vbs`・exe (Tauri の
アプリ、Python から作った exe) のどれも指定できる。コードを書き換えずに済ませることが
目的 (要件定義書 §13.3)。

    ┌──────────────────────────────────────────────┐
    │                 ランチャー設定                │
    ├──────────────────────────────────────────────┤
    │ 日報                                          │
    │ [ C:\\業務ツール\\日報\\start.bat     ] [参照] │
    │   (Start.vbs も指定できます)                  │
    │ ポート [8733] 起動引数 [--no-browser]         │
    │                                              │
    │ ログの出力先 [ 共有フォルダー ] [参照][既定][開く] │
    │ 配布先フォルダ [作る] [置き換える] [パスワード] │
    │                    [保存] [キャンセル]        │
    └──────────────────────────────────────────────┘
"""
from __future__ import annotations

import os
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import (app_config, distribution, fileprobe, tool_entries,
               tool_registry, trace)
from ..logging_utils import get_logger
from ..tool_registry import STOP_METHODS, UI_LABELS, Tool
from . import password, theme

log = get_logger("ui.settings")

# ダイアログの高さの上限。ツールが増えても画面からはみ出さないよう、
# ここを超えたら中身を巻物にする
MAX_BODY_HEIGHT = 460

# 起動ファイルを選ぶダイアログの種類
ENTRY_FILETYPES = [("起動ファイル", "*.bat *.vbs *.exe"),
                   ("バッチファイル", "*.bat"),
                   ("VBScript", "*.vbs"),
                   ("アプリ (exe)", "*.exe"),
                   ("すべてのファイル", "*.*")]


class SettingsDialog:
    """モーダルの設定画面。閉じるまで戻らない。"""

    def __init__(self, parent: tk.Misc) -> None:
        self.saved = False
        self.rows: list[_ToolRow] = []

        self.top = tk.Toplevel(parent)
        self.top.title(f"{app_config.display_name()} "
                       f"{app_config.version_label()} - 設定")
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
            self.top, text="各ツールの起動ファイル (start.bat / Start.vbs / exe) "
                            "の場所を指定してください",
            bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD, anchor="w")
        header.pack(fill="x", padx=16, pady=(14, 2))
        tk.Label(self.top,
                 text="PCごとに置き場所が違っていても、ここを変えるだけで動きます。"
                      "ツールの追加・削除もここで行えます。\n"
                      "各行の下の「ヒント」(橙色) に、選んだ起動ファイルでのおすすめの"
                      "設定が出ます (ポート・停止方法・登録しないほうがよいもの)。",
                 justify="left",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(fill="x", padx=16, pady=(0, 10))

        self.body = self._scrollable_body()
        tools = tool_registry.all_tools(include_disabled=True)
        # 開いたときの設定。保存したとき**何が変わったか**を記録に残す
        self._before_tools = list(tools)
        for tool in tools:
            self.rows.append(_ToolRow(self.body, tool))
        if not tools:
            # 出荷時は空。1行目の作り方を案内する
            self._empty_hint = tk.Label(
                self.body,
                text="まだツールがありません。\n"
                     "下の［＋ ツールを追加］で、各ツールの start.bat (または "
                     "Start.vbs) を選んでください。\n"
                     "アプリID・表示名・ポートは、そのツールの config/app.json "
                     "から読み取ります。",
                bg=theme.BG, fg=theme.MUTED, font=theme.FONT, justify="left",
                anchor="w")
            self._empty_hint.pack(fill="x", padx=6, pady=(4, 12))

        self._build_add_button()
        self._build_pc_mode()
        self._build_bar_position()
        self._build_log_dir()
        self._build_distribution()
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

    def _build_add_button(self) -> None:
        """5個目以降のツールを足す (要件定義書 §14)。"""
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(6, 0))
        tk.Button(frame, text="＋ ツールを追加", command=self.add_tool,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=14, pady=5, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="left")
        tk.Label(frame, text="起動ファイル (bat / vbs / exe) を選ぶと、"
                              "アプリIDと表示名を読み取ります",
                 bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left", padx=(10, 0))

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
        hint = getattr(self, "_empty_hint", None)
        if hint is not None:
            hint.destroy()
            self._empty_hint = None
        row = _ToolRow(self.body, tool, is_new=True)
        row.show_note(reason)
        self.rows.append(row)
        if not found:
            messagebox.showinfo(
                "設定",
                "そのフォルダーから設定を読み取れませんでした。\n"
                "アプリIDと表示名を入力してください。\n\n"
                "アプリIDは、そのツールの config/app.json の app_id と"
                "同じ値にしてください。",
                parent=self.top)

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

        起動した直後は**いつも画面中央** (探さずに見つかる)。ツールを
        起動したら、ここで選んだ場所へ寄る。**手で動かすとそちらが優先
        される**ので、自動に戻す道もここに用意する。
        """
        # 鍵と呼び名は tkinter に触らない `geometry` が持つ。`bar` から
        # 取ると、`bar` → `settings_dialog` → `bar` の輪ができる
        from .geometry import ACTIVE_ANCHORS, POSITION_KEY, parse_saved

        self._position_key = POSITION_KEY
        self._anchor_names = dict(ACTIVE_ANCHORS)
        saved = parse_saved(tool_registry.get_pc_setting(POSITION_KEY))
        self.reset_position = tk.BooleanVar(value=False)

        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(frame, text="ツールを起動したあとのバーの位置", bg=theme.BG,
                 fg=theme.FG, font=theme.FONT_BOLD).pack(side="left")
        current = tool_registry.active_bar_position()
        self.active_position_var = tk.StringVar(
            value=self._anchor_names.get(current, "左下"))
        ttk.Combobox(frame, textvariable=self.active_position_var, width=8,
                     values=list(self._anchor_names.values()), state="readonly",
                     font=theme.FONT).pack(side="left", padx=(10, 0))
        tk.Label(frame, text="(起動した直後はいつも画面中央)", bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_SMALL).pack(side="left",
                                                             padx=(8, 0))

        if saved is None:
            return
        manual = tk.Frame(self.top, bg=theme.BG)
        manual.pack(fill="x", padx=16, pady=(4, 0))
        tk.Label(manual, text=f"いまは手で置いた場所（{saved[0]}, {saved[1]}）"
                              "に固定しています",
                 bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).pack(side="left")
        tk.Checkbutton(manual, text="自動に戻す", variable=self.reset_position,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0,
                       highlightthickness=0).pack(side="left", padx=(10, 0))

    def _build_log_dir(self) -> None:
        """ログの出力先 (後追い・なぜなぜ分析の記録)。

        共有フォルダーを指定すると、全端末の記録が1か所に集まる (端末名の
        フォルダーに分けて書く)。**書けない場所でもランチャーは止めない**
        ── 書けるようになるまで端末の中に書く。
        """
        self._log_dir_before = tool_registry.get_pc_setting(trace.LOG_DIR_KEY)
        self.log_dir_var = tk.StringVar(value=self._log_dir_before)

        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(frame, text="ログの出力先", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Entry(frame, textvariable=self.log_dir_var, width=40,
                 font=theme.FONT).pack(side="left", padx=(10, 0))
        for text, command in (("参照", self.browse_log_dir),
                              ("既定に戻す", lambda: self.log_dir_var.set("")),
                              ("開く", self.open_log_dir)):
            self._action_button(frame, text, command).pack(side="left",
                                                           padx=(6, 0))

        shared = distribution.log_dir()
        blank = (f"配布先フォルダの指定 ({shared})" if shared
                 else f"この端末の中 ({trace.local_dir()})")
        for text in (f"空欄のとき: {blank}",
                     "共有フォルダーを指定すると、端末名のフォルダー "
                     f"({trace.computer_name()}) に分けて書きます"
                     " (全端末の記録が1か所に集まります)"):
            tk.Label(self.top, text=text, bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL, anchor="w", justify="left",
                     wraplength=640).pack(fill="x", padx=16, pady=(4, 0))
        dest = trace.destination()
        tk.Label(self.top, text=f"いまの書き先: {dest.describe()}",
                 bg=theme.BG,
                 fg=theme.STATE_COLORS["error"] if dest.problem else theme.MUTED,
                 font=theme.FONT_SMALL, anchor="w", justify="left",
                 wraplength=640).pack(fill="x", padx=16, pady=(2, 0))

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

    def _build_distribution(self) -> None:
        """配布先フォルダ (`distribution/`)。

        1台を整えてから作り、ランチャーのフォルダーごと配る。配った先
        では起動時に読み込まれる (**その端末にすでにある設定が優先**)。
        **手で JSON を書かせない**ための入口。
        """
        frame = tk.Frame(self.top, bg=theme.BG)
        frame.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(frame, text="配布先フォルダ", bg=theme.BG, fg=theme.FG,
                 font=theme.FONT_BOLD).pack(side="left")
        tk.Label(frame, text=distribution.state_text(), bg=theme.BG,
                 fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").pack(side="left", padx=(10, 0))

        actions = tk.Frame(self.top, bg=theme.BG)
        actions.pack(fill="x", padx=16, pady=(4, 0))
        self._action_button(actions, "配布先フォルダを作る",
                            self.export_distribution).pack(side="left")
        reload = self._action_button(actions, "配布先フォルダの内容で置き換える",
                                     self.reload_distribution)
        reload.pack(side="left", padx=(8, 0))
        if not distribution.tools():
            reload.configure(state="disabled")
        self._action_button(actions, "管理者パスワードを変える",
                            self.change_password).pack(side="left", padx=(8, 0))

        # 配った先でもツールがランチャーと同じ並びに置かれるなら、相対
        # パスにしておくとドライブ名やフォルダー名が違っても動く
        self.relative_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self.top,
                       text="起動ファイルはランチャーのフォルダーからの相対パスで"
                            "書く (配る先でも同じ並びに置く場合)",
                       variable=self.relative_var,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0, highlightthickness=0,
                       anchor="w").pack(fill="x", padx=16, pady=(4, 0))

    @staticmethod
    def _action_button(parent: tk.Widget, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command,
                         bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                         padx=14, pady=5, font=theme.FONT_SMALL,
                         disabledforeground=theme.MUTED, cursor="hand2")

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
            message += "\n\n管理者パスワードが入っていません。"
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

    def change_password(self) -> None:
        password.change(self.top)

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
        if self._commit():
            self.saved = True
            self.top.destroy()

    def _commit(self) -> bool:
        """保存時チェック (要件定義書 §13.2)。保存できたら真。

        **1つでも駄目なら何も保存しない。** 半分だけ書き換わった状態は、
        あとから見て何が起きたのか分からなくなる。
        """
        problems: list[str] = []
        updated: list[Tool] = []
        removed: list[str] = []
        seen: set[str] = set()

        for row in self.rows:
            label = row.label()
            if row.marked_for_delete():
                if not row.is_new:
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
            updated.append(tool)

        if problems:
            messagebox.showerror("設定を保存できません",
                                 "\n".join(problems), parent=self.top)
            return False
        if not self._check_log_dir():
            return False

        warnings = [_delivery_warning(tool) for tool in updated]
        warnings = [text for text in warnings if text] + _shared_port_warnings(updated)
        warnings += [text for text in (_exe_port_warning(tool) for tool in updated) if text]
        if warnings and not messagebox.askyesno(
                "設定",
                "\n\n".join(warnings) + "\n\nこのまま保存しますか?",
                parent=self.top):
            return False

        if removed and not messagebox.askyesno(
                "設定", f"{len(removed)}件のツールの登録を消します。よろしいですか?\n"
                        "(ツール本体は消えません。登録だけです)",
                parent=self.top):
            return False

        # 書き換える前に控えを取る。4つ分のパスを入れ直すのは手間なので
        tool_registry.backup()
        for app_id in removed:
            tool_registry.delete_tool(app_id)
        tool_registry.save_all(updated)
        tool_entries.forget()                 # 起動ファイルが変われば入口も変わる
        mode_before = tool_registry.pc_mode()
        tool_registry.set_pc_mode(self.mode_var.get().strip())
        if self.reset_position.get():
            tool_registry.clear_pc_setting(self._position_key)
        chosen = {name: key for key, name in self._anchor_names.items()}.get(
            self.active_position_var.get())
        changes = tool_registry.describe_changes(
            self._before_tools, tool_registry.all_tools(include_disabled=True))
        if chosen and chosen != tool_registry.active_bar_position():
            changes.append(f"起動後のバーの位置「{self.active_position_var.get()}」")
            tool_registry.set_active_bar_position(chosen)
        if self.reset_position.get():
            changes.append("バーの位置を自動に戻す")
        if tool_registry.pc_mode() != mode_before:
            changes.append(f"このPCのモード「{mode_before or '(空)'}」→"
                           f"「{tool_registry.pc_mode() or '(空)'}」")
        log_dir = self.log_dir_var.get().strip()
        if log_dir != self._log_dir_before.strip():
            changes.append(f"ログの出力先「{self._log_dir_before or '(既定)'}」→"
                           f"「{log_dir or '(既定)'}」")
            trace.set_log_dir(log_dir)
        log.info("設定を保存しました (%d件 / 削除 %d件)", len(updated), len(removed))
        # 「昨日まで動いていたのに」を追うとき、**いつ何を変えたか**が要る
        if changes:
            trace.event("設定変更", trace.OK, detail="\n".join(changes))
        return True

    def cancel(self) -> None:
        self.top.destroy()


class _ToolRow:
    """1つのツールぶんの入力欄。"""

    def __init__(self, parent: tk.Widget, tool: Tool, *,
                 is_new: bool = False) -> None:
        self.tool = tool
        self.is_new = is_new

        box = tk.Frame(parent, bg=theme.BG)
        box.pack(fill="x", pady=(0, 12), padx=6)

        title = tk.Frame(box, bg=theme.BG)
        title.pack(fill="x")

        self.name_var = tk.StringVar(value=tool.display_name)
        tk.Entry(title, textvariable=self.name_var, width=14,
                 font=theme.FONT_BOLD).pack(side="left")

        # **アプリIDは既存の行では変えられない。** `/api/health` の照合と
        # 実行中の記録がこの値で結びついているので、途中で変えると
        # 動いているツールを見失う。新しい行だけ入力できる
        self.app_id_var = tk.StringVar(value=tool.app_id)
        if is_new:
            tk.Label(title, text=" アプリID", bg=theme.BG, fg=theme.MUTED,
                     font=theme.FONT_SMALL).pack(side="left")
            tk.Entry(title, textvariable=self.app_id_var, width=22,
                     font=theme.FONT_SMALL).pack(side="left", padx=(4, 0))
        else:
            tk.Label(title, text=f"  {tool.app_id}", bg=theme.BG,
                     fg=theme.MUTED, font=theme.FONT_SMALL).pack(side="left")

        # **［使う］と［削除］は同時に選べない。** 両方にチェックが入ると、
        # どちらになるのか分からない。［削除］を選んだら［使う］は外して
        # 押せなくし、［削除］を外したら元に戻す
        self.delete_var = tk.BooleanVar(value=False)
        tk.Checkbutton(title, text="削除", variable=self.delete_var,
                       command=self._on_delete_toggled,
                       bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
                       activebackground=theme.BG, activeforeground=theme.FG,
                       font=theme.FONT_SMALL, bd=0,
                       highlightthickness=0).pack(side="right")
        self.enabled_var = tk.BooleanVar(value=tool.enabled)
        self._enabled_before_delete = tool.enabled
        self.enabled_check = tk.Checkbutton(
            title, text="使う", variable=self.enabled_var,
            bg=theme.BG, fg=theme.MUTED, selectcolor=theme.BUTTON_BG,
            activebackground=theme.BG, activeforeground=theme.FG,
            disabledforeground="#5c6672",
            font=theme.FONT_SMALL, bd=0, highlightthickness=0)
        self.enabled_check.pack(side="right", padx=(0, 10))

        # --- 起動ファイルのパス (要件定義書 §13.1) ---
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
        # 画面の出し方。「自動」は exe でポートが無ければアプリの窓
        self._ui_keys = {label: key for key, label in UI_LABELS.items()}
        self.ui_var = tk.StringVar(value=UI_LABELS.get(tool.ui_mode, "自動"))

        _label(detail, "ポート")
        tk.Entry(detail, textvariable=self.port_var, width=7,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 12))
        _label(detail, "起動引数")
        tk.Entry(detail, textvariable=self.args_var, width=16,
                 font=theme.FONT_SMALL).pack(side="left", padx=(0, 2))
        # 何を入れればよいか分からないときは、起動ファイルとツールの中身を
        # 見て決める (`recommend_start_args`)
        tk.Button(detail, text="自動", command=self.recommend_args,
                  bg=theme.BUTTON_BG, fg=theme.FG, relief="flat", bd=0,
                  padx=8, pady=1, font=theme.FONT_SMALL,
                  cursor="hand2").pack(side="left", padx=(0, 12))
        _label(detail, "停止方法")
        ttk.Combobox(detail, textvariable=self.stop_var, width=12,
                     values=list(STOP_METHODS), state="readonly",
                     font=theme.FONT_SMALL).pack(side="left", padx=(0, 12))
        # ブラウザーで開くか、アプリが自分の窓を出すか (Tauri などの exe)
        _label(detail, "画面")
        ttk.Combobox(detail, textvariable=self.ui_var, width=9,
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
                                   wraplength=680)
        self._probe_cache: tuple[str, dict] = ("", {})
        self._hint_job = None
        for var in (self.path_var, self.port_var, self.stop_var, self.ui_var,
                    self.args_var):
            var.trace_add("write", lambda *_: self._schedule_hints())
        self.refresh_hints()

    def _schedule_hints(self) -> None:
        """打っているあいだは出し直さない (打ち終わってから)。"""
        if self._hint_job is not None:
            try:
                self.hint_label.after_cancel(self._hint_job)
            except tk.TclError:
                pass
        self._hint_job = self.hint_label.after(400, self.refresh_hints)

    def draft(self) -> Tool:
        """いま入っている値の Tool (確かめずに。ヒント用)。"""
        raw_port = self.port_var.get().strip()
        try:
            port = int(raw_port) if raw_port else 0
        except ValueError:
            port = 0
        return replace(self.tool,
                       start_command=self.path_var.get().strip().strip('"'),
                       start_args=self.args_var.get().strip(), port=port,
                       stop_method=self.stop_var.get().strip() or "auto",
                       ui_mode=self._ui_keys.get(self.ui_var.get(), ""))

    def refresh_hints(self) -> None:
        self._hint_job = None
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
            self.hint_label.configure(text="\n".join(f"ヒント: {h}" for h in hints))
            self.hint_label.pack(fill="x", pady=(2, 0))
        else:
            self.hint_label.pack_forget()

    def show_note(self, text: str) -> None:
        if text:
            self.note.configure(text=text)
            self.note.pack(fill="x", pady=(2, 0))
        else:
            self.note.pack_forget()

    def recommend_args(self) -> None:
        """［自動］。いまの起動ファイルから起動引数を決め直す。"""
        path = self.path_var.get().strip().strip('"')
        args, reason = tool_registry.recommend_start_args(path)
        if fileprobe.is_file(self.path_var.get()):
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
        current = self.path_var.get().strip()
        initial = str(Path(current).parent) if current else ""
        chosen = filedialog.askopenfilename(
            title=f"{self.tool.display_name} の起動ファイルを選んでください",
            initialdir=initial or None, filetypes=ENTRY_FILETYPES)
        if chosen:
            self.path_var.set(chosen)
            note = ""
            if chosen.lower().endswith(".exe"):
                found = tool_registry.probe_tool_folder(chosen)
                if self.ui_var.get() == "自動" and found.get("ui_mode"):
                    self.ui_var.set(UI_LABELS[found["ui_mode"]])
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
        """入力を確かめて、新しい設定を組み立てる。"""
        app_id = self.app_id_var.get().strip()
        problem = tool_registry.validate_app_id(app_id)
        if problem:
            return self.tool, problem
        if self.is_new and tool_registry.get(app_id) is not None:
            return self.tool, f"アプリID {app_id} はすでに登録されています"

        name = self.name_var.get().strip()
        if not name:
            return self.tool, "表示名を入力してください"

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
        problem = tool.ui_problem()
        if problem:
            return self.tool, problem
        return tool, ""


def _port_note(found: dict) -> str:
    """exe の行のポートを空にした理由 (ブラウザー版のポートだった)。"""
    port = found.get("browser_port")
    if not port:
        return ""
    return (f"ポートは空にしました。config/app.json の {port} はブラウザー版のもので、"
            "exe は待ち受けません (exe 自身が待ち受けるときだけ入れてください)")


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
    """
    by_port: dict[int, list[str]] = {}
    for tool in tools:
        if tool.enabled and tool.port > 0:
            by_port.setdefault(tool.port, []).append(tool.display_name or tool.app_id)
    return [f"{'、'.join(names)} が同じポート {port} を使います。"
            "同時には動かせません (あとから押したほうは起動できません)。"
            for port, names in sorted(by_port.items()) if len(names) > 1]


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
