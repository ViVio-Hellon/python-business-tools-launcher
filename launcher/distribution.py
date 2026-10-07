"""配布先フォルダ (`distribution/`)

**ランチャーのフォルダーと一緒に運ばれる設定。** 端末ごとの設定
(`%LOCALAPPDATA%` の設定DB) はフォルダーをコピーしても移らないので、
配った先でも最初から設定済みにしたいものはここに置く。

    ランチャー/
    ├─ config/launcher.json   製品の既定値。ランチャーを更新すると入れ替わる
    └─ distribution/          配布先フォルダ。**ランチャーを更新しても残す**
       ├─ settings.json       ツールの設定 (起動ファイルは相対パス)
       ├─ password.json       ［設定］を開く管理者パスワードの照合値
       └─ README.txt          このフォルダーの説明 (人が読む用)

    %LOCALAPPDATA%\\...\\launcher.db   その端末の設定 (動作中の参照先)

【作り方】
1台を［設定］で整えてから「配布先フォルダを作る」(`export_tools`)。
手で書かない ── JSON はコメントを許さず、`\\` を2つ重ねる決まりもあって、
1文字の誤りで設定全体が読めなくなる。

【読み込み方】
起動時にフォルダーがあれば読む (`tool_registry` が見る)。

* 配布先フォルダがあれば、**そのツール一覧が正**。製品の既定値の一覧は
  混ぜない (配布元で消したツールが、配った先に出てこないように)
* **その端末にすでにあるデータが優先。** 入るのは、その端末にまだ無い
  ツールと空欄の起動ファイルだけ
* ただし**工場出荷のまま** (起動ファイルが空で、既定値から何も変えて
  いない) の行は「まだデータが無い」とみなし、配布先フォルダにそろえる。
  配布先フォルダを置く前に一度起動しただけの端末も、きれいに引き継げる
* 手を入れた端末を配布先フォルダにそろえたいときは、その端末の［設定］
  から明示的に置き換える

【パスワード】
設定とは別のファイルに置く。作り直してもパスワードは変わらず、
パスワードを忘れたときは `password.json` だけ消せば決め直せる。

**これは誤操作を防ぐための鍵で、守りの固い鍵ではない。** このフォルダー
を書き換えられる人なら外せる。ライン作業者が起動ファイルをうっかり
変えてしまう、を防ぐのが目的。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("distribution")

FORMAT = 1
FOLDER_NAME = "distribution"
SETTINGS_FILE = "settings.json"
PASSWORD_FILE = "password.json"
README_FILE = "README.txt"

# パスワードの寄せ方。標準ライブラリだけで使える、ゆっくりした計算にする。
# 1回あたり 0.1〜0.5 秒ほどで、画面で打つぶんには気にならないが、
# ファイルを盗み見て総当たりするには重い
ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000

# パスワードの最短の長さ。短すぎると誤操作防止にもならない
MIN_PASSWORD_LENGTH = 4

# 差し替えを断られたときに試し直す回数と間隔
REPLACE_ATTEMPTS = 5
REPLACE_WAIT_SEC = 0.2

# 配布先フォルダに書き出す項目。**端末ごとに違うべきもの (作業
# ディレクトリの上書き、起動確認URLの差し替え) は入れない**
EXPORT_FIELDS = ("app_id", "display_name", "order_no", "repository",
                 "start_command", "start_args", "port", "health_path",
                 "stop_method", "enabled", "ui_mode")


def folder() -> Path:
    """配布先フォルダ。ランチャーのフォルダーの中に置く (一緒に運ばれる)。"""
    return app_config.APP_ROOT / FOLDER_NAME


def settings_path() -> Path:
    return folder() / SETTINGS_FILE


def password_path() -> Path:
    return folder() / PASSWORD_FILE


def exists() -> bool:
    return folder().is_dir()


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def _read_json(target: Path, what: str) -> tuple[Optional[dict], str]:
    """`(中身, 問題)`。無ければ `(None, "")`、壊れていれば `(None, 理由)`。"""
    try:
        # メモ帳で保存し直すと BOM が付くことがある。それでも読めるように
        raw = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"{what}を読めません ({target}): {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"{what}が壊れています ({target}): {exc}"
    if not isinstance(data, dict):
        return None, f"{what}の形が違います ({target})"
    return data, ""


def load() -> tuple[Optional[dict], str]:
    """配布先フォルダのツール設定を読む。**壊れていても起動は止めない。**"""
    return _read_json(settings_path(), "配布先フォルダの設定")


def _raw_items() -> list:
    data, _ = load()
    items = (data or {}).get("tools") or []
    return items if isinstance(items, list) else []


def _usable(item: Any) -> bool:
    """使える1件か。アプリIDが無い・空白入りのものは使わない。

    アプリIDは `/api/health` の照合に使う鍵。空白が入っていると
    どのツールとも一致せず、起動確認が永久に通らない。
    """
    if not isinstance(item, dict):
        return False
    app_id = str(item.get("app_id", "")).strip()
    return bool(app_id) and not any(c.isspace() for c in app_id)


def tools() -> list[dict[str, Any]]:
    """配布先フォルダのツール一覧 (使えるものだけ)。無ければ空。

    同じアプリIDが2度あれば1つにまとめる (後に書かれた項目が勝つ)。
    画面から作れば重ならないが、手で直したときの取りこぼしに備える。
    """
    unique: dict[str, dict[str, Any]] = {}
    for item in _raw_items():
        if _usable(item):
            app_id = str(item["app_id"]).strip()
            unique.setdefault(app_id, {}).update(item, app_id=app_id)
    return list(unique.values())


def rejected() -> list[str]:
    """使えなかった項目 (知らせる用)。"""
    return [repr(item.get("app_id", "") if isinstance(item, dict) else item)
            for item in _raw_items() if not _usable(item)]


def merged_tools() -> list[dict[str, Any]]:
    """その端末へ入れるツールの元になる一覧。

    * 配布先フォルダにツールがあれば、**その一覧だけ**。製品の既定値の
      一覧は混ぜない ── 混ぜると、配布元で消したツールが配った先に
      「未設定」で出てくる。同じアプリIDの既定値は、配布先フォルダに
      書かれていない項目を補うのにだけ使う
    * 無ければ (壊れている・空も含む) 製品の既定値

    **その端末にすでにあるツールには使わない** (既存のデータが優先)。
    """
    defaults = {str(item.get("app_id", "")).strip(): dict(item)
                for item in app_config.default_tools()
                if str(item.get("app_id", "")).strip()}
    listed = tools()
    if not listed:
        return list(defaults.values())
    return [{**defaults.get(item["app_id"], {}), **item} for item in listed]


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
def _write(target: Path, text: str) -> Path:
    """丸ごと書き直す。**書きかけの状態を残さない。**

    途中で落ちると壊れた JSON が残り、配った先で読めなくなる。
    別名に書いてから差し替える (差し替えは一瞬で済む)。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, target)
                return target
            except PermissionError:
                # Windows では、ほかの端末がちょうど読んでいると差し替えを
                # 断られる (共有フォルダーに置いている場合)。少し待てば空く
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_WAIT_SEC)
    finally:
        # 差し替えられなかったときに、書きかけを残さない
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target


