"""快照构建、内容指纹、受影响日结与原子发布边界测试。"""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from src.ledger import build_ledger
from src.snapshot import (
    LATEST_LINK,
    SUCCESS_MARKER,
    canonical_raw_rows,
    compute_build_id,
    publish_build,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "out"

    def test_build_contains_all_artifacts_and_manifest_fact_set(self):
        result = publish_build([FIX / "movements.csv"],
                               build_ledger([FIX / "movements.csv"]), self.out)
        d = result.build_dir
        for name in (
            "manifest.json", "accepted_facts.csv", "daily_location.csv",
            "daily_transit.csv", "daily_book.csv", "quarantine.csv",
            "pending.csv", "duplicates.csv", "affected_vs_previous.json",
            SUCCESS_MARKER,
        ):
            self.assertTrue((d / name).exists(), name)
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["accepted_event_ids"], ["m1", "m2", "m3", "m4", "m5"]
        )
        self.assertEqual(manifest["conservation"], {"b1": 10})
        # latest 指针已原子切换并可解析
        self.assertTrue((self.out / LATEST_LINK).is_symlink())
        self.assertEqual(
            (self.out / LATEST_LINK).resolve(), d.resolve()
        )

    def test_identical_input_set_same_build_id_even_segmented(self):
        rows_one = canonical_raw_rows([FIX / "movements.csv"])
        rows_seg = canonical_raw_rows(
            [FIX / "segment_a.csv", FIX / "segment_b.csv"]
        )
        b_one = build_ledger([FIX / "movements.csv"])
        b_seg = build_ledger(
            [FIX / "segment_a.csv", FIX / "segment_b.csv"]
        )
        self.assertEqual(
            compute_build_id(b_one, rows_one),
            compute_build_id(b_seg, rows_seg),
        )

    def test_rebuild_is_idempotent_and_no_duplicate_build_dir(self):
        first = publish_build([FIX / "movements.csv"],
                              build_ledger([FIX / "movements.csv"]), self.out)
        second = publish_build([FIX / "movements.csv"],
                               build_ledger([FIX / "movements.csv"]), self.out)
        self.assertEqual(first.build_id, second.build_id)
        build_dirs = [
            p for p in self.out.iterdir()
            if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
        ]
        self.assertEqual(len(build_dirs), 1)

    def test_late_arrival_lists_affected_old_days(self):
        # 第一版：期初、调拨、签收、售出都已到齐（末日 09-04，B 余 1 册）
        base_inputs = [
            FIX / "segment_a.csv",
            FIX / "segment_b.csv",
        ]
        # 去掉 segment_b 中的迟到退回 m5，构造“退回尚未登记”的基线。
        no_return = Path(self.tmp.name) / "base_no_return.csv"
        no_return.write_text(
            (FIX / "segment_b.csv").read_text(encoding="utf-8").replace(
                "m5,RETURN,b1,B,,,1,2026-09-04,2026-09-08T00:00:00+08:00,\n",
                "",
            ),
            encoding="utf-8",
        )
        publish_build(
            [FIX / "segment_a.csv", no_return],
            build_ledger([FIX / "segment_a.csv", no_return]),
            self.out,
        )
        # 第二版：补入 recorded_at=09-08、effective_day=09-04 的迟到退回 m5
        result = publish_build(
            base_inputs,
            build_ledger(base_inputs),
            self.out,
        )
        loc_changes = [
            a for a in result.affected
            if a["table"] == "daily_location"
            and a["key"].get("location") == "B"
        ]
        changed_days = {a["key"]["effective_day"] for a in loc_changes}
        # 09-04 的 B 日结被迟到退回改写；09-03 及更早不受影响
        self.assertIn("2026-09-04", changed_days)
        self.assertNotIn("2026-09-03", changed_days)
        # 字段级 diff 给出新旧值：在库 1 -> 2
        day4 = next(
            a for a in loc_changes
            if a["key"]["effective_day"] == "2026-09-04"
        )
        self.assertEqual(day4["change"], "changed")
        self.assertEqual(day4["fields"]["onhand_eod"]["old"], "1")
        self.assertEqual(day4["fields"]["onhand_eod"]["new"], "2")

    def test_quarantine_rows_land_in_report_and_legal_snapshot_published(self):
        result = publish_build(
            [FIX / "illegal_cases.csv"],
            build_ledger([FIX / "illegal_cases.csv"]),
            self.out,
        )
        with (result.build_dir / "quarantine.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            rows = list(csv.DictReader(stream))
        reasons = {r["event_id"]: r["reason"] for r in rows}
        self.assertIn("i3", reasons)  # 超收
        self.assertIn("c1", reasons)  # 循环冲回
        self.assertIn("d1", reasons)  # 内容冲突
        # 隔离不阻断发布，合法快照仍带 _SUCCESS
        self.assertTrue((result.build_dir / SUCCESS_MARKER).exists())
        with (result.build_dir / "pending.csv").open(
            encoding="utf-8", newline=""
        ) as stream:
            pending = list(csv.DictReader(stream))
        self.assertEqual({r["event_id"] for r in pending}, {"p1", "p2"})

    def test_failure_leaves_previous_published_version_intact(self):
        first = publish_build(
            [FIX / "segment_a.csv"],
            build_ledger([FIX / "segment_a.csv"]),
            self.out,
        )
        first_id = first.build_id

        # 让发布在写盘后、rename 前失败：模拟 staging 目录创建后磁盘异常
        import src.snapshot as snapshot_mod

        original_rename = snapshot_mod.os.rename
        calls = {"n": 0}

        def flaky_rename(src, dst):
            if str(src).startswith(str(self.out / f".staging-")) and calls["n"] == 0:
                calls["n"] += 1
                raise OSError("simulated disk failure")
            return original_rename(src, dst)

        snapshot_mod.os.rename = flaky_rename
        try:
            with self.assertRaises(OSError):
                publish_build(
                    [FIX / "segment_a.csv", FIX / "segment_b.csv"],
                    build_ledger(
                        [FIX / "segment_a.csv", FIX / "segment_b.csv"]
                    ),
                    self.out,
                )
        finally:
            snapshot_mod.os.rename = original_rename

        # latest 仍指向旧的完整版本，暂存目录已清理，读者看不到半成品
        self.assertEqual((self.out / LATEST_LINK).resolve(), first.build_dir.resolve())
        self.assertTrue((first.build_dir / SUCCESS_MARKER).exists())
        staging_left = [p for p in self.out.iterdir() if p.name.startswith(".staging-")]
        self.assertEqual(staging_left, [])
        # 恢复后重试可正常发布
        retried = publish_build(
            [FIX / "segment_a.csv", FIX / "segment_b.csv"],
            build_ledger(
                [FIX / "segment_a.csv", FIX / "segment_b.csv"]
            ),
            self.out,
        )
        self.assertNotEqual(retried.build_id, first_id)

    def test_restart_from_scratch_reproduces_byte_identical_snapshot(self):
        inputs = [FIX / "segment_a.csv", FIX / "segment_b.csv"]
        r1 = publish_build(inputs, build_ledger(inputs), self.out)
        with tempfile.TemporaryDirectory() as other:
            out2 = Path(other) / "out2"
            r2 = publish_build(inputs, build_ledger(inputs), out2)
            self.assertEqual(r1.build_id, r2.build_id)
            for name in ("daily_location.csv", "daily_transit.csv",
                         "daily_book.csv", "manifest.json"):
                self.assertEqual(
                    (r1.build_dir / name).read_bytes(),
                    (r2.build_dir / name).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
