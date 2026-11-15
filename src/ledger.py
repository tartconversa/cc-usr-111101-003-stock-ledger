"""流转流水的分类引擎：接受、隔离、挂起。

设计要点（对应需求中的账目规则）：

* 事件不可变，分类结果只由“本次输入的完整流水集合”决定，与读取顺序、
  分段次数、进程是否重启无关，保证一次导入与多次补齐得到同一账面。
* 依赖未到达（TRANSFER_IN 的发出单、REVERSAL 的原事件）先挂起，不参与
  本次快照；依赖到达后下一次构建自动继续。
* 超收、循环冲回、内容冲突、负库存等非法流水独立隔离并给出原因码，
  合法流水仍然形成自洽快照。
* 回放在依赖图上以 (effective_day, event_id) 为次序做确定性 Kahn 拓扑排序；
  effective_day 决定归属哪一天的日结，recorded_at 只用于审计迟到。
"""

from __future__ import annotations

import heapq
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .events import (
    REVERSIBLE_KINDS,
    Movement,
    ParseError,
    iter_rows,
    parse_row,
)

# ---- 隔离原因码（稳定字符串，直接落到 quarantine.csv） ----
R_PARSE = "unparseable_row"
R_CONFLICT = "event_id_conflict"
R_BAD_REVERSAL_TARGET = "bad_reversal_target"
R_BAD_REVERSAL_DAY = "bad_reversal_effective_day"
R_BAD_REVERSAL_MATCH = "bad_reversal_mismatch"
R_DUP_REVERSAL = "duplicate_reversal"
R_TRANSFER_REUSE = "transfer_id_reuse"
R_BAD_TRANSFER_PAIR = "bad_transfer_pair"
R_RECEIPT_BEFORE_DISPATCH = "receipt_before_dispatch"
R_CYCLE = "reversal_cycle"
R_DEPENDENCY_QUARANTINED = "dependency_quarantined"
R_OVER_RECEIPT = "over_receipt"
R_INSUFFICIENT_BALANCE = "insufficient_balance"

PENDING_TRANSFER = "missing_transfer_out"
PENDING_REVERSAL = "missing_reversal_target"


@dataclass(frozen=True, slots=True)
class Quarantine:
    event_id: str
    reason: str
    source_file: str
    line_no: int
    detail: str
    raw: str


@dataclass(frozen=True, slots=True)
class Pending:
    event_id: str
    reason: str
    waiting_for: str
    source_file: str
    line_no: int


@dataclass(frozen=True, slots=True)
class Duplicate:
    event_id: str
    source_file: str
    line_no: int


@dataclass(frozen=True, slots=True)
class LedgerBuild:
    accepted: tuple[Movement, ...]
    quarantined: tuple[Quarantine, ...]
    pending: tuple[Pending, ...]
    duplicates: tuple[Duplicate, ...]
    by_id: dict[str, Movement]
    input_event_ids: frozenset[str]
    conservation: dict[str, int]

    @property
    def accepted_ids(self) -> tuple[str, ...]:
        return tuple(m.event_id for m in self.accepted)


# ---------------------------------------------------------------------------
# 阶段 0：读取 + 解析 + event_id 幂等去重 / 冲突
# ---------------------------------------------------------------------------

