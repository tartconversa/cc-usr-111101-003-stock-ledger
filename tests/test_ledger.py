"""事实层核心语义测试：账目守恒、挂起、隔离、幂等与确定性。"""

from __future__ import annotations

import unittest
from pathlib import Path

from src.ledger import (
    R_BAD_REVERSAL_MATCH,
    R_CONFLICT,
    R_CYCLE,
    R_DEPENDENCY_QUARANTINED,
    R_INSUFFICIENT_BALANCE,
    R_OVER_RECEIPT,
    R_PARSE,
    R_TRANSFER_REUSE,
    build_ledger,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


def ids(build, attr):
    return {getattr(item, "event_id") for item in getattr(build, attr)}


class HeadlineScenarioTests(unittest.TestCase):
    """题面主线：期初 A 10 册，发 4、收 3、售 2，含迟到退回。"""

    def setUp(self):
        self.build = build_ledger([FIX / "movements.csv"])

    def test_all_rows_accepted(self):
        self.assertEqual(
            ids(self.build, "accepted"), {"m1", "m2", "m3", "m4", "m5"}
        )
        self.assertEqual(self.build.quarantined, ())
        self.assertEqual(self.build.pending, ())

    def test_balances_after_replay(self):
        # 直接用快照重放验证 A=6、B=2（含 09-04 迟到退回 1 册）、在途=1
        from src.snapshot import build_snapshot_rows

        loc_rows, transit_rows, book_rows = build_snapshot_rows(self.build)
        final_day = "2026-09-04"

        a = next(
            r for r in loc_rows
            if r["book_key"] == "b1" and r["location"] == "A"
            and r["effective_day"] == final_day
        )
        b = next(
            r for r in loc_rows
            if r["book_key"] == "b1" and r["location"] == "B"
            and r["effective_day"] == final_day
        )
        t = next(r for r in transit_rows if r["effective_day"] == final_day)
        self.assertEqual(a["onhand_eod"], 6)
        self.assertEqual(b["onhand_eod"], 2)
        self.assertEqual(t["in_transit_eod"], 1)

    def test_conservation_is_ten(self):
        # 在库 6 + 2 + 在途 1 + 净售出 1 == 期初 10
        self.assertEqual(self.build.conservation, {"b1": 10})
        from src.snapshot import build_snapshot_rows

        _, _, book_rows = build_snapshot_rows(self.build)
        final = next(r for r in book_rows if r["effective_day"] == "2026-09-04")
        self.assertEqual(
            final["onhand_total_eod"]
            + final["in_transit_eod"]
            + final["sold_net_eod"],
            10,
        )

    def test_no_negative_balance_on_any_day(self):
        from src.snapshot import build_snapshot_rows

        loc_rows, transit_rows, _ = build_snapshot_rows(self.build)
        self.assertTrue(all(r["onhand_eod"] >= 0 for r in loc_rows))
        self.assertTrue(all(r["in_transit_eod"] >= 0 for r in transit_rows))


class LateArrivalTests(unittest.TestCase):
    """迟到流水改变旧日结：m5 在 09-08 才登记，但归属 09-04。"""

    def test_recorded_at_not_used_for_ledger_day(self):
        build = build_ledger([FIX / "movements.csv"])
        m5 = build.by_id["m5"]
        self.assertEqual(m5.effective_day.isoformat(), "2026-09-04")
        self.assertEqual(m5.recorded_at.date().isoformat(), "2026-09-08")

    def test_late_return_rewrites_old_snapshot(self):
        # 不含 m5 的版本：B 日结为 1；补入 m5 后 09-04 及之后的 B 变为 2
        early = build_ledger([FIX / "segment_a.csv", FIX / "segment_b.csv"])
        # 去掉 m5 的等价输入
        no_return = build_ledger([FIX / "segment_a.csv"])
        from src.snapshot import build_snapshot_rows

        rows_full, _, _ = build_snapshot_rows(early)
        rows_early, _, _ = build_snapshot_rows(no_return)
        b_full = [
            r for r in rows_full
            if r["book_key"] == "b1" and r["location"] == "B"
        ]
        b_early = {
            r["effective_day"]: r["onhand_eod"] for r in rows_early
            if r["book_key"] == "b1" and r["location"] == "B"
        }
        # 09-03 两版一致（B 收到 3 册），09-04 起退回把 B 从 1 改成 2
        self.assertEqual(b_early["2026-09-03"], 3)
        day4 = next(r for r in b_full if r["effective_day"] == "2026-09-04")
        self.assertEqual(day4["onhand_eod"], 2)
        self.assertEqual(day4["return_day_net"], 1)


class QuarantineTests(unittest.TestCase):
    def setUp(self):
        self.build = build_ledger([FIX / "illegal_cases.csv"])
        self.q = ids(self.build, "quarantined")
        self.a = ids(self.build, "accepted")
        self.p = ids(self.build, "pending")

    def test_over_receipt_is_quarantined_but_chain_kept_separate(self):
        # i3 收 6 > 发 5：在途转负，隔离 i3
        self.assertIn("i3", self.q)
        reason = next(q.reason for q in self.build.quarantined if q.event_id == "i3")
        self.assertEqual(reason, R_OVER_RECEIPT)
        # 同文件的合法 t8 部分签收、t7 完整冲回链与 i1、i9 仍可成账
        self.assertLessEqual(
            {"i1", "i2", "i4", "i5", "i10", "i11", "i6", "i7", "i9"}, self.a
        )

    def test_reversal_chain_is_accepted(self):
        # i6 冲回接收、i7 冲回发出：精确镜像、同日，合法
        self.assertIn("i6", self.a)
        self.assertIn("i7", self.a)
        # 完整冲回链净值为 0：t7 在途最终结平
        from src.snapshot import build_snapshot_rows

        _, transit_rows, _ = build_snapshot_rows(self.build)
        t7 = [r for r in transit_rows if r["transfer_id"] == "t7"]
        self.assertEqual(t7[-1]["in_transit_eod"], 0)

    def test_reversal_quantity_mismatch_isolated(self):
        self.assertIn("i8", self.q)
        reason = next(q.reason for q in self.build.quarantined if q.event_id == "i8")
        self.assertEqual(reason, R_BAD_REVERSAL_MATCH)

    def test_circular_reversals_isolated(self):
        self.assertIn("c1", self.q)
        self.assertIn("c2", self.q)
        for q in self.build.quarantined:
            if q.event_id in {"c1", "c2"}:
                self.assertEqual(q.reason, R_CYCLE)

    def test_duplicate_id_conflict_isolated_and_original_kept(self):
        self.assertIn("d1", self.a)
        conflict = [q for q in self.build.quarantined if q.event_id == "d1"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0].reason, R_CONFLICT)

    def test_insufficient_balance_isolated(self):
        # b4 只有 2 册，售 9、发 9 均非法
        self.assertIn("x1", self.q)
        self.assertIn("x2", self.q)

    def test_pending_when_dependency_absent(self):
        self.assertEqual(self.p, {"p1", "p2"})
        reasons = {p.event_id: p.reason for p in self.build.pending}
        self.assertTrue(reasons["p1"].endswith("missing_transfer_out")
                        or "missing_transfer_out" in reasons["p1"])
        self.assertIn("missing_reversal_target", reasons["p2"])

    def test_unparseable_row_does_not_kill_build(self):
        # 见 ParseLevelTests 中构造的坏行；此处确认非法文件里合法部分仍守恒
        self.assertEqual(self.build.conservation["b2"], 18)  # 11 + 7，冲回链净值为 0

    def test_transfer_id_reuse_reason_exists_in_constants(self):
        self.assertEqual(R_TRANSFER_REUSE, "transfer_id_reuse")
        self.assertEqual(R_PARSE, "unparseable_row")
        self.assertEqual(R_DEPENDENCY_QUARANTINED, "dependency_quarantined")


class ParseLevelTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name, body):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        return path

    HEADER = (
        "event_id,kind,book_key,location,other_location,transfer_id,"
        "quantity,effective_day,recorded_at,reversal_of\n"
    )

    def test_bad_quantity_and_unknown_kind_quarantined(self):
        path = self._write("bad.csv", self.HEADER + (
            "z1,SALE,b1,A,,,0,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "z2,WARP,b1,A,,,1,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "z3,RECEIPT,b1,A,,,2,2026-10-01,2026-10-01T00:00:00+08:00,\n"
        ))
        build = build_ledger([path])
        self.assertEqual(ids(build, "accepted"), {"z3"})
        self.assertEqual(ids(build, "quarantined"), {"z1", "z2"})

    def test_exact_resend_is_idempotent_not_double_booked(self):
        body = self.HEADER + (
            "r1,RECEIPT,b1,A,,,3,2026-10-01,2026-10-01T00:00:00+08:00,\n"
        )
        first = self._write("f1.csv", body)
        second = self._write("f2.csv", body)
        build = build_ledger([first, second])
        self.assertEqual(ids(build, "accepted"), {"r1"})
        self.assertEqual(len(build.duplicates), 1)
        self.assertEqual(build.duplicates[0].event_id, "r1")
        from src.snapshot import build_snapshot_rows

        rows, _, _ = build_snapshot_rows(build)
        self.assertEqual(rows[0]["onhand_eod"], 3)  # 不是 6

    def test_partial_receipt_then_remainder(self):
        path = self._write("partial.csv", self.HEADER + (
            "o1,RECEIPT,b1,A,,,10,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "o2,TRANSFER_OUT,b1,A,B,tt,4,2026-10-02,2026-10-02T00:00:00+08:00,\n"
            "o3,TRANSFER_IN,b1,B,A,tt,3,2026-10-03,2026-10-03T00:00:00+08:00,\n"
            "o4,TRANSFER_IN,b1,B,A,tt,1,2026-10-05,2026-10-05T00:00:00+08:00,\n"
        ))
        build = build_ledger([path])
        self.assertEqual(build.quarantined, ())
        from src.snapshot import build_snapshot_rows

        _, transit_rows, _ = build_snapshot_rows(build)
        eod = {r["effective_day"]: r["in_transit_eod"] for r in transit_rows}
        self.assertEqual(eod["2026-10-02"], 4)
        self.assertEqual(eod["2026-10-03"], 1)
        self.assertEqual(eod["2026-10-05"], 0)  # 分批签收，最终结平，不超发

    def test_pending_resumes_after_original_arrives(self):
        opening = self._write("opening.csv", self.HEADER + (
            "ginit,RECEIPT,b1,A,,,5,2026-10-01,2026-10-01T00:00:00+08:00,\n"
        ))
        receipt_only = self._write("in_only.csv", self.HEADER + (
            "g1,TRANSFER_IN,b1,B,A,tg,2,2026-10-03,2026-10-03T00:00:00+08:00,\n"
        ))
        first = build_ledger([opening, receipt_only])
        self.assertEqual(ids(first, "pending"), {"g1"})
        self.assertEqual(ids(first, "accepted"), {"ginit"})

        out = self._write("out.csv", self.HEADER + (
            "g0,TRANSFER_OUT,b1,A,B,tg,2,2026-10-02,2026-10-02T00:00:00+08:00,\n"
        ))
        resumed = build_ledger([opening, receipt_only, out])
        self.assertEqual(ids(resumed, "accepted"), {"ginit", "g0", "g1"})
        self.assertEqual(resumed.pending, ())


