"""どのツールをどう起動するかの設定 (要件定義書 §13 / §14 / §15)

**端末ごとにリポジトリの置き場所が違う**ので、`start.bat` のパスを
コードに埋め込まない。設定画面から変更でき、その値がSQLiteに残る
(要件定義書 §13.3「コードを書き換えず、設定変更だけで対応できること」)。

    config/launcher.json   同梱の既定値。初回の投入だけに使う
            ↓ 初回起動
    %LOCALAPPDATA%\\BusinessToolsLauncher\\data\\launcher.db
            ↑ 設定画面
    ここが動作中の唯一の参照先

新しいツールが5個目・6個目と増えても、行を足すだけで済む形にする
(要件定義書 §14「将来5個目、6個目のツールを追加しやすい構造」)。
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("tool_registry")

SCHEMA_VERSION = 2

# 相手ツールの起動確認URLの既定。4リポジトリとも `/api/health` で
# 揃っている (要件定義書 §7.2 の例は `/health`。**どちらでも良いよう
# 設定にした**ので、相手側を書き換えずに済む)
DEFAULT_HEALTH_PATH = "/api/health"

# 停止のやり方。詳しくは `process_manager` を参照
STOP_METHODS = ("auto", "stop_bat", "shutdown_api", "pid")

# 起動に使える入口。
#
#   .bat … `%*` で引数をそのまま渡す。出力も戻り値も取れる (推奨)
#   .vbs … 利用者がふだん押す入口。**引数を転送するとは限らない**ので、
#          中身を見て確かめる (`forwards_args`)
ENTRY_SUFFIXES = (".bat", ".vbs")

# ツール側にブラウザーを開かせないための指定
NO_BROWSER_ARG = "--no-browser"

# VBS が引数を転送しているかを見分ける印
_VBS_ARGS_MARK = "wscript.arguments"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tools (
    app_id              TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    order_no            INTEGER NOT NULL DEFAULT 0,
    repository          TEXT NOT NULL DEFAULT '',
    start_command       TEXT NOT NULL DEFAULT '',
    start_args          TEXT NOT NULL DEFAULT '',
    stop_command        TEXT NOT NULL DEFAULT '',
    work_dir            TEXT NOT NULL DEFAULT '',
    port                INTEGER NOT NULL DEFAULT 0,
    health_path         TEXT NOT NULL DEFAULT '/api/health',
    health_url_override TEXT NOT NULL DEFAULT '',
    stop_method         TEXT NOT NULL DEFAULT 'auto',
    enabled             INTEGER NOT NULL DEFAULT 1,
    updated_at          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pc_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 同梱の既定値から投入したことのあるアプリID。
--
-- **「まだ投入していないものだけ入れる」ための記録。** 全体で1つの
-- 「投入済み」印にすると、あとから `config/launcher.json` へ5個目を
-- 書き足しても、すでに使っている端末には永久に入らない。逆に印が
-- 無いと、利用者が消したツールが次の起動で復活する。
CREATE TABLE IF NOT EXISTS seeded_tools (
    app_id    TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Tool:
    """1つの業務ツールの起動設定 (要件定義書 §14)。"""

    app_id: str
    display_name: str
    order_no: int = 0
    repository: str = ""
    start_command: str = ""
    # `start.bat` へ渡す引数。4つの業務ツールはどれも同じ起動基盤で、
    # `--no-browser` を受け付ける。**ランチャーが起動完了を確認してから
    # 自分でブラウザーを開く**ので、ツール側にも開かせるとタブが2枚出る
    # (要件定義書 §7.1)。引数を受け付けないBATを登録したときは、
    # 設定画面でここを空にする
    start_args: str = ""
    stop_command: str = ""
    work_dir: str = ""
    port: int = 0
    health_path: str = DEFAULT_HEALTH_PATH
    health_url_override: str = ""
    stop_method: str = "auto"
    enabled: bool = True
    updated_at: str = ""

    @property
    def health_url(self) -> str:
        """起動確認URL。

        **出どころは1つ**にする。ふだんはポートとパスから組み立て、
        どうしても違う形が要るとき (別ホスト、別のパス) だけ
        `health_url_override` で丸ごと差し替える。両方を対等に持つと、
        ポートを直したのにURLが古いまま、という食い違いが起きる。
        """
        if self.health_url_override.strip():
            return self.health_url_override.strip()
        if self.port <= 0:
            return ""
        path = self.health_path or DEFAULT_HEALTH_PATH
        if not path.startswith("/"):
            path = "/" + path
        return f"http://127.0.0.1:{self.port}{path}"

    @property
    def home_url(self) -> str:
        """ブラウザーで開く画面のURL。"""
        if self.port <= 0:
            return ""
        return f"http://127.0.0.1:{self.port}/"

    @property
    def resolved_work_dir(self) -> str:
        """作業ディレクトリ。未設定なら `start.bat` の置き場所。

        相手の `start.bat` は `pushd "%~dp0"` で自分の場所へ移るように
        書かれているが、そうでないBATを登録されても動くようにする。
        """
        if self.work_dir.strip():
            return self.work_dir.strip()
        if self.start_command.strip():
            return str(Path(self.start_command.strip()).parent)
        return ""

    @property
    def is_configured(self) -> bool:
        """起動できる状態か。起動入口が設定され、実在すること。"""
        path = self.start_command.strip()
        return bool(path) and Path(path).is_file()

    @property
    def entry_kind(self) -> str:
        """起動入口の種類。`bat` / `vbs` / 空文字。"""
        path = self.start_command.strip().strip('"')
        if not path:
            return ""
        suffix = Path(path).suffix.lower()
        return suffix[1:] if suffix in ENTRY_SUFFIXES else ""

    @property
    def forwards_args(self) -> bool:
        """起動入口が、渡した引数をツールへ届けるか。

        `.bat` は `%*` で渡す作りが前提 (4ツールともそうなっている)。
        `.vbs` は**転送するとは限らない** ── 実際、4ツールの `Start.vbs`
        は `pythonw "<script>"` を決め打ちで実行しており、引数を渡す
        処理が無い。中身に `WScript.Arguments` があるかで見分ける。

        届かないまま `--no-browser` を設定していると、ツールは自分で
        ブラウザーを開く。そこへランチャーも画面を開くと**2枚**になる。
        """
        kind = self.entry_kind
        if kind == "bat":
            return True
        if kind != "vbs":
            return False
        return _vbs_forwards_args(self.start_command.strip().strip('"'))

    @property
    def suppresses_browser(self) -> bool:
        """ツール側にブラウザーを開かせない指定が、実際に効くか。

        効かないなら、**ランチャーは自分の画面を開かない** ── ツールが
        自分で開くので、両方開けば2枚になる。
        """
        if NO_BROWSER_ARG not in self.start_args:
            return False
        return self.forwards_args


# ------------------------------------------------------------------
# 保存時チェック (要件定義書 §13.2)
# ------------------------------------------------------------------
def validate_start_command(path: str) -> str:
    """`start.bat` のパスを確かめる。問題があれば理由、無ければ空文字。

    未設定は許す。**4つ全部を用意していない端末でもランチャーを
    使えるようにする**ため (日報だけ入っている端末がある)。
    起動しようとした時点で「設定されていません」と出る。
    """
    text = (path or "").strip().strip('"')
    if not text:
        return ""

    candidate = Path(text)
    if not candidate.is_absolute():
        # 相対パスは、どこから起動したかで指す先が変わる。
        # ランチャーは共有フォルダーやショートカットから起動されるので、
        # 相対パスを許すと端末ごとに違う場所を指す
        return "絶対パスで指定してください (例: C:\\業務ツール\\日報\\start.bat)"
    if candidate.suffix.lower() not in ENTRY_SUFFIXES:
        return "拡張子が .bat または .vbs のファイルを指定してください"
    if not candidate.exists():
        return f"ファイルが見つかりません: {candidate}"
    if not candidate.is_file():
        return f"ファイルではありません: {candidate}"
    return ""


def resolve_config_path(text: str) -> str:
    """配布設定 (`config/launcher.json`) に書かれたパスを、この端末の絶対パスにする。

    **ランチャーのフォルダーを起点にした相対パスを許す。** 配布では
    ランチャーと業務ツールを1つのフォルダーにまとめてコピーすることが
    多く、置き場所はPCごとに違う (`C:\\業務ツール\\` だったり
    `D:\\Tools\\` だったり)。起点をランチャーに固定すれば、どこへ
    置いても同じ書き方で指せる。

        C:\\業務ツール\\
        ├─ ランチャー\\     ← ここが起点
        └─ 日報\\start.bat  ← `..\\日報\\start.bat` で指せる

    設定画面で入力するパスは絶対パスのまま (相対パスは起動した場所で
    指す先が変わるため)。**起点が決まっている配布設定だけ**の扱い。

    `%USERPROFILE%` などの環境変数も展開する。
    """
    raw = (text or "").strip().strip('"')
    if not raw:
        return ""
    expanded = os.path.expandvars(raw)
    path = Path(expanded)
    if not path.is_absolute():
        path = app_config.APP_ROOT / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _vbs_forwards_args(path: str) -> bool:
    """その VBS が `WScript.Arguments` を使っているか。

    読めなければ「転送しない」に倒す。**届くと思い込んで2枚開くより、
    届かない前提で1枚に寄せるほうが害が小さい。**
    """
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return False
    for encoding in ("cp932", "utf-8"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return _VBS_ARGS_MARK in text.lower()
    return _VBS_ARGS_MARK in raw.decode("utf-8", "replace").lower()


def validate_port(port: int) -> str:
    if port == 0:
        return ""                    # 未設定は許す
    if not 1 <= port <= 65535:
        return "ポート番号は 1〜65535 の範囲で指定してください"
    return ""


# ------------------------------------------------------------------
# データベース
# ------------------------------------------------------------------
@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """設定DBを開いて、**必ず閉じる**。

    `with sqlite3.connect(...) as conn` は commit はするが閉じない。
    設定はボタンを描くたびに読むので、閉じずに済ませると接続が
    増え続ける。commit と close の両方をここで面倒を見る。
    """
    app_config.ensure_local_dirs()
    conn = sqlite3.connect(app_config.settings_db_path())
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize() -> None:
    """設定DBを用意し、まだ入れていない既定値を入れる。冪等。"""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
                     ("schema_version", str(SCHEMA_VERSION)))
        _migrate(conn)
        _seed_missing(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """古い形の設定DBを新しい形へ。

    以前は「投入済み」を全体で1つの印にしていた。その印がある設定DBは、
    そのときの既定値を投入し終えている。**いま入っているぶんを
    投入済みとして記録し直す**ことで、利用者が消したツールが復活せず、
    かつ新しく足された既定値は入るようにする。
    """
    legacy = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'seeded'").fetchone()
    if legacy is None:
        return
    already = conn.execute("SELECT COUNT(*) FROM seeded_tools").fetchone()[0]
    if already:
        return

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    known = {row["app_id"] for row in conn.execute("SELECT app_id FROM tools")}
    known.update(str(item.get("app_id", "")).strip()
                 for item in app_config.default_tools())
    for app_id in sorted(a for a in known if a):
        conn.execute("INSERT OR IGNORE INTO seeded_tools(app_id, seeded_at) "
                     "VALUES(?, ?)", (app_id, now))
    log.info("投入済みの記録を作り直しました (%d件)", len(known))


def _seed_missing(conn: sqlite3.Connection) -> None:
    """`config/launcher.json` のうち、**まだ入れていないものだけ**入れる。

    これで、配布物の `config/launcher.json` に5個目を書き足せば、すでに
    使っている端末にも次の起動で入る。**すでにある行には触らない** ──
    利用者が設定した `start.bat` のパスを上書きしない。

    **`start.bat` のパスは入れない。** 端末ごとに違うので、既定値として
    もっともらしいパスを入れると「設定したつもりで別の場所を指している」
    状態を作る。空にしておけば、設定画面が「未設定」と出す。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    seeded = {row["app_id"] for row in conn.execute(
        "SELECT app_id FROM seeded_tools")}

    added = []
    for item in app_config.default_tools():
        app_id = str(item.get("app_id", "")).strip()
        if not app_id or app_id in seeded:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO tools
               (app_id, display_name, order_no, repository, start_command,
                start_args, port, health_path, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (app_id,
             str(item.get("display_name", app_id)),
             int(item.get("order_no", 0)),
             str(item.get("repository", "")),
             _configured_start_command(item),
             str(item.get("start_args", "")),
             int(item.get("port", 0)),
             str(item.get("health_path", DEFAULT_HEALTH_PATH)),
             now))
        conn.execute("INSERT OR IGNORE INTO seeded_tools(app_id, seeded_at) "
                     "VALUES(?, ?)", (app_id, now))
        added.append(app_id)

    if added:
        log.info("既定のツール定義を投入しました: %s", "、".join(added))

    _fill_blank_paths(conn)


