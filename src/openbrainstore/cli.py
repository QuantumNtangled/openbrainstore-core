"""obs — CLI for the local OKF memory service.

    obs serve                 run the MCP server (stdio)
    obs remember "..." --type fact [--entities a,b] [--tags x,y] [--kv k=v ...]
    obs recall "query" [--type t] [--entities a,b] [--deep]
    obs schema | stats | export | reindex | embed | purge
    obs forget mem_...

Backend selection: OBS_BACKEND=sqlite (default) or postgres (OBS_PG_DSN).
"""

import argparse
import json
import sys

from . import config, embeddings, service, store
from .backends import get_backend
from .recall import recall as run_recall


def _csv(s: str | None) -> list[str] | None:
    return [x.strip() for x in s.split(",") if x.strip()] if s else None


def _kv_pairs(pairs: list[str] | None) -> dict | None:
    if not pairs:
        return None
    out = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--kv expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="obs", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    p.add_argument("--http", action="store_true",
                   help="serve MCP over streamable HTTP instead of stdio (no auth yet — keep it on loopback)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)

    p = sub.add_parser("remember", help="store a memory")
    p.add_argument("content")
    p.add_argument("--type", required=True, choices=sorted(config.MEMORY_TYPES))
    p.add_argument("--entities")
    p.add_argument("--tags")
    p.add_argument("--kv", action="append")
    p.add_argument("--links", help="comma-separated ids of related memories")

    p = sub.add_parser("link", help="link an existing memory to related memories")
    p.add_argument("id")
    p.add_argument("to", help="comma-separated memory ids to link to")

    p = sub.add_parser("recall", help="retrieve memories")
    p.add_argument("query", nargs="?")
    p.add_argument("--type", choices=sorted(config.MEMORY_TYPES))
    p.add_argument("--entities")
    p.add_argument("--tags")
    p.add_argument("--kv", action="append")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--deep", action="store_true")
    p.add_argument("--limit", type=int, default=config.DEFAULT_RECALL_LIMIT)

    p = sub.add_parser("forget", help="tombstone a memory")
    p.add_argument("id")

    sub.add_parser("schema", help="show the live memory vocabulary")
    sub.add_parser("stats", help="corpus + recall instrumentation summary")
    p = sub.add_parser("export", help="tar.gz of memories (OKF bundle by default)")
    p.add_argument("--raw", action="store_true", help="full-fidelity internal dump instead of the OKF bundle")
    sub.add_parser("reindex", help="rebuild all projections from blobs")
    sub.add_parser("embed", help="backfill embeddings (requires [vector] extra)")
    sub.add_parser("purge", help="purge expired tombstones")

    args = parser.parse_args(argv)

    if args.cmd == "serve":
        from .server import main as serve_main
        serve_main(http=args.http, host=args.host, port=args.port)
        return

    if args.cmd == "export":
        _print(service.export(profile="raw" if args.raw else "okf"))
        return

    if args.cmd == "purge":
        _print({"purged": store.purge_tombstones(config.user_id())})
        return

    with get_backend() as backend:
        if args.cmd == "remember":
            _print(service.remember(
                backend, args.content, args.type,
                entities=_csv(args.entities), tags=_csv(args.tags),
                kv=_kv_pairs(args.kv), links=_csv(args.links),
                source_harness="cli",
            ))
        elif args.cmd == "link":
            _print(service.link(backend, args.id, _csv(args.to) or []))
        elif args.cmd == "recall":
            filters = {k: v for k, v in {
                "type": args.type, "tags": _csv(args.tags), "kv": _kv_pairs(args.kv),
                "since": args.since, "until": args.until,
            }.items() if v}
            _print(run_recall(
                backend, config.user_id(), query=args.query, filters=filters,
                entities=_csv(args.entities), depth=args.depth,
                deep=args.deep, limit=args.limit,
            ))
        elif args.cmd == "forget":
            _print(service.forget(backend, args.id))
        elif args.cmd == "schema":
            _print(service.get_memory_schema(backend))
        elif args.cmd == "stats":
            _print(service.stats(backend))
        elif args.cmd == "reindex":
            _print(service.reindex(backend))
        elif args.cmd == "embed":
            try:
                model_path = embeddings.download_model()  # no-op if already cached
            except RuntimeError as e:
                print(str(e), file=sys.stderr)
                raise SystemExit(1)
            _print({
                "model_dir": model_path,
                "embedded": embeddings.ensure_embedded(backend, config.user_id()),
            })


if __name__ == "__main__":
    main()
