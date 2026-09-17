"""Local database and browser entry points."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from job_search_assistant.app_services import build_foundation
from job_search_assistant.core.errors import ApplicationError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local command-line entry point and return a process exit code."""
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="job-search-assistant")
    parser.add_argument(
        "--database",
        type=Path,
        default=project_root / ".job-search-assistant" / "job-search-assistant.sqlite",
        help="Path for the local SQLite database.",
    )
    parser.add_argument(
        "--migrations",
        type=Path,
        default=project_root / "migrations",
        help="Directory that contains ordered SQL migrations.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Apply all foundation migrations.")
    subparsers.add_parser("db-info", help="Apply migrations and print foundation database counts.")
    serve = subparsers.add_parser("serve", help="Start the local browser UI.")
    serve.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if args.command == "serve" and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")

    try:
        if args.command == "serve":
            return _serve(args)
        services = build_foundation(database_path=args.database, migrations_path=args.migrations)
        if args.command == "init-db":
            _print_json({"status": "ready", "database": str(services.database.database_path)})
            return 0
        if args.command == "db-info":
            _print_json(
                {
                    "database": str(services.database.database_path),
                    "migrations": _count(services, "schema_migrations"),
                    "idempotency_records": _count(services, "idempotency_records"),
                    "audit_events": _count(services, "audit_events"),
                }
            )
            return 0
    except ApplicationError as exc:
        _print_json({"status": "error", "error": exc.to_dict()}, stream="stderr")
        return 1
    return 2


def _serve(args) -> int:
    from job_search_assistant.adapters.web import LocalHTTPServer
    from job_search_assistant.app_services.local_ui import build_local_ui

    service = build_local_ui(database_path=args.database, migrations_path=args.migrations)
    try:
        server = LocalHTTPServer(
            (args.host, args.port), service,
            incoming_dir=args.database.resolve().parent / "incoming",
        )
    except OSError:
        _print_json({"status": "error", "message":
                     f"Cannot bind 127.0.0.1:{args.port}; port may be in use. Try --port 8001."},
                    stream="stderr")
        return 1
    with server:
        print(f"http://127.0.0.1:{server.server_port}/ — Stop with Ctrl-C", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def _count(services, table_name: str) -> int:
    row = services.database.fetch_all(f"SELECT COUNT(*) AS count FROM {table_name}")[0]
    return int(row["count"])


def _print_json(payload: dict, *, stream: str = "stdout") -> None:
    import sys

    output = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    print(output, file=sys.stderr if stream == "stderr" else sys.stdout)


if __name__ == "__main__":
    raise SystemExit(main())
