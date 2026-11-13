"""湖区分区业务规则：状态流转、引用保护、归档联动与筛选口径都收在这里。

归档状态按 ``已启用 -> 待归档 -> 已归档`` 逐级推进，任何动作都只能跨一级；
快照、回填作业、下游聚合视图会按目标表与分区值匹配到当前方案。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "lakezone"
REQUIRED_FIELDS = ["方案编号", "目标表名", "分区字段"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "待归档", "已归档"]
ARCHIVE_PENDING = "待归档"
ARCHIVED = "已归档"
ARCHIVE_TASK_STATUSES = [ARCHIVE_PENDING, ARCHIVED]
# 回填作业只有“已完成”才不再阻塞归档；失败作业仍意味着分区数据不完整，继续阻塞。
OPEN_BACKFILL_STATUSES = {"草稿", "待执行", "运行中", "已失败"}
NEGATIVE_ACTIONS = ["申请归档", "停用方案"]
ACTION_ALIASES = {
    # 旧页面的“停用方案”统一收口到受引用保护的归档申请，避免绕过检查直接停用。
    "停用方案": "申请归档",
    "归档方案": "确认归档",
}

_PARTITION_DATE_RE = re.compile(r"dt\s*=\s*['\"]?([0-9]{4}-[0-9]{2}-[0-9]{2})", re.IGNORECASE)
_PLAIN_DATE_RE = re.compile(r"(?<![0-9])([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])")


@dataclass
class ActionOutcome:
    """一次动作的完整结果：HTTP 层需要把阻塞项和依赖项原样返回给前端。"""

    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    blocking: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    changed: bool = False
    details: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _table_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _partition_values(*values: Any) -> set[str]:
    """从 ``dt=2025-12-31``、``2025-12-31`` 等写法里取出日期，兼容手工录入。"""
    found: set[str] = set()
    for value in values:
        text = str(value or "")
        found.update(_PARTITION_DATE_RE.findall(text))
        found.update(_PLAIN_DATE_RE.findall(text))
    return found


def _next_id(module: str) -> int:
    return max((int(row.get("id", 0)) for row in store.rows(module)), default=0) + 1


def _row_code(row: dict[str, Any], *fields: str) -> str:
    for name in fields:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return f"#{row.get('id', '')}"


def _append_history(entry: dict[str, Any], event: dict[str, Any]) -> None:
    """方案历史只追加，不覆盖：保留天数以事件发生时的配置为准。"""
    entry.setdefault("方案历史", []).append(event)


class LakezoneService:
    # ------------------------------------------------------------------ 查询
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        target_table: str | None = None,
        partition: str | None = None,
        business_domain: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("方案编号", "")) or keyword in str(row.get("目标表名", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if target_table:
            target = _table_name(target_table)
            rows = [row for row in rows if _table_name(row.get("目标表名")) == target]
        if partition:
            values = _partition_values(partition)
            if values:
                rows = [
                    row
                    for row in rows
                    if values & _partition_values(row.get("分区值"), row.get("分区字段"))
                ]
            else:
                rows = [row for row in rows if partition in str(row.get("分区值", ""))]
        if business_domain:
            rows = [row for row in rows if row.get("业务域") == business_domain]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def archive_tasks(
        self,
        *,
        target_table: str | None = None,
        status: str | None = None,
        business_domain: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """归档任务清单：默认只列待归档/已归档方案，并可按目标表精确筛选。"""
        rows = store.rows(MODULE)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        else:
            rows = [row for row in rows if row.get("status") in ARCHIVE_TASK_STATUSES]
        if target_table:
            target = _table_name(target_table)
            rows = [row for row in rows if _table_name(row.get("目标表名")) == target]
        if business_domain:
            rows = [row for row in rows if row.get("业务域") == business_domain]
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("方案编号", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        items: list[dict[str, Any]] = []
        for row in rows[start:start + size]:
            item = dict(row)
            archive_info = row.get("归档信息") or {}
            item["归档任务状态"] = row.get("status")
            item["归档时间"] = archive_info.get("归档时间")
            item["归档保留天数"] = archive_info.get("保留天数", row.get("保留天数"))
            item["归档目标表名"] = archive_info.get("目标表名", row.get("目标表名"))
            items.append(item)
        return items, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_partition(self, partition: str, target_table: str | None = None) -> list[dict[str, Any]]:
        values = _partition_values(partition)
        rows = store.rows(MODULE)
        if target_table:
            target = _table_name(target_table)
            rows = [row for row in rows if _table_name(row.get("目标表名")) == target]
        if values:
            rows = [
                row for row in rows if values & _partition_values(row.get("分区值"), row.get("分区字段"))
            ]
        return rows

    # ------------------------------------------------------------------ 登记 / 修改
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field_name in ("分区值", "分区粒度", "保留天数", "冷热分层", "归档路径", "小文件合并", "业务域", "负责人"):
            if values.get(field_name) not in (None, ""):
                entry[field_name] = values.get(field_name)
        entry.setdefault("方案历史", [])
        entry["归档信息"] = {}
        entry["归档生效次数"] = 0
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def update_entry(
        self, entry_id: int, values: dict[str, Any], remark: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"分区方案 {entry_id} 不存在或已归档"
        if entry.get("status") in (ARCHIVE_PENDING, ARCHIVED):
            return None, "方案已进入归档流程，配置只能在恢复为已启用后再调整"

        allowed = {"分区值", "分区粒度", "保留天数", "冷热分层", "归档路径", "小文件合并", "业务域", "负责人", "目标表名"}
        touched = [field_name for field_name in allowed if field_name in values]
        if not touched:
            return None, "没有可更新的配置字段"

        before_retention = entry.get("保留天数")
        for field_name in touched:
            entry[field_name] = values[field_name]
        if "保留天数" in touched and before_retention != entry.get("保留天数"):
            _append_history(
                entry,
                {
                    "动作": "调整保留天数",
                    "状态": entry.get("status"),
                    "保留天数": entry.get("保留天数"),
                    "原保留天数": before_retention,
                    "发生时间": _now(),
                    "说明": remark or "归档前调整保留配置",
                },
            )
        elif remark:
            _append_history(
                entry,
                {
                    "动作": "调整配置",
                    "状态": entry.get("status"),
                    "保留天数": entry.get("保留天数"),
                    "发生时间": _now(),
                    "说明": remark,
                },
            )
        return entry, ""

    # ------------------------------------------------------------------ 动作入口
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容原有调用方：失败时 entry 为 None，成功时返回最新记录。"""
        outcome = self.execute_action(entry_id, action)
        return (outcome.entry if outcome.ok else None), outcome.message

    def execute_action(self, entry_id: int, action: str) -> ActionOutcome:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return ActionOutcome(False, f"分区方案 {entry_id} 不存在或已归档")

        raw_action = str(action or "").strip()
        action = ACTION_ALIASES.get(raw_action, raw_action)

        if action == "申请归档" or raw_action == "再次归档":
            # 处于待归档时，“再次归档”等同于确认归档；仍按相邻状态流转，不跳级。
            if raw_action == "再次归档" and entry.get("status") == ARCHIVE_PENDING:
                return self._confirm_archive(entry, raw_action)
            return self._apply_archive(entry, raw_action)
        if action == "确认归档":
            return self._confirm_archive(entry, raw_action)
        if action == "恢复方案":
            return self._restore(entry, raw_action)
        return self._flow_action(entry, raw_action)

    def _flow_action(self, entry: dict[str, Any], action: str) -> ActionOutcome:
        rules = {
            "提交校验": ("草稿", "待校验"),
            "启用方案": ("待校验", "已启用"),
        }
        if action not in rules:
            return ActionOutcome(False, f"动作「{action}」不属于湖区分区可执行范围")
        source, target = rules[action]
        current = entry.get("status")
        if current != source:
            if current in (ARCHIVE_PENDING, ARCHIVED):
                return ActionOutcome(
                    False,
                    f"方案处于「{current}」，只能按顺序恢复或继续归档，不允许执行「{action}」跳级",
                    entry=entry,
                )
            return ActionOutcome(False, f"动作「{action}」要求当前状态为「{source}」，实际为「{current}」", entry=entry)
        if abs(STATUS_ORDER.index(target) - STATUS_ORDER.index(current)) != 1:
            return ActionOutcome(False, "状态只能按顺序逐级流转，不允许跳级", entry=entry)

        entry["status"] = target
        entry["pending"] = target != ARCHIVED
        entry["abnormal"] = False
        _append_history(
            entry,
            {
                "动作": action,
                "状态": target,
                "保留天数": entry.get("保留天数"),
                "发生时间": _now(),
                "说明": f"{source} -> {target}",
            },
        )
        return ActionOutcome(True, f"分区方案已{action}", entry=entry, changed=True)

    # ------------------------------------------------------------------ 引用检查
    def reference_report(self, entry_or_id: dict[str, Any] | int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_or_id) if isinstance(entry_or_id, int) else entry_or_id
        if entry is None:
            return None
        return self._build_reference_report(entry, mark_dependencies=False)

    def _matches(
        self, entry: dict[str, Any], row: dict[str, Any], table_fields: tuple[str, ...]
    ) -> bool:
        target = _table_name(entry.get("目标表名"))
        row_targets = {_table_name(row.get(field_name)) for field_name in table_fields}
        if target not in row_targets:
            return False

        scheme_values = _partition_values(entry.get("分区值"), entry.get("分区字段"))
        row_values = _partition_values(
            row.get("分区值"), row.get("分区范围"), row.get("分区字段"), row.get("快照时间")
        )
        # 任一侧没有明确分区值时按目标表匹配，避免因样例字段缺失漏掉依赖。
        if not scheme_values or not row_values:
            return True
        return bool(scheme_values & row_values)

    def _build_reference_report(
        self, entry: dict[str, Any], *, mark_dependencies: bool
    ) -> dict[str, Any]:
        target = str(entry.get("目标表名") or "")
        partition = str(entry.get("分区值") or entry.get("分区字段") or "")
        blocking: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []

        for row in store.rows("backfill"):
            if row.get("status") not in OPEN_BACKFILL_STATUSES:
                continue
            if not self._matches(entry, row, ("目标表名",)):
                continue
            blocking.append(
                {
                    "module": "backfill",
                    "type": "回填作业",
                    "id": row.get("id"),
                    "code": _row_code(row, "作业编号"),
                    "name": row.get("目标表名"),
                    "status": row.get("status"),
                    "blocking": True,
                    "reason": f"回填作业未完成（{row.get('status')}），仍引用分区 {partition or target}",
                }
            )

        for row in store.rows("snapshot"):
            if row.get("status") != "已生效":
                continue
            if not self._matches(entry, row, ("目标表名",)):
                continue
            dependencies.append(
                {
                    "module": "snapshot",
                    "type": "版本快照",
                    "id": row.get("id"),
                    "code": _row_code(row, "快照编号"),
                    "name": row.get("目标表名"),
                    "status": row.get("status"),
                    "blocking": False,
                    "reason": "已生效快照仍可读取，仅登记为归档依赖",
                }
            )
            if mark_dependencies:
                row["归档依赖"] = True
                row["关联方案编号"] = entry.get("方案编号")
                row["依赖状态"] = entry.get("status")
                row["归档目标表名"] = entry.get("目标表名")

        for row in store.rows("gold"):
            if not self._matches(entry, row, ("关联目标表", "目标表名", "上游清洗表")):
                continue
            dependencies.append(
                {
                    "module": "gold",
                    "type": "聚合视图",
                    "id": row.get("id"),
                    "code": _row_code(row, "视图编号"),
                    "name": row.get("关联目标表") or row.get("上游清洗表"),
                    "status": row.get("status"),
                    "blocking": False,
                    "reason": "下游聚合视图仍可读取，仅登记为归档依赖",
                }
            )
            if mark_dependencies:
                row["归档依赖"] = True
                row["关联方案编号"] = entry.get("方案编号")
                row["依赖状态"] = entry.get("status")
                row["归档目标表名"] = entry.get("目标表名")

        return {
            "allowed": not blocking,
            "target_table": target,
            "partition": partition,
            "blocking": blocking,
            "dependencies": dependencies,
            "checked_at": _now(),
        }

    # ------------------------------------------------------------------ 归档申请
    def _apply_archive(self, entry: dict[str, Any], action_label: str) -> ActionOutcome:
        current = entry.get("status")
        if current == ARCHIVE_PENDING:
            report = entry.get("引用检查") or self._build_reference_report(entry, mark_dependencies=True)
            return ActionOutcome(
                True,
                "方案已在待归档，本次申请未重复生效",
                entry=entry,
                blocking=[],
                dependencies=report.get("dependencies", []),
                changed=False,
                details={"reference": report},
            )
        if current == ARCHIVED:
            return ActionOutcome(
                True,
                "方案已归档，本次重复归档未再次生效；如需重新归档请先恢复",
                entry=entry,
                changed=False,
                details={"归档信息": entry.get("归档信息", {})},
            )
        if current != "已启用":
            return ActionOutcome(
                False,
                f"申请归档要求当前状态为「已启用」，实际为「{current}」",
                entry=entry,
            )

        report = self._build_reference_report(entry, mark_dependencies=True)
        entry["引用检查"] = report
        retention = entry.get("保留天数")
        if report["blocking"]:
            blockers = report["blocking"]
            codes = "、".join(f"{item['type']} {item['code']}（{item['status']}）" for item in blockers)
            _append_history(
                entry,
                {
                    "动作": action_label,
                    "状态": current,
                    "结果": "被阻塞",
                    "保留天数": retention,
                    "发生时间": _now(),
                    "说明": f"存在未完成引用：{codes}",
                    "阻塞对象": [item.get("code") for item in blockers],
                },
            )
            return ActionOutcome(
                False,
                f"存在未完成引用，拒绝申请归档（拒绝停用）：{codes}；快照与聚合视图仅标记为依赖，仍可读取",
                entry=entry,
                blocking=blockers,
                dependencies=report["dependencies"],
                changed=False,
                details={"reference": report},
            )

        entry["status"] = ARCHIVE_PENDING
        entry["pending"] = True
        entry["abnormal"] = False
        entry.setdefault("归档信息", {})
        entry["归档信息"].update(
            {
                "归档状态": ARCHIVE_PENDING,
                "目标表名": entry.get("目标表名"),
                "分区值": entry.get("分区值"),
                "申请时间": _now(),
            }
        )
        _append_history(
            entry,
            {
                "动作": action_label,
                "状态": ARCHIVE_PENDING,
                "结果": "成功",
                "保留天数": retention,
                "发生时间": _now(),
                "说明": "引用检查通过，进入待归档",
            },
        )
        return ActionOutcome(
            True,
            "分区方案已申请归档，状态进入待归档",
            entry=entry,
            dependencies=report["dependencies"],
            changed=True,
            details={"reference": report},
        )

    # ------------------------------------------------------------------ 归档确认
    def _confirm_archive(self, entry: dict[str, Any], action_label: str = "确认归档") -> ActionOutcome:
        current = entry.get("status")
        if current == ARCHIVED:
            return ActionOutcome(
                True,
                "方案已归档，本次未重复生效",
                entry=entry,
                changed=False,
                details={"归档信息": entry.get("归档信息", {})},
            )
        if current != ARCHIVE_PENDING:
            return ActionOutcome(
                False,
                f"确认归档要求当前状态为「{ARCHIVE_PENDING}」，实际为「{current}」，不允许跳级归档",
                entry=entry,
            )

        retention = entry.get("保留天数")
        now = _now()
        entry["status"] = ARCHIVED
        entry["pending"] = False
        entry["abnormal"] = False
        effects = self._sync_business_modules(entry, retention, now)

        times = int(entry.get("归档生效次数", 0)) + 1
        entry["归档生效次数"] = times
        archive_info = entry.setdefault("归档信息", {})
        archive_info.update(
            {
                "归档状态": ARCHIVED,
                "归档时间": now,
                "保留天数": retention,
                "目标表名": entry.get("目标表名"),
                "分区值": entry.get("分区值"),
                "归档路径": entry.get("归档路径"),
                "归档生效次数": times,
                "联动": effects,
            }
        )
        entry.setdefault("归档历史", []).append(
            {
                "归档时间": now,
                "保留天数": retention,
                "归档前状态": ARCHIVE_PENDING,
                "归档后状态": ARCHIVED,
                "目录编号": effects["catalog"]["编目编号"],
                "血缘节点编号": effects["lineage"]["节点编号"],
                "质量指标编号": effects["quality"]["指标编号"],
            }
        )
        _append_history(
            entry,
            {
                "动作": action_label,
                "状态": ARCHIVED,
                "结果": "成功",
                "保留天数": retention,
                "发生时间": now,
                "说明": "归档完成，目录、血缘、质量监控已同步",
            },
        )
        return ActionOutcome(
            True,
            f"分区方案已归档，保留天数 {retention} 天已留档，目录、血缘与质量监控已同步",
            entry=entry,
            changed=True,
            details={"effects": effects, "归档信息": archive_info},
        )

    def _sync_business_modules(
        self, entry: dict[str, Any], retention: Any, now: str
    ) -> dict[str, dict[str, Any]]:
        scheme_code = str(entry.get("方案编号") or "")
        target = str(entry.get("目标表名") or "")
        partition = str(entry.get("分区值") or "")
        archive_path = str(entry.get("归档路径") or target)
        business_domain = str(entry.get("业务域") or "")

        effects: dict[str, dict[str, Any]] = {}

        # 1. 数据编目：按关联方案编号去重，重复归档只更新同一条目录。
        catalog = self._find_linked("catalog", scheme_code)
        if catalog is None:
            catalog = {
                "id": _next_id("catalog"),
                "status": "已发布",
                "pending": False,
                "abnormal": False,
                "编目编号": f"CATA-ARCH-{scheme_code}",
            }
            store.rows("catalog").append(catalog)
        catalog.update(
            {
                "数据集名称": target,
                "存储路径": archive_path,
                "文件格式": "parquet",
                "所属分层": "归档层",
                "负责人": entry.get("负责人") or "数据平台",
                "业务域标签": business_domain or "归档域",
                "编目版本": f"归档-{entry.get('归档生效次数', 0) + 1}",
                "关联方案编号": scheme_code,
                "归档分区": partition,
                "归档时间": now,
                "保留天数": retention,
                "status": "已发布",
                "pending": False,
                "abnormal": False,
            }
        )
        effects["catalog"] = {"id": catalog["id"], "编目编号": catalog["编目编号"]}

        # 2. 血缘图谱：归档分区作为节点，挂上仍引用它的聚合视图。
        downstream = [
            _row_code(row, "视图编号")
            for row in store.rows("gold")
            if self._matches(entry, row, ("关联目标表", "目标表名", "上游清洗表"))
        ]
        lineage = self._find_linked("lineage", scheme_code)
        if lineage is None:
            lineage = {
                "id": _next_id("lineage"),
                "status": "已启用",
                "pending": False,
                "abnormal": False,
                "节点编号": f"LINE-ARCH-{scheme_code}",
            }
            store.rows("lineage").append(lineage)
        lineage.update(
            {
                "节点类型": "湖区分区归档",
                "节点名称": target,
                "上游节点列表": [],
                "下游节点列表": downstream,
                "转换说明": f"归档分区 {partition}，保留 {retention} 天",
                "采集时间": now,
                "图谱版本": f"归档-{scheme_code}",
                "关联方案编号": scheme_code,
                "归档时间": now,
                "status": "已启用",
                "pending": False,
                "abnormal": False,
            }
        )
        effects["lineage"] = {"id": lineage["id"], "节点编号": lineage["节点编号"]}

        # 3. 质量监控：为归档分区建立完整性指标并同步归档保留天数。
        quality = self._find_linked("quality", scheme_code)
        if quality is None:
            quality = {
                "id": _next_id("quality"),
                "status": "已启用",
                "pending": False,
                "abnormal": False,
                "指标编号": f"QUAL-ARCH-{scheme_code}",
            }
            store.rows("quality").append(quality)
        quality.update(
            {
                "监控对象": target,
                "指标类型": "归档完整性",
                "阈值范围": "缺失分区=0",
                "检测周期": "每日",
                "告警级别": "中",
                "通知渠道": "数据平台",
                "恢复策略": "补回填后复核",
                "关联方案编号": scheme_code,
                "归档分区": partition,
                "归档保留天数": retention,
                "归档时间": now,
                "status": "已启用",
                "pending": False,
                "abnormal": False,
            }
        )
        effects["quality"] = {"id": quality["id"], "指标编号": quality["指标编号"]}

        # 4. 快照业务面清单同步：保留天数以本次归档时的配置为准。
        snapshot_ids: list[int] = []
        for row in store.rows("snapshot"):
            if not self._matches(entry, row, ("目标表名",)):
                continue
            row["关联方案编号"] = scheme_code
            row["归档目标表名"] = target
            row["归档保留天数"] = retention
            row["归档状态"] = ARCHIVED
            row["归档时间"] = now
            row["过期策略"] = f"归档保留{retention}天"
            row["归档依赖"] = True
            row["依赖状态"] = ARCHIVED
            snapshot_ids.append(int(row.get("id", 0)))
        effects["snapshots"] = {"ids": snapshot_ids, "归档保留天数": retention}

        gold_ids: list[int] = []
        for row in store.rows("gold"):
            if not self._matches(entry, row, ("关联目标表", "目标表名", "上游清洗表")):
                continue
            row["关联方案编号"] = scheme_code
            row["归档目标表名"] = target
            row["归档状态"] = ARCHIVED
            row["归档保留天数"] = retention
            row["归档依赖"] = True
            row["依赖状态"] = ARCHIVED
            gold_ids.append(int(row.get("id", 0)))
        effects["gold"] = {"ids": gold_ids}

        return effects

    def _find_linked(self, module: str, scheme_code: str) -> dict[str, Any] | None:
        for row in store.rows(module):
            if row.get("关联方案编号") == scheme_code:
                return row
        return None

    # ------------------------------------------------------------------ 恢复
    def _restore(self, entry: dict[str, Any], action_label: str) -> ActionOutcome:
        current = entry.get("status")
        if current == ARCHIVED:
            target = ARCHIVE_PENDING
        elif current == ARCHIVE_PENDING:
            target = "已启用"
        else:
            return ActionOutcome(
                False,
                f"恢复方案只适用于「{ARCHIVE_PENDING}」或「{ARCHIVED}」，当前状态为「{current}」",
                entry=entry,
            )
        if abs(STATUS_ORDER.index(target) - STATUS_ORDER.index(current)) != 1:
            return ActionOutcome(False, "恢复只能回退到相邻状态，不允许跳级", entry=entry)

        entry["status"] = target
        entry["pending"] = target != ARCHIVED
        entry["abnormal"] = False
        archive_info = entry.setdefault("归档信息", {})
        archive_info["归档状态"] = target
        archive_info["恢复时间"] = _now()
        _append_history(
            entry,
            {
                "动作": action_label,
                "状态": target,
                "结果": "成功",
                "保留天数": entry.get("保留天数"),
                "发生时间": _now(),
                "说明": f"{current} -> {target}，逐级恢复",
            },
        )
        return ActionOutcome(
            True,
            f"分区方案已恢复，状态按顺序回退至「{target}」",
            entry=entry,
            changed=True,
            details={"归档信息": archive_info},
        )
