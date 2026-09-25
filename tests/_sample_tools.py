"""試験で使う「製品の既定値に書かれたツール」。

出荷する `config/launcher.json` のツール一覧は**空**にしてある (現場ごとに
使うツールが違い、［＋ ツールを追加］で起動ファイルを選べばアプリIDも
ポートも読み取れるため)。それでも「既定値にツールを書いておけば各端末へ
入る」仕組みは残してあるので、その試験にはこの一覧を使う。
(以前の版で出荷していた4ツールと同じ中身)
"""
SAMPLE_TOOLS = [
    {
        "app_id": "nlm.nippou-tool",
        "display_name": "日報",
        "order_no": 10,
        "repository": "ViVio-Hellon/vba-daily-report-python-migration",
        "port": 8733,
        "health_path": "/api/health",
        "start_args": "--no-browser"
    },
    {
        "app_id": "nlm.line-calendar",
        "display_name": "カレンダー",
        "order_no": 20,
        "repository": "ViVio-Hellon/vba-calendar-python-migration",
        "port": 8730,
        "health_path": "/api/health",
        "start_args": "--no-browser"
    },
    {
        "app_id": "nlm.kanban-system",
        "display_name": "看板",
        "order_no": 30,
        "repository": "ViVio-Hellon/vba-production-board-python-migration",
        "port": 8741,
        "health_path": "/api/health",
        "start_args": "--no-browser"
    },
    {
        "app_id": "nlm.packaging-tool",
        "display_name": "総合ツール",
        "order_no": 40,
        "repository": "ViVio-Hellon/python-web-tools",
        "port": 8713,
        "health_path": "/api/health",
        "start_args": "--no-browser"
    }
]
