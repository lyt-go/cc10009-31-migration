"""增量标记接口：维护增量水位，并承载旧版字符串水位迁移到带类型+回退快照的新版。

迁移相关入口都收在 /api/incrementmark/migration/* 下，避免和单条水位的 CRUD 混在一起。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.incrementmark import (
    LEGACY_VERSION,
    MIGRATION_STATUSES,
    SCHEMA_VERSION,
    SUPPORTED_VERSIONS,
    IncrementmarkService,
)

router = APIRouter(prefix="/api/incrementmark", tags=["增量标记"])

service = IncrementmarkService()

LIST_FIELDS = [
    "水位编号",
    "目标源",
    "增量字段名",
    "当前水位值",
    "水位类型",
    "水位版本",
    "迁移状态",
    "原字符串水位",
    "迁移版本号",
    "迁移时间",
    "回退水位",
]
STATUSES = ["草稿", "标记中", "已确认", "已过期"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按水位编号检索"),
    status: str | None = Query(default=None, description="草稿、标记中、已确认、已过期"),
    migration_status: str | None = Query(default=None, description="未迁移、迁移中、已迁移、待人工处理、迁移失败"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按水位编号、状态与迁移状态过滤增量标记列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if migration_status and migration_status not in MIGRATION_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"迁移状态必须是：{'、'.join(MIGRATION_STATUSES)}"
        )
    items, total = service.list_entries(
        keyword=keyword, status=status, migration_status=migration_status, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 迁移核对
@router.get("/migration/preflight")
def migration_preflight() -> dict[str, Any]:
    """迁移前核对依赖、数据库环境、部署版本与本地开发数据兼容性。"""
    return service.preflight()


@router.get("/migration/preview")
def migration_preview(
    entry_id: int | None = Query(default=None, description="指定条目；不传则预览全部")
) -> dict[str, Any]:
    """迁移预览：逐项给出识别类型与转换结果，不写入任何数据。"""
    if entry_id is not None:
        result, message = service.preview_entry(entry_id)
        if result is None:
            raise HTTPException(status_code=404, detail=message)
        return {"items": [result]}
    return service.migrate_all(dry_run=True)


@router.post("/migration/run", response_model=ActionResult)
def migration_run(payload: EntryPayload) -> ActionResult:
    """执行迁移；dry_run=true 时只预览。传入 entry_ids 可只处理指定条目。

    已迁移条目会被跳过（不生成第二个版本），中断后重跑只继续未完成条目。
    """
    dry_run = bool(payload.values.get("dry_run", False))
    limit = payload.values.get("limit")
    entry_ids = payload.values.get("entry_ids")
    if entry_ids:
        results = []
        for raw_id in entry_ids:
            try:
                entry_id = int(raw_id)
            except (TypeError, ValueError):
                return ActionResult(ok=False, message=f"条目 ID「{raw_id}」不是整数")
            if dry_run:
                result, message = service.preview_entry(entry_id)
                if result is None:
                    return ActionResult(ok=False, message=message)
                results.append(result)
            else:
                entry, message = service.migrate_entry(entry_id)
                if entry is None:
                    return ActionResult(ok=False, message=message)
                results.append(entry)
        return ActionResult(
            ok=True,
            message="迁移预览完成" if dry_run else "指定条目迁移完成",
            entry={"items": results, "dry_run": dry_run},
        )
    summary = service.migrate_all(dry_run=dry_run, limit=int(limit) if limit else None)
    if not summary.get("ok"):
        return ActionResult(ok=False, message=str(summary.get("message")), entry=summary)
    return ActionResult(ok=True, message=str(summary.get("message")), entry=summary)


@router.post("/{entry_id}/migration", response_model=ActionResult)
def migrate_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """迁移单条水位。类型识别不了时进入待人工处理清单，不会猜类型继续推进。"""
    value_type = str(payload.values.get("水位类型") or "").strip() or None
    entry, message = service.migrate_entry(entry_id, value_type=value_type)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/migration/manual-queue")
def manual_queue() -> dict[str, Any]:
    """待人工处理清单：类型无法自动识别的水位都收在这里等待明确指定类型。"""
    items = service.manual_queue()
    return {"module": "incrementmark", "total": len(items), "items": items}


@router.post("/{entry_id}/manual-confirm", response_model=ActionResult)
def confirm_manual(entry_id: int, payload: EntryPayload) -> ActionResult:
    """人工指定类型后推进迁移；必须带明确的水位类型。"""
    value_type = str(payload.values.get("水位类型") or "").strip()
    entry, message = service.confirm_manual(entry_id, value_type)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ---------------------------------------------------------------- 快照/审计
@router.get("/migration/audit")
def migration_audit() -> dict[str, Any]:
    """迁移审计：逐条对照原字符串水位、转换后水位、采集顺序与迁移时间。"""
    return service.audit_trace()


@router.get("/migration/snapshots")
def list_snapshots() -> dict[str, Any]:
    """可追溯水位快照清单，随水位记录一起保留。"""
    items = service.snapshots()
    return {
        "module": "incrementmark",
        "total": len(items),
        "items": items,
        "versions": service.snapshot_versions(),
        "registry": service.snapshot_registry(),
    }


@router.get("/migration/verify")
def verify_migration() -> dict[str, Any]:
    """逐项核对快照与输入：原字符串水位、转换结果、类型、采集顺序、迁移时间。"""
    return service.verify_migration()


@router.post("/migration/snapshots", response_model=ActionResult)
def register_snapshot(payload: EntryPayload) -> ActionResult:
    """登记一份可追溯水位快照；同一迁移版本号重复提交按幂等只生效一次。"""
    record, message, created = service.register_snapshot(payload.values)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=created, message=message, entry=record)


# ---------------------------------------------------------------- 续采
@router.get("/migration/crawl-manifest")
def crawl_manifest() -> dict[str, Any]:
    """采集执行侧清单：已迁移水位使用新版，未迁移水位以原字符串只读续采。"""
    return service.crawl_manifest()


@router.post("/migration/crawl-sync", response_model=ActionResult)
def crawl_sync(payload: EntryPayload) -> ActionResult:
    """把续采清单回写到采集执行侧（抓取作业），另一业务清单同步变化。"""
    operator = str(payload.values.get("operator") or payload.remark or "值班管理员")
    result = service.crawl_sync(operator)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出增量标记清单：返回当前过滤条件下的全量数据，含迁移状态与回退水位。"""
    items, total = service.list_entries(page=1, size=10000)
    return {
        "module": "incrementmark",
        "total": total,
        "items": items,
        "schema_version": SCHEMA_VERSION,
        "supported_versions": list(SUPPORTED_VERSIONS),
        "legacy_version": LEGACY_VERSION,
    }


# ---------------------------------------------------------------- 单条 CRUD/动作
@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条增量水位明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"增量水位 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条增量水位；迁移期间新写入必须显式声明水位版本（v1 或 v2）。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="增量水位已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条增量水位执行标记水位、确认水位、标记过期；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
