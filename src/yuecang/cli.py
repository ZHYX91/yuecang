from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path
import threading
import webbrowser

from yuecang.storage.project import DomainError, Project


def default_project():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YueCang" / "Library"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "yuecang" / "library"


def main(argv=None):
    parser = argparse.ArgumentParser(description="阅藏：本地图书整理工作台")
    parser.add_argument("--project", type=Path, default=default_project(), help="独立书库目录")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="启动本地管理界面")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true")
    sub.add_parser("init", help="创建空书库")
    sub.add_parser("verify", help="验证数据库、对象和来源")
    sub.add_parser("backup", help="生成独立备份")
    restore = sub.add_parser("restore", help="恢复到新的空目录")
    restore.add_argument("snapshot", type=Path)
    sub.add_parser("cache", help="清理可重建临时对象")
    args = parser.parse_args(argv)
    try:
        if args.command in {None, "serve"}:
            import uvicorn
            from yuecang.api import create_app
            port = getattr(args, "port", 8765)
            app = create_app(args.project)
            if not getattr(args, "no_browser", False):
                threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
            uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
            return 0
        from yuecang.storage import maintenance
        if args.command == "restore":
            result = maintenance.restore_project(args.snapshot, args.project)
        else:
            project = Project(args.project)
            result = {"project": str(project.root)} if args.command == "init" else {
                "verify": maintenance.verify_project,
                "backup": maintenance.backup_project,
                "cache": lambda p: maintenance.cleanup_cache(p, apply=True),
            }[args.command](project)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DomainError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