def _write_json(target: Path, data: dict) -> Path:
    return _write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


# 置き場所をそのまま書いたパス (`C:/...`、`/...`、`//server/...`)
_ABSOLUTE = re.compile(r"^([A-Za-z]:|[/\\])")


def to_relative(start_command: str) -> str:
    """ランチャーのフォルダーを起点にした相対パスへ。

    配った先で置き場所が違っても同じ書き方で指せるようにするため。
    区切りは `/` にする ── JSON で `\\` を使うと2つ重ねる必要があり、
    手で直したときに壊しやすい。

    別のドライブにあるなど、相対にできなければ絶対パスのまま返す。
    """
    text = (start_command or "").strip().strip('"')
    if not text:
        return ""
    # **両方とも実体の場所にそろえてから比べる。** ランチャーの場所は
    # 実体で持っている (`Path.resolve()`)。ネットワークドライブ (Z:) は
    # `\\server\share` に、ジャンクションは本当の場所に置き換わるので、
    # 選んだ起動ファイルが `Z:/...` のままだと「別のドライブ」と誤って
    # 相対にできない
    try:
        relative = os.path.relpath(os.path.realpath(text),
                                   os.path.realpath(str(app_config.APP_ROOT)))
    except ValueError:
        return text.replace("\\", "/")        # 本当に別のドライブ
    return relative.replace("\\", "/")


