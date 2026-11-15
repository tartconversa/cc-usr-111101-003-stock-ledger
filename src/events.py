"""流转事件的解析与单事件校验。

本模块只负责把 CSV 行解析为不可变的 :class:`Movement`，不做跨事件的
依赖判断（在 :mod:`src.ledger` 中完成）。所有字段在解析后即定型，
后续构建器不得修改事件本身，只能接受、挂起或隔离。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

KINDS = frozenset(
    {"RECEIPT", "SALE", "RETURN", "TRANSFER_OUT", "TRANSFER_IN", "REVERSAL"}
)
TRANSFER_KINDS = frozenset({"TRANSFER_OUT", "TRANSFER_IN"})
# 可被 REVERSAL 精确冲回的业务事件类型（冲回事件本身不能再被冲回）。
REVERSIBLE_KINDS = frozenset(
    {"RECEIPT", "SALE", "RETURN", "TRANSFER_OUT", "TRANSFER_IN"}
)

COLUMNS = (
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
)


@dataclass(frozen=True, slots=True)
class Movement:
    """一条不可变流转流水。

    effective_day 是业务归属日（决定进入哪一天的日结），recorded_at 只是
    系统登记时间，用于审计迟到流水，不参与任何排序或记账。
    quantity 恒为正整数；冲回由独立的 REVERSAL 事件表达，不允许负数。
    """

    event_id: str
    kind: str
    book_key: str
    location: str
    other_location: str
    transfer_id: str
    quantity: int
    effective_day: date
    recorded_at: datetime
    reversal_of: str
    source_file: str
    line_no: int

    @property
    def is_transfer(self) -> bool:
        return self.kind in TRANSFER_KINDS

    @property
    def is_reversal(self) -> bool:
        return self.kind == "REVERSAL"

    def canonical_tuple(self) -> tuple[str, ...]:
        """跨输入形态完全一致的规范化内容元组，用于幂等与内容冲突判定。"""
        return (
            self.event_id,
            self.kind,
            self.book_key,
            self.location,
            self.other_location,
            self.transfer_id,
            str(self.quantity),
            self.effective_day.isoformat(),
            self.recorded_at.isoformat(),
            self.reversal_of,
        )


class ParseError(ValueError):
    """单行流水无法解析为合法事件；原因码进入隔离区。"""


def _require_nonempty(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ParseError(f"missing_{field}")
    return value


def parse_row(row: dict[str, str], source_file: str, line_no: int) -> Movement:
    """把一行原始字典解析为 :class:`Movement`，失败抛 :class:`ParseError`。"""
    missing = [name for name in COLUMNS if name not in row]
    if missing:
        raise ParseError("missing_columns:" + ",".join(missing))

    event_id = _require_nonempty(row["event_id"], "event_id")
    kind = _require_nonempty(row["kind"], "kind")
    if kind not in KINDS:
        raise ParseError(f"unknown_kind:{kind}")
    book_key = _require_nonempty(row["book_key"], "book_key")
    location = _require_nonempty(row["location"], "location")
    other_location = row["other_location"].strip()
    transfer_id = row["transfer_id"].strip()
    reversal_of = row["reversal_of"].strip()

    raw_quantity = row["quantity"].strip()
    if not raw_quantity.isdigit():
        raise ParseError("quantity_not_positive_int")
    quantity = int(raw_quantity)
    if quantity <= 0:
        raise ParseError("quantity_not_positive_int")

    try:
        effective_day = date.fromisoformat(row["effective_day"].strip())
    except ValueError as exc:
        raise ParseError("bad_effective_day") from exc
    try:
        recorded_at = datetime.fromisoformat(row["recorded_at"].strip())
    except ValueError as exc:
        raise ParseError("bad_recorded_at") from exc

    if kind in TRANSFER_KINDS:
        if not other_location:
            raise ParseError("transfer_missing_other_location")
        if not transfer_id:
            raise ParseError("transfer_missing_transfer_id")
        if other_location == location:
            raise ParseError("transfer_self_location")
    elif kind == "REVERSAL":
        # 冲回调拨事件允许带 transfer_id（在途桶标识），但不允许方向字段；
        # 其他字段必须与原事件镜像，由 ledger 做跨事件校验。
        if other_location:
            raise ParseError("reversal_has_other_location")
    elif other_location or transfer_id:
        raise ParseError("non_transfer_has_transfer_fields")

    if kind == "REVERSAL":
        if not reversal_of:
            raise ParseError("reversal_missing_target")
        if reversal_of == event_id:
            raise ParseError("reversal_self_reference")
    elif reversal_of:
        raise ParseError("non_reversal_has_reversal_of")

    return Movement(
        event_id=event_id,
        kind=kind,
        book_key=book_key,
        location=location,
        other_location=other_location,
        transfer_id=transfer_id,
        quantity=quantity,
        effective_day=effective_day,
        recorded_at=recorded_at,
        reversal_of=reversal_of,
        source_file=source_file,
        line_no=line_no,
    )


def iter_rows(path: str | Path) -> list[tuple[dict[str, str], str, int]]:
    """读取单个 CSV，返回 (原始行, 文件名, 行号)。行号从 2 起（含表头）。"""
    path = Path(path)
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ParseError("empty_file")
        unknown = [name for name in reader.fieldnames if name not in COLUMNS]
        if unknown:
            raise ParseError("unknown_columns:" + ",".join(unknown))
        return [(dict(row), path.name, i) for i, row in enumerate(reader, start=2)]
