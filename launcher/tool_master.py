"""ツール管理マスタ — PC ごとに表示するツールを、前もって決めておく (1.7.14)

1台ずつ［設定］を開いてツールを登録しなくても、マスタに「どのログインID・
どの PC に・どのツールを出すか」を書いておけば、その PC のランチャーは起動した
ときにマスタを読み、**自分の行の表示ツールだけをバーに並べる**。

    <マスタの場所>\\launcher_ツール管理マスタ.sqlite3   (SQLite。共有フォルダーに置ける)
        tools      ツール         表示名・起動ファイル・並び・備考
        pcs        PC ごとの行    管理番号・ログインID・PC名・有効・備考
        pc_tools   表示ツール     その行に出すツール

【決まり】
* マスタの場所は［設定］で決める (この端末の設定 → 配布先フォルダ の順)。
  決めていなければ使わない (これまでどおり［設定］の内容で並べる)
* **指定した場所にマスタが無ければ作る** (中身は空。［設定］の
  ［ツール管理マスタを開く］で書く)
* この端末の行の探し方: 有効な行のうち、ログインIDと PC 名が合うもの。
  空欄は「どれでも」。両方合う行 > PC 名だけ合う行 > ログインIDだけ合う行
  > 両方空欄の行 (全端末の既定) の順に選び、同じなら管理番号の小さいほう
* **マスタに書いた起動ファイルがこの端末に無ければ、そのツールは飛ばす**
  (知らせない。並べるのは、この端末にあるものだけ)。起動ファイルは `;` で
  区切って複数書ける (この端末にある最初のものを使う)
* 行が見つかれば、**表示ツール以外はバーから隠す** (消さない)。隠したツールは
  覚えておき、行が無くなればもとに戻す。ポートなど、この端末で直した値は
  起動ファイルが同じあいだは残す
* マスタに届かない (共有フォルダーにつながらない) ときは、**前に読んだ
  内容のまま**起動する (設定DBに残っている)

マスタは SQLite なので、共有フォルダーに置くときは WAL を使わない (既定の
DELETE ジャーナル)。書くのは［ツール管理マスタ］の画面から、まれにだけ。
"""
from __future__ import annotations

import contextlib
import getpass
import json
import os
import platform
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Optional

from . import app_config, distribution, fileprobe, tool_registry, trace
from .logging_utils import get_logger
from .paths import normalize
from .tool_registry import Tool

log = get_logger("tool_master")

# 場所にフォルダーを指定したときのファイル名。ほかのツールのマスタ (日報複合
# ツールの「ツール管理マスタ」など) と同じフォルダーに置いても取り違えない
FILE_NAME = "launcher_ツール管理マスタ.sqlite3"
# 場所にこの拡張子のファイルを指定したら、そのファイルを使う
FILE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
SCHEMA_VERSION = 1

# 読むのを待つ上限 (秒)。共有フォルダーが遅くても、ランチャーの起動を止めない
READ_TIMEOUT_SEC = 8.0
# 書くときにほかの人の書き込みを待つ上限 (秒)
LOCK_TIMEOUT_SEC = 5.0

# マスタが隠したツール (設定DBの PC 設定。JSON のアプリIDの並び)
HIDDEN_KEY = "tool_master_hidden"