def is_portable(start_command: str) -> bool:
    """ランチャーのフォルダーを起点にした書き方か。"""
    text = (start_command or "").strip()
    return bool(text) and not _ABSOLUTE.match(text)


def absolute_entries() -> list[str]:
    """配布先フォルダのうち、置き場所をそのまま書いたツール。

    相対にできなかった (別のドライブにある) ものを知らせるのに使う。
    配る先でも**まったく同じ場所**に置かないと見つからない。
    """
    return [f"{item.get('display_name') or item['app_id']}: "
            f"{item['start_command']}"
            for item in tools()
            if item.get("start_command") and not is_portable(
                str(item["start_command"]))]


def bar_position_active() -> str:
    """配布先フォルダに書かれた「ツールを起動したあとのバーの位置」。無ければ空。"""
    data, _ = load()
    bar = (data or {}).get("bar")
    value = bar.get("position_active") if isinstance(bar, dict) else ""
    return value if isinstance(value, str) else ""


def log_dir() -> str:
    """配布先フォルダに書かれた「ログの出力先」。無ければ空。"""
    data, _ = load()
    logs = (data or {}).get("logs")
    value = logs.get("dir") if isinstance(logs, dict) else ""
    return value if isinstance(value, str) else ""


def export_tools(registry_tools, *, relative: bool = True,
                 bar_position_active: str = "", log_dir: str = "") -> Path:
    """端末の設定から配布先フォルダを作る (作り直す)。

    **パスワードには触らない** (別のファイル)。作り直すたびに鍵が
    外れると、配った先で誰でも開けてしまう。
    """
    items = []
    for tool in registry_tools:
        item = {
            "app_id": tool.app_id,
            "display_name": tool.display_name,
            "order_no": tool.order_no,
            "repository": tool.repository,
            "start_command": (to_relative(tool.start_command) if relative
                              else tool.start_command.replace("\\", "/")),
            "start_args": tool.start_args,
            "port": tool.port,
            "health_path": tool.health_path,
            "stop_method": tool.stop_method,
            "enabled": bool(tool.enabled),
            "ui_mode": getattr(tool, "ui_mode", ""),
        }
        items.append({k: item[k] for k in EXPORT_FIELDS})

    data = {
        "format": FORMAT,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "generated_on": _computer_name(),
        "launcher_version": app_config.version(),
        "tools": items,
    }
    if bar_position_active:
        # 端末ごとの位置 (手で動かした場所) ではなく、**ツールを起動した
        # あとにどこへ寄るか**の決まりだけを配る
        data["bar"] = {"position_active": bar_position_active}
    if log_dir:
        # 共有フォルダーを書いておけば、配った全端末の記録が1か所に集まる
        # (端末ごとに、端末名のフォルダーに分けて書く)
        data["logs"] = {"dir": log_dir}
    target = _write_json(settings_path(), data)
    _write_readme()
    log.info("配布先フォルダを作りました: %s (%d件)", folder(), len(items))
    return target


def _computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or ""


_README = """\
このフォルダーは「配布先フォルダ」です。

業務ツール統合ランチャーが起動するときに読み込み、配った先の端末でも
最初からツールの設定が入った状態にします。

  settings.json   ツールの設定 (起動ファイルはランチャーのフォルダーからの相対パス)
  password.json   ［設定］を開くための管理者パスワードの照合値

・手で書き換えないでください。ランチャーの［設定］→「配布先フォルダを作る」で
  作り直せます。
・このフォルダーがあれば、ツールの一覧はこのフォルダーのとおりになります
  (配布元で消したツールは、配った先にも出ません)。
・その端末にすでにある設定が優先されます。このフォルダーから入るのは、
  その端末にまだ無いツールと、空欄の起動ファイルだけです。
  ただし、まだ何も設定していない端末 (工場出荷のまま) は、
  このフォルダーのとおりにそろえます。
  設定済みの端末をこのフォルダーの内容で置き換えたいときは、その端末で
  ［設定］→「配布先フォルダの内容で置き換える」を押してください。
・ランチャーを新しい版に入れ替えるときも、このフォルダーは残してください。
・管理者パスワードを忘れたときは password.json を消してください。
  次に［設定］を開いたときに決め直せます (ツールの設定は消えません)。
"""


