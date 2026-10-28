"""ツール管理マスタの画面 (1.7.14)

［設定］の［ツール管理マスタを開く］から開く。PC ごとに、どのツールを出すかを
前もって書いておく。**押したらすぐマスタに書く** (［直す］［消す］のたびに保存)。

    ┌ ツール管理マスタ ─────────────────────────────────────────────┐
    │ 場所: \\\\server\\share\\launcher_ツール管理マスタ.sqlite3            │
    │ この端末 (ログインID … / PC名 …): 管理番号 3 の行で並べます     │
    │ [PC ごとの表示ツール] [ツール]                                 │
    │ 管理番号 ログインID   PC名      表示ツール      有効 備考        │
    │ 1        Xfa-Ngy…   NLM-NGY-… 日報 看板 …     有効 管理用 [直す][消す] │
    │                                                    [閉じる] │
    └────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Callable, Optional

from .. import fileprobe, tool_master, tool_registry, trace
from ..logging_utils import get_logger
from ..tool_master import Master, MasterError, MasterPc, MasterTool
from . import theme

log = get_logger("ui.master")

PC_TAB = "pcs"
TOOL_TAB = "tools"

# 表の列の幅 (文字数)。画面の幅 (1366) に収まるようにする
PC_COLUMNS = (("管理番号", 6), ("ログインID", 16), ("PC名", 16), ("表示ツール", 34),
              ("有効", 5), ("備考", 14))
TOOL_COLUMNS = (("No", 4), ("表示名", 14), ("起動ファイル", 40), ("この端末", 7),
                ("使っている行", 9), ("備考", 12))

ENTRY_FILETYPES = [("起動ファイル", "*.bat *.vbs *.exe"), ("すべてのファイル", "*.*")]


class MasterDialog:
    """モーダル。閉じるまで戻らない。マスタを書き換えたら `changed` が真。"""

    def __init__(self, parent: tk.Misc, path: Path) -> None:
        self.path = path
        self.changed = False
        self.tab = PC_TAB
        self.master: Optional[Master] = None

        self.top = tk.Toplevel(parent)
        self.top.title("ツール管理マスタ")
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        try:
            created = tool_master.ensure(path)
            if created:
                self.changed = True
            self.master = tool_master.read(path)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=parent)
            self.top.destroy()
            return

        self._build()
        self._render()
        self._place()
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.top.wait_window()

    # --------------------------------------------------------------
    def _build(self) -> None:
        tk.Label(self.top, text="ツール管理マスタ — PC ごとに出すツールを前もって決めておく",
                 bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD,
                 anchor="w").pack(fill="x", padx=16, pady=(14, 2))
        where = tk.Entry(self.top, font=theme.FONT_SMALL, relief="flat",
                         readonlybackground=theme.BG, fg=theme.MUTED, bd=0,
                         highlightthickness=0)
        where.insert(0, f"場所: {trace.real_path(self.path)}")
        where.configure(state="readonly")
        where.pack(fill="x", padx=16)
        self.here = tk.Label(self.top, text="", bg=theme.BG, fg=theme.FG,
                             font=theme.FONT_SMALL, anchor="w", justify="left",
                             wraplength=920)
        self.here.pack(fill="x", padx=16, pady=(4, 6))

        tabs = tk.Frame(self.top, bg=theme.BG)
        tabs.pack(fill="x", padx=16)
        self.tab_buttons = {}
        for key, text in ((PC_TAB, "PC ごとの表示ツール"), (TOOL_TAB, "ツール")):
            button = tk.Button(tabs, text=text, command=lambda k=key: self.show_tab(k),
                               relief="flat", bd=0, padx=14, pady=5, font=theme.FONT_BOLD,
                               cursor="hand2")
            button.pack(side="left", padx=(0, 4))
            self.tab_buttons[key] = button

        bottom = tk.Frame(self.top, bg=theme.BG)
        bottom.pack(side="bottom", fill="x", padx=16, pady=(8, 12))
        tk.Button(bottom, text="閉じる", command=self.close, bg=theme.BUTTON_CURRENT,
                  fg=theme.FG, relief="flat", bd=0, padx=22, pady=6,
                  font=theme.FONT_BOLD, cursor="hand2").pack(side="right")
        tk.Label(bottom, text="［直す］［消す］を押すと、すぐマスタに書きます。"
                              "この端末のバーには、閉じたときに反映します。"
                              "ほかの端末は、次にランチャーを起動したときに反映します。",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w", justify="left", wraplength=760).pack(side="left", fill="x")

        self.actions = tk.Frame(self.top, bg=theme.BG)
        self.actions.pack(fill="x", padx=16, pady=(8, 2))
        self.note = tk.Label(self.top, text="", bg=theme.BG, fg=theme.MUTED,
                             font=theme.FONT_SMALL, anchor="w", justify="left",
                             wraplength=920)
        self.note.pack(fill="x", padx=16)
        self.table = _ScrollTable(self.top, height=self._table_height())
        self.table.outer.pack(fill="both", expand=True, padx=16, pady=(6, 0))

    def _table_height(self) -> int:
        return max(200, min(520, self.top.winfo_screenheight() - 330))

    def _place(self) -> None:
        """画面の中央やや上。大きさは中身に合わせる (表の幅は画面に収める)。"""
        self.top.update_idletasks()
        width = min(self.top.winfo_reqwidth(), self.top.winfo_screenwidth() - 40)
        height = min(self.top.winfo_reqheight(), self.top.winfo_screenheight() - 80)
        x = max(0, (self.top.winfo_screenwidth() - width) // 2)
        y = max(0, (self.top.winfo_screenheight() - height) // 3)
        self.top.geometry(f"+{x}+{y}")

    # --------------------------------------------------------------
    def show_tab(self, key: str) -> None:
        self.tab = key
        self._render()

    def _reload(self) -> None:
        try:
            self.master = tool_master.read(self.path)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=self.top)

    def _render(self) -> None:
        for key, button in self.tab_buttons.items():
            current = key == self.tab
            button.configure(bg=theme.TOOL_ACTIVE if current else theme.BUTTON_BG,
                             fg=theme.FG if current else theme.MUTED)
        login, pc_name = tool_master.this_pc()
        row = self.master.row_for(login, pc_name) if self.master else None
        text = f"この端末 (ログインID {login or '?'} / PC名 {pc_name or '?'}): "
        if row is None:
            text += "行がありません。［設定］の内容で並べます"
        else:
            names = "、".join(t.label for t in self.master.tools_of(row)) or "なし"
            text += f"管理番号 {row.pc_no} の行で並べます ({names})"
        self.here.configure(text=text)
        for child in self.actions.winfo_children():
            child.destroy()
        if self.tab == PC_TAB:
            self._render_pcs(row)
        else:
            self._render_tools()
        # 表の幅を中身に合わせる (［直す］［消す］が右へ切れないように)
        self.top.update_idletasks()
        self.table.fit(self.top.winfo_screenwidth() - 80)

    def _render_pcs(self, mine: Optional[MasterPc]) -> None:
        _small_button(self.actions, "＋ 追加", lambda: self.edit_pc(None)).pack(side="left")
        _small_button(self.actions, "＋ この端末の行を追加",
                      self.add_this_pc).pack(side="left", padx=(6, 0))
        self.note.configure(
            text="ログインID・PC名を空にすると「どれでも」になります。両方が合う行 → PC名だけ"
                 "合う行 → ログインIDだけ合う行 → 両方空の行 (全端末の既定) の順に使います。"
                 "どの行にも合わない端末は、［設定］の内容で並べます。")
        self.table.clear()
        self.table.header([text for text, _ in PC_COLUMNS], [w for _, w in PC_COLUMNS])
        master = self.master
        for pc in master.pcs:
            names = "  ".join(t.label for t in master.tools_of(pc)) or "(なし)"
            cells = [str(pc.pc_no), pc.login_id or "(どれでも)", pc.pc_name or "(どれでも)",
                     names, "有効" if pc.enabled else "無効", pc.note]
            self.table.row(cells, [w for _, w in PC_COLUMNS],
                           buttons=[("直す", lambda p=pc: self.edit_pc(p)),
                                    ("消す", lambda p=pc: self.delete_pc(p))],
                           highlight=mine is not None and pc.pc_no == mine.pc_no,
                           muted=not pc.enabled)
        if not master.pcs:
            self.table.message("まだ行がありません。［＋ この端末の行を追加］か［＋ 追加］で"
                               "作ってください。先に［ツール］でツールを登録しておきます。")

    def _render_tools(self) -> None:
        _small_button(self.actions, "＋ 追加", lambda: self.edit_tool(None)).pack(side="left")
        _small_button(self.actions, "今の設定から取り込む",
                      self.import_current).pack(side="left", padx=(6, 0))
        self.note.configure(
            text="起動ファイルは ; で区切って複数書けます (この端末にある最初のものを使います)。"
                 "起動ファイルが無い端末では、そのツールは出しません (知らせません)。"
                 "ポートなどはツールのフォルダーの設定から読みます。")
        self.table.clear()
        self.table.header([text for text, _ in TOOL_COLUMNS], [w for _, w in TOOL_COLUMNS])
        master = self.master
        for tool in master.tools:
            start, _why = tool_master.resolve_start(tool)
            here = "ある" if start else ("？" if _unknown(tool) else "無い")
            using = len(tool_master.pcs_using(master, tool.tool_no))
            cells = [str(tool.tool_no), tool.label, tool.start_command.replace("\n", " ; "),
                     here, f"{using}行", tool.note]
            self.table.row(cells, [w for _, w in TOOL_COLUMNS],
                           buttons=[("↑", lambda t=tool: self.move_tool(t, -1)),
                                    ("↓", lambda t=tool: self.move_tool(t, 1)),
                                    ("直す", lambda t=tool: self.edit_tool(t)),
                                    ("消す", lambda t=tool: self.delete_tool(t))])
        if not master.tools:
            self.table.message("まだツールがありません。［＋ 追加］で起動ファイルを選ぶか、"
                               "［今の設定から取り込む］で、この端末の［設定］のツールを入れて"
                               "ください。")

    # --------------------------------------------------------------
    # PC の行
    # --------------------------------------------------------------
    def add_this_pc(self) -> None:
        login, pc_name = tool_master.this_pc()
        self.edit_pc(MasterPc(0, login_id=login, pc_name=pc_name))

    def edit_pc(self, pc: Optional[MasterPc]) -> None:
        if not self.master.tools:
            messagebox.showinfo("ツール管理マスタ",
                                "先に［ツール］でツールを登録してください。", parent=self.top)
            self.show_tab(TOOL_TAB)
            return
        PcEditor(self.top, self.master, pc or MasterPc(0), on_save=self.store_pc)

    def store_pc(self, pc: MasterPc) -> str:
        """行を書く。駄目なら理由 (編集の窓は閉じない)。"""
        try:
            number = tool_master.save_pc(self.path, pc)
        except (ValueError, MasterError) as exc:
            return str(exc)
        self._wrote("PCの行を保存", f"管理番号 {number}: {pc.login_id or '*'} / "
                                   f"{pc.pc_name or '*'}")
        return ""

    def delete_pc(self, pc: MasterPc) -> None:
        if not messagebox.askyesno(
                "ツール管理マスタ",
                f"管理番号 {pc.pc_no} ({pc.login_id or 'どれでも'} / "
                f"{pc.pc_name or 'どれでも'}) の行を消します。よろしいですか?\n"
                "(その端末は、［設定］の内容で並べるようになります)", parent=self.top):
            return
        try:
            tool_master.delete_pc(self.path, pc.pc_no)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=self.top)
            return
        self._wrote("PCの行を削除", f"管理番号 {pc.pc_no}")

    # --------------------------------------------------------------
    # ツール
    # --------------------------------------------------------------
    def edit_tool(self, tool: Optional[MasterTool]) -> None:
        ToolEditor(self.top, tool or MasterTool(0), on_save=self.store_tool)

    def store_tool(self, tool: MasterTool) -> str:
        try:
            number = tool_master.save_tool(self.path, tool)
        except (ValueError, MasterError) as exc:
            return str(exc)
        self._wrote("ツールを保存", f"No.{number}: {tool.label}")
        return ""

    def delete_tool(self, tool: MasterTool) -> None:
        using = tool_master.pcs_using(self.master, tool.tool_no)
        extra = (f"\n{len(using)} 行の表示ツールから外れます。" if using else "")
        if not messagebox.askyesno("ツール管理マスタ",
                                   f"「{tool.label}」を消します。よろしいですか?{extra}",
                                   parent=self.top):
            return
        try:
            tool_master.delete_tool(self.path, tool.tool_no)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=self.top)
            return
        self._wrote("ツールを削除", f"No.{tool.tool_no}: {tool.label}")

    def move_tool(self, tool: MasterTool, step: int) -> None:
        try:
            tool_master.move_tool(self.path, tool.tool_no, step)
        except MasterError as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=self.top)
            return
        self._wrote("ツールの並び", f"No.{tool.tool_no}: {tool.label}", quiet=True)

    def import_current(self) -> None:
        try:
            added = tool_master.import_tools(
                self.path, tool_registry.all_tools(include_disabled=True))
        except (ValueError, MasterError) as exc:
            messagebox.showerror("ツール管理マスタ", str(exc), parent=self.top)
            return
        if added:
            self._wrote("ツールを取り込む", "、".join(added))
        messagebox.showinfo("ツール管理マスタ",
                            f"{len(added)}件取り込みました。" + (
                                "\n" + "、".join(added) if added else
                                "\n(［設定］のツールは、もうマスタにあります)"),
                            parent=self.top)

    # --------------------------------------------------------------
    def _wrote(self, what: str, detail: str, *, quiet: bool = False) -> None:
        self.changed = True
        if not quiet:
            trace.event("ツール管理マスタ", trace.OK, cause=what, detail=detail)
        self._reload()
        self._render()

    def close(self) -> None:
        self.top.destroy()


def _unknown(tool: MasterTool) -> bool:
    return any(fileprobe.probe(c).unknown for c in tool.candidates())


# ------------------------------------------------------------------
# 編集の窓
# ------------------------------------------------------------------
class _Editor:
    """小さなモーダル。［保存］で `on_save(値)` を呼び、空が返れば閉じる。"""

    title = ""

    def __init__(self, parent: tk.Misc, on_save: Callable[..., str]) -> None:
        self.on_save = on_save
        self.top = tk.Toplevel(parent)
        self.top.title(self.title)
        self.top.configure(bg=theme.BG)
        self.top.transient(parent)
        self.body = tk.Frame(self.top, bg=theme.BG)
        self.body.pack(fill="both", expand=True, padx=16, pady=(14, 4))
        self.build()
        self.error = tk.Label(self.top, text="", bg=theme.BG,
                              fg=theme.STATE_COLORS["error"], font=theme.FONT_SMALL,
                              anchor="w", justify="left", wraplength=560)
        self.error.pack(fill="x", padx=16)
        buttons = tk.Frame(self.top, bg=theme.BG)
        buttons.pack(fill="x", padx=16, pady=(6, 12))
        tk.Button(buttons, text="キャンセル", command=self.top.destroy, bg=theme.BUTTON_BG,
                  fg=theme.MUTED, relief="flat", bd=0, padx=14, pady=5,
                  font=theme.FONT, cursor="hand2").pack(side="right")
        tk.Button(buttons, text="保存", command=self.save, bg=theme.BUTTON_CURRENT,
                  fg=theme.FG, relief="flat", bd=0, padx=20, pady=5,
                  font=theme.FONT_BOLD, cursor="hand2").pack(side="right", padx=(0, 8))
        self.top.bind("<Escape>", lambda _e: self.top.destroy())
        self.top.update_idletasks()
        x = parent.winfo_rootx() + 60
        y = parent.winfo_rooty() + 60
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.top.grab_set()
        self.top.wait_window()

    def build(self) -> None:
        raise NotImplementedError

    def value(self):
        raise NotImplementedError

    def save(self) -> None:
        problem = self.on_save(self.value())
        if problem:
            self.error.configure(text=problem)
            return
        self.top.destroy()

    def field(self, label: str, row: int, *, width: int = 30, value: str = "") -> tk.StringVar:
        tk.Label(self.body, text=label, bg=theme.BG, fg=theme.FG, font=theme.FONT,
                 anchor="w").grid(row=row, column=0, sticky="w", pady=3)
        var = tk.StringVar(value=value)
        tk.Entry(self.body, textvariable=var, width=width,
                 font=theme.FONT).grid(row=row, column=1, sticky="we", pady=3, padx=(10, 0))
        return var


class PcEditor(_Editor):
    """PC の行 (ログインID・PC名・表示ツール・有効・備考)。"""

    title = "ツール管理マスタ - PC の行"

    def __init__(self, parent: tk.Misc, master: Master, pc: MasterPc, *,
                 on_save: Callable[[MasterPc], str]) -> None:
        self.master = master
        self.pc = pc
        super().__init__(parent, on_save)

    def build(self) -> None:
        heading = f"管理番号 {self.pc.pc_no}" if self.pc.pc_no else "新しい行"
        tk.Label(self.body, text=heading, bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD,
                 anchor="w").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.login_var = self.field("ログインID", 1, value=self.pc.login_id)
        self.pc_var = self.field("PC名", 2, value=self.pc.pc_name)
        tk.Label(self.body, text="(空欄は「どれでも」。この端末: "
                                 f"{' / '.join(tool_master.this_pc())})",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").grid(row=3, column=1, sticky="w", padx=(10, 0))
        tk.Label(self.body, text="表示ツール", bg=theme.BG, fg=theme.FG, font=theme.FONT,
                 anchor="nw").grid(row=4, column=0, sticky="nw", pady=(8, 3))
        box = tk.Frame(self.body, bg=theme.BG)
        box.grid(row=4, column=1, sticky="w", padx=(10, 0), pady=(8, 3))
        self.tool_vars: list[tuple[int, tk.BooleanVar]] = []
        chosen = set(self.pc.tool_nos)
        for index, tool in enumerate(self.master.tools):
            var = tk.BooleanVar(value=tool.tool_no in chosen)
            tk.Checkbutton(box, text=tool.label, variable=var, bg=theme.BG, fg=theme.FG,
                           selectcolor=theme.BUTTON_BG, activebackground=theme.BG,
                           activeforeground=theme.FG, font=theme.FONT, bd=0,
                           highlightthickness=0, anchor="w").grid(
                row=index // 3, column=index % 3, sticky="w", padx=(0, 14))
            self.tool_vars.append((tool.tool_no, var))
        self.enabled_var = tk.BooleanVar(value=self.pc.enabled)
        tk.Checkbutton(self.body, text="有効 (外すと、この行は使いません)",
                       variable=self.enabled_var, bg=theme.BG, fg=theme.FG,
                       selectcolor=theme.BUTTON_BG, activebackground=theme.BG,
                       activeforeground=theme.FG, font=theme.FONT, bd=0,
                       highlightthickness=0).grid(row=5, column=1, sticky="w",
                                                  padx=(10, 0), pady=3)
        self.note_var = self.field("備考", 6, value=self.pc.note)

    def value(self) -> MasterPc:
        return MasterPc(pc_no=self.pc.pc_no, login_id=self.login_var.get(),
                        pc_name=self.pc_var.get(),
                        tool_nos=tuple(no for no, var in self.tool_vars if var.get()),
                        enabled=bool(self.enabled_var.get()), note=self.note_var.get())


class ToolEditor(_Editor):
    """ツール (表示名・起動ファイル・備考)。"""

    title = "ツール管理マスタ - ツール"

    def __init__(self, parent: tk.Misc, tool: MasterTool, *,
                 on_save: Callable[[MasterTool], str]) -> None:
        self.tool = tool
        super().__init__(parent, on_save)

    def build(self) -> None:
        heading = f"No.{self.tool.tool_no}" if self.tool.tool_no else "新しいツール"
        tk.Label(self.body, text=heading, bg=theme.BG, fg=theme.FG, font=theme.FONT_BOLD,
                 anchor="w").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        self.name_var = self.field("表示名", 1, value=self.tool.name)
        tk.Label(self.body, text="(空ならツールの設定の名前)", bg=theme.BG, fg=theme.MUTED,
                 font=theme.FONT_SMALL).grid(row=1, column=2, sticky="w", padx=(6, 0))
        self.start_var = self.field("起動ファイル", 2, width=56,
                                    value=self.tool.start_command)
        tk.Button(self.body, text="参照", command=self.browse, bg=theme.BUTTON_BG,
                  fg=theme.FG, relief="flat", bd=0, padx=10, pady=2, font=theme.FONT_SMALL,
                  cursor="hand2").grid(row=2, column=2, sticky="w", padx=(6, 0))
        tk.Label(self.body, text="(Start.vbs・start.bat・exe。; で区切って複数書けます。"
                                 "この端末に無くても保存できます)",
                 bg=theme.BG, fg=theme.MUTED, font=theme.FONT_SMALL,
                 anchor="w").grid(row=3, column=1, columnspan=2, sticky="w", padx=(10, 0))
        self.note_var = self.field("備考", 4, value=self.tool.note)

    def browse(self) -> None:
        current = MasterTool(0, start_command=self.start_var.get()).candidates()
        start = Path(current[0]).parent if current else Path.home()
        chosen = filedialog.askopenfilename(parent=self.top, title="起動ファイル",
                                            filetypes=ENTRY_FILETYPES,
                                            initialdir=str(start if start.is_dir()
                                                           else Path.home()))
        if chosen:
            self.start_var.set(str(Path(chosen)))
            if not self.name_var.get().strip():
                found = tool_registry.probe_tool_folder(str(Path(chosen)))
                if found.get("display_name"):
                    self.name_var.set(found["display_name"])

    def value(self) -> MasterTool:
        return MasterTool(tool_no=self.tool.tool_no, name=self.name_var.get(),
                          start_command=self.start_var.get().strip(),
                          order_no=self.tool.order_no, note=self.note_var.get())


# ------------------------------------------------------------------
# 表 (ラベルを並べる。ttk の表は Windows で地の色を変えられないので使わない)
# ------------------------------------------------------------------
class _ScrollTable:

    def __init__(self, parent: tk.Misc, *, height: int) -> None:
        self.outer = tk.Frame(parent, bg=theme.UTIL_BG)
        self.canvas = tk.Canvas(self.outer, bg=theme.UTIL_BG, highlightthickness=0,
                                height=height, width=900)
        bar = tk.Scrollbar(self.outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=theme.UTIL_BG)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all(
            "<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self.rows = 0

    def fit(self, max_width: int) -> None:
        """表の幅を中身に合わせる。画面より広ければ画面の幅まで。"""
        self.canvas.configure(width=min(max(self.inner.winfo_reqwidth(), 600), max_width))

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1),
                                 "units")

    def clear(self) -> None:
        for child in self.inner.winfo_children():
            child.destroy()
        self.rows = 0
        self.canvas.yview_moveto(0)

    def header(self, titles: list[str], widths: list[int]) -> None:
        for column, (title, width) in enumerate(zip(titles, widths)):
            tk.Label(self.inner, text=title, width=width, anchor="w", bg=theme.UTIL_BG,
                     fg=theme.MUTED, font=theme.FONT_BOLD).grid(
                row=0, column=column, sticky="w", padx=(6, 0), pady=(6, 4))
        self.rows = 1

    def row(self, cells: list[str], widths: list[int], *,
            buttons: list[tuple[str, Callable[[], None]]], highlight: bool = False,
            muted: bool = False) -> None:
        bg = theme.TOOL_ACTIVE if highlight else theme.BG
        fg = theme.MUTED if muted else theme.FG
        for column, (text, width) in enumerate(zip(cells, widths)):
            tk.Label(self.inner, text=text, width=width, anchor="w", bg=bg, fg=fg,
                     font=theme.FONT, wraplength=width * 9, justify="left").grid(
                row=self.rows, column=column, sticky="nsew", padx=(6, 0), pady=1)
        box = tk.Frame(self.inner, bg=bg)
        box.grid(row=self.rows, column=len(cells), sticky="nsew", padx=(6, 6), pady=1)
        for text, command in buttons:
            _small_button(box, text, command).pack(side="left", padx=(0, 4), pady=2)
        self.rows += 1

    def message(self, text: str) -> None:
        tk.Label(self.inner, text=text, anchor="w", justify="left", bg=theme.UTIL_BG,
                 fg=theme.MUTED, font=theme.FONT, wraplength=880).grid(
            row=self.rows, column=0, columnspan=8, sticky="w", padx=6, pady=10)
        self.rows += 1


def _small_button(parent: tk.Widget, text: str, command) -> tk.Button:
    return tk.Button(parent, text=text, command=command, bg=theme.BUTTON_BG, fg=theme.FG,
                     activebackground=theme.BUTTON_ACTIVE, activeforeground=theme.FG,
                     relief="flat", bd=0, padx=10, pady=3, font=theme.FONT_SMALL,
                     cursor="hand2")
