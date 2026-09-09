"""偽ツールを `start.bat` / `stop.bat` の形で用意する手伝い。

ランチャーは**BATしか受け付けない** (要件定義書 §13.2)。Windows以外で
試験するために、`.bat` という名前のシェルスクリプトを置く。中身は
`_fake_tool.py` を起こすだけなので、確かめたいこと (BATを呼ぶ →
起動確認 → 停止) はそのまま通せる。
"""
from __future__ import annotations

import os
import sys
import socket
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def free_port() -> int:
    """いま空いているポートを1つ借りる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_tool_dir(base: Path, *, app_id: str, port: int,
                  display_name: str = "偽ツール",
                  ready_after: float = 0.0, busy: bool = False,
                  exit_code: int | None = None,
                  with_stop_bat: bool = True,
                  version: str = "1.0.0",
                  installed_version: str | None = None) -> Path:
    """1つの業務ツールらしいフォルダを作る。

    `exit_code` を渡すと、**起動せずにその戻り値で終わる** BATになる。
    起動に失敗したときの見え方を確かめるために使う。

    `installed_version` を `version` と違う値にすると、「置いてある版と
    動いている版が食い違う」状態を作れる ── 入れ替えたのに古いプロセスが
    残っている場面。
    """
    root = base / app_id
    (root / "config").mkdir(parents=True, exist_ok=True)

    start = root / "start.bat"
    if exit_code is not None:
        start.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    else:
        args = [sys.executable, str(TESTS_DIR / "_fake_tool.py"),
                "--port", str(port), "--app-id", app_id,
                "--display-name", display_name,
                "--app-root", str(root),
                "--version", version,
                "--ready-after", str(ready_after)]
        if busy:
            args.append("--busy")
        start.write_text(
            "#!/bin/sh\n" + " ".join(_quote(a) for a in args) + ' "$@"\n',
            encoding="utf-8")
    os.chmod(start, 0o755)

    if with_stop_bat:
        stop = root / "stop.bat"
        stop.write_text(
            "#!/bin/sh\n"
            + " ".join(_quote(a) for a in [
                sys.executable, str(TESTS_DIR / "_stop_tool.py"), str(port)])
            + ' "$@"\n',
            encoding="utf-8")
        os.chmod(stop, 0o755)

    # 本物と同じ場所に `config/app.json` を置く。
    # `process_manager.find_shutdown_token` がここを読む
    (root / "config" / "app.json").write_text(
        f'{{"app_id": "{app_id}", "display_name": "{display_name}",'
        f' "version": "{installed_version or version}",'
        f' "local_dir_name": "Fake_{app_id}",'
        f' "server": {{"port": {port}}}}}',
        encoding="utf-8")
    return root


def _quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"