def _load_candidates(inputs: list[Path]):
    candidates: dict[str, Movement] = {}
    quarantined: list[Quarantine] = []
    duplicates: list[Duplicate] = []
    raw_event_ids: set[str] = set()

    for path in inputs:
        for row, source_file, line_no in iter_rows(path):
            try:
                movement = parse_row(row, source_file, line_no)
            except ParseError as exc:
                quarantined.append(
                    Quarantine(
                        event_id=(row.get("event_id") or "").strip()
                        or f"{source_file}:{line_no}",
                        reason=R_PARSE,
                        source_file=source_file,
                        line_no=line_no,
                        detail=str(exc),
                        raw=json.dumps(row, ensure_ascii=False, sort_keys=True),
                    )
                )
                continue

            raw_event_ids.add(movement.event_id)
            existing = candidates.get(movement.event_id)
            if existing is None:
                candidates[movement.event_id] = movement
                continue
            if existing.canonical_tuple() == movement.canonical_tuple():
                # 重送不双记：内容完全一致即幂等跳过，仅留痕。
                duplicates.append(
                    Duplicate(movement.event_id, source_file, line_no)
                )
            else:
                # 同一 event_id 内容冲突：后来者隔离，先到者保持候选。
                quarantined.append(
                    Quarantine(
                        event_id=movement.event_id,
                        reason=R_CONFLICT,
                        source_file=source_file,
                        line_no=line_no,
                        detail="content differs from earlier event_id",
                        raw=json.dumps(
                            dict(
                                zip(
                                    (
                                        "event_id",
                                        "kind",
                                        "book_key",
                                        "location",
                                        "other_location",
                                        "transfer_id",
                                        "quantity",
                                        "effective_day",
                                        "recorded_at",
                                        "reversal_of",
                                    ),
                                    movement.canonical_tuple(),
                                    strict=True,
                                )
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    )
                )
    return candidates, quarantined, duplicates, raw_event_ids


# ---------------------------------------------------------------------------
# 依赖图
# ---------------------------------------------------------------------------

def _build_edges(candidates: dict[str, Movement]):
    """返回 edges: event_id -> 它依赖的事件 id 集合。

    只有 REVERSAL -> 原事件 是严格的拓扑依赖。TRANSFER_IN 与 TRANSFER_OUT
    之间建“次序边”而非“合法性边”：在途桶是多条接收共享的可签收额度，部分
    签收无法用单一边表达，超收由回放时的桶余额非负拦截；但同一 transfer_id
    内必须保证发出先于接收、先签收的先于后签收的，次序边只用于 Kahn 排序，
    回放拒绝时不沿它级联（级联只走 reversal_of 链）。
    """
    edges: dict[str, set[str]] = {eid: set() for eid in candidates}
    transfer_outs: dict[str, Movement] = {}
    transfer_ins: dict[str, list[Movement]] = {}

    for movement in candidates.values():
        if movement.kind == "TRANSFER_OUT":
            transfer_outs.setdefault(movement.transfer_id, movement)
        elif movement.kind == "TRANSFER_IN":
            transfer_ins.setdefault(movement.transfer_id, []).append(movement)
        elif movement.is_reversal:
            edges[movement.event_id].add(movement.reversal_of)

    for tid, out in transfer_outs.items():
        prior = out.event_id
        for m in sorted(
            transfer_ins.get(tid, []),
            key=lambda x: (x.effective_day, x.event_id),
        ):
            edges[m.event_id].add(prior)
            prior = m.event_id

    # 冲回一笔 TRANSFER_OUT（整笔调拨作废）必须排在该 transfer 的接收侧
    # 事件之后：先由冲回接收把在途恢复，再由冲回发出把在途清零并退回原地点，
    # 这样任何中间时刻在途桶都不为负。这里只加次序边，合法性仍由回放余额判定。
    reversal_outs = [
        m for m in candidates.values()
        if m.is_reversal
        and candidates.get(m.reversal_of) is not None
        and candidates[m.reversal_of].kind == "TRANSFER_OUT"
    ]
    for rev in reversal_outs:
        target_tid = candidates[rev.reversal_of].transfer_id
        for m in transfer_ins.get(target_tid, []):
            if m.event_id in edges:
                edges[rev.event_id].add(m.event_id)
        # 接收侧的冲回也要先于发出侧冲回。
        for m in candidates.values():
            if (
                m.is_reversal
                and candidates.get(m.reversal_of) is not None
                and candidates[m.reversal_of].kind == "TRANSFER_IN"
                and candidates[m.reversal_of].transfer_id == target_tid
            ):
                edges[rev.event_id].add(m.event_id)

    return edges, transfer_outs, transfer_ins


def _topo_order(ids: set[str], edges: dict[str, set[str]], by_id: dict[str, Movement]):
    """Kahn 拓扑排序，堆按 (effective_day, event_id) 打破并列，保证确定性。

    返回 (order, unresolved)；unresolved 非空即存在环。
    """
    indegree = {eid: 0 for eid in ids}
    dependents: dict[str, list[str]] = {eid: [] for eid in ids}
    for eid in ids:
        for dep in edges[eid]:
            if dep in ids:
                indegree[eid] += 1
                dependents[dep].append(eid)

    heap: list[tuple] = []
    for eid, deg in indegree.items():
        if deg == 0:
            m = by_id[eid]
            heapq.heappush(heap, (m.effective_day, m.event_id, eid))

    order: list[str] = []
    while heap:
        _, _, eid = heapq.heappop(heap)
        order.append(eid)
        for child in dependents[eid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                m = by_id[child]
                heapq.heappush(heap, (m.effective_day, m.event_id, child))
    return order, set(ids) - set(order)


def _replay_dependents(
    offender: str,
    pool: set[str],
    candidates: dict[str, Movement],
) -> set[str]:
    """回放拒绝一个事件后，找必须连带隔离的下游（传递闭包）。

    * 冲回与其原事件同生共死：reversal_of 指向 offender 的事件连带；
    * 冲回一笔 TRANSFER_OUT 等于整笔调拨作废，该 transfer_id 的接收及其
      冲回失去在途来源，必须连带（否则会凭空造出负在途）；
    * 普通超收（接收量超过在途）只拒绝接收单本身，发出单与其他接收保留，
      在途桶按剩余事件重算，部分签收由此自然得到正确余额。
    """
    seed = candidates[offender]
    cancel_transfer = (
        seed.is_reversal
        and candidates[seed.reversal_of].kind == "TRANSFER_OUT"
    )
    related: set[str] = set()

    def direct(eid: str) -> set[str]:
        m = candidates[eid]
        out: set[str] = set()
        for other in pool:
            if other in related or other == offender:
                continue
            o = candidates[other]
            if o.reversal_of == eid:
                out.add(other)
            elif (
                cancel_transfer
                and m.transfer_id
                and o.transfer_id == m.transfer_id
                and o.kind in {"TRANSFER_IN", "REVERSAL"}
            ):
                out.add(other)
        return out

    frontier = direct(offender)
    related |= frontier
    while frontier:
        nxt: set[str] = set()
        for eid in frontier:
            nxt |= direct(eid) - related
        related |= nxt
        frontier = nxt
    return related


def _tarjan_cycles(ids: set[str], edges: dict[str, set[str]]):
    """返回参与有向环的节点集合（Tarjan SCC，环 = 大小>1 或自环）。"""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    cyclic: set[str] = set()

    def strongconnect(v: str) -> None:
        indices[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in edges[v]:
            if w not in ids:
                continue
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            component = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                component.append(w)
                if w == v:
                    break
            if len(component) > 1 or v in edges[v]:
                cyclic.update(component)

    for eid in sorted(ids):
        if eid not in indices:
            strongconnect(eid)
    return cyclic


def _cascade_dependents(
    removed: set[str],
    ids: set[str],
    edges: dict[str, set[str]],
) -> set[str]:
    """所有（传递）依赖了 removed 中节点的候选，级联隔离。"""
    reverse: dict[str, set[str]] = {eid: set() for eid in ids}
    for eid in ids:
        for dep in edges[eid]:
            # dep 可能已被隔离（不在 ids 中），仍要登记它在 live 内的下游。
            reverse.setdefault(dep, set()).add(eid)
    seen: set[str] = set()
    frontier = set(removed)
    while frontier:
        nxt: set[str] = set()
        for node in frontier:
            for child in reverse.get(node, ()):
                if child not in seen and child not in removed:
                    nxt.add(child)
        seen |= nxt
        frontier = nxt
    return seen


# ---------------------------------------------------------------------------
# 记账语义
# ---------------------------------------------------------------------------

def _apply(movement: Movement, by_id: dict[str, Movement], onhand, transit, sold_net):
    """把事件 delta 落到三个账上；返回 (账户, 键) 列表，供非负检查。"""
    q = movement.quantity
    book = movement.book_key
    loc = movement.location

    if movement.kind == "RECEIPT":
        onhand[(book, loc)] += q
        return [("onhand", (book, loc))]
    if movement.kind == "SALE":
        onhand[(book, loc)] -= q
        sold_net[(book, loc)] += q
        return [("onhand", (book, loc))]
    if movement.kind == "RETURN":
        onhand[(book, loc)] += q
        sold_net[(book, loc)] -= q
        return [("onhand", (book, loc))]
    if movement.kind == "TRANSFER_OUT":
        onhand[(book, loc)] -= q
        transit[movement.transfer_id] += q
        return [("onhand", (book, loc)), ("transit", movement.transfer_id)]
    if movement.kind == "TRANSFER_IN":
        transit[movement.transfer_id] -= q
        onhand[(book, loc)] += q
        return [("transit", movement.transfer_id), ("onhand", (book, loc))]

    # REVERSAL：精确冲回 = 对原事件施加反向 delta
    target = by_id[movement.reversal_of]
    tq = target.quantity
    tb = target.book_key
    tl = target.location
    if target.kind == "RECEIPT":
        onhand[(tb, tl)] -= tq
        return [("onhand", (tb, tl))]
    if target.kind == "SALE":
        onhand[(tb, tl)] += tq
        sold_net[(tb, tl)] -= tq
        return [("onhand", (tb, tl))]
    if target.kind == "RETURN":
        onhand[(tb, tl)] -= tq
        sold_net[(tb, tl)] += tq
        return [("onhand", (tb, tl))]
    if target.kind == "TRANSFER_OUT":
        onhand[(tb, tl)] += tq
        transit[target.transfer_id] -= tq
        return [("onhand", (tb, tl)), ("transit", target.transfer_id)]
    # target.kind == "TRANSFER_IN"
    transit[target.transfer_id] += tq
    onhand[(tb, tl)] -= tq
    return [("transit", target.transfer_id), ("onhand", (tb, tl))]

# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_ledger(inputs: list[str | Path]) -> LedgerBuild:
    inputs = [Path(p) for p in inputs]
    candidates, quarantined, duplicates, input_event_ids = _load_candidates(inputs)
    input_event_ids = frozenset(input_event_ids)

    edges, transfer_outs, transfer_ins = _build_edges(candidates)

    # -- 挂起：依赖在本次输入中根本不存在（原单尚未到达）。
    # 迭代处理：若挂起节点本身也是别人的依赖（例如冲回的原事件缺失，
    # 而它又被另一条流水引用），下游一并挂起，保证不存在悬空引用。
    # TRANSFER_IN 的发出单缺失走 transfer_id 配对（二者无拓扑边，
    # 在途额度由回放余额约束）；REVERSAL 走依赖图。 --
    pending: list[Pending] = []
    while True:
        edges, transfer_outs, transfer_ins = _build_edges(candidates)
        suspended: list[Pending] = []
        for movement in list(candidates.values()):
            if movement.kind == "TRANSFER_IN" and movement.transfer_id not in transfer_outs:
                suspended.append(
                    Pending(
                        movement.event_id,
                        PENDING_TRANSFER,
                        f"transfer_id={movement.transfer_id}",
                        movement.source_file,
                        movement.line_no,
                    )
                )
                continue
            missing_dep = next(
                (dep for dep in edges[movement.event_id] if dep not in candidates),
                None,
            )
            if missing_dep is not None:
                suspended.append(
                    Pending(
                        movement.event_id,
                        PENDING_REVERSAL,
                        f"event_id={missing_dep}",
                        movement.source_file,
                        movement.line_no,
                    )
                )
        if not suspended:
            break
        for item in suspended:
            pending.append(item)
            del candidates[item.event_id]

    def quarantine(m: Movement, reason: str, detail: str = "") -> None:
        quarantined.append(
            Quarantine(
                event_id=m.event_id,
                reason=reason,
                source_file=m.source_file,
                line_no=m.line_no,
                detail=detail,
                raw=json.dumps(
                    dict(
                        zip(
                            (
                                "event_id",
                                "kind",
                                "book_key",
                                "location",
                                "other_location",
                                "transfer_id",
                                "quantity",
                                "effective_day",
                                "recorded_at",
                                "reversal_of",
                            ),
                            m.canonical_tuple(),
                            strict=True,
                        )
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )

    # -- 环检测前置：循环冲回（c1<->c2）优先定性为 reversal_cycle，
    # 否则会在镜像校验里被误报成 bad_reversal_target。 --
    # stage_bad 记录本阶段隔离的“候选”事件；解析期隔离（如内容冲突的后来者）
    # 不在其中，其先到同名事件仍可正常成账。 --
    stage_bad: set[str] = set()

    def stage_quarantine(eid: str, reason: str, detail: str) -> None:
        stage_bad.add(eid)
        quarantine(candidates[eid], reason, detail)

    initial_live = set(candidates)
    initial_edges = {
        eid: {d for d in edges[eid] if d in initial_live}
        for eid in initial_live
    }
    cyclic = _tarjan_cycles(initial_live, initial_edges)
    for eid in sorted(cyclic):
        stage_quarantine(eid, R_CYCLE, "cycle in reversal graph")
    if cyclic:
        ring_cascade = _cascade_dependents(cyclic, initial_live - cyclic, initial_edges)
        for eid in sorted(ring_cascade):
            stage_quarantine(eid, R_DEPENDENCY_QUARANTINED, "cycle upstream")

    # -- 静态跨事件校验（跳过已因环隔离的节点） --
    reversed_targets: set[str] = set()
    for movement in list(candidates.values()):
        if not movement.is_reversal or movement.event_id in cyclic:
            continue
        target = candidates.get(movement.reversal_of)
        if target is None:
            continue  # 已挂起
        if target.kind not in REVERSIBLE_KINDS:
            stage_quarantine(movement.event_id, R_BAD_REVERSAL_TARGET, f"target={target.kind}")
            continue
        if target.effective_day != movement.effective_day:
            stage_quarantine(
                movement.event_id,
                R_BAD_REVERSAL_DAY,
                f"reversal={movement.effective_day} target={target.effective_day}",
            )
            continue
        if (
            target.book_key != movement.book_key
            or target.location != movement.location
            or target.quantity != movement.quantity
        ):
            stage_quarantine(
                movement.event_id,
                R_BAD_REVERSAL_MATCH,
                "book/location/quantity must mirror target",
            )
            continue
        # 冲回调拨时 transfer_id 必须与原单一致，便于定位在途桶。
        if target.transfer_id != movement.transfer_id:
            stage_quarantine(
                movement.event_id,
                R_BAD_REVERSAL_MATCH,
                "transfer_id must mirror target transfer",
            )
            continue
        if target.event_id in reversed_targets:
            stage_quarantine(
                movement.event_id, R_DUP_REVERSAL, f"target={target.event_id}"
            )
            continue
        reversed_targets.add(target.event_id)

    # 同一 transfer_id 只允许一个发出；多个接收（分批部分签收）合法。
    seen_out: dict[str, Movement] = {}
    for movement in list(candidates.values()):
        if movement.kind != "TRANSFER_OUT" or movement.event_id not in candidates:
            continue
        if movement.transfer_id in seen_out:
            stage_quarantine(
                movement.event_id,
                R_TRANSFER_REUSE,
                f"transfer_id={movement.transfer_id} reused",
            )
        else:
            seen_out[movement.transfer_id] = movement

    for tid, ins in transfer_ins.items():
        out = transfer_outs.get(tid)
        if out is None:
            continue
        for movement in ins:
            if movement.event_id not in candidates:
                continue
            pair_ok = (
                movement.book_key == out.book_key
                and movement.location == out.other_location
                and movement.other_location == out.location
            )
            if not pair_ok:
                stage_quarantine(
                    movement.event_id,
                    R_BAD_TRANSFER_PAIR,
                    f"out={out.event_id} direction/book mismatch",
                )
            elif movement.effective_day < out.effective_day:
                stage_quarantine(
                    movement.event_id,
                    R_RECEIPT_BEFORE_DISPATCH,
                    f"in={movement.effective_day} out={out.effective_day}",
                )

    # 重算图（候选可能已被静态隔离），把隔离节点及其下游级联清出。
    static_bad = set(stage_bad)
    live = set(candidates) - static_bad
    live_edges = {eid: {d for d in edges[eid] if d in live} for eid in live}
    cascaded = _cascade_dependents(static_bad, live, live_edges)
    for eid in sorted(cascaded):
        stage_quarantine(eid, R_DEPENDENCY_QUARANTINED, "upstream rejected")
    live = {eid for eid in live if eid not in cascaded}
    live_edges = {eid: {d for d in edges[eid] if d in live} for eid in live}

    # 回放字典只包含通过全部静态校验的事件；已隔离事件物理上不在其中，
    # _apply 的冲回分支不可能再引用到它们。
    replay_by_id = {eid: candidates[eid] for eid in live}

    # -- 余额回放：按书目独立成账。
    # 一本书的超收/负库存只隔离该书的相关流水，不能连累其他书目（“合法部分
    # 仍可形成一致快照”）。每个书目子图独立做 Kahn 回放，非法节点隔离后重放。 --
    accepted_order: list[str] = []
    final_onhand: dict[tuple[str, str], int] = defaultdict(int)
    final_transit: dict[str, int] = defaultdict(int)
    final_sold: dict[tuple[str, str], int] = defaultdict(int)

    all_books = sorted({candidates[eid].book_key for eid in live})
    for book in all_books:
        book_start = {eid for eid in live if candidates[eid].book_key == book}
        sub_live = set(book_start)
        sub_edges = {
            eid: {d for d in live_edges[eid] if d in sub_live}
            for eid in sub_live
        }
        while True:
            order, unresolved = _topo_order(sub_live, sub_edges, replay_by_id)
            if unresolved:  # 兜底：环理论上已在前面处理
                for eid in sorted(unresolved):
                    quarantine(candidates[eid], R_CYCLE, "unresolved dependency cycle")
                sub_live -= unresolved
                sub_edges = {
                    eid: {d for d in sub_edges[eid] if d in sub_live}
                    for eid in sub_live
                }
                continue

            onhand: dict[tuple[str, str], int] = defaultdict(int)
            transit: dict[str, int] = defaultdict(int)
            sold_net: dict[tuple[str, str], int] = defaultdict(int)
            offender: str | None = None
            offender_reason = ""
            for eid in order:
                movement = replay_by_id[eid]
                touched = _apply(movement, replay_by_id, onhand, transit, sold_net)
                for account, key in touched:
                    value = transit[key] if account == "transit" else onhand[key]
                    if value < 0:
                        offender_reason = (
                            R_OVER_RECEIPT if account == "transit"
                            else R_INSUFFICIENT_BALANCE
                        )
                        offender = eid
                        break
                if offender is not None:
                    break

            if offender is None:
                final_onhand.update(onhand)
                final_transit.update(transit)
                final_sold.update(sold_net)
                accepted_order.extend(order)
                break

            quarantine(
                candidates[offender], offender_reason, "negative balance on replay"
            )
            down = _replay_dependents(
                offender, sub_live - {offender}, replay_by_id
            )
            for eid in sorted(down):
                quarantine(
                    candidates[eid], R_DEPENDENCY_QUARANTINED, "upstream rejected"
                )
            sub_live = {
                eid for eid in sub_live if eid != offender and eid not in down
            }
            sub_edges = {
                eid: {d for d in sub_edges[eid] if d in sub_live}
                for eid in sub_live
            }

        # 本书目在回放中隔离的节点，同步从全局 live 与回放字典移除。
        rejected = book_start - sub_live
        live = {eid for eid in live if eid not in rejected}
        for eid in rejected:
            replay_by_id.pop(eid, None)
        live_edges = {eid: {d for d in edges[eid] if d in live} for eid in live}

    # 输出顺序与全局确定性 Kahn 次序保持一致（按 effective_day, event_id）。
    accepted_order = [
        eid for eid in _topo_order(set(accepted_order), live_edges, replay_by_id)[0]
    ]

    accepted = tuple(candidates[eid] for eid in accepted_order)
    by_id = {m.event_id: m for m in accepted}

    # -- 守恒校验：每本书 在库 + 在途 + 净售出 == 净入库。
    # 冲回只是反向 delta，售出与冲回售出在 sold_net 内自然抵消，
    # 因此“来源”只需统计 RECEIPT 与对 RECEIPT 的冲回，无需特殊处理其他冲回。 --
    conservation: dict[str, int] = {}
    books = {m.book_key for m in accepted}
    for book in sorted(books):
        supply = sum(
            m.quantity
            for m in accepted
            if m.book_key == book and m.kind == "RECEIPT"
        ) - sum(
            m.quantity
            for m in accepted
            if m.book_key == book
            and m.is_reversal
            and candidates[m.reversal_of].kind == "RECEIPT"
        )
        held = sum(v for (b, _), v in final_onhand.items() if b == book)
        moving = sum(
            v for tid, v in final_transit.items()
            if any(m.transfer_id == tid and m.book_key == book for m in accepted)
        )
        sold = sum(v for (b, _), v in final_sold.items() if b == book)
        assert held + moving + sold == supply, (
            f"conservation broken for {book}: "
            f"onhand={held} transit={moving} sold={sold} supply={supply}"
        )
        conservation[book] = held + moving + sold

    quarantined_sorted = tuple(
        sorted(quarantined, key=lambda q: (q.source_file, q.line_no, q.event_id))
    )
    pending_sorted = tuple(sorted(pending, key=lambda p: p.event_id))
    duplicates_sorted = tuple(
        sorted(duplicates, key=lambda d: (d.source_file, d.line_no))
    )
    return LedgerBuild(
        accepted=accepted,
        quarantined=quarantined_sorted,
        pending=pending_sorted,
        duplicates=duplicates_sorted,
        by_id=by_id,
        input_event_ids=input_event_ids,
        conservation=conservation,
    )