# 起動ファイルの区切り (複数書ける)
_SPLIT = re.compile(r"[;\n]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tools (
    tool_no       INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    start_command TEXT NOT NULL DEFAULT '',
    order_no      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    updated_by    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pcs (
    pc_no      INTEGER PRIMARY KEY AUTOINCREMENT,
    login_id   TEXT NOT NULL DEFAULT '',
    pc_name    TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pc_tools (
    pc_no   INTEGER NOT NULL REFERENCES pcs(pc_no) ON DELETE CASCADE,
    tool_no INTEGER NOT NULL REFERENCES tools(tool_no) ON DELETE CASCADE,
    PRIMARY KEY (pc_no, tool_no)
);
"""


class MasterError(Exception):
    """マスタを読めない・書けない。理由は文で持つ (画面にそのまま出す)。"""


# ------------------------------------------------------------------
# 中身
# ------------------------------------------------------------------
@dataclass(frozen=True)
class MasterTool:
    """マスタのツール 1 つ。"""

    tool_no: int
    name: str = ""
    start_command: str = ""
    order_no: int = 0
    note: str = ""

    def candidates(self) -> list[str]:
        """起動ファイルの候補 (`;` か改行で区切って複数書ける)。環境変数は展開する。"""
        items = []
        for part in _SPLIT.split(self.start_command or ""):
            text = os.path.expandvars(part.strip().strip('"'))
            if text:
                items.append(text)
        return items

    @property
    def label(self) -> str:
        """画面に出す名前 (表示名が空なら起動ファイルのフォルダー名)。"""
        if self.name.strip():
            return self.name.strip()
        first = self.candidates()
        if not first:
            return f"ツール {self.tool_no}"
        entry = Path(first[0].replace("\\", "/"))
        return entry.parent.name or entry.stem


@dataclass(frozen=True)
class MasterPc:
    """PC ごとの行 1 つ (管理番号 = `pc_no`)。"""

    pc_no: int
    login_id: str = ""
    pc_name: str = ""
    tool_nos: tuple[int, ...] = ()
    enabled: bool = True
    note: str = ""

    def matches(self, login_id: str, pc_name: str) -> bool:
        """このログインID・PC 名の端末の行か (空欄は「どれでも」)。"""
        return (_fits(self.login_id, login_id) and _fits(self.pc_name, pc_name))

    def rank(self) -> int:
        """合い方の強さ。両方 > PC 名だけ > ログインIDだけ > どちらも空欄。"""
        return ((2 if self.pc_name.strip() else 0)
                + (1 if self.login_id.strip() else 0))


@dataclass(frozen=True)
class Master:
    path: Path
    tools: tuple[MasterTool, ...] = ()
    pcs: tuple[MasterPc, ...] = ()

    def tool(self, tool_no: int) -> Optional[MasterTool]:
        return next((t for t in self.tools if t.tool_no == tool_no), None)

    def tools_of(self, pc: MasterPc) -> list[MasterTool]:
        """その行の表示ツール (マスタの並び順)。"""
        wanted = set(pc.tool_nos)
        return [t for t in self.tools if t.tool_no in wanted]

    def row_for(self, login_id: str, pc_name: str) -> Optional[MasterPc]:
        """この端末の行。無ければ None。"""
        rows = [p for p in self.pcs if p.enabled and p.matches(login_id, pc_name)]
        if not rows:
            return None
        return sorted(rows, key=lambda p: (-p.rank(), p.pc_no))[0]


def _fits(written: str, actual: str) -> bool:
    written = (written or "").strip()
    return not written or written.casefold() == (actual or "").strip().casefold()


def this_pc() -> tuple[str, str]:
    """この端末の (ログインID, PC 名)。Windows のサインインの名前とコンピューター名。"""
    try:
        login = os.environ.get("USERNAME") or getpass.getuser()
    except Exception:                         # noqa: BLE001 - 取れなくても続ける
        login = ""
    pc = os.environ.get("COMPUTERNAME") or platform.node() or ""
    return login, pc


# ------------------------------------------------------------------
# 場所
# ------------------------------------------------------------------
def configured() -> tuple[str, str]:
    """指定された場所と、その出どころ。無ければ `("", "")`。

    その端末の［設定］ → 配布先フォルダ の順。設定DBを作らずに読む
    (起動のいちばん最初でも使えるように)。
    """
    value = tool_registry.peek_pc_setting(tool_registry.TOOL_MASTER_KEY).strip()
    if value:
        return value, "この端末の設定"
    value = distribution.tool_master().strip()
    if value:
        return value, "配布先フォルダ"
    return "", ""


def path_for(text: str) -> Path:
    """指定された場所 (フォルダーかファイル) から、マスタのファイルの道。

    フォルダーなら `launcher_ツール管理マスタ.sqlite3` を足す。環境変数を展開し、
    相対ならランチャーのフォルダーから数える。
    """
    raw = os.path.expandvars((text or "").strip().strip('"'))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = app_config.APP_ROOT / path
    if path.suffix.lower() in FILE_SUFFIXES:
        return path
    return path / FILE_NAME


def current_path() -> Optional[Path]:
    text, _ = configured()
    return path_for(text) if text else None


# ------------------------------------------------------------------
# 読む・書く
# ------------------------------------------------------------------
@contextlib.contextmanager
def _open(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(str(path), timeout=LOCK_TIMEOUT_SEC)
    except sqlite3.Error as exc:
        raise MasterError(f"ツール管理マスタを開けません: {path} ({exc})") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            yield conn
    except sqlite3.Error as exc:
        raise MasterError(f"ツール管理マスタを読み書きできません: {path} ({exc})") from exc
    finally:
        conn.close()


def ensure(path: Path) -> bool:
    """マスタが無ければ作る (中身は空)。作ったら真。あれば表の不足だけ足す。"""
    created = not path.exists()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MasterError(f"ツール管理マスタのフォルダーを作れません: {path.parent} ({exc})") \
            from exc
    with _open(path) as conn:
        conn.executescript(_SCHEMA)
        now = _now()
        for key, value in (("schema_version", str(SCHEMA_VERSION)),
                           ("created_at", now),
                           ("created_on", this_pc()[1]),
                           ("created_by", this_pc()[0])):
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES(?, ?)",
                         (key, value))
    if created:
        log.info("ツール管理マスタを作りました: %s", path)
        trace.event("ツール管理マスタを作成", trace.OK, detail=str(path))
    return created


def read(path: Path) -> Master:
    """マスタを読む。無い・読めないときは `MasterError`。"""
    if not path.is_file():
        raise MasterError(f"ツール管理マスタがありません: {path}")
    with _open(path) as conn:
        try:
            tools = tuple(
                MasterTool(tool_no=r["tool_no"], name=r["name"],
                           start_command=r["start_command"], order_no=r["order_no"],
                           note=r["note"])
                for r in conn.execute(
                    "SELECT * FROM tools ORDER BY order_no, tool_no"))
            links: dict[int, list[int]] = {}
            for r in conn.execute("SELECT pc_no, tool_no FROM pc_tools"):
                links.setdefault(r["pc_no"], []).append(r["tool_no"])
            pcs = tuple(
                MasterPc(pc_no=r["pc_no"], login_id=r["login_id"], pc_name=r["pc_name"],
                         tool_nos=tuple(links.get(r["pc_no"], ())),
                         enabled=bool(r["enabled"]), note=r["note"])
                for r in conn.execute("SELECT * FROM pcs ORDER BY pc_no"))
        except sqlite3.Error as exc:
            raise MasterError(f"ツール管理マスタの形が違います: {path} ({exc})") from exc
    return Master(path=path, tools=tools, pcs=pcs)


def _read_waiting(path: Path, timeout: float = READ_TIMEOUT_SEC) -> Master:
    """読む。**`timeout` 秒より待たない** (共有フォルダーが遅くても起動を止めない)。"""
    box: dict = {}

    def work() -> None:
        try:
            box["master"] = read(path)
        except Exception as exc:              # noqa: BLE001 - 呼び出し側で理由にする
            box["error"] = exc

    worker = threading.Thread(target=work, name="tool-master", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise MasterError(f"ツール管理マスタの応答が遅いため、読むのをやめました: {path}")
    if "error" in box:
        error = box["error"]
        raise error if isinstance(error, MasterError) else MasterError(str(error))
    return box["master"]


def save_tool(path: Path, tool: MasterTool) -> int:
    """ツールを書く (`tool_no` が 0 なら足す)。番号を返す。"""
    if not tool.start_command.strip():
        raise ValueError("起動ファイルを入れてください")
    for part in tool.candidates():
        if Path(part).suffix.lower() not in tool_registry.ENTRY_SUFFIXES:
            raise ValueError(f"起動ファイルは .bat・.vbs・.exe にしてください: {part}")
    ensure(path)
    with _open(path) as conn:
        values = (tool.name.strip(), tool.start_command.strip(), tool.note.strip(),
                  _now(), this_pc()[0])
        if tool.tool_no:
            conn.execute("""UPDATE tools SET name = ?, start_command = ?, note = ?,
                              updated_at = ?, updated_by = ? WHERE tool_no = ?""",
                         (*values, tool.tool_no))
            return tool.tool_no
        order = conn.execute("SELECT COALESCE(MAX(order_no), 0) + 10 FROM tools").fetchone()[0]
        cur = conn.execute("""INSERT INTO tools(name, start_command, note, updated_at,
                                                updated_by, order_no)
                              VALUES(?, ?, ?, ?, ?, ?)""", (*values, order))
        return int(cur.lastrowid)


def delete_tool(path: Path, tool_no: int) -> None:
    with _open(path) as conn:
        conn.execute("DELETE FROM pc_tools WHERE tool_no = ?", (tool_no,))
        conn.execute("DELETE FROM tools WHERE tool_no = ?", (tool_no,))


def move_tool(path: Path, tool_no: int, step: int) -> None:
    """並びを 1 つ上 (`step=-1`) か下 (`+1`) へ。"""
    with _open(path) as conn:
        order = [r["tool_no"] for r in conn.execute(
            "SELECT tool_no FROM tools ORDER BY order_no, tool_no")]
        if tool_no not in order:
            return
        index = order.index(tool_no)
        other = index + step
        if not 0 <= other < len(order):
            return
        order[index], order[other] = order[other], order[index]
        for position, number in enumerate(order, start=1):
            conn.execute("UPDATE tools SET order_no = ? WHERE tool_no = ?",
                         (position * 10, number))


def pcs_using(master: Master, tool_no: int) -> list[MasterPc]:
    return [p for p in master.pcs if tool_no in p.tool_nos]


def save_pc(path: Path, pc: MasterPc) -> int:
    """PC の行を書く (`pc_no` が 0 なら足す)。管理番号を返す。

    同じログインID・PC 名の行が2つあると、どちらで並べるか決まらないので断る。
    """
    login, name = pc.login_id.strip(), pc.pc_name.strip()
    ensure(path)
    master = read(path)
    for other in master.pcs:
        if other.pc_no == pc.pc_no:
            continue
        if other.login_id.strip().casefold() == login.casefold() \
                and other.pc_name.strip().casefold() == name.casefold():
            raise ValueError(f"同じログインID・PC名の行が、管理番号 {other.pc_no} に"
                             "あります。そちらを直してください")
    known = {t.tool_no for t in master.tools}
    with _open(path) as conn:
        values = (login, name, 1 if pc.enabled else 0, pc.note.strip(), _now(),
                  this_pc()[0])
        if pc.pc_no:
            conn.execute("""UPDATE pcs SET login_id = ?, pc_name = ?, enabled = ?, note = ?,
                              updated_at = ?, updated_by = ? WHERE pc_no = ?""",
                         (*values, pc.pc_no))
            number = pc.pc_no
        else:
            cur = conn.execute("""INSERT INTO pcs(login_id, pc_name, enabled, note,
                                                  updated_at, updated_by)
                                  VALUES(?, ?, ?, ?, ?, ?)""", values)
            number = int(cur.lastrowid)
        conn.execute("DELETE FROM pc_tools WHERE pc_no = ?", (number,))
        for tool_no in dict.fromkeys(pc.tool_nos):
            if tool_no in known:
                conn.execute("INSERT INTO pc_tools(pc_no, tool_no) VALUES(?, ?)",
                             (number, tool_no))
    return number


def delete_pc(path: Path, pc_no: int) -> None:
    with _open(path) as conn:
        conn.execute("DELETE FROM pc_tools WHERE pc_no = ?", (pc_no,))
        conn.execute("DELETE FROM pcs WHERE pc_no = ?", (pc_no,))


def import_tools(path: Path, tools: Iterable[Tool]) -> list[str]:
    """この端末の［設定］のツールを、マスタのツールに足す。足した名前を返す。

    はじめてマスタを作ったときに、1つずつ書き写さずに済ませるため。
    同じ起動ファイルがもうマスタにあれば足さない。
    """
    ensure(path)
    master = read(path)
    known = {normalize(c) for t in master.tools for c in t.candidates()}
    added = []
    for tool in tools:
        start = tool.start_command.strip()
        if not start or normalize(start) in known:
            continue
        save_tool(path, MasterTool(tool_no=0, name=tool.display_name, start_command=start))
        known.add(normalize(start))
        added.append(tool.display_name or tool.app_id)
    return added


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def describe() -> str:
    """`start_debug.bat --check` に出す1枚。**読むだけ** (設定DBは変えない)。"""
    text, source = configured()
    lines = ["ツール管理マスタ:"]
    if not text:
        lines.append("  使っていません (［設定］の内容で並べます)")
        return "\n".join(lines)
    path = path_for(text)
    lines.append(f"  場所: {path} ({source}で指定)")
    found = fileprobe.probe(path)
    if not found.found:
        lines.append(f"  [注意] {found.describe()} (ランチャーを起動すると作ります)"
                     if not found.unknown else f"  [エラー] 届きません ({found.reason})")
        return "\n".join(lines)
    try:
        master = _read_waiting(path)
    except MasterError as exc:
        lines.append(f"  [エラー] {exc}")
        return "\n".join(lines)
    login, pc_name = this_pc()
    lines.append(f"  ツール {len(master.tools)}件 / PC の行 {len(master.pcs)}件")
    row = master.row_for(login, pc_name)
    if row is None:
        lines.append(f"  この端末 ({login} / {pc_name}) の行はありません"
                     " (［設定］の内容で並べます)")
        return "\n".join(lines)
    lines.append(f"  この端末 ({login} / {pc_name}) は管理番号 {row.pc_no} の行")
    for tool in master.tools_of(row):
        start, why = resolve_start(tool)
        lines.append(f"    {tool.label}: " + (start if start else f"出しません — {why}"))
    return "\n".join(lines)


# ------------------------------------------------------------------
# この端末に反映する
# ------------------------------------------------------------------
OFF = "off"                  # 場所を決めていない
UNREACHABLE = "unreachable"  # 届かない (前に読んだ内容のまま)
ERROR = "error"              # 読めない・作れない (前に読んだ内容のまま)
NO_ROW = "no_row"            # この端末の行が無い (［設定］の内容で並べる)
APPLIED = "applied"          # この端末の行で並べた


@dataclass
class SyncResult:
    state: str
    path: Optional[Path] = None
    pc: Optional[MasterPc] = None
    shown: list[str] = field(default_factory=list)      # 並べたツール
    skipped: list[str] = field(default_factory=list)    # 起動ファイルが無いので飛ばした
    hidden: list[str] = field(default_factory=list)     # マスタが隠したツール
    created: bool = False
    changed: bool = False
    problem: str = ""

    def describe(self) -> str:
        """［設定］と診断に出す1行。"""
        login, pc = this_pc()
        who = f"この端末 (ログインID {login or '?'} / PC名 {pc or '?'})"
        if self.state == OFF:
            return "使っていません (［設定］の内容で並べています)"
        if self.state in (UNREACHABLE, ERROR):
            return f"読めません。前に読んだ内容のまま並べています ({self.problem})"
        if self.state == NO_ROW:
            return (f"{who} の行がありません。［設定］の内容で並べています")
        text = f"{who} は管理番号 {self.pc.pc_no if self.pc else '?'} の行で並べています"
        if self.shown:
            text += f": {'、'.join(self.shown)}"
        if self.skipped:
            text += f" (この端末に起動ファイルが無いので出していないもの: {'、'.join(self.skipped)})"
        return text


_last: Optional[SyncResult] = None


def last_result() -> Optional[SyncResult]:
    """このランチャーで最後に反映した結果 (まだなら None)。"""
    return _last


def sync(*, create: bool = True) -> SyncResult:
    """マスタを読み、この端末の行のとおりにバーのツールをそろえる。

    **例外を出さない** (マスタのせいでランチャーを起動しなくしない)。
    """
    global _last
    try:
        result = _sync(create=create)
    except Exception as exc:                  # noqa: BLE001 - 起動を止めない
        log.exception("ツール管理マスタを反映できませんでした")
        result = SyncResult(ERROR, problem=f"{type(exc).__name__}: {exc}")
    _last = result
    _record(result)
    return result


def _sync(*, create: bool) -> SyncResult:
    text, _source = configured()
    if not text:
        changed = _restore_hidden()
        return SyncResult(OFF, changed=changed)
    path = path_for(text)
    found = fileprobe.probe(path)
    if found.unknown:
        return SyncResult(UNREACHABLE, path=path,
                          problem=f"届きません: {path} ({found.reason})")
    created = False
    if not found.found:
        if not create:
            return SyncResult(ERROR, path=path, problem=f"ありません: {path}")
        try:
            created = ensure(path)
        except MasterError as exc:
            return SyncResult(ERROR, path=path, problem=str(exc))
    try:
        master = _read_waiting(path)
    except MasterError as exc:
        return SyncResult(UNREACHABLE if "遅い" in str(exc) else ERROR,
                          path=path, problem=str(exc), created=created)
    login, pc_name = this_pc()
    row = master.row_for(login, pc_name)
    if row is None:
        changed = _restore_hidden()
        return SyncResult(NO_ROW, path=path, created=created, changed=changed)
    result = _apply(master, row)
    result.path, result.created = path, created
    return result


def resolve_start(tool: MasterTool) -> tuple[Optional[str], str]:
    """この端末にある起動ファイル (候補の最初のもの) と、無いときの理由。"""
    reasons = []
    for candidate in tool.candidates():
        probe = fileprobe.probe(candidate)
        if probe.found:
            return candidate, ""
        reasons.append(f"{candidate} ({probe.describe()})")
    return None, " / ".join(reasons) or "起動ファイルが書かれていません"


def _apply(master: Master, row: MasterPc) -> SyncResult:
    result = SyncResult(APPLIED, pc=row)
    before = {t.app_id: t for t in tool_registry.all_tools(include_disabled=True)}
    by_start = {normalize(t.start_command): t for t in before.values() if t.start_command}
    shown_ids: list[str] = []
    after: dict[str, Tool] = {}
    order = 10
    for master_tool in master.tools_of(row):
        start, why = resolve_start(master_tool)
        if start is None:
            # **この端末に無ければ飛ばす** (知らせない)
            log.info("起動ファイルがこの端末に無いので出しません: %s — %s",
                     master_tool.label, why)
            result.skipped.append(master_tool.label)
            continue
        tool = by_start.get(normalize(start)) or _from_start(master_tool, start, before)
        if tool.app_id in shown_ids:
            log.warning("同じツール (アプリID %s) が2つ選ばれています。2つ目は出しません: %s",
                        tool.app_id, master_tool.label)
            result.skipped.append(master_tool.label)
            continue
        tool = replace(tool, enabled=True, order_no=order,
                       display_name=master_tool.name.strip() or tool.display_name)
        order += 10
        shown_ids.append(tool.app_id)
        after[tool.app_id] = tool
        result.shown.append(tool.display_name)

    # 前に隠したもの (まだ登録があるもの) と、今回隠すもの
    hidden = (set(_hidden()) & set(before)) - set(shown_ids)
    for app_id, tool in before.items():
        if app_id in after:
            continue
        if tool.enabled:
            hidden.add(app_id)
            after[app_id] = replace(tool, enabled=False)
    for app_id, tool in after.items():
        old = before.get(app_id)
        if old is None or _differs(old, tool):
            tool_registry.save(tool)
            result.changed = True
    _set_hidden(sorted(hidden))
    result.hidden = sorted(before[a].display_name for a in hidden if a in before)
    return result


def _from_start(master_tool: MasterTool, start: str, before: dict[str, Tool]) -> Tool:
    """マスタの起動ファイルから、ランチャーのツールを作る (［＋ ツールを追加］と同じ読み方)。

    同じアプリIDのツールがもう登録されていれば (Start.vbs と exe など)、その
    起動ファイルをマスタのものに置き換える。
    """
    found = tool_registry.probe_tool_folder(start)
    entry = Path(start)
    app_id = found.get("app_id") or (tool_registry.exe_app_id(entry)
                                     if entry.suffix.lower() == ".exe"
                                     else tool_registry.script_app_id(entry))
    args, _why = tool_registry.recommend_start_args(start)
    name = (master_tool.name.strip() or found.get("display_name")
            or entry.parent.name or entry.stem)
    base = before.get(app_id) or Tool(app_id=app_id, display_name=name)
    return replace(base, start_command=start, start_args=args,
                   port=int(found.get("port") or 0),
                   ui_mode=found.get("ui_mode", ""))


def _differs(old: Tool, new: Tool) -> bool:
    return replace(old, updated_at="") != replace(new, updated_at="")


def _hidden() -> list[str]:
    try:
        value = json.loads(tool_registry.get_pc_setting(HIDDEN_KEY) or "[]")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _set_hidden(app_ids: list[str]) -> None:
    if app_ids:
        tool_registry.set_pc_setting(HIDDEN_KEY, json.dumps(app_ids, ensure_ascii=False))
    elif tool_registry.get_pc_setting(HIDDEN_KEY):
        tool_registry.clear_pc_setting(HIDDEN_KEY)


def _restore_hidden() -> bool:
    """マスタが隠したツールを戻す (この端末の行が無くなった・マスタをやめた)。"""
    changed = False
    for app_id in _hidden():
        tool = tool_registry.get(app_id)
        if tool is not None and not tool.enabled:
            tool_registry.save(replace(tool, enabled=True))
            changed = True
    _set_hidden([])
    return changed


def _record(result: SyncResult) -> None:
    """記録に残す (何を並べたか・なぜ並べなかったか)。"""
    if result.state == OFF:
        return
    log.info("ツール管理マスタ: %s", result.describe())
    if result.state in (UNREACHABLE, ERROR):
        trace.event("ツール管理マスタ", trace.WARNING, cause=result.problem,
                    detail=str(result.path or ""))
    elif result.state == APPLIED and (result.changed or result.skipped):
        trace.event("ツール管理マスタ", trace.OK,
                    cause=f"管理番号 {result.pc.pc_no if result.pc else '?'} の行で並べた",
                    detail=(f"出す: {'、'.join(result.shown) or 'なし'}"
                            + (f" / 起動ファイルが無い: {'、'.join(result.skipped)}"
                               if result.skipped else "")
                            + (f" / 隠す: {'、'.join(result.hidden)}"
                               if result.hidden else "")))