def _configured_start_command(item: dict) -> str:
    """配布設定の起動ファイル。**この端末に実在するときだけ**返す。

    実在しないパスを入れてしまうと、空欄が「間違った値」に変わる。
    そうなると次の起動で埋め直せず (空欄ではないので)、手で直すしか
    なくなる。実在しなければ空のままにしておけば、あとからツールを
    置いたときに次の起動で自動的に埋まる。
    """
    resolved = resolve_config_path(str(item.get("start_command", "")))
    if not resolved:
        return ""
    return resolved if Path(resolved).is_file() else ""


def _fill_blank_paths(conn: sqlite3.Connection) -> None:
    """配布設定に起動ファイルがあれば、**空欄だけ**埋める。

    起動のたびに見る。すでに使っている端末でも、配布設定を更新すれば
    次の起動で入る。**利用者が設定画面で入れた値には触らない** ──
    空欄でなければ、それがその端末の正しい値。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    filled = []
    for item in app_config.default_tools():
        app_id = str(item.get("app_id", "")).strip()
        path = _configured_start_command(item)
        if not app_id or not path:
            continue
        cursor = conn.execute(
            """UPDATE tools SET start_command = ?, updated_at = ?
               WHERE app_id = ? AND start_command = ''""",
            (path, now, app_id))
        if cursor.rowcount:
            filled.append(app_id)
    if filled:
        log.info("配布設定から起動ファイルを埋めました: %s", "、".join(filled))


def _row_to_tool(row: sqlite3.Row) -> Tool:
    return Tool(
        app_id=row["app_id"],
        display_name=row["display_name"],
        order_no=row["order_no"],
        repository=row["repository"],
        start_command=row["start_command"],
        start_args=row["start_args"],
        stop_command=row["stop_command"],
        work_dir=row["work_dir"],
        port=row["port"],
        health_path=row["health_path"],
        health_url_override=row["health_url_override"],
        stop_method=row["stop_method"],
        enabled=bool(row["enabled"]),
        updated_at=row["updated_at"],
    )


def all_tools(*, include_disabled: bool = False) -> list[Tool]:
    """並び順どおりのツール一覧。ボタンの並びはこれに従う。"""
    initialize()
    sql = "SELECT * FROM tools"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY order_no, display_name"
    with _connect() as conn:
        return [_row_to_tool(r) for r in conn.execute(sql)]


def get(app_id: str) -> Optional[Tool]:
    initialize()
    with _connect() as conn:
        row = conn.execute("SELECT * FROM tools WHERE app_id = ?",
                           (app_id,)).fetchone()
    return _row_to_tool(row) if row else None


def save(tool: Tool) -> None:
    """1つのツール設定を書く。無ければ足す (5個目以降の追加もこれ)。"""
    initialize()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO tools
                 (app_id, display_name, order_no, repository, start_command,
                  start_args, stop_command, work_dir, port, health_path,
                  health_url_override, stop_method, enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(app_id) DO UPDATE SET
                 display_name        = excluded.display_name,
                 order_no            = excluded.order_no,
                 repository          = excluded.repository,
                 start_command       = excluded.start_command,
                 start_args          = excluded.start_args,
                 stop_command        = excluded.stop_command,
                 work_dir            = excluded.work_dir,
                 port                = excluded.port,
                 health_path         = excluded.health_path,
                 health_url_override = excluded.health_url_override,
                 stop_method         = excluded.stop_method,
                 enabled             = excluded.enabled,
                 updated_at          = excluded.updated_at""",
            (tool.app_id, tool.display_name, tool.order_no, tool.repository,
             tool.start_command.strip(), tool.start_args.strip(),
             tool.stop_command.strip(),
             tool.work_dir.strip(), tool.port, tool.health_path,
             tool.health_url_override.strip(), tool.stop_method,
             1 if tool.enabled else 0, now))
    log.info("設定を保存しました: %s (start=%s)", tool.app_id, tool.start_command)


