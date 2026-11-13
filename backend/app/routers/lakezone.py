"""湖区分区接口：维护分区方案，覆盖提交校验、启用方案、归档申请/确认、恢复等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.lakezone import LakezoneService

router = APIRouter(prefix="/api/lakezone", tags=["湖区分区"])

service = LakezoneService()

LIST_FIELDS = ["方案编号", "目标表名", "分区字段", "分区粒度", "保留天数", "冷热分层", "归档路径", "小文件合并"]
STATUSES = ["草稿", "待校验", "已启用", "待归档", "已归档"]


def _action_result(outcome: Any) -> ActionResult:
    return ActionResult(
        ok=outcome.ok,
        message=outcome.message,
        entry=outcome.entry,
        blocking=outcome.blocking,
        dependencies=outcome.dependencies,
        changed=outcome.changed,
        details=outcome.details,
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按方案编号或目标表名检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、待归档、已归档"),
    target_table: str | None = Query(default=None, description="按目标表名精确筛选"),
    partition: str | None = Query(default=None, description="按分区值筛选，例如 dt=2025-12-31"),
    business_domain: str | None = Query(default=None, description="按业务域筛选"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按方案编号、状态、目标表与分区过滤湖区分区列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        target_table=target_table,
        partition=partition,
        business_domain=business_domain,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/archive-tasks", response_model=PageResult[dict])
def list_archive_tasks(
    target_table: str | None = Query(default=None, description="按目标表筛选，避免混入其他业务域"),
    status: str | None = Query(default=None, description="默认列出待归档与已归档"),
    business_domain: str | None = Query(default=None, description="按业务域筛选"),
    keyword: str | None = Query(default=None, description="按方案编号检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """归档任务清单：默认只列待归档/已归档方案，支持按目标表精确筛选。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.archive_tasks(
        target_table=target_table,
        status=status,
        business_domain=business_domain,
        keyword=keyword,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    target_table: str | None = Query(default=None, description="按目标表名精确筛选"),
) -> dict[str, Any]:
    """导出湖区分区清单：返回当前过滤条件下的全量数据；可按目标表筛选。"""
    items, total = service.list_entries(target_table=target_table, page=1, size=10000)
    return {"module": "lakezone", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条分区方案明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"分区方案 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/references", response_model=dict)
def get_references(entry_id: int) -> dict:
    """申请归档前的引用检查：返回阻塞对象和可读取的依赖对象，不改变任何状态。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"分区方案 {entry_id} 不存在")
    report = service.reference_report(entry_id)
    return report or {}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条分区方案，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="分区方案已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """调整归档前配置（例如保留天数）；进入归档流程后不再允许修改。"""
    entry, message = service.update_entry(entry_id, payload.values, payload.remark)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="分区方案配置已更新", entry=entry, changed=True)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交校验、启用方案、申请归档、确认归档、再次归档、恢复方案。

    存在未完成回填时申请归档会被拒绝并返回阻塞对象；快照与聚合视图只作为依赖返回。
    状态只能逐级流转，重复归档不会二次生效。
    """
    action = str(payload.values.get("action") or "").strip()
    outcome = service.execute_action(entry_id, action)
    return _action_result(outcome)