def _write_readme() -> None:
    """人が読む説明。メモ帳で文字化けしないよう BOM 付き UTF-8・CRLF。"""
    try:
        target = folder() / README_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        # `write_text(newline=...)` は Python 3.10 から。バイトで書けば 3.9 でも同じ
        target.write_bytes(_README.replace("\n", "\r\n").encode("utf-8-sig"))
    except OSError:
        log.warning("配布先フォルダの説明を書けませんでした", exc_info=True)


# ------------------------------------------------------------------
# パスワード
# ------------------------------------------------------------------
def load_password() -> tuple[Optional[dict], str]:
    """パスワードの照合値を読む。無ければ `(None, "")`。"""
    return _read_json(password_path(), "管理者パスワードのファイル")


def has_password() -> bool:
    record, _ = load_password()
    return isinstance(record, dict) and bool(record.get("hash"))


def validate_new_password(text: str) -> str:
    """新しいパスワードを確かめる。問題があれば理由。"""
    if len(text or "") < MIN_PASSWORD_LENGTH:
        return f"{MIN_PASSWORD_LENGTH}文字以上にしてください"
    if text.strip() != text:
        return "前後に空白を入れないでください"
    return ""


def set_password(text: str) -> Path:
    """パスワードを決める (変える)。**ツールの設定には触らない。**"""
    problem = validate_new_password(text)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(16)
    record = {
        "format": FORMAT,
        "algorithm": ALGORITHM,
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(text, salt, ITERATIONS).hex(),
    }
    target = _write_json(password_path(), record)
    _write_readme()
    log.info("管理者パスワードを設定しました: %s", target)
    return target


def verify_password(text: str) -> bool:
    """合っているか。**パスワードが無ければ常に偽。**"""
    record, _ = load_password()
    if not isinstance(record, dict) or record.get("algorithm") != ALGORITHM:
        return False
    try:
        salt = bytes.fromhex(str(record["salt"]))
        expected = bytes.fromhex(str(record["hash"]))
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError):
        return False
    actual = _derive(text or "", salt, iterations)
    # 比べる時間で中身が漏れないようにする
    return hmac.compare_digest(actual, expected)


def _derive(text: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt, iterations)


# ------------------------------------------------------------------
# 様子
# ------------------------------------------------------------------
def state_text() -> str:
    """［設定］に出す1行。"""
    data, problem = load()
    if problem:
        return "設定を読めません (起動時の案内を見てください)"
    if data is None or not tools():
        return "まだ作っていません"
    made = data.get("generated_at", "")
    where = data.get("generated_on", "")
    text = f"{len(tools())}件"
    if made:
        text += f" / {made}" + (f" に {where} で作成" if where else "")
    return text


def describe() -> str:
    """`start_debug.bat --check` に出す1枚。"""
    lines = [f"配布先フォルダ: {folder()}"]
    if not exists():
        lines.append("  なし (端末ごとの設定だけで動きます)")
        return "\n".join(lines)

    data, problem = load()
    if problem:
        lines.append(f"  [エラー] {problem}")
    elif data is None:
        lines.append("  設定: なし")
    else:
        made = data.get("generated_at", "")
        where = data.get("generated_on", "")
        lines.append(f"  設定: ツール {len(tools())}件"
                     + (f" / {made} に {where} で作成" if made else ""))
        for app_id in rejected():
            lines.append(f"  [注意] アプリIDが使えないので読みません: {app_id}")

    _, broken = load_password()
    if broken:
        lines.append(f"  [エラー] {broken}")
    else:
        lines.append(f"  パスワード: {'あり' if has_password() else 'なし'}")
    lines.append("  その端末にすでにある設定が優先されます")
    return "\n".join(lines)