def save_all(tools: Iterable[Tool]) -> None:
    for tool in tools:
        save(tool)


# ------------------------------------------------------------------
# ツールを増やす / 減らす (要件定義書 §14)
# ------------------------------------------------------------------
def add_tool(tool: Tool) -> None:
    """新しいツールを登録する。**同じアプリIDがあれば断る。**

    アプリIDは `/api/health` の照合に使う鍵なので、重複させると
    どちらのツールを見ているのか分からなくなる。
    """
    problem = validate_app_id(tool.app_id, existing=True)
    if problem:
        raise ValueError(problem)
    save(tool)
    log.info("ツールを追加しました: %s (%s)", tool.display_name, tool.app_id)


def delete_tool(app_id: str) -> None:
    """ツールの登録を消す。

    `seeded_tools` の記録は**残す**。消したのに次の起動で復活すると、
    消したことにならない。
    """
    initialize()
    with _connect() as conn:
        conn.execute("DELETE FROM tools WHERE app_id = ?", (app_id,))
    log.info("ツールを削除しました: %s", app_id)


def validate_app_id(app_id: str, *, existing: bool = False) -> str:
    """アプリIDを確かめる。問題があれば理由、無ければ空文字。

    `existing=True` のときは、すでに登録されていないことまで見る。
    """
    text = (app_id or "").strip()
    if not text:
        return "アプリIDを入力してください"
    if any(c.isspace() for c in text):
        return "アプリIDに空白は使えません"
    if existing and get(text) is not None:
        return f"そのアプリIDはすでに登録されています: {text}"
    return ""


