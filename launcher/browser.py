"""ブラウザー画面の開閉 (要件定義書 §8.3)

切り替えのときに、**そのツールを映している画面だけ**を閉じる。利用者が
別に開いている Google やメールのタブは閉じない。

    Chrome
    ├─ 日報      ← ランチャーが開いた   → 切り替え時に閉じる
    ├─ Google    ← 利用者が開いた       → 触らない
    └─ メール    ← 利用者が開いた       → 触らない

【なぜ `webbrowser.open()` では駄目か】

`webbrowser.open(url)` は既定のブラウザーへURLを渡すだけで、**開いた
タブへの手がかりを何も返さない**。あとからそのタブだけを閉じる方法が
無い。かといって Chrome のプロセスを落とすと、Google もメールも道連れに
なる ── 要件定義書 §8.3 がいちばん避けたい状態そのもの。

【この実装の考え方】

ブラウザーを **専用プロファイルのアプリウィンドウ** として起こす。

    chrome.exe --app=http://127.0.0.1:8733/
               --user-data-dir=%LOCALAPPDATA%\\...\\browser\\nlm.nippou-tool

こうすると3つが同時に手に入る。

    1. ランチャー自身の子プロセスになる     → PIDで閉じられる
    2. 利用者のふだんのブラウザーとは別インスタンス
       (プロファイルが違うので、ウィンドウもプロセスも共有しない)
       → **落としても Google やメールに手が届かない**
    3. タブ бар もアドレスバーも無い1画面   → 業務ツール専用の窓になる

「関係ないタブを閉じない」ことを、後から見分けるのではなく
**構造上あり得なくする**のが要点。

プロファイルはツールごとに分ける。前の窓が閉じきる前に次を起こしても、
同じプロファイルを掴み合って**新しい窓が既存プロセスに合流し、
こちらの手がかりが消える**ことを避けるため。

【見つからなかったとき】

Chromium系のブラウザーが1つも無い端末では、これまでどおり
`webbrowser.open()` に落とす。**その場合は閉じられない**ので、
`closable=False` を返して、画面には「手で閉じてください」と出す。
黙って閉じたことにしない。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import app_config
from .logging_utils import get_logger

log = get_logger("browser")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 起こした画面のプロセスの控え。**引き取るために持つ。**
# 手放したままにすると、閉じたあとも終了済みのプロセスがPID表に残り
# (POSIXの引き取り待ち)、「まだ開いている」と誤って判定される
_spawned: dict[int, subprocess.Popen] = {}

# 探す順。Edge はWindowsに必ず入っているので既定の先頭に置く。
# `config/launcher.json` の `browser.preferred` で変えられる
KNOWN_BROWSERS = ("edge", "chrome", "chromium")

# 実行ファイルの候補。環境変数は展開してから使う
_WINDOWS_CANDIDATES = {
    "edge": (
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    ),
    "chromium": (
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    ),
}

# 開発機と試験用 (Linux / macOS)
_POSIX_CANDIDATES = {
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "chrome": ("google-chrome", "google-chrome-stable"),
    "chromium": ("chromium", "chromium-browser"),
}


@dataclass
class BrowserSession:
    """ランチャーが開いた1つのブラウザー画面。"""

    app_id: str
    url: str = ""
    pid: int = 0
    # 閉じられるか。既定ブラウザーへ投げただけのときは False
    closable: bool = False
    # 使った実行ファイル (診断用)
    executable: str = ""
    # 専用プロファイルの場所。**停止前の照合の印に使う**
    profile_dir: str = ""

    @property
    def opened(self) -> bool:
        return bool(self.url)


# ------------------------------------------------------------------
# ブラウザーを探す
# ------------------------------------------------------------------
def _expand(path: str) -> str:
    return os.path.expandvars(path)


def find_browser(preferred: Optional[list[str]] = None) -> tuple[str, str]:
    """使える Chromium系ブラウザーを1つ返す。`(名前, 実行ファイル)`。

    見つからなければ `("", "")`。**探せないことを失敗にしない** ──
    既定ブラウザーへ落とせば、閉じられないだけで画面は開く。
    """
    order = preferred or list(KNOWN_BROWSERS)
    table = _WINDOWS_CANDIDATES if os.name == "nt" else _POSIX_CANDIDATES

    for name in order:
        for candidate in table.get(name, ()):
            if os.name == "nt":
                path = _expand(candidate)
                if path and Path(path).is_file():
                    return name, path
            else:
                found = shutil.which(candidate)
                if found:
                    return name, found

    # 設定で明示された実行ファイル (見つけられない置き方をしている端末用)
    for extra in _configured_paths():
        if Path(extra).is_file():
            return "custom", extra
    return "", ""


def _settings() -> dict:
    raw = app_config.load().get("browser")
    return raw if isinstance(raw, dict) else {}


def _configured_paths() -> list[str]:
    values = _settings().get("extra_paths") or []
    return [_expand(str(v)) for v in values if str(v).strip()]


def _preferred() -> list[str]:
    values = _settings().get("preferred") or []
    order = [str(v) for v in values if str(v) in KNOWN_BROWSERS]
    return order or list(KNOWN_BROWSERS)


def mode() -> str:
    """`app_window` (閉じられる) か `default` (既定ブラウザー)。"""
    value = str(_settings().get("mode", "app_window"))
    return value if value in ("app_window", "default") else "app_window"


# ------------------------------------------------------------------
# 開く
# ------------------------------------------------------------------
def profile_dir(app_id: str) -> Path:
    """ツールごとの専用プロファイル。

    ツールごとに分けるのは、前の窓が閉じきる前に次を起こしたときに
    プロファイルを掴み合わないようにするため。この道が**そのまま
    停止前の照合の印**にもなる ── この長いパスがコマンドラインに
    入っているプロセスだけを、ランチャーは閉じてよい。
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in app_id)
    return app_config.local_dir("browser") / safe


