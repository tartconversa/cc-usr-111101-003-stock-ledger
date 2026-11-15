"""命令行入口：构建事实层快照、按书目/地点查询库存账。

    python -m src.cli build -i f1.csv -i f2.csv -o .workspace-output
    python -m src.cli query .workspace-output --book b1 --location B
    python -m src.cli query .workspace-output --in-transit
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .ledger import build_ledger
from .snapshot import LATEST_LINK, publish_build


def _resolve_build_dir(output_root: Path) -> Path:
    latest = output_root / LATEST_LINK
    if not latest.exists():
        raise SystemExit(f"未找到已发布快照：{latest} 不存在，请先运行 build")
    build_dir = latest.resolve()
    if not (build_dir / "_SUCCESS").exists():
        raise SystemExit(f"快照缺少 _SUCCESS 标记，拒绝读取不完整结果：{build_dir}")
    return build_dir


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def cmd_build(args: argparse.Namespace) -> int:
    inputs = [Path(p) for p in args.inputs]
    for path in inputs:
        if not path.exists():
            raise SystemExit(f"输入不存在：{path}")
    build = build_ledger(inputs)
    result = publish_build(inputs, build, args.output)

    summary = {
        "build_id": result.build_id,
        "build_dir": str(result.build_dir),
        "accepted": result.manifest["accepted_count"],
        "quarantined": result.manifest["quarantined_count"],
        "pending": result.manifest["pending_count"],
        "duplicates": result.manifest["duplicate_count"],
        "affected_cells": len(result.affected),
        "conservation": result.manifest["conservation"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if build.quarantined:
        print("\n隔离流水（合法部分已照常成账）：", file=sys.stderr)
        for q in build.quarantined:
            print(f"  - {q.event_id}: {q.reason} ({q.detail})", file=sys.stderr)
    if build.pending:
        print("\n挂起流水（依赖到达后自动续算）：", file=sys.stderr)
        for p in build.pending:
            print(f"  - {p.event_id}: {p.reason} 等待 {p.waiting_for}", file=sys.stderr)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    build_dir = _resolve_build_dir(Path(args.output_root))

    if args.in_transit:
        rows = _read_csv(build_dir / "daily_transit.csv")
        if args.transfer_id:
            rows = [r for r in rows if r["transfer_id"] == args.transfer_id]
        if args.book:
            rows = [r for r in rows if r["book_key"] == args.book]
    else:
        rows = _read_csv(build_dir / "daily_location.csv")
        if args.book:
            rows = [r for r in rows if r["book_key"] == args.book]
        if args.location:
            rows = [r for r in rows if r["location"] == args.location]
    if args.day:
        rows = [r for r in rows if r["effective_day"] == args.day]

    if args.format == "json":
        print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        if not rows:
            return 0
        writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return 0


def cmd_affected(args: argparse.Namespace) -> int:
    build_dir = _resolve_build_dir(Path(args.output_root))
    payload = json.loads(
        (build_dir / "affected_vs_previous.json").read_text(encoding="utf-8")
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="图书流转事实层构建与查询")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="读取追加式 CSV，构建并原子发布日快照")
    p_build.add_argument(
        "-i", "--input", dest="inputs", action="append", required=True,
        help="输入 CSV，可重复传入以支持分段导入",
    )
    p_build.add_argument("-o", "--output", required=True, help="快照输出根目录")
    p_build.set_defaults(func=cmd_build)

    p_query = sub.add_parser("query", help="按书目/地点/日期查询已发布库存账")
    p_query.add_argument("output_root", help="build 使用的输出根目录")
    p_query.add_argument("--book")
    p_query.add_argument("--location")
    p_query.add_argument("--day")
    p_query.add_argument("--in-transit", action="store_true", help="查询在途账")
    p_query.add_argument("--transfer-id")
    p_query.add_argument("--format", choices=("csv", "json"), default="csv")
    p_query.set_defaults(func=cmd_query)

    p_affected = sub.add_parser(
        "affected", help="列出本次构建相对上一版发生变化的旧日结单元"
    )
    p_affected.add_argument("output_root")
    p_affected.set_defaults(func=cmd_affected)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