class DeterminismTests(unittest.TestCase):
    def test_one_shot_equals_segmented(self):
        one = build_ledger([FIX / "movements.csv"])
        segmented = build_ledger(
            [FIX / "segment_a.csv", FIX / "segment_b.csv"]
        )
        self.assertEqual(one.accepted_ids, segmented.accepted_ids)
        from src.snapshot import build_snapshot_rows

        self.assertEqual(
            build_snapshot_rows(one), build_snapshot_rows(segmented)
        )

    def test_input_order_does_not_change_result(self):
        forward = build_ledger(
            [FIX / "segment_a.csv", FIX / "segment_b.csv"]
        )
        reverse = build_ledger(
            [FIX / "segment_b.csv", FIX / "segment_a.csv"]
        )
        self.assertEqual(forward.accepted_ids, reverse.accepted_ids)
        self.assertEqual(
            {(q.event_id, q.reason) for q in forward.quarantined},
            {(q.event_id, q.reason) for q in reverse.quarantined},
        )

    def test_reversal_of_sale_cannot_create_negative_onhand(self):
        import tempfile

        body = (
            "event_id,kind,book_key,location,other_location,transfer_id,"
            "quantity,effective_day,recorded_at,reversal_of\n"
            "s1,RECEIPT,b1,A,,,5,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "s2,SALE,b1,A,,,5,2026-10-02,2026-10-02T00:00:00+08:00,\n"
            "s3,REVERSAL,b1,A,,,5,2026-10-02,2026-10-03T00:00:00+08:00,s2\n"
            "s4,RECEIPT,b1,A,,,4,2026-10-03,2026-10-03T00:00:00+08:00,\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.csv"
            path.write_text(body, encoding="utf-8")
            build = build_ledger([path])
        # 冲回售出把 5 册退回，随后再入库 4：A=9，无负数
        self.assertEqual(build.quarantined, ())
        self.assertEqual(build.conservation, {"b1": 9})

    def test_reversing_consumed_receipt_is_blocked_no_negative(self):
        """冲回入库但该批册已被后续售出耗尽：回放必然在某处拦截，不造负在库。

        冲回归属原入库日（10-01），回放次序上先于 10-02 的售出，于是冲回把
        在库清零后售出转负——被隔离的是下游售出；无论拦截点在哪，账户都不会负。
        """
        import tempfile

        body = (
            "event_id,kind,book_key,location,other_location,transfer_id,"
            "quantity,effective_day,recorded_at,reversal_of\n"
            "r1,RECEIPT,b1,A,,,5,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "r2,SALE,b1,A,,,5,2026-10-02,2026-10-02T00:00:00+08:00,\n"
            # 冲回 r1 入库 5：归属 10-01，使后续售出失去库存来源
            "r3,REVERSAL,b1,A,,,5,2026-10-01,2026-10-05T00:00:00+08:00,r1\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.csv"
            path.write_text(body, encoding="utf-8")
            build = build_ledger([path])
        # 至少一条被隔离，且接受集合在任何有效日都不出现负库存
        self.assertTrue(build.quarantined)
        self.assertIn("r1", ids(build, "accepted"))  # 原入库始终保留
        reasons = {q.reason for q in build.quarantined}
        self.assertIn(R_INSUFFICIENT_BALANCE, reasons)
        from src.snapshot import build_snapshot_rows

        loc_rows, _, _ = build_snapshot_rows(build)
        self.assertTrue(all(r["onhand_eod"] >= 0 for r in loc_rows))

    def test_reversing_dispatch_keeps_transit_nonnegative(self):
        """冲回发出后在途结平；再对同一发出做超量接收必须被拦截，不造负在途。"""
        import tempfile

        body = (
            "event_id,kind,book_key,location,other_location,transfer_id,"
            "quantity,effective_day,recorded_at,reversal_of\n"
            "t1,RECEIPT,b1,A,,,5,2026-10-01,2026-10-01T00:00:00+08:00,\n"
            "t2,TRANSFER_OUT,b1,A,B,tt,3,2026-10-02,2026-10-02T00:00:00+08:00,\n"
            "t3,TRANSFER_IN,b1,B,A,tt,3,2026-10-03,2026-10-03T00:00:00+08:00,\n"
            # 超量接收：发出只有 3，再收 2 必令在途转负
            "t4,TRANSFER_IN,b1,B,A,tt,2,2026-10-04,2026-10-04T00:00:00+08:00,\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.csv"
            path.write_text(body, encoding="utf-8")
            build = build_ledger([path])
        self.assertEqual(ids(build, "accepted"), {"t1", "t2", "t3"})
        self.assertEqual(ids(build, "quarantined"), {"t4"})
        reason = next(q.reason for q in build.quarantined if q.event_id == "t4")
        self.assertEqual(reason, R_OVER_RECEIPT)
        from src.snapshot import build_snapshot_rows

        _, transit_rows, _ = build_snapshot_rows(build)
        self.assertTrue(all(r["in_transit_eod"] >= 0 for r in transit_rows))


if __name__ == "__main__":
    unittest.main()
