"""ツール側が用意する「入口」 (ランチャー連携 要件定義 §3・§10)

**ランチャーは各ツールの作りを知らない。** ツールが自分の起動ファイルの
隣に次のファイルを置いていれば、ランチャーは自分の推測 (起動確認の URL・
待ち受けポート探し・窓の検出・停止方法の自動選択) より**優先して**それを
使う。置いていないツールは、これまでどおりランチャーが補う。

    launcher_check.bat   起動確認の入口。ランチャーが繰り返し実行する
                           終了コード 0 … 使える (起動完了)
                           終了コード 2 … 動いているが準備中
                           それ以外     … 動いていない
                         最後に出力した1行は、起動中の窓に「準備の段階」
                         として出す (任意)
    launcher_stop.bat    終了の入口。ツールが自分で後片付けして終わる。
                         終わったかどうかは起動確認の入口で確かめる
    launcher.json        (任意) 名前を変えたいとき・確認を URL でするとき

        {
          "check": "launcher_check.bat",
          "check_url": "http://127.0.0.1:8700/api/health",
          "stop": "launcher_stop.bat"
        }

        check と check_url はどちらか一方 (両方あれば check)。check_url は
        HTTP 200 で「使える」(JSON に "ready": false があれば準備中)、
        つながらなければ「動いていない」。パスは launcher.json からの相対。

配布先設定 (［設定］) で選ぶのは**起動ファイルだけ** (要件 §7)。入口は
起動ファイルのフォルダーから見つけるので、PC ごとに A.exe／A.vbs を
選び分けても、同じフォルダーなら同じ入口を使う。

**画面の部品 (tkinter) は読み込まない。**
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import fileprobe, health
from .logging_utils import get_logger

log = get_logger("tool_entries")

CHECK_FILE = "launcher_check.bat"
STOP_FILE = "launcher_stop.bat"
MANIFEST = "launcher.json"

# 起動確認の入口を待つ上限 (秒)。これを過ぎたら「分からない」
CHECK_TIMEOUT_SEC = 10.0
# 終了の入口を待つ上限 (秒)
STOP_TIMEOUT_SEC = 60.0
# 見つけた入口を覚えておく長さ (秒)。見回りのたびにフォルダーを見にいかない
CACHE_SEC = 10.0

READY = "ready"            # 使える
STARTING = "starting"      # 動いているが準備中
STOPPED = "stopped"        # 動いていない
UNKNOWN = "unknown"        # 確かめられなかった (時間切れ・実行できない)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_lock = threading.Lock()
_cache: dict[str, tuple[float, "Entries"]] = {}


@dataclass(frozen=True)
class Entries:
    """そのツールが用意している入口。無ければ空。"""

    folder: str = ""
    check: str = ""            # 起動確認の入口 (ファイル)
    check_url: str = ""        # 起動確認の入口 (URL)
    stop: str = ""             # 終了の入口 (ファイル)
    problem: str = ""          # launcher.json を読めなかった、など

    @property
    def has_check(self) -> bool:
        return bool(self.check or self.check_url)

    @property
    def any(self) -> bool:
        return self.has_check or bool(self.stop)

    def describe(self) -> str:
        """1行の説明 (［設定］・診断に出す)。"""
        parts = []
        if self.check:
            parts.append(f"起動確認 {Path(self.check).name}")
        elif self.check_url:
            parts.append(f"起動確認 {self.check_url}")
        if self.stop:
            parts.append(f"終了 {Path(self.stop).name}")
        text = "ツールの入口: " + "・".join(parts) if parts else ""
        if self.problem:
            text = (text + " / " if text else "") + self.problem
        return text


@dataclass(frozen=True)
class Check:
    """起動確認の入口の答え。"""

    state: str
    note: str = ""             # 準備の段階・理由
    code: Optional[int] = None

    @property
    def ready(self) -> bool:
        return self.state == READY

    @property
    def alive(self) -> bool:
        return self.state in (READY, STARTING)


# ------------------------------------------------------------------
# 見つける
# ------------------------------------------------------------------
def folder_of(start_command: str) -> str:
    text = (start_command or "").strip().strip('"')
    return os.path.dirname(text) if text else ""


def for_start(start_command: str) -> Entries:
    """起動ファイルのフォルダーにある入口。**例外を出さない。**"""
    folder = folder_of(start_command)
    if not folder:
        return Entries()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(folder)
        if hit is not None and hit[0] > now:
            return hit[1]
    # 起動ファイルが**あると確かめられたときだけ**隣を見る。古い置き場所
    # (つながらない共有フォルダー) を、入口を探すために何度も問い合わせない
    # (起動ファイルの確かめは `fileprobe` が覚えているので重ならない)
    if not fileprobe.probe(start_command).found:
        entries = Entries()
    else:
        entries = _find(folder)
    with _lock:
        _cache[folder] = (now + CACHE_SEC, entries)
    if entries.any or entries.problem:
        log.debug("ツールの入口 (%s): %s", folder, entries.describe())
    return entries


def for_tool(tool) -> Entries:
    """`Tool` でも `RunningTool` でもよい (起動ファイルを持っていれば)。"""
    return for_start(getattr(tool, "start_command", "") or "")


def for_record(running) -> Entries:
    """実行中の記録 (`RunningTool`) から。"""
    return for_tool(running)


def forget() -> None:
    """覚えた入口を忘れる (設定の保存後・試験)。"""
    with _lock:
        _cache.clear()


def _find(folder: str) -> Entries:
    base = Path(folder)
    manifest = base / MANIFEST
    check_file = base / CHECK_FILE
    stop_file = base / STOP_FILE
    found = fileprobe.probe_many([manifest, check_file, stop_file])

    def exists(path: Path) -> bool:
        probe = found.get(str(path))
        return bool(probe and probe.found)

    check = str(check_file) if exists(check_file) else ""
    stop = str(stop_file) if exists(stop_file) else ""
    check_url = ""
    problems: list[str] = []
    if exists(manifest):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("中身が { } ではありません")
        except (OSError, ValueError) as exc:
            problems.append(f"{MANIFEST} を読めません ({exc})")
            data = {}
        named_check = _named_file(base, data.get("check"), "check", problems)
        named_stop = _named_file(base, data.get("stop"), "stop", problems)
        url = str(data.get("check_url") or "").strip()
        if url and not url.lower().startswith(("http://127.0.0.1", "http://localhost")):
            problems.append(f"check_url はこの端末 (127.0.0.1) の URL にしてください: {url}")
            url = ""
        if named_check:
            check = named_check
        elif url:
            check, check_url = "", url
        if named_stop:
            stop = named_stop
    return Entries(folder=str(base), check=check, check_url=check_url, stop=stop,
                   problem="・".join(problems))


def _named_file(base: Path, value, key: str, problems: list[str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text) if os.path.isabs(text) else base / text
    if fileprobe.is_file(path):
        return str(path)
    problems.append(f"{MANIFEST} の {key} が見つかりません: {text}")
    return ""


# ------------------------------------------------------------------
# 確かめる・止める
# ------------------------------------------------------------------
def check(entries: Entries, *, timeout: Optional[float] = None) -> Check:
    """起動確認の入口に聞く。入口が無ければ UNKNOWN。**例外を出さない。**"""
    timeout = CHECK_TIMEOUT_SEC if timeout is None else timeout
    if entries.check:
        return _check_by_file(entries, timeout)
    if entries.check_url:
        return _check_by_url(entries.check_url, timeout)
    return Check(UNKNOWN, "起動確認の入口がありません")


def _run(path: str, folder: str, timeout: float,
         args: tuple = ()) -> tuple[Optional[int], str, str]:
    """入口を実行する。`(終了コード, 出力, 失敗の理由)`。時間切れなら終了コードは None。

    **出力はパイプではなく一時ファイルで受ける。** 入口が起こした子
    (`start` で起こしたもの・`timeout` コマンドなど) がパイプを持ったまま
    残ると、パイプで受けていれば子が終わるまでランチャーが待たされる。
    時間切れのときは、入口とその子をまとめて止める。
    """
    try:
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(
                [path, *args], cwd=folder or None,
                stdin=subprocess.DEVNULL,      # `pause` で固まらせない
                stdout=out, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
            try:
                code: Optional[int] = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                code = None
            out.seek(0)
            data = out.read(64 * 1024)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return None, "", f"{Path(path).name} を実行できません: {exc}"
    text = _decode(data)
    if code is None:
        return None, text, f"{Path(path).name} が {timeout:.0f}秒で終わりません"
    return code, text, ""


def _kill_tree(proc: subprocess.Popen) -> None:
    from . import desktop

    try:
        pids = desktop.process_tree(proc.pid) | {proc.pid}
    except Exception:                         # noqa: BLE001
        pids = {proc.pid}
    for pid in sorted(pids, reverse=True):
        if pid == proc.pid:
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=5,
                               creationflags=NO_WINDOW)
            else:
                os.kill(pid, 9)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _decode(data: bytes) -> str:
    """bat の出力は CP932 のことが多い。UTF-8 で読めなければ CP932。"""
    for encoding in ("utf-8", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _check_by_file(entries: Entries, timeout: float) -> Check:
    code, output, problem = _run(entries.check, entries.folder, timeout)
    if code is None:
        return Check(UNKNOWN, problem)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    note = lines[-1][:80] if lines else ""
    if code == 0:
        return Check(READY, note, 0)
    if code == 2:
        return Check(STARTING, note or "準備中", 2)
    return Check(STOPPED, note, code)


def _check_by_url(url: str, timeout: float) -> Check:
    try:
        with health.local_request(url, timeout=min(timeout, 3.0)) as res:
            body = res.read(64 * 1024)
    except urllib.error.HTTPError as exc:
        return Check(STARTING, f"HTTP {exc.code}")      # 答えてはいる
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError,)) or "timed out" in str(reason):
            return Check(UNKNOWN, "応答が遅い")
        return Check(STOPPED, "つながりません")
    try:
        payload = json.loads(body.decode("utf-8"))
    except ValueError:
        return Check(READY)
    if isinstance(payload, dict) and payload.get("ready") is False:
        return Check(STARTING, health.stage_text(payload) or "準備中")
    return Check(READY)


def run_stop(entries: Entries, *, timeout: Optional[float] = None,
             force: bool = False) -> tuple[bool, str]:
    """終了の入口を実行する。`(実行して終了コード 0 だったか, 説明)`。

    利用者が強制終了 (中断) を選んだときは `--force` を付けて呼ぶ。
    受け付けないツールは無視してよい。
    """
    timeout = STOP_TIMEOUT_SEC if timeout is None else timeout
    name = Path(entries.stop).name
    code, output, problem = _run(entries.stop, entries.folder, timeout,
                                 ("--force",) if force else ())
    if code is None:
        return False, problem
    output = output.strip()
    if code != 0:
        tail = output.splitlines()[-1][:120] if output else ""
        return False, f"{name} の終了コードが {code} でした" + (f": {tail}" if tail else "")
    return True, f"{name} で終了しました"


def wait_until(entries: Entries, want_alive: bool, timeout: float,
               interval: float = 0.5) -> bool:
    """起動確認の入口が「動いている／動いていない」になるまで待つ。"""
    deadline = time.monotonic() + timeout
    while True:
        result = check(entries, timeout=min(CHECK_TIMEOUT_SEC, max(1.0, timeout)))
        if result.state != UNKNOWN and result.alive == want_alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