def build_command(executable: str, url: str, profile: Path) -> list[str]:
    """アプリウィンドウとして開くための起動行。"""
    return [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile}",
        # 初回の案内や既定ブラウザーの確認を出さない。
        # 専用プロファイルなので毎回「初回」になる
        "--no-first-run",
        "--no-default-browser-check",
        # 前回落とし方が荒かったときの復元の案内を出さない
        "--disable-session-crashed-bubble",
    ]


def open_window(app_id: str, url: str) -> BrowserSession:
    """業務ツールの画面を開く。閉じられる形を優先する。"""
    if not url:
        return BrowserSession(app_id=app_id)

    if mode() == "default":
        return _open_default(app_id, url, reason="設定で既定ブラウザーを使う指定です")

    name, executable = find_browser(_preferred())
    if not executable:
        return _open_default(
            app_id, url,
            reason="Chromium系のブラウザー(Edge / Chrome)が見つかりませんでした")

    profile = profile_dir(app_id)
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _open_default(app_id, url,
                             reason=f"プロファイルを作れませんでした: {exc}")

    command = build_command(executable, url, profile)
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return _open_default(app_id, url,
                             reason=f"ブラウザーを起こせませんでした: {exc}")

    _spawned[proc.pid] = proc
    log.info("画面を開きました: %s (%s pid=%s)", url, name, proc.pid)
    return BrowserSession(app_id=app_id, url=url, pid=proc.pid, closable=True,
                          executable=executable, profile_dir=str(profile))


# ------------------------------------------------------------------
# 後始末
# ------------------------------------------------------------------
# 閉じた画面が消えるのを待つ上限 (秒)
FORGET_WAIT_SEC = 3.0


def forget(pid: int) -> None:
    """閉じた画面のプロセスを引き取る。

    `process_manager.close_browser` が落としたあとに呼ぶ。ここで
    引き取らないと、終了済みのプロセスが残って次の判定を惑わせる。
    """
    proc = _spawned.pop(pid, None)
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.wait(timeout=FORGET_WAIT_SEC)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("画面のプロセスを引き取れませんでした (pid=%s): %s", pid, exc)


def reap() -> None:
    """利用者が手で閉じた画面を引き取る。

    生存監視のたびに呼ぶ。閉じられたことに気づくのはランチャーなので、
    片付けもこちらでやる。
    """
    for pid, proc in list(_spawned.items()):
        try:
            if proc.poll() is not None:
                _spawned.pop(pid, None)
        except (OSError, ValueError):
            _spawned.pop(pid, None)


def managed_pids() -> list[int]:
    """いまランチャーが持っている画面のPID (診断用)。"""
    return sorted(_spawned)


def purge_unused(known_app_ids) -> list[str]:
    """登録されていないツールのプロファイルを消す。

    プロファイルはツールごとに作られ、中身は Chromium のキャッシュなので
    **使い続けるだけ膨らむ**。設定からツールを消しても残り続けると、
    誰も使わないキャッシュが端末に溜まる。

    消すのは**登録が無くなったものだけ**。使っているツールのぶんは
    残す ── 消すと次に開いたときに作り直しになり、体感が落ちる。

    ランチャーの起動時に1度だけ呼ぶ。消した名前を返す (記録用)。
    """
    wanted = {profile_dir(app_id).name for app_id in known_app_ids}
    removed: list[str] = []
    try:
        root = app_config.local_dir("browser")
        if not root.exists():
            return removed
        for child in root.iterdir():
            if not child.is_dir() or child.name in wanted:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
    except OSError as exc:
        log.warning("使われていないプロファイルを片付けられませんでした: %s", exc)
        return removed

    if removed:
        log.info("使われていない画面のプロファイルを消しました: %s",
                 "、".join(removed))
    return removed


def _open_default(app_id: str, url: str, *, reason: str) -> BrowserSession:
    """既定ブラウザーへ投げる。**この画面は閉じられない。**"""
    log.info("既定のブラウザーで開きます (%s): %s", reason, url)
    try:
        webbrowser.open(url)
    except Exception as exc:                  # noqa: BLE001 - 開けなくても続ける
        log.warning("ブラウザーを開けませんでした (%s): %s", url, exc)
        return BrowserSession(app_id=app_id)
    return BrowserSession(app_id=app_id, url=url, closable=False)


def describe() -> str:
    """診断用の1枚 (`start_debug.bat --check` が出す)。"""
    lines = [f"画面の開き方  : {mode()}"]
    if mode() == "default":
        lines.append("  既定のブラウザーへ渡します。"
                     "切り替えのとき画面は自動で閉じません。")
        return "\n".join(lines)

    name, executable = find_browser(_preferred())
    if executable:
        lines.append(f"使うブラウザー: {name} ({executable})")
        lines.append(f"プロファイル  : {app_config.local_dir('browser')}")
        lines.append("  切り替えのとき、この画面だけを閉じます。")
    else:
        lines.append("使うブラウザー: (見つかりません)")
        lines.append("  Edge / Chrome が見つからないため、既定のブラウザーへ"
                     "渡します。")
        lines.append("  その場合、切り替えのとき画面は自動で閉じません。")
    return "\n".join(lines)
