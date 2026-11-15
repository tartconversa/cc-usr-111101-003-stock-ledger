"""把接受的事实流水重放为按书目/地点查询的日快照，并原子发布。

与 :mod:`src.ledger` 的职责分界：ledger 决定哪些事件合法（接受/隔离/挂起），
本模块只对“已接受集合”做纯函数式重放，产出每个有效日的日结。快照只追加、
不覆盖：每次构建写入带内容指纹的新目录，再以原子替换切换 latest 指针，
读者要么看到完整的一版，要么看不到，绝不会读到半套结果。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .events import COLUMNS
from .ledger import LedgerBuild

SUCCESS_MARKER = "_SUCCESS"
LATEST_LINK = "latest"


@dataclass(frozen=True, slots=True)
class BuildResult:
    build_id: str
    build_dir: Path
    affected: tuple[dict[str, object], ...]
    manifest: dict[str, object]


# ---------------------------------------------------------------------------
# 内容指纹：同一流水集合（与输入顺序、分段方式无关）得到同一 build_id
# ---------------------------------------------------------------------------

def compute_build_id(build: LedgerBuild, raw_rows: tuple[tuple[str, ...], ...]) -> str:
    digest = hashlib.sha256()
    for canonical in sorted(raw_rows):
        digest.update(b"\x1f".join(part.encode("utf-8") for part in canonical))
        digest.update(b"\x1e")
    return digest.hexdigest()[:16]


def input_set_fingerprint(raw_rows: tuple[tuple[str, ...], ...]) -> str:
    """与 compute_build_id 同口径的完整输入集 SHA-256（用于 manifest 审计）。"""
    digest = hashlib.sha256()
    for canonical in sorted(raw_rows):
        digest.update(b"\x1f".join(part.encode("utf-8") for part in canonical))
        digest.update(b"\x1e")
    return digest.hexdigest()


def canonical_raw_rows(inputs: list[str | Path]) -> tuple[tuple[str, ...], ...]:
    """所有输入行的规范化内容（含被隔离/挂起的行），用于指纹与输入审计。"""
    from .events import iter_rows, parse_row
    from .ledger import ParseError

    rows: list[tuple[str, ...]] = []
    for path in inputs:
        for row, _source, _line in iter_rows(path):
            try:
                movement = parse_row(row, _source, _line)
            except ParseError:
                # 解析失败的行也参与指纹，但以排序后的原始列形式进入。
                rows.append(
                    tuple((row.get(name) or "").strip() for name in COLUMNS)
                )
            else:
                rows.append(movement.canonical_tuple())
    return tuple(rows)


# ---------------------------------------------------------------------------
# 日快照重放
# ---------------------------------------------------------------------------

def _replay(build: LedgerBuild):
    """重放 accepted，返回日增量与每日账户余额。

    daily_delta[(day, book, loc)] = 当日 onhand/sold/receipt 的净变动；
    调拨按 transfer_id 单独记账，见 transit_delta。
    """
    onhand_delta: dict[tuple[date, str, str], dict[str, int]] = defaultdict(
        lambda: {"receipt": 0, "sale": 0, "return_net": 0, "transfer_in": 0,
                 "transfer_out": 0, "reversal": 0}
    )
    transit_delta: dict[tuple[date, str], dict[str, int]] = defaultdict(
        lambda: {"out": 0, "in": 0, "reversal": 0}
    )
    onhand_balance: dict[tuple[str, str], int] = defaultdict(int)
    transit_balance: dict[str, int] = defaultdict(int)
    sold_balance: dict[tuple[str, str], int] = defaultdict(int)
    transfer_book: dict[str, str] = {}

    days: set[date] = set()
    locations: set[tuple[str, str]] = set()

    def add(day: date, book: str, loc: str, field: str, amount: int) -> None:
        onhand_delta[(day, book, loc)][field] += amount

    for m in build.accepted:
        d, book, loc, q = m.effective_day, m.book_key, m.location, m.quantity
        days.add(d)
        locations.add((book, loc))

        if m.kind == "RECEIPT":
            onhand_balance[(book, loc)] += q
            add(d, book, loc, "receipt", q)
        elif m.kind == "SALE":
            onhand_balance[(book, loc)] -= q
            sold_balance[(book, loc)] += q
            add(d, book, loc, "sale", q)
        elif m.kind == "RETURN":
            onhand_balance[(book, loc)] += q
            sold_balance[(book, loc)] -= q
            add(d, book, loc, "return_net", -q)  # 净售出视角为负
        elif m.kind == "TRANSFER_OUT":
            onhand_balance[(book, loc)] -= q
            transit_balance[m.transfer_id] += q
            transfer_book[m.transfer_id] = book
            add(d, book, loc, "transfer_out", q)
            transit_delta[(d, m.transfer_id)]["out"] += q
        elif m.kind == "TRANSFER_IN":
            onhand_balance[(book, loc)] += q
            transit_balance[m.transfer_id] -= q
            transfer_book[m.transfer_id] = book
            add(d, book, loc, "transfer_in", q)
            transit_delta[(d, m.transfer_id)]["in"] += q
        else:  # REVERSAL：在原事件归属日做反向登记
            target = build.by_id[m.reversal_of]
            td, tb, tl, tq = (
                target.effective_day,
                target.book_key,
                target.location,
                target.quantity,
            )
            days.add(td)
            locations.add((tb, tl))
            add(td, tb, tl, "reversal", -tq if target.kind in {"RECEIPT", "RETURN", "TRANSFER_IN"} else tq)
            if target.kind in {"TRANSFER_OUT", "TRANSFER_IN"}:
                transit_delta[(td, target.transfer_id)]["reversal"] += (
                    -tq if target.kind == "TRANSFER_OUT" else tq
                )
            if target.kind == "SALE":
                sold_balance[(tb, tl)] -= tq
            elif target.kind == "RETURN":
                sold_balance[(tb, tl)] += tq

    return (
        onhand_delta,
        transit_delta,
        days,
        locations,
        transfer_book,
    )


def build_snapshot_rows(build: LedgerBuild):
    """生成三张快照表的行（字典列表），行序完全确定。"""
    (
        onhand_delta,
        transit_delta,
        days,
        locations,
        transfer_book,
    ) = _replay(build)
    if not days:
        return [], [], []
    day_grid = sorted(days)

    # --- 地点日快照：从每个 (书目, 地点) 首个活动日前向填充到全局最后业务日。
    # 这样即使某地点在后续日期没有新事件（如 A 调出后再无动作），仍能在任意
    # 晚些日子查到它的日结余额，而不是只在它自己的最后活动日有行。 ---
    global_last = day_grid[-1]
    location_rows: list[dict[str, object]] = []
    for book, loc in sorted(locations):
        active_days = sorted(d for (d, b, l) in onhand_delta if b == book and l == loc)
        first = active_days[0]
        running_onhand = 0
        running_sold = 0
        for d in day_grid:
            if d < first or d > global_last:
                continue
            delta = onhand_delta.get((d, book, loc))
            if delta is not None:
                running_onhand += (
                    delta["receipt"] - delta["sale"] - delta["return_net"]
                    + delta["transfer_in"] - delta["transfer_out"]
                    + delta["reversal"]
                )
                running_sold += delta["sale"] + delta["return_net"]
            location_rows.append(
                {
                    "book_key": book,
                    "location": loc,
                    "effective_day": d.isoformat(),
                    "onhand_eod": running_onhand,
                    "sold_net_eod": running_sold,
                    "receipt_day": delta["receipt"] if delta else 0,
                    "sale_day": delta["sale"] if delta else 0,
                    "return_day_net": -delta["return_net"] if delta else 0,
                    "transfer_in_day": delta["transfer_in"] if delta else 0,
                    "transfer_out_day": delta["transfer_out"] if delta else 0,
                    "reversal_day_net": delta["reversal"] if delta else 0,
                }
            )

    # --- 在途日快照：每个 transfer_id 从发出日前向填充到全局最后业务日，
    # 结平后余额为 0 也持续出 row，保证任意晚些日子都能查到该在途单状态。 ---
    transit_rows: list[dict[str, object]] = []
    tids = sorted({m.transfer_id for m in build.accepted if m.transfer_id})
    for tid in tids:
        active = sorted(d for (d, t) in transit_delta if t == tid)
        first = active[0]
        running = 0
        for d in day_grid:
            if d < first or d > global_last:
                continue
            delta = transit_delta.get((d, tid))
            if delta is not None:
                # 累计到当日的 out/in/reversal（reversal 已在 _replay 带符号）。
                running = sum(
                    v["out"] - v["in"] + v["reversal"]
                    for (dd, t), v in transit_delta.items()
                    if t == tid and dd <= d
                )
            transit_rows.append(
                {
                    "transfer_id": tid,
                    "book_key": transfer_book.get(tid, ""),
                    "effective_day": d.isoformat(),
                    "in_transit_eod": running,
                    "out_day": delta["out"] if delta else 0,
                    "in_day": delta["in"] if delta else 0,
                    "reversal_day_net": delta["reversal"] if delta else 0,
                }
            )

    # --- 书目日汇总：在库合计 + 在途合计 + 净售出（守恒视图） ---
    book_rows: list[dict[str, object]] = []
    books = sorted({b for b, _ in locations})
    for book in books:
        onhand_by_day: dict[str, int] = defaultdict(int)
        sold_by_day: dict[str, int] = defaultdict(int)
        for row in location_rows:
            if row["book_key"] == book:
                onhand_by_day[row["effective_day"]] += row["onhand_eod"]
                sold_by_day[row["effective_day"]] += row["sold_net_eod"]
        transit_by_day: dict[str, int] = defaultdict(int)
        for row in transit_rows:
            if row["book_key"] == book:
                transit_by_day[row["effective_day"]] += row["in_transit_eod"]
        for d in day_grid:
            ds = d.isoformat()
            if ds not in onhand_by_day and ds not in transit_by_day:
                continue
            book_rows.append(
                {
                    "book_key": book,
                    "effective_day": ds,
                    "onhand_total_eod": onhand_by_day[ds],
                    "in_transit_eod": transit_by_day[ds],
                    "sold_net_eod": sold_by_day[ds],
                    "conservation_eod": (
                        onhand_by_day[ds] + transit_by_day[ds] + sold_by_day[ds]
                    ),
                }
            )
    return location_rows, transit_rows, book_rows


# ---------------------------------------------------------------------------
# 落盘与原子发布
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        if not rows:
            stream.write("")
            return
        writer = csv.DictWriter(stream, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _load_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def diff_snapshots(old_rows: list[dict[str, str]], new_rows: list[dict[str, str]],
                   key_fields: tuple[str, ...]) -> list[dict[str, object]]:
    """以业务主键对齐两版快照，逐字段列出变化（迟到流水影响旧日结的依据）。"""
    def key(row):
        return tuple(row[k] for k in key_fields)

    old_map = {key(r): r for r in old_rows}
    new_map = {key(r): r for r in new_rows}
    affected: list[dict[str, object]] = []
    for k in sorted(set(old_map) | set(new_map)):
        old, new = old_map.get(k), new_map.get(k)
        if old is None:
            affected.append({"key": dict(zip(key_fields, k, strict=True)),
                             "change": "added",
                             "fields": {kk: str(vv) for kk, vv in new.items()}})
            continue
        if new is None:
            affected.append({"key": dict(zip(key_fields, k, strict=True)),
                             "change": "removed", "fields": dict(old)})
            continue
        changed = {
            field: {"old": old[field], "new": str(new[field])}
            for field in new
            if old.get(field) != str(new[field])
        }
        if changed:
            affected.append({"key": dict(zip(key_fields, k, strict=True)),
                             "change": "changed", "fields": changed})
    return affected


def publish_build(
    inputs: list[str | Path],
    build: LedgerBuild,
    output_root: str | Path,
) -> BuildResult:
    """构建快照并原子发布；返回构建结果与相对上一版的受影响清单。"""
    output_root = Path(output_root)
    raw_rows = canonical_raw_rows(inputs)
    build_id = compute_build_id(build, raw_rows)
    location_rows, transit_rows, book_rows = build_snapshot_rows(build)

    output_root.mkdir(parents=True, exist_ok=True)

    # 幂等短路：同一输出根下已存在带 _SUCCESS 的同指纹构建，直接复用。
    # 必须同时校验目录存在且完整，避免跨根目录时误判为空目录。
    existing = output_root / build_id
    if existing.is_dir() and (existing / SUCCESS_MARKER).exists():
        return BuildResult(
            build_id=build_id,
            build_dir=existing,
            affected=tuple(
                json.loads(
                    (existing / "affected_vs_previous.json").read_text(
                        encoding="utf-8"
                    )
                )
            ),
            manifest=json.loads(
                (existing / "manifest.json").read_text(encoding="utf-8")
            ),
        )

    staging = output_root / f".staging-{build_id}-{os.getpid()}"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        _write_csv(staging / "accepted_facts.csv", [
            dict(zip(COLUMNS, m.canonical_tuple(), strict=True))
            for m in build.accepted
        ])
        _write_csv(staging / "daily_location.csv", location_rows)
        _write_csv(staging / "daily_transit.csv", transit_rows)
        _write_csv(staging / "daily_book.csv", book_rows)
        _write_csv(staging / "quarantine.csv", [
            {
                "event_id": q.event_id,
                "reason": q.reason,
                "source_file": q.source_file,
                "line_no": q.line_no,
                "detail": q.detail,
                "raw": q.raw,
            }
            for q in build.quarantined
        ])
        _write_csv(staging / "pending.csv", [
            {
                "event_id": p.event_id,
                "reason": p.reason,
                "waiting_for": p.waiting_for,
                "source_file": p.source_file,
                "line_no": p.line_no,
            }
            for p in build.pending
        ])
        _write_csv(staging / "duplicates.csv", [
            {"event_id": d.event_id, "source_file": d.source_file, "line_no": d.line_no}
            for d in build.duplicates
        ])

        # 与已发布的上一版对比，列出受影响的地点/在途单与日期。
        latest = output_root / LATEST_LINK
        previous = latest.resolve() if latest.exists() else None
        affected: list[dict[str, object]] = []
        if previous is not None and previous != staging:
            affected.extend(
                {"table": "daily_location", **item}
                for item in diff_snapshots(
                    _load_csv(previous / "daily_location.csv"),
                    [dict(r) for r in location_rows],
                    ("book_key", "location", "effective_day"),
                )
            )
            affected.extend(
                {"table": "daily_transit", **item}
                for item in diff_snapshots(
                    _load_csv(previous / "daily_transit.csv"),
                    [dict(r) for r in transit_rows],
                    ("transfer_id", "effective_day"),
                )
            )
            affected.extend(
                {"table": "daily_book", **item}
                for item in diff_snapshots(
                    _load_csv(previous / "daily_book.csv"),
                    [dict(r) for r in book_rows],
                    ("book_key", "effective_day"),
                )
            )
        (staging / "affected_vs_previous.json").write_text(
            json.dumps(affected, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        manifest = {
            "build_id": build_id,
            "input_files": [str(Path(p)) for p in inputs],
            "input_row_count": len(raw_rows),
            "input_event_ids": sorted(build.input_event_ids),
            "accepted_event_ids": list(build.accepted_ids),
            "accepted_count": len(build.accepted),
            "quarantined_count": len(build.quarantined),
            "pending_count": len(build.pending),
            "duplicate_count": len(build.duplicates),
            "quarantine_reasons": sorted({q.reason for q in build.quarantined}),
            "conservation": build.conservation,
            "fact_set_sha256": hashlib.sha256(
                "\n".join(build.accepted_ids).encode("utf-8")
            ).hexdigest(),
            "input_set_sha256": input_set_fingerprint(raw_rows),
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # 发布边界：先刷盘并写 _SUCCESS，再原子 rename 目录与 latest 指针。
        for entry in staging.iterdir():
            with entry.open("rb") as stream:
                os.fsync(stream.fileno())
        (staging / SUCCESS_MARKER).write_text(build_id, encoding="utf-8")
        os.fsync(os.open(staging, os.O_RDONLY))

        final_dir = output_root / build_id
        os.rename(staging, final_dir)

        link_tmp = output_root / f".latest-tmp-{os.getpid()}"
        if link_tmp.exists() or link_tmp.is_symlink():
            link_tmp.unlink()
        os.symlink(final_dir.name, link_tmp)
        os.rename(link_tmp, latest)  # 同文件系统原子替换
        return BuildResult(
            build_id=build_id,
            build_dir=final_dir,
            affected=tuple(affected),
            manifest=manifest,
        )
    except BaseException:
        # 任何失败都清理暂存区，latest 保持指向上一个完整版本。
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise
