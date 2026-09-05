"""相手ツールの起動確認 (要件定義書 §7.2 / 基盤仕様書 2.3)

`start.bat` を実行しただけでは起動完了と判断しない。対象ツールが実際に
応答するようになってからブラウザーを開く (要件定義書 §21「`start.bat` を
実行しただけで起動成功と判断しない」)。

**HTTPが返るだけでは足りない。** 同じポートを別のアプリが使っている
ことがあるので、`app_id` の一致まで見る (基盤仕様書 2.3)。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from .logging_utils import get_logger

log = get_logger("health")

# 1回の問い合わせの待ち時間 (秒)。相手は同じPCの中なので短くてよい。
# 長くすると、死んだツールの後片付けで毎回この分だけ待たされる
TIMEOUT_SEC = 1.5

# 自分自身 (127.0.0.1) への通信に**プロキシを通さない**ための送信口。
#
# `urllib.request.urlopen()` の既定は、環境変数 `HTTP_PROXY` や
# Windowsのインターネット設定からプロキシを拾う。社内PCではたいてい
# プロキシが入っており、除外一覧に `127.0.0.1` が無いと、**自分自身への
# 通信までプロキシへ送られて失敗する**。相手ツールは待ち受けを始めて
# いるのに応答が返らず、時間切れで「起動できませんでした」になる。
# ループバックにプロキシを挟む理由は無いので、ここで明示的に外す。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_request(url: str, *, timeout: float, data: Optional[bytes] = None,
                  method: Optional[str] = None,
                  headers: Optional[dict] = None):
    """127.0.0.1 への要求。プロキシを経由しない。

    ランチャーが外に出す通信はこれだけなので、送信口を1つに集約する。
    """
    request = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
    return _LOCAL_OPENER.open(request, timeout=timeout)


def probe(url: str, *, timeout: float = TIMEOUT_SEC) -> Optional[dict[str, Any]]:
    """起動確認URLを叩く。応答しない・JSONでないなら `None`。"""
    if not url:
        return None
    try:
        with local_request(url, timeout=timeout) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("応答なし (%s): %s", url, exc)
        return None
    return payload if isinstance(payload, dict) else None


def is_tool(health: Optional[dict[str, Any]], app_id: str) -> bool:
    """その応答が**目的のツール**のものか。

    `app_id` を見ないと、たまたま同じポートを使っている別のアプリを
    目的のツールだと誤認する。要件定義書 §7.2 が `/health` に `app_id`
    を持たせているのはこのため。

    相手が `app_id` を返さない場合は照合できない。**その場合は
    「別のアプリではない」と言い切れないので偽とする** ── 誤って
    別アプリのプロセスを掴み、あとで停止してしまうより害が小さい。
    """
    if not health:
        return False
    return str(health.get("app_id", "")) == app_id


def is_ready(health: Optional[dict[str, Any]]) -> bool:
    """準備まで終わっているか。

    相手が `ready` を返さないなら、応答している時点で準備済みとみなす。
    起動待機画面を持たないツールもあるため。
    """
    if not health:
        return False
    if "ready" not in health:
        return True
    return bool(health["ready"])


def stage_text(health: Optional[dict[str, Any]]) -> str:
    """起動待機表示に出す一言 (基盤仕様書 2.2)。"""
    if not health:
        return ""
    for key in ("stage", "message", "status"):
        value = health.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def wait_ready(url: str, app_id: str, *, timeout: float,
               interval: float = 0.5,
               on_progress: Optional[Callable[[float, Optional[dict]], None]] = None,
               should_stop: Optional[Callable[[], bool]] = None,
               ) -> Optional[dict[str, Any]]:
    """目的のツールが応答するまで待つ。時間切れなら `None`。

    `on_progress` には経過秒と直近の応答を渡す。画面が「起動しています…」
    のまま無反応に見えないようにするため (基盤仕様書 2.2)。
    `should_stop` が真を返したら、時間切れを待たずにあきらめる ──
    利用者が切替をやり直したときに、前の待機が居座らないようにする。
    """
    started = time.monotonic()
    while True:
        if should_stop is not None and should_stop():
            log.info("起動待ちを中止しました: %s", url)
            return None

        health = probe(url)
        elapsed = time.monotonic() - started
        if on_progress is not None:
            on_progress(elapsed, health)

        if is_tool(health, app_id) and is_ready(health):
            log.info("起動を確認しました: %s (%.1f秒)", app_id, elapsed)
            return health

        if elapsed >= timeout:
            log.warning("起動を確認できませんでした: %s (%.0f秒)", app_id, timeout)
            return None
        time.sleep(interval)


def wait_gone(url: str, app_id: str, *, timeout: float,
              interval: float = 0.3) -> bool:
    """目的のツールが応答しなくなるまで待つ。

    停止したことの確認に使う (要件定義書 §8.1「日報の終了を確認」)。
    **別のアプリが同じポートで応答し始めた場合も「終了した」とみなす。**
    見ているのは `app_id` の一致であって、ポートの空きではない。
    """
    started = time.monotonic()
    while True:
        if not is_tool(probe(url), app_id):
            return True
        if time.monotonic() - started >= timeout:
            return False
        time.sleep(interval)


def is_port_accepting(port: int, *, timeout: float = 0.5) -> bool:
    """TCPで繋がるか。HTTPまでは見ない。

    「誰も待ち受けていない」のか「待ち受けてはいるが応答が返らない」
    のかを分けるために使う。後者はプロキシやセキュリティ製品が
    挟まっていることが多く、直し方がまったく違う。
    """
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def proxy_settings() -> dict[str, str]:
    """いま効いているプロキシ設定。原因調査のためだけに使う。"""
    try:
        return {k: v for k, v in urllib.request.getproxies().items()
                if k in ("http", "https")}
    except Exception:                    # noqa: BLE001 - 調査用なので握る
        return {}
