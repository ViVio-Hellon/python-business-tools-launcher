"""偽ツールの `stop.bat` の中身。

本物の `stop.bat` と同じことをする ── `POST /api/shutdown` で
正常終了を頼み、駄目なら 1 で終わる。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def main(argv: list[str]) -> int:
    port = int(argv[0])
    force = "--force" in argv[1:]
    url = f"http://127.0.0.1:{port}/api/shutdown"
    body = json.dumps({"force": force}).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=5) as res:
            payload = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"停止できません (HTTP {exc.code})")
        return 1
    except (urllib.error.URLError, OSError):
        print("動いていませんでした")
        return 0
    print(payload.get("message", ""))
    return 0 if payload.get("stopped") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
