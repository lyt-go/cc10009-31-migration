"""增量水位迁移的服务层回归测试（只用标准库 unittest，便于随仓库直接运行）。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.seed import SEED_ROWS  # noqa: E402
from app.services import incrementmark as migration  # noqa: E402
from app.services.incrementmark import IncrementmarkService  # noqa: E402
from app.services.watermark_types import classify, convert_with_type  # noqa: E402
from app.store import store  # noqa: E402


class IncrementmarkMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        store._tables["incrementmark"] = [dict(row) for row in SEED_ROWS["incrementmark"]]
        store._tables["crawljob"] = [dict(row) for row in SEED_ROWS["crawljob"]]
        migration._SNAPSHOT_HISTORY.clear()
        self.service = IncrementmarkService()

    # ------------------------------------------------------------ 类型识别
    def test_classify_supported_types(self) -> None:
        self.assertEqual(classify("1024")[:2], ("整数", "1024"))
        self.assertEqual(classify("42.75")[:2], ("小数", "42.75"))
        self.assertEqual(classify("2026-09-01")[:2], ("日期", "2026-09-01"))
        self.assertEqual(classify("2026-09-01 00:00:00")[:2], ("日期时间", "2026-09-01T00:00:00"))
        self.assertEqual(classify("2026-09-03T08:30:00Z")[:2], ("日期时间", "2026-09-03T08:30:00Z"))

    def test_classify_unknown_returns_reason(self) -> None:
        value_type, converted, error = classify("cur_9f3a1b")
        self.assertIsNone(value_type)
        self.assertIsNone(converted)
        self.assertIsNotNone(error)
        self.assertIsNotNone(classify("")[2])

    def test_manual_type_conversion_is_strict(self) -> None:
        self.assertEqual(convert_with_type("cur_9f3a1b", "字符串"), ("cur_9f3a1b", None))
        self.assertIsNotNone(convert_with_type("cur_9f3a1b", "整数")[1])

    # ------------------------------------------------------------ 迁移结果
    def test_auto_migration_preserves_snapshot_and_order(self) -> None:
        result = self.service.migrate_all()
        self.assertEqual(result["summary"]["migrated"], 4)
        entry = store.find("incrementmark", 1)
        self.assertEqual(entry["迁移状态"], "已迁移")
        self.assertEqual(entry["水位版本"], "v2")
        self.assertEqual(entry["当前水位值"], "2026-09-01T00:00:00")
        self.assertEqual(entry["回退水位"], "2026-09-01 00:00:00")
        snapshot = entry["回退水位快照"]
        self.assertEqual(snapshot["原字符串水位"], "2026-09-01 00:00:00")
        self.assertEqual(snapshot["采集顺序"], 1)
        self.assertTrue(snapshot["迁移时间"])
        self.assertTrue(result["migrated"][0]["迁移版本号"])

    def test_unknown_type_goes_to_manual_queue(self) -> None:
        self.service.migrate_all()
        manual = {item["水位编号"] for item in self.service.manual_queue()}
        self.assertEqual(manual, {"INCR-0004"})
        entry = store.find("incrementmark", 4)
        self.assertEqual(entry["迁移状态"], "待人工处理")
        self.assertEqual(entry["当前水位值"], "cur_9f3a1b")
        self.assertIsNone(entry["回退水位快照"])

    def test_repeat_migration_does_not_create_second_version(self) -> None:
        self.service.migrate_all()
        before = store.find("incrementmark", 2)["迁移版本号"]
        entry, message = self.service.migrate_entry(2)
        self.assertEqual(entry["迁移版本号"], before)
        self.assertIn("未生成新版本", message)
        self.assertEqual(len(store.rows("incrementmark")), 5)

    def test_interrupted_migration_only_resumes_pending(self) -> None:
        first = self.service.migrate_all(limit=2)
        self.assertEqual(first["summary"]["migrated"], 2)
        done = {row["水位编号"] for row in store.rows("incrementmark") if row["迁移状态"] == "已迁移"}
        second = self.service.migrate_all()
        self.assertEqual(second["summary"]["skipped"], 2)
        self.assertEqual(len(done), 2)
        self.assertEqual(store.find("incrementmark", 1)["当前水位值"], "2026-09-01T00:00:00")

    def test_migrated_record_never_reverts_to_string(self) -> None:
        self.service.migrate_all()
        entry = store.find("incrementmark", 2)
        self.service.migrate_entry(2, value_type="字符串")
        self.assertEqual(entry["水位类型"], "整数")
        self.assertEqual(entry["当前水位值"], "1024")

    def test_manual_confirm_requires_explicit_type(self) -> None:
        self.service.migrate_all()
        entry, message = self.service.confirm_manual(4, "整数")
        self.assertIsNone(entry)
        self.assertIn("不是合法整数", message)
        self.assertEqual(self.service.migration_status(store.find("incrementmark", 4)), "待人工处理")
        entry, _ = self.service.confirm_manual(4, "字符串")
        self.assertEqual(entry["迁移状态"], "已迁移")
        self.assertEqual(entry["回退水位快照"]["来源"], "人工确认")
        self.assertTrue(self.service.verify_migration()["ok"])

    # ------------------------------------------------------------ 快照幂等
    def test_snapshot_registration_is_idempotent(self) -> None:
        payload = {"id": 1, "迁移版本号": "WM-1", "原字符串水位": "1", "转换后水位": "1", "水位类型": "整数"}
        first, _, created_first = self.service.register_snapshot(payload)
        second, message, created_second = self.service.register_snapshot(payload)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.service.snapshot_registry()), 1)
        self.assertIn("幂等", message)

    def test_same_source_versions_are_ordered(self) -> None:
        duplicate = dict(store.find("incrementmark", 1))
        duplicate.update({"id": 99, "水位编号": "INCR-0099", "采集顺序": 9})
        store.rows("incrementmark").append(duplicate)
        self.service.migrate_all()
        conflicts = self.service.audit_trace()["conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["目标源"], "order_db")
        self.assertEqual(len(conflicts[0]["版本先后"]), 2)
        self.assertTrue(conflicts[0]["采用版本"])

    # ------------------------------------------------------------ 续采/同步
    def test_legacy_rows_stay_readonly_during_migration(self) -> None:
        self.service.migrate_entry(1)
        manifest = self.service.crawl_manifest()
        legacy_ids = {item["水位编号"] for item in manifest["legacy_readonly"]}
        self.assertIn("INCR-0002", legacy_ids)
        legacy = next(item for item in manifest["legacy_readonly"] if item["水位编号"] == "INCR-0002")
        self.assertTrue(legacy["只读"])
        self.assertEqual(legacy["水位版本"], "v1")
        self.assertEqual(legacy["当前水位值"], "1024")

    def test_migration_writes_back_to_crawl_manifest(self) -> None:
        self.service.migrate_entry(1)
        job = next(row for row in store.rows("crawljob") if row["作业编号"] == "CRAW-0001")
        self.assertEqual(job["水位版本"], "v2")
        self.assertFalse(job["续采只读"])
        self.assertEqual(job["当前水位值"], "2026-09-01T00:00:00")

    def test_crawl_sync_updates_other_module(self) -> None:
        self.service.migrate_all()
        result = self.service.crawl_sync()
        self.assertEqual(result["changed"], 3)
        self.assertTrue(all(row["水位版本"] == "v2" for row in store.rows("crawljob")))

    # ------------------------------------------------------------ 写入版本/核对
    def test_new_writes_require_explicit_version(self) -> None:
        entry, missing = self.service.create_entry(
            {"水位编号": "INCR-0100", "目标源": "z", "增量字段名": "t", "当前水位值": "3"}
        )
        self.assertIsNone(entry)
        self.assertTrue(missing)

    def test_v2_writes_are_typed_and_not_migrated_again(self) -> None:
        entry, missing = self.service.create_entry(
            {
                "水位编号": "INCR-0101",
                "目标源": "z",
                "增量字段名": "t",
                "当前水位值": "2026-10-01",
                "水位版本": "v2",
                "水位类型": "日期",
            }
        )
        self.assertEqual(missing, [])
        self.assertEqual(entry["迁移状态"], "无需迁移")
        self.assertEqual(entry["原字符串水位"], "2026-10-01")
        skipped, message = self.service.migrate_entry(entry["id"])
        self.assertIn("未生成新版本", message)
        self.assertEqual(skipped["水位类型"], "日期")

    def test_preflight_checks_all_dimensions(self) -> None:
        result = self.service.preflight()
        names = {item["name"] for item in result["checks"]}
        self.assertEqual(names, {"依赖检查", "数据库环境检查", "部署版本检查", "本地开发数据兼容检查"})
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
