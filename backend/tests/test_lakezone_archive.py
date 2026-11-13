"""湖区分区归档规则的回归测试：引用保护、逐级流转、重复归档与留档。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import unittest

from app.seed import SEED_ROWS
from app.services.backfill import BackfillService
from app.services.lakezone import LakezoneService
from app.store import store

SAMPLE_ID = 4
SAMPLE_CODE = "LAKE-2025-1231"
SAMPLE_TABLE = "dwd_order_detail"


def reset_store() -> None:
    store._tables = {name: [copy.deepcopy(row) for row in rows] for name, rows in SEED_ROWS.items()}


class LakezoneArchiveTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.service = LakezoneService()
        self.backfill = BackfillService()

    def _actions(self, entry_id: int, action: str):
        return self.service.execute_action(entry_id, action)

    def test_blocked_by_unfinished_backfill_only(self) -> None:
        outcome = self._actions(SAMPLE_ID, "申请归档")
        self.assertFalse(outcome.ok)
        self.assertEqual([item["code"] for item in outcome.blocking], ["BACK-2025-1231"])
        self.assertEqual({item["module"] for item in outcome.dependencies}, {"snapshot", "gold"})
        self.assertIn("未完成引用", outcome.message)
        self.assertEqual(store.find("lakezone", SAMPLE_ID)["status"], "已启用")

        # 快照与聚合视图只被标记为依赖，记录本身仍然可读取（状态不变）。
        snapshot = next(row for row in store.rows("snapshot") if row["快照编号"] == "SNAP-2025-1231")
        gold = next(row for row in store.rows("gold") if row["视图编号"] == "GOLD-0004")
        self.assertEqual(snapshot["status"], "已生效")
        self.assertEqual(gold["status"], "已停用")
        self.assertTrue(snapshot["归档依赖"])
        self.assertTrue(gold["归档依赖"])

    def test_direct_disable_alias_is_protected(self) -> None:
        outcome = self._actions(SAMPLE_ID, "停用方案")
        self.assertFalse(outcome.ok)
        self.assertEqual([item["code"] for item in outcome.blocking], ["BACK-2025-1231"])

    def test_archive_flow_after_backfill_completed(self) -> None:
        self.backfill.run_action(4, "归档结果")
        applied = self._actions(SAMPLE_ID, "申请归档")
        self.assertTrue(applied.ok)
        self.assertEqual(applied.entry["status"], "待归档")

        confirmed = self._actions(SAMPLE_ID, "确认归档")
        self.assertTrue(confirmed.ok)
        self.assertEqual(confirmed.entry["status"], "已归档")

        effects = confirmed.details["effects"]
        self.assertEqual(effects["catalog"]["编目编号"], f"CATA-ARCH-{SAMPLE_CODE}")
        self.assertEqual(effects["lineage"]["节点编号"], f"LINE-ARCH-{SAMPLE_CODE}")
        self.assertEqual(effects["quality"]["指标编号"], f"QUAL-ARCH-{SAMPLE_CODE}")
        self.assertEqual(effects["snapshots"]["归档保留天数"], 30)

        catalog = next(row for row in store.rows("catalog") if row.get("关联方案编号") == SAMPLE_CODE)
        self.assertEqual(catalog["保留天数"], 30)
        quality = next(row for row in store.rows("quality") if row.get("关联方案编号") == SAMPLE_CODE)
        self.assertEqual(quality["归档保留天数"], 30)
        snapshot = next(row for row in store.rows("snapshot") if row["快照编号"] == "SNAP-2025-1231")
        self.assertEqual(snapshot["归档保留天数"], 30)
        self.assertEqual(snapshot["归档状态"], "已归档")

        history = store.find("lakezone", SAMPLE_ID)["方案历史"]
        self.assertEqual([item["状态"] for item in history], ["待归档", "已归档"])
        self.assertEqual([item["保留天数"] for item in history], [30, 30])

    def test_repeat_archive_takes_effect_once(self) -> None:
        self.backfill.run_action(4, "归档结果")
        self.assertTrue(self._actions(SAMPLE_ID, "申请归档").ok)
        self.assertTrue(self._actions(SAMPLE_ID, "确认归档").ok)
        entry = store.find("lakezone", SAMPLE_ID)
        first_count = entry["归档生效次数"]

        repeat = self._actions(SAMPLE_ID, "再次归档")
        self.assertTrue(repeat.ok)
        self.assertFalse(repeat.changed)
        self.assertEqual(store.find("lakezone", SAMPLE_ID)["归档生效次数"], first_count)
        self.assertEqual(len([r for r in store.rows("catalog") if r.get("关联方案编号") == SAMPLE_CODE]), 1)

    def test_status_moves_one_step_only(self) -> None:
        # 已启用不能直接确认归档；待归档不能直接恢复成已启用。
        direct = self._actions(SAMPLE_ID, "确认归档")
        self.assertFalse(direct.ok)
        self.assertEqual(store.find("lakezone", SAMPLE_ID)["status"], "已启用")

        self.backfill.run_action(4, "归档结果")
        self.assertTrue(self._actions(SAMPLE_ID, "申请归档").ok)
        skipped_restore = self._actions(SAMPLE_ID, "恢复方案")
        self.assertTrue(skipped_restore.ok)
        self.assertEqual(store.find("lakezone", SAMPLE_ID)["status"], "已启用")

        # 归档后恢复只能回到待归档，再回退到已启用。
        self.assertTrue(self._actions(SAMPLE_ID, "申请归档").ok)
        self.assertTrue(self._actions(SAMPLE_ID, "确认归档").ok)
        restore = self._actions(SAMPLE_ID, "恢复方案")
        self.assertEqual(restore.entry["status"], "待归档")
        restore_again = self._actions(SAMPLE_ID, "恢复方案")
        self.assertEqual(restore_again.entry["status"], "已启用")

    def test_retention_history_keeps_old_value(self) -> None:
        self.backfill.run_action(4, "归档结果")
        entry, _ = self.service.update_entry(SAMPLE_ID, {"保留天数": 45}, "归档前调整")
        self.assertEqual(entry["保留天数"], 45)
        self.assertTrue(self._actions(SAMPLE_ID, "申请归档").ok)
        self.assertTrue(self._actions(SAMPLE_ID, "确认归档").ok)

        updated, message = self.service.update_entry(SAMPLE_ID, {"保留天数": 60})
        self.assertIsNone(updated)
        self.assertIn("归档", message)
        archive_info = store.find("lakezone", SAMPLE_ID)["归档信息"]
        self.assertEqual(archive_info["保留天数"], 45)
        self.assertEqual(store.find("lakezone", SAMPLE_ID)["归档历史"][0]["保留天数"], 45)

    def test_archive_tasks_filter_by_target_table(self) -> None:
        self.backfill.run_action(4, "归档结果")
        self.assertTrue(self._actions(SAMPLE_ID, "申请归档").ok)
        tasks, total = self.service.archive_tasks(target_table=SAMPLE_TABLE)
        self.assertEqual(total, 1)
        self.assertEqual(tasks[0]["方案编号"], SAMPLE_CODE)
        self.assertEqual(tasks[0]["归档任务状态"], "待归档")

        unfiltered, unfiltered_total = self.service.archive_tasks()
        self.assertGreaterEqual(unfiltered_total, 2)
        self.assertNotEqual({item["目标表名"] for item in unfiltered}, {SAMPLE_TABLE})

        # 目标表筛选不区分大小写，且不会把其他业务域的方案带进来。
        mixed_case, mixed_total = self.service.archive_tasks(target_table="DWD_ORDER_DETAIL")
        self.assertEqual(mixed_total, 1)
        self.assertEqual(mixed_case[0]["方案编号"], SAMPLE_CODE)


if __name__ == "__main__":
    unittest.main()
