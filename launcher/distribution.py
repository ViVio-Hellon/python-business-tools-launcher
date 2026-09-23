"""配布設定 (`config/distribution.json`)

**ランチャーのフォルダーと一緒に運ばれる設定。** 端末ごとの設定
(`%LOCALAPPDATA%` の設定DB) はフォルダーをコピーしても移らないので、
配った先でも最初から設定済みにしたいものはここに置く。

    config/launcher.json      製品の既定値。ランチャーを更新すると入れ替わる
    config/distribution.json  現場の設定。**ランチャーを更新しても残す**
    %LOCALAPPDATA%\\...\\launcher.db  その端末の設定

分けてあるのは、ランチャーを新しい版へ入れ替えたときに、現場で作った
設定まで消えないようにするため。

【作り方】
手で書かない。JSON はコメントを許さず、`\\` を2つ重ねる決まりもあって、
1文字の誤りで設定全体が読めなくなる。1台を［設定］で整えてから
「この端末の設定を配布設定として書き出す」で作る (`export_tools`)。

【読み込み方】
内容が変わったときだけ端末の設定へ反映する (`tool_registry` が見る)。
配布し直せば全端末に届き、そのあと端末で直した値は次の配布まで残る。

【パスワード】
［設定］画面を開くためのパスワードもここに置く。全端末で共通になる。

**これは誤操作を防ぐための鍵で、守りの固い鍵ではない。** このファイル
を消すか書き換えられる人なら外せる。ライン作業者が起動ファイルを
うっかり変えてしまう、を防ぐのが目的。
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
FILE_NAME = "distribution.json"

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

# 配布設定に書き出す項目。**端末ごとに違うべきもの (作業ディレクトリの
# 上書き、起動確認URLの差し替え) は入れない**
EXPORT_FIELDS = ("app_id", "display_name", "order_no", "repository",
                 "start_command", "start_args", "port", "health_path",
                 "stop_method", "enabled")


def path() -> Path:
    return app_config.APP_ROOT / "config" / FILE_NAME


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
def load() -> tuple[Optional[dict], str]:
    """配布設定を読む。`(中身, 問題)`。

    無ければ `(None, "")` ── 配布設定が無いのは普通のこと。
    壊れていれば `(None, 理由)`。**起動は止めない。**
    """
    target = path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"配布設定を読めません ({target}): {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"配布設定が壊れています ({target}): {exc}"
    if not isinstance(data, dict):
        return None, f"配布設定の形が違います ({target})"
    return data, ""


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
    """配布設定のツール一覧 (使えるものだけ)。無ければ空。"""
    return [dict(item, app_id=str(item["app_id"]).strip())
            for item in _raw_items() if _usable(item)]


def rejected() -> list[str]:
    """使えなかった項目 (知らせる用)。"""
    return [repr(item.get("app_id", "") if isinstance(item, dict) else item)
            for item in _raw_items() if not _usable(item)]


def merged_tools() -> list[dict[str, Any]]:
    """製品の既定値に、配布設定を重ねた一覧。

    同じアプリIDなら**配布設定の項目が勝つ** (書かれている項目だけ)。
    配布設定にしか無いツール (5個目以降) は後ろに足す。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in app_config.default_tools():
        app_id = str(item.get("app_id", "")).strip()
        if app_id:
            merged[app_id] = dict(item)
            order.append(app_id)
    for item in tools():
        app_id = str(item["app_id"]).strip()
        if app_id in merged:
            merged[app_id].update(item)
        else:
            merged[app_id] = dict(item)
            order.append(app_id)
    return [merged[app_id] for app_id in order]


def tools_hash(data: Optional[dict] = None) -> str:
    """ツール設定の指紋。**変わったときだけ反映する**ための印。

    パスワードや書き出し日時は含めない。パスワードを変えただけで全端末の
    ツール設定が上書きし直されるのは困る。
    """
    if data is None:
        data, _ = load()
    items = (data or {}).get("tools") or []
    if not items:
        return ""                     # パスワードだけの配布設定
    text = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------
