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

SCHEMA_VERSION = 1

# 相手ツールの起動確認URLの既定。4リポジトリとも `/api/health` で
# 揃っている (要件定義書 §7.2 の例は `/health`。**どちらでも良いよう
# 設定にした**ので、相手側を書き換えずに済む)
DEFAULT_HEALTH_PATH = "/api/health"

# 停止のやり方。詳しくは `process_manager` を参照
STOP_METHODS = ("auto", "stop_bat", "shutdown_api", "pid")

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
        """起動できる状態か。`start.bat` が設定され、実在すること。"""
        path = self.start_command.strip()
        return bool(path) and Path(path).is_file()


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
    if candidate.suffix.lower() != ".bat":
        return "拡張子が .bat のファイルを指定してください"
    if not candidate.exists():
        return f"ファイルが見つかりません: {candidate}"
    if not candidate.is_file():
        return f"ファイルではありません: {candidate}"
    return ""


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
    """設定DBを用意し、初回だけ既定値を入れる。冪等。"""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES(?, ?)",
                     ("schema_version", str(SCHEMA_VERSION)))
        seeded = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'seeded'").fetchone()
        if seeded is None:
            _seed(conn)
            conn.execute("INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                         ("seeded", time.strftime("%Y-%m-%d %H:%M:%S")))
            log.info("既定のツール定義を投入しました")


def _seed(conn: sqlite3.Connection) -> None:
    """`config/launcher.json` の既定値を入れる。

    **`start.bat` のパスは入れない。** 端末ごとに違うので、既定値として
    もっともらしいパスを入れると「設定したつもりで別の場所を指している」
    状態を作る。空にしておけば、設定画面が「未設定」と出す。
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for item in app_config.default_tools():
        app_id = str(item.get("app_id", "")).strip()
        if not app_id:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO tools
               (app_id, display_name, order_no, repository, start_args,
                port, health_path, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (app_id,
             str(item.get("display_name", app_id)),
             int(item.get("order_no", 0)),
             str(item.get("repository", "")),
             str(item.get("start_args", "")),
             int(item.get("port", 0)),
             str(item.get("health_path", DEFAULT_HEALTH_PATH)),
             now))


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
    for tool in all_tools(include_disabled=True):
        mark = "OK" if tool.is_configured else "未設定"
        lines.append(f"  [{mark:>4}] {tool.display_name} ({tool.app_id})")
        lines.append(f"         起動: {tool.start_command or '(未設定)'}")
        lines.append(f"         確認: {tool.health_url or '(ポート未設定)'}")
    return "\n".join(lines)
