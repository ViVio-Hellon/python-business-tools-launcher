"""起動ファイルがあるかを、**止まらずに・待ちすぎずに**確かめる

ツールを別の場所へ移したあと、**古い置き場所が設定に残っている**ことがある。
Windows では、その置き場所によって「ファイルがあるか」の問い合わせそのものが
うまくいかない:

    アクセス権を外されたフォルダー   PermissionError
                                     (`Path.is_file()` は False でなく例外を出す)
    つながらない共有フォルダー        OSError (WinError 53 / 64 / 1326 など)、
                                     または応答まで数十秒待たされる
    抜いた USB・空の DVD ドライブ     OSError (WinError 21)

ランチャーは起動のたびに起動ファイルを確かめる (ボタンの色・配布先フォルダの
読み込み)。例外ならランチャーが**起動しなくなり**、待たされれば起動に何分も
かかる。そこで:

* **例外を出さない。** 「ある」「無い」「確かめられない」の3つで答える
* 1か所あたり `CHECK_TIMEOUT_SEC` までしか待たない (問い合わせは裏のスレッド)
* 確かめられなかった場所・**答えが遅い場所**は `UNREACHABLE_TTL_SEC` のあいだ
  覚えて、問い合わせ直さない (遅れて届いた答えがあれば、それを覚える)
* すぐ答える場所の「ある」「無い」は覚えない ── あとから置いたファイルに
  すぐ気づくため

**画面の部品 (tkinter) は読み込まない。**
"""
from __future__ import annotations

import os
import stat
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from .logging_utils import get_logger

log = get_logger("fileprobe")

# 1か所の問い合わせを待つ上限 (秒)。ふつうのフォルダーなら一瞬で返る
CHECK_TIMEOUT_SEC = 2.0
# 確かめられなかった場所を覚えておく長さ (秒)
UNREACHABLE_TTL_SEC = 60.0

FOUND = "found"
MISSING = "missing"
NOT_FILE = "not_file"
UNKNOWN = "unknown"

# 名前の形が正しくない (使えない文字など)。**無い**のと同じに扱う
_INVALID_NAME_WINERRORS = {123, 161}

_lock = threading.Lock()
# 確かめられなかった・答えが遅かった場所と、その答え (期限つき)
_slow: dict[str, tuple[float, "Probe"]] = {}


@dataclass(frozen=True)
class Probe:
    """確かめた結果。"""

    state: str                # FOUND / MISSING / NOT_FILE / UNKNOWN
    reason: str = ""          # 確かめられなかった理由 (UNKNOWN のとき)

    @property
    def found(self) -> bool:
        return self.state == FOUND

    @property
    def unknown(self) -> bool:
        return self.state == UNKNOWN

    def describe(self) -> str:
        return {FOUND: "ある", MISSING: "無い", NOT_FILE: "ファイルではない"}.get(
            self.state, f"確かめられない ({self.reason})")


def _clean(path) -> str:
    return str(path or "").strip().strip('"')


def _check(text: str) -> Probe:
    """実際に問い合わせる。**例外は出さない。**"""
    try:
        st = os.stat(text)
    except (FileNotFoundError, NotADirectoryError):
        return Probe(MISSING)
    except ValueError:
        return Probe(MISSING, "使えない文字が入っています")
    except OSError as exc:
        if getattr(exc, "winerror", None) in _INVALID_NAME_WINERRORS:
            return Probe(MISSING, "名前の形が正しくありません")
        return Probe(UNKNOWN, _reason(exc))
    return Probe(FOUND if stat.S_ISREG(st.st_mode) else NOT_FILE)


def _reason(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return f"アクセスが拒否されました: {exc.strerror or exc}"
    text = exc.strerror or str(exc)
    code = getattr(exc, "winerror", None)
    return f"{text} (WinError {code})" if code else text


def _cached(text: str) -> Optional[Probe]:
    with _lock:
        hit = _slow.get(text)
        if hit is None:
            return None
        until, probe = hit
        if until <= time.monotonic():
            del _slow[text]
            return None
    return probe


def _remember(text: str, probe: Probe, *, slow: bool) -> None:
    """覚える。**すぐ答えた「ある」「無い」は覚えない** (覚えていれば消す)。"""
    with _lock:
        if probe.unknown or slow:
            _slow[text] = (time.monotonic() + UNREACHABLE_TTL_SEC, probe)
        else:
            _slow.pop(text, None)


def probe_many(paths: Iterable, *,
               timeout: Optional[float] = None) -> dict[str, Probe]:
    """いくつもの場所を**同時に**確かめる。全体で `timeout` 秒しか待たない。

    戻り値の鍵は、渡した値から前後の空白と `"` を除いたもの。
    `timeout` を省くと `CHECK_TIMEOUT_SEC`。
    """
    if timeout is None:
        timeout = CHECK_TIMEOUT_SEC
    results: dict[str, Probe] = {}
    pending: dict[str, threading.Thread] = {}
    boxes: dict[str, dict] = {}
    for path in paths:
        text = _clean(path)
        if not text or text in results or text in pending:
            continue
        hit = _cached(text)
        if hit is not None:
            results[text] = hit
            continue
        box: dict = {}

        def work(text=text, box=box) -> None:
            probe = _check(text)
            with _lock:
                late = box.get("late", False)
                box["probe"] = probe
            # 待ちきれずに「確かめられない」と答えたあとで答えが来たら、
            # **その答えを覚える** (次はすぐ答えられる。遅い場所なので覚えておく)
            _remember(text, probe, slow=late)

        thread = threading.Thread(target=work, name="fileprobe", daemon=True)
        thread.start()
        pending[text] = thread
        boxes[text] = box

    deadline = time.monotonic() + timeout
    for text, thread in pending.items():
        thread.join(max(0.0, deadline - time.monotonic()))
        with _lock:
            probe = boxes[text].get("probe")
            if probe is None:
                boxes[text]["late"] = True
        if probe is None:
            probe = Probe(UNKNOWN, f"{timeout:.0f}秒待っても応答がありません")
            _remember(text, probe, slow=True)
            log.warning("起動ファイルを確かめられません (応答なし): %s", text)
        elif probe.unknown:
            log.warning("起動ファイルを確かめられません: %s (%s)", text, probe.reason)
        results[text] = probe
    return results


def probe(path, *, timeout: Optional[float] = None) -> Probe:
    """1か所を確かめる。空なら MISSING。"""
    text = _clean(path)
    if not text:
        return Probe(MISSING)
    return probe_many([text], timeout=timeout)[text]


def is_file(path, *, timeout: Optional[float] = None) -> bool:
    """ファイルが**あると確かめられた**ときだけ真。例外は出さない。"""
    return probe(path, timeout=timeout).found


def forget() -> None:
    """覚えている「確かめられなかった・遅い場所」を忘れる (試験・設定の保存後)。"""
    with _lock:
        _slow.clear()