# 書く
# ------------------------------------------------------------------
def _write(data: dict) -> Path:
    """丸ごと書き直す。**書きかけの状態を残さない。**

    途中で落ちると壊れた JSON が残り、配った先で読めなくなる。
    別名に書いてから差し替える (差し替えは一瞬で済む)。
    """
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
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


def _current_or_new() -> dict:
    data, problem = load()
    if problem:
        # 壊れたものの上に書くと、残っていたパスワードまで消える。
        # **黙って上書きしない**
        raise ValueError(problem)
    return data or {"format": FORMAT}


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
    try:
        relative = os.path.relpath(text, str(app_config.APP_ROOT))
    except ValueError:
        return text.replace("\\", "/")        # 別ドライブ
    return relative.replace("\\", "/")


# 置き場所をそのまま書いたパス (`C:/...`、`/...`、`//server/...`)
_ABSOLUTE = re.compile(r"^([A-Za-z]:|[/\\])")


def is_portable(start_command: str) -> bool:
    """ランチャーのフォルダーを起点にした書き方か。"""
    text = (start_command or "").strip()
    return bool(text) and not _ABSOLUTE.match(text)


def absolute_entries() -> list[str]:
    """配布設定のうち、置き場所をそのまま書いたツール。

    相対にできなかった (別のドライブにある) ものを知らせるのに使う。
    配る先でも**まったく同じ場所**に置かないと見つからない。
    """
    return [f"{item.get('display_name') or item['app_id']}: "
            f"{item['start_command']}"
            for item in tools()
            if item.get("start_command") and not is_portable(
                str(item["start_command"]))]


def export_tools(registry_tools, *, relative: bool = True) -> Path:
    """端末の設定を配布設定として書き出す。

    **パスワードは残す。** 書き出すたびに消えると、配った先の鍵が
    外れてしまう。
    """
    data = _current_or_new()
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
        }
        items.append({k: item[k] for k in EXPORT_FIELDS})

    data["format"] = FORMAT
    data["tools"] = items
    data["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data["generated_on"] = _computer_name()
    data["launcher_version"] = app_config.version()
    target = _write(data)
    log.info("配布設定を書き出しました: %s (%d件)", target, len(items))
    return target


def _computer_name() -> str:
    return os.environ.get("COMPUTERNAME") or platform.node() or ""


# ------------------------------------------------------------------
# パスワード
# ------------------------------------------------------------------
def has_password() -> bool:
    data, _ = load()
    record = (data or {}).get("password")
    return isinstance(record, dict) and bool(record.get("hash"))


def validate_new_password(text: str) -> str:
    """新しいパスワードを確かめる。問題があれば理由。"""
    if len(text or "") < MIN_PASSWORD_LENGTH:
        return f"{MIN_PASSWORD_LENGTH}文字以上にしてください"
    if text.strip() != text:
        return "前後に空白を入れないでください"
    return ""


def set_password(text: str) -> Path:
    """パスワードを決める (変える)。**ツール設定には触らない。**"""
    problem = validate_new_password(text)
    if problem:
        raise ValueError(problem)
    data = _current_or_new()
    salt = secrets.token_bytes(16)
    data["password"] = {
        "algorithm": ALGORITHM,
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "hash": _derive(text, salt, ITERATIONS).hex(),
    }
    target = _write(data)
    log.info("管理者パスワードを設定しました: %s", target)
    return target


def verify_password(text: str) -> bool:
    """合っているか。**パスワードが無ければ常に偽。**"""
    data, _ = load()
    record = (data or {}).get("password")
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
# 診断
# ------------------------------------------------------------------
def describe() -> str:
    """`start_debug.bat --check` に出す1枚。"""
    data, problem = load()
    lines = [f"配布設定      : {path()}"]
    if problem:
        lines.append(f"  [エラー] {problem}")
        return "\n".join(lines)
    if data is None:
        lines.append("  なし (端末ごとの設定だけで動きます)")
        lines.append("  パスワード: なし")
        return "\n".join(lines)

    items = data.get("tools") or []
    made = data.get("generated_at", "")
    where = data.get("generated_on", "")
    lines.append(f"  ツール: {len(items)}件"
                 + (f" / {made} に {where} で書き出し" if made else ""))
    lines.append(f"  パスワード: {'あり' if has_password() else 'なし'}")
    return "\n".join(lines)