def next_order_no() -> int:
    """並びの最後に置くための番号。"""
    tools = all_tools(include_disabled=True)
    return (max((t.order_no for t in tools), default=0) // 10 + 1) * 10


def probe_tool_folder(start_command: str) -> dict:
    """`start.bat` の隣の `config/app.json` から、そのツールの素性を読む。

    4つの業務ツールは同じ起動基盤なので、アプリID・表示名・版・ポートが
    そこに入っている。**利用者に手で写させない**ためにこれを読む ──
    アプリIDが1文字違うだけで起動確認が永久に通らず、しかも画面には
    「応答がありません」としか出ないので、原因にたどり着きにくい。

    読めなければ空の辞書。手で入れてもらう。
    """
    path = (start_command or "").strip().strip('"')
    if not path:
        return {}
    try:
        raw = json.loads((Path(path).parent / "config" / "app.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("相手の設定を読めませんでした (%s): %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}

    found = {}
    app_id = str(raw.get("app_id", "")).strip()
    if app_id:
        found["app_id"] = app_id
    name = str(raw.get("display_name", "")).strip()
    if name:
        found["display_name"] = name
    version = str(raw.get("version", "")).strip()
    if version:
        found["version"] = version
    port = _first_port(raw.get("server"))
    if port:
        found["port"] = port
    return found


def _first_port(server) -> int:
    """`server` からポートを1つ拾う。

    役割 (現場 / 資材、看板 / 倉庫 / 閲覧) を持つツールは `roles` の下に
    分かれている。**最初の1つを既定にする** ── どれを使うかは端末ごとに
    違うので、あとから設定画面で直せるようにしてある。
    """
    if not isinstance(server, dict):
        return 0
    port = server.get("port")
    if isinstance(port, int) and port > 0:
        return port
    roles = server.get("roles")
    if isinstance(roles, dict):
        for role in roles.values():
            if isinstance(role, dict):
                value = role.get("port")
                if isinstance(value, int) and value > 0:
                    return value
    return 0


def set_start_command(app_id: str, path: str) -> Tool:
    """設定画面からの主な操作 (要件定義書 §13)。"""
    tool = get(app_id)
    if tool is None:
        raise KeyError(f"未登録のツール: {app_id}")
    updated = replace(tool, start_command=(path or "").strip().strip('"'))
    save(updated)
    return updated


# ------------------------------------------------------------------
# PCごとの設定 (要件定義書 §15)
# ------------------------------------------------------------------
# 利用者に毎回PC名を選ばせないための保存場所。**ランチャーは値を
# 預かるだけ**で、モードの中身 (中板・小板…) は各業務ツール側の
# 要件なので、ここでは解釈しない (要件定義書 §15)
PC_MODE_KEY = "pc_mode"


def get_pc_setting(key: str, default: str = "") -> str:
    initialize()
    with _connect() as conn:
        row = conn.execute("SELECT value FROM pc_settings WHERE key = ?",
                           (key,)).fetchone()
    return row["value"] if row else default


def set_pc_setting(key: str, value: str) -> None:
    initialize()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO pc_settings(key, value, updated_at) VALUES(?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, now))
    log.info("PC設定を保存しました: %s = %s", key, value)


def clear_pc_setting(key: str) -> None:
    """PC設定を1つ消す。**「既定に戻す」はこれで表す。**

    空文字を入れるのとは別物。値が無いことと、空の値が入っていることを
    区別したい場面がある (バーの位置は、無ければ自動、あれば手動)。
    """
    initialize()
    with _connect() as conn:
        conn.execute("DELETE FROM pc_settings WHERE key = ?", (key,))
    log.info("PC設定を消しました: %s", key)


def pc_mode() -> str:
    return get_pc_setting(PC_MODE_KEY)


def set_pc_mode(mode: str) -> None:
    set_pc_setting(PC_MODE_KEY, mode)


# ------------------------------------------------------------------
# 控え
# ------------------------------------------------------------------
def backup() -> Optional[Path]:
    """設定DBの控えを取る。設定画面で保存する前に呼ぶ。

    端末を入れ替えたとき、4つ分のパスを入れ直すのは手間なので
    残しておく (基盤仕様書 4.6 の `backup`)。
    """
    source = app_config.settings_db_path()
    if not source.exists():
        return None
    target = (app_config.local_dir("backup")
              / f"launcher_{time.strftime('%Y%m%d_%H%M%S')}.db")
    try:
        with _connect() as conn, contextlib.closing(
                sqlite3.connect(target)) as dest:
            conn.backup(dest)
    except (OSError, sqlite3.Error) as exc:
        log.warning("設定の控えを取れませんでした: %s", exc)
        return None
    _purge_old_backups()
    return target


# 控えをいくつ残すか。設定を保存するたびに1つ増えるので、上限を決める
KEEP_BACKUPS = 10


def _purge_old_backups() -> None:
    try:
        files = sorted(app_config.local_dir("backup").glob("launcher_*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[KEEP_BACKUPS:]:
            stale.unlink()
    except OSError:
        pass


def describe() -> str:
    """診断用の一覧 (基盤仕様書 2.6)。"""
    lines = [f"設定DB: {app_config.settings_db_path()}"]
    mode = pc_mode()
    lines.append(f"このPCのモード: {mode or '(未設定)'}")
    planned = {str(item.get("app_id", "")): str(item.get("start_command", ""))
               for item in app_config.default_tools()}
    for tool in all_tools(include_disabled=True):
        mark = "OK" if tool.is_configured else "未設定"
        lines.append(f"  [{mark:>4}] {tool.display_name} ({tool.app_id})")
        lines.append(f"         起動: {tool.start_command or '(未設定)'}")
        want = planned.get(tool.app_id, "")
        if want and not tool.is_configured:
            # **配布設定はあるのに入っていない。** 配置が想定と違う
            resolved = resolve_config_path(want)
            lines.append(f"         配布設定: {want}")
            lines.append(f"                 → {resolved} (見つかりません)")
        lines.append(f"         確認: {tool.health_url or '(ポート未設定)'}")
    return "\n".join(lines)
