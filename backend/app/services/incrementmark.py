"""增量水位业务规则：状态流转、字段校验、筛选口径与旧版水位迁移。

迁移规则（对应需求里的几条硬约束）：
- 每条水位只有一个迁移版本号；重复迁移直接复用既有版本，不再生成第二个。
- 迁移按条目推进并逐条落库：中断后只继续“未迁移”的条目，已转换的记录不会被改回字符串。
- 类型识别不出来的条目进入待人工处理清单，人工显式指定类型并确认后才推进。
- 每次迁移都为该条目保留一份回退水位快照（原字符串值、新值、类型、迁移时间、采集顺序），
  可以逐项对照输入审计；同一源只有历史遗留的重复快照会被合并成一份。
- 迁移期间新批次写入必须显式声明水位版本（v1 旧版或 v2 新版），不替调用方猜版本。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.services.watermark_types import MANUAL_TYPES, classify, convert_with_type, preview
from app.store import store

MODULE = "incrementmark"
REQUIRED_FIELDS = ["水位编号", "目标源", "增量字段名"]
STATUS_ORDER = ["草稿", "标记中", "已确认", "已过期"]
ACTION_RULES = {"标记水位": "标记中", "确认水位": "已确认", "标记过期": "已过期"}
NEGATIVE_ACTIONS = []

SCHEMA_VERSION = settings.watermark_schema_version
LEGACY_VERSION = "v1"
SUPPORTED_VERSIONS = (LEGACY_VERSION, SCHEMA_VERSION)
MIGRATION_STATUSES = ("未迁移", "迁移中", "已迁移", "待人工处理", "迁移失败", "无需迁移")
V2_READY_STATUSES = ("已迁移", "无需迁移")
CRAWL_MODULE = "crawljob"

# 单进程内存仓库下的并发保护：迁移、版本快照登记与续采清单同步共用一把锁。
# 真实部署换成数据库时应替换为行级锁或唯一约束。
_LOCK = threading.RLock()

# 可追溯水位快照的登记历史：只保留首次提交，作为重复提交幂等的判断依据。
_SNAPSHOT_HISTORY: list[dict[str, Any]] = []


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _version_lt(left: str, right: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(piece) for piece in value.split(".") if piece.isdigit())

    return parts(left) < parts(right)


class IncrementmarkService:
    # ------------------------------------------------------------------ 列表/明细
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        migration_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("水位编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if migration_status:
            rows = [row for row in rows if self.migration_status(row) == migration_status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    @staticmethod
    def migration_status(entry: dict[str, Any]) -> str:
        """兼容读取：老数据没有迁移字段时按未迁移处理。"""
        return str(entry.get("迁移状态") or "未迁移")

    @staticmethod
    def watermark_version(entry: dict[str, Any]) -> str:
        return str(entry.get("水位版本") or LEGACY_VERSION)

    # ------------------------------------------------------------------ 登记/流转
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        version = str(values.get("水位版本") or "").strip()
        if version not in SUPPORTED_VERSIONS:
            return None, [f"水位版本必须显式指定为 {' 或 '.join(SUPPORTED_VERSIONS)}"]
        rows = store.rows(MODULE)
        with _LOCK:
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["采集顺序"] = max((int(row.get("采集顺序") or 0) for row in rows), default=0) + 1
            if version == LEGACY_VERSION:
                raw = values.get("当前水位值")
                entry["当前水位值"] = "" if raw is None else str(raw)
                entry["水位版本"] = LEGACY_VERSION
                entry["迁移状态"] = "未迁移"
                entry["原字符串水位"] = entry["当前水位值"]
                entry["水位类型"] = None
                entry["迁移版本号"] = None
                entry["迁移时间"] = None
                entry["回退水位快照"] = None
                entry["人工确认"] = False
                entry["待人工处理原因"] = None
            else:
                value_type = str(values.get("水位类型") or "").strip()
                raw = values.get("当前水位值")
                if value_type not in MANUAL_TYPES:
                    return None, ["新版水位必须声明水位类型（整数、小数、日期、日期时间、字符串）"]
                converted, error = convert_with_type("" if raw is None else str(raw), value_type)
                if error is not None:
                    return None, [error]
                entry["当前水位值"] = converted
                entry["水位版本"] = SCHEMA_VERSION
                entry["水位类型"] = value_type
                entry["迁移状态"] = "无需迁移"
                entry["原字符串水位"] = "" if raw is None else str(raw)
                entry["迁移版本号"] = None
                entry["迁移时间"] = None
                entry["回退水位快照"] = None
                entry["人工确认"] = False
                entry["待人工处理原因"] = None
            rows.append(entry)
            return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量水位 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于增量标记可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"增量水位已{action}"

    # ------------------------------------------------------------------ 迁移核对
    def preflight(self) -> dict[str, Any]:
        """迁移前核对：依赖、数据库环境、部署版本、本地开发数据兼容。"""
        checks: list[dict[str, Any]] = []

        check_engine = {
            "name": "依赖检查",
            "detail": "watermark_types 类型转换模块可用、内存仓库可读写",
        }
        try:
            probe = classify("1")
            rows = store.rows(MODULE)
            engine_ok = probe[0] == "整数" and isinstance(rows, list)
        except Exception as exc:  # pragma: no cover - 仅在环境损坏时触发
            check_engine["detail"] = f"依赖检查异常：{exc}"
            engine_ok = False
        check_engine["ok"] = engine_ok
        checks.append(check_engine)

        check_env = {
            "name": "数据库环境检查",
            "detail": "当前使用进程内内存仓库（APP_ENV=%s），迁移结果随进程生命周期保留" % settings.env,
        }
        check_env["ok"] = settings.env == "local"
        checks.append(check_env)

        check_version = {
            "name": "部署版本检查",
            "detail": "当前部署版本 %s，迁移要求不低于 %s"
            % (settings.app_version, settings.min_migration_version),
        }
        check_version["ok"] = not _version_lt(settings.app_version, settings.min_migration_version)
        checks.append(check_version)

        legacy_rows = [row for row in store.rows(MODULE) if self.watermark_version(row) == LEGACY_VERSION]
        unknown_fields = [
            row.get("水位编号") for row in store.rows(MODULE) if "迁移状态" not in row
        ]
        check_data = {
            "name": "本地开发数据兼容检查",
            "detail": "旧版字符串水位 %d 条，缺少迁移字段的旧记录 %d 条（读取时按未迁移兼容）"
            % (len(legacy_rows), len(unknown_fields)),
        }
        check_data["ok"] = True
        checks.append(check_data)

        return {"ok": all(item["ok"] for item in checks), "version": SCHEMA_VERSION, "checks": checks}

    # ------------------------------------------------------------------ 预览/单条迁移
    def preview_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量水位 {entry_id} 不存在或已归档"
        current = self.migration_status(entry)
        result = preview(entry.get("原字符串水位", entry.get("当前水位值")))
        result.update(
            {
                "id": int(entry.get("id", 0)),
                "水位编号": entry.get("水位编号"),
                "目标源": entry.get("目标源"),
                "迁移状态": current,
                "已迁移": current in V2_READY_STATUSES,
                "迁移版本号": entry.get("迁移版本号"),
                "迁移时间": entry.get("迁移时间"),
                "回退水位快照": entry.get("回退水位快照"),
            }
        )
        return result, ""

    def migrate_entry(
        self, entry_id: int, *, value_type: str | None = None, operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, str]:
        """迁移单条水位。

        - 已迁移：直接返回既有结果，保证同一条目不会生成第二个版本。
        - 识别不了且未显式指定类型：进入待人工处理清单并返回失败说明，不猜类型。
        - 显式指定类型：按指定类型转换，转换失败仍进人工清单。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量水位 {entry_id} 不存在或已归档"
        with _LOCK:
            if self.migration_status(entry) in V2_READY_STATUSES:
                return entry, "该水位已完成迁移，沿用既有迁移版本，未生成新版本"
            if "迁移状态" not in entry:
                entry.setdefault("迁移状态", "未迁移")
            if entry.get("原字符串水位") is None:
                entry["原字符串水位"] = entry.get("当前水位值")
            raw = entry.get("原字符串水位", "")
            entry["迁移状态"] = "迁移中"

            if value_type:
                if value_type not in MANUAL_TYPES:
                    entry["迁移状态"] = "待人工处理"
                    entry["待人工处理原因"] = f"不支持的水位类型「{value_type}」"
                    return None, entry["待人工处理原因"]
                converted, error = convert_with_type(raw, value_type)
                resolved_type = value_type
            else:
                resolved_type, converted, error = classify(raw)

            if error is not None or converted is None:
                entry["迁移状态"] = "待人工处理"
                entry["待人工处理原因"] = error or "水位无法识别"
                entry["abnormal"] = True
                return None, f"水位无法自动识别，已转入待人工处理清单：{entry['待人工处理原因']}"

            self._apply_migration(entry, raw, resolved_type, converted, operator)
            return entry, f"增量水位已迁移为{resolved_type}类型，回退水位快照已保留"

    def confirm_manual(
        self, entry_id: int, value_type: str, *, operator: str = "值班管理员"
    ) -> tuple[dict[str, Any] | None, str]:
        """人工确认类型后推进迁移；确认本身要显式给出类型，不做默认推断。"""
        if value_type not in MANUAL_TYPES:
            return None, f"人工处理必须指定明确类型：{'、'.join(MANUAL_TYPES)}"
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量水位 {entry_id} 不存在或已归档"
        with _LOCK:
            if self.migration_status(entry) in V2_READY_STATUSES:
                return entry, "该水位已完成迁移，人工确认不重复生效"
            entry["人工确认"] = True
            entry["待人工处理原因"] = None
            entry["migrated_from_manual"] = True
            return self.migrate_entry(entry_id, value_type=value_type, operator=operator)

    def _apply_migration(
        self,
        entry: dict[str, Any],
        raw: Any,
        value_type: str,
        converted: str,
        operator: str,
    ) -> None:
        migrated_at = _now()
        entry["迁移版本号"] = f"WM-{int(entry.get('id', 0)):06d}-{SCHEMA_VERSION}"
        entry["迁移时间"] = migrated_at
        entry["水位类型"] = value_type
        entry["水位版本"] = SCHEMA_VERSION
        entry["迁移状态"] = "已迁移"
        entry["abnormal"] = False
        entry["待人工处理原因"] = None
        entry["当前水位值"] = converted
        entry["回退水位"] = "" if raw is None else str(raw)
        entry["回退水位快照"] = {
            "迁移版本号": entry["迁移版本号"],
            "原字符串水位": "" if raw is None else str(raw),
            "转换后水位": converted,
            "水位类型": value_type,
            "采集顺序": entry.get("采集顺序"),
            "迁移时间": migrated_at,
            "操作人": operator,
            "状态快照": entry.get("status"),
            "来源": "人工确认" if entry.pop("migrated_from_manual", False) else "自动识别",
        }
        self._sync_crawl_manifest(entry)

    @staticmethod
    def _sync_crawl_manifest(entry: dict[str, Any]) -> None:
        """迁移落库后立刻回写采集执行侧清单，保证另一业务清单同步变化。"""
        snapshot = entry.get("回退水位快照") if isinstance(entry.get("回退水位快照"), dict) else {}
        for job in store.rows(CRAWL_MODULE):
            if job.get("增量水位编号") == entry.get("水位编号") or str(
                job.get("关联目标源")
            ) == str(entry.get("目标源")):
                job["当前水位值"] = entry.get("当前水位值")
                job["水位版本"] = entry.get("水位版本")
                job["水位类型"] = entry.get("水位类型")
                job["续采只读"] = False
                job["水位同步时间"] = entry.get("迁移时间")
                job["水位同步操作人"] = snapshot.get("操作人")

    # ------------------------------------------------------------------ 批量/中断续做
    def migrate_all(self, *, dry_run: bool = False, limit: int | None = None) -> dict[str, Any]:
        """按采集顺序批量迁移；只处理未迁移与等待人工确认的条目。

        dry_run 只做识别预览，不落库；中断后重新调用会跳过已迁移条目。
        """
        preflight = self.preflight()
        if not preflight["ok"] and not dry_run:
            return {
                "ok": False,
                "dry_run": dry_run,
                "message": "迁移前核对未通过，已停止迁移",
                "preflight": preflight,
                "migrated": [],
                "manual": [],
                "failures": [],
                "skipped": [],
                "summary": {"migrated": 0, "manual": 0, "failures": 0, "skipped": 0},
            }

        with _LOCK:
            rows = sorted(store.rows(MODULE), key=lambda row: int(row.get("采集顺序") or 0))
            migrated: list[dict[str, Any]] = []
            manual: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            failures: list[dict[str, Any]] = []
            for row in rows:
                state = self.migration_status(row)
                if state in V2_READY_STATUSES:
                    skipped.append({"id": row.get("id"), "水位编号": row.get("水位编号"), "原因": "已迁移，沿用既有版本"})
                    continue
                if limit is not None and len(migrated) >= limit:
                    skipped.append({"id": row.get("id"), "水位编号": row.get("水位编号"), "原因": "本轮达到处理上限"})
                    continue
                outcome = preview(row.get("原字符串水位", row.get("当前水位值")))
                if not outcome["可直接迁移"]:
                    if not dry_run:
                        row.setdefault("迁移状态", "未迁移")
                        row["迁移状态"] = "待人工处理"
                        row["待人工处理原因"] = outcome["待人工处理原因"]
                        row["abnormal"] = True
                    manual.append({**outcome, "id": row.get("id"), "水位编号": row.get("水位编号")})
                    continue
                if dry_run:
                    migrated.append({**outcome, "id": row.get("id"), "水位编号": row.get("水位编号")})
                    continue
                entry, message = self.migrate_entry(int(row.get("id", 0)))
                if entry is None:
                    failures.append({"id": row.get("id"), "水位编号": row.get("水位编号"), "原因": message})
                else:
                    migrated.append(
                        {
                            "id": entry.get("id"),
                            "水位编号": entry.get("水位编号"),
                            "原字符串水位": entry.get("原字符串水位"),
                            "转换后水位": entry.get("当前水位值"),
                            "水位类型": entry.get("水位类型"),
                            "迁移版本号": entry.get("迁移版本号"),
                            "迁移时间": entry.get("迁移时间"),
                        }
                    )

        return {
            "ok": True,
            "dry_run": dry_run,
            "message": ("预览完成，未写入任何数据" if dry_run else "增量水位迁移已完成本轮处理"),
            "preflight": preflight,
            "migrated": migrated,
            "manual": manual,
            "failures": failures,
            "skipped": skipped,
            "summary": {
                "migrated": len(migrated),
                "manual": len(manual),
                "failures": len(failures),
                "skipped": len(skipped),
            },
        }

    # ------------------------------------------------------------------ 人工清单
    def manual_queue(self) -> list[dict[str, Any]]:
        return [
            {
                "id": row.get("id"),
                "水位编号": row.get("水位编号"),
                "目标源": row.get("目标源"),
                "增量字段名": row.get("增量字段名"),
                "原字符串水位": row.get("原字符串水位", row.get("当前水位值")),
                "待人工处理原因": row.get("待人工处理原因"),
                "迁移状态": self.migration_status(row),
            }
            for row in store.rows(MODULE)
            if self.migration_status(row) == "待人工处理"
        ]

    # ------------------------------------------------------------------ 快照/审计
    def snapshots(self) -> list[dict[str, Any]]:
        """可追溯水位快照清单：每条已迁移水位一份，按采集顺序排列。"""
        items = []
        for row in store.rows(MODULE):
            snapshot = row.get("回退水位快照")
            if not isinstance(snapshot, dict):
                continue
            items.append({"id": row.get("id"), "水位编号": row.get("水位编号"), **snapshot})
        return sorted(items, key=lambda item: int(item.get("采集顺序") or 0))

    def snapshots_by_source(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in store.rows(MODULE):
            snapshot = row.get("回退水位快照")
            if not isinstance(snapshot, dict):
                continue
            grouped.setdefault(str(row.get("目标源")), []).append({"id": row.get("id"), **snapshot})
        return grouped

    def snapshot_versions(self) -> list[dict[str, Any]]:
        """可追溯水位快照的两个版本视图。

        同一源同时存在原字符串水位（旧版只读依据）与已转换水位（新版续采依据）时，
        把两个版本并列给出，标明采集顺序与迁移时间，便于判断先后关系。
        """
        versions: list[dict[str, Any]] = []
        for source, rows in self.snapshots_by_source().items():
            for row in rows:
                versions.append({"目标源": source, "来源版本": "已转换水位", **row})
        for row in store.rows(MODULE):
            if not isinstance(row.get("回退水位快照"), dict):
                continue
            versions.append(
                {
                    "目标源": row.get("目标源"),
                    "来源版本": "原字符串水位",
                    "id": row.get("id"),
                    "迁移版本号": row.get("迁移版本号"),
                    "水位编号": row.get("水位编号"),
                    "原字符串水位": row.get("回退水位快照", {}).get("原字符串水位"),
                    "转换后水位": None,
                    "水位类型": "字符串",
                    "采集顺序": row.get("采集顺序"),
                    "迁移时间": row.get("回退水位快照", {}).get("迁移时间"),
                }
            )
        return versions

    def register_snapshot(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        """登记一次可追溯水位快照；同一迁移版本号重复提交只生效一次。"""
        entry_id = payload.get("id")
        version_no = str(payload.get("迁移版本号") or "").strip()
        if not version_no:
            return None, "登记快照必须提供迁移版本号", False
        with _LOCK:
            history = _SNAPSHOT_HISTORY
            duplicate = next(
                (item for item in history if str(item.get("迁移版本号")) == version_no), None
            )
            if duplicate is not None:
                return duplicate, "该迁移版本号已登记，按幂等只保留首次结果", False
            entry = store.find(MODULE, int(entry_id)) if str(entry_id or "").isdigit() else None
            record = {
                "id": max((int(item.get("id", 0)) for item in history), default=0) + 1,
                "迁移版本号": version_no,
                "watermark_id": entry.get("id") if entry else entry_id,
                "水位编号": payload.get("水位编号") or (entry or {}).get("水位编号"),
                "原字符串水位": payload.get("原字符串水位"),
                "转换后水位": payload.get("转换后水位"),
                "水位类型": payload.get("水位类型"),
                "迁移时间": payload.get("迁移时间") or _now(),
            }
            history.append(record)
            if entry is not None and not isinstance(entry.get("回退水位快照"), dict):
                entry["回退水位快照"] = {
                    key: record[key]
                    for key in ("迁移版本号", "原字符串水位", "转换后水位", "水位类型", "迁移时间")
                }
            return record, "快照已登记", True

    def snapshot_registry(self) -> list[dict[str, Any]]:
        """快照登记历史（只读），用于核对重复提交是否只生效一次。"""
        with _LOCK:
            return [dict(item) for item in _SNAPSHOT_HISTORY]

    def verify_migration(self) -> dict[str, Any]:
        """逐项核对：每份快照的输入、转换结果、类型、采集顺序与迁移时间是否自洽。"""
        checks: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if self.migration_status(row) not in V2_READY_STATUSES:
                continue
            snapshot = row.get("回退水位快照")
            problems: list[str] = []
            if not isinstance(snapshot, dict):
                if self.migration_status(row) == "已迁移":
                    problems.append("缺少回退水位快照")
                snapshot = {}
            else:
                if snapshot.get("原字符串水位") != row.get("原字符串水位"):
                    problems.append("快照原字符串水位与记录不一致")
                if snapshot.get("转换后水位") != row.get("当前水位值"):
                    problems.append("快照转换后水位与当前水位不一致")
                if snapshot.get("水位类型") != row.get("水位类型"):
                    problems.append("快照水位类型与记录不一致")
                if snapshot.get("采集顺序") != row.get("采集顺序"):
                    problems.append("快照采集顺序与记录不一致")
                if not snapshot.get("迁移时间"):
                    problems.append("快照缺少迁移时间")
                if not snapshot.get("迁移版本号"):
                    problems.append("快照缺少迁移版本号")
                expected_type, expected_value, error = classify(snapshot.get("原字符串水位"))
                if snapshot.get("来源") == "人工确认":
                    expected_value, error = convert_with_type(
                        str(snapshot.get("原字符串水位") or ""), str(row.get("水位类型"))
                    )
                    expected_type = row.get("水位类型")
                if error is not None:
                    problems.append(f"原字符串水位无法复算：{error}")
                elif expected_value != row.get("当前水位值") or expected_type != row.get("水位类型"):
                    problems.append("转换结果无法由原字符串水位复算")
            checks.append(
                {
                    "id": row.get("id"),
                    "水位编号": row.get("水位编号"),
                    "原字符串水位": row.get("原字符串水位"),
                    "转换后水位": row.get("当前水位值"),
                    "水位类型": row.get("水位类型"),
                    "采集顺序": row.get("采集顺序"),
                    "迁移时间": snapshot.get("迁移时间"),
                    "可对照": not problems,
                    "问题": problems,
                }
            )
        return {
            "ok": all(item["可对照"] for item in checks),
            "total": len(checks),
            "items": checks,
        }

    def audit_trace(self) -> dict[str, Any]:
        """迁移审计视图：每条水位都能对照原字符串水位、新值、采集顺序与迁移时间。

        同一源若因历史原因同时存在原字符串水位与已转换水位，这里把两个版本按迁移
        时间的先后列出并标记冲突，续采侧只采用时间更晚的已转换版本。
        """
        traced: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        rows = store.rows(MODULE)
        sources: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            sources.setdefault(str(row.get("目标源")), []).append(row)
        conflict_by_source: dict[str, dict[str, Any]] = {}

        for row in rows:
            if self.migration_status(row) not in V2_READY_STATUSES:
                continue
            snapshot = row.get("回退水位快照") if isinstance(row.get("回退水位快照"), dict) else {}
            trace = {
                "id": row.get("id"),
                "水位编号": row.get("水位编号"),
                "目标源": row.get("目标源"),
                "增量字段名": row.get("增量字段名"),
                "采集顺序": row.get("采集顺序"),
                "原字符串水位": snapshot.get("原字符串水位", row.get("原字符串水位")),
                "转换后水位": row.get("当前水位值"),
                "水位类型": row.get("水位类型"),
                "水位版本": self.watermark_version(row),
                "迁移版本号": row.get("迁移版本号"),
                "迁移时间": snapshot.get("迁移时间", row.get("迁移时间")),
                "快照来源": snapshot.get("来源"),
                "快照": snapshot,
            }
            same_source = sources.get(str(row.get("目标源")), [])
            if len(same_source) > 1:
                source_key = str(row.get("目标源"))
                if source_key not in conflict_by_source:
                    versions = []
                    for candidate in same_source:
                        candidate_snapshot = (
                            candidate.get("回退水位快照")
                            if isinstance(candidate.get("回退水位快照"), dict)
                            else {}
                        )
                        versions.append(
                            {
                                "水位编号": candidate.get("水位编号"),
                                "水位版本": self.watermark_version(candidate),
                                "迁移状态": self.migration_status(candidate),
                                "原字符串水位": candidate.get(
                                    "原字符串水位", candidate.get("当前水位值")
                                ),
                                "转换后水位": (
                                    candidate.get("当前水位值")
                                    if self.migration_status(candidate) in V2_READY_STATUSES
                                    else None
                                ),
                                "采集顺序": candidate.get("采集顺序"),
                                "迁移时间": candidate_snapshot.get(
                                    "迁移时间", candidate.get("迁移时间")
                                ),
                            }
                        )
                    ordered = sorted(
                        versions,
                        key=lambda item: (
                            item.get("迁移时间") is not None,
                            str(item.get("迁移时间") or ""),
                            int(item.get("采集顺序") or 0),
                        ),
                    )
                    adopted = next(
                        (item for item in reversed(ordered) if item.get("水位版本") == SCHEMA_VERSION),
                        ordered[-1],
                    )
                    conflict_by_source[source_key] = {
                        "目标源": row.get("目标源"),
                        "版本先后": [item.get("水位编号") or item.get("水位版本") for item in ordered],
                        "采用版本": adopted.get("水位编号") or adopted.get("水位版本"),
                        "采用迁移版本号": next(
                            (
                                candidate.get("迁移版本号")
                                for candidate in same_source
                                if candidate.get("水位编号") == adopted.get("水位编号")
                            ),
                            None,
                        ),
                        "版本明细": ordered,
                        "说明": "同一源同时存在原字符串水位与已转换水位，续采按迁移时间采用较晚的已转换版本",
                    }
                    conflicts.append(conflict_by_source[source_key])
                trace["快照冲突"] = True
                trace["采用快照版本"] = conflict_by_source[source_key]["采用版本"]
            traced.append(trace)
        return {
            "items": sorted(traced, key=lambda item: int(item.get("采集顺序") or 0)),
            "conflicts": conflicts,
            "snapshots": self.snapshots(),
            "manual": self.manual_queue(),
        }

    # ------------------------------------------------------------------ 续采
    def crawl_manifest(self) -> dict[str, Any]:
        """采集执行侧清单：已迁移的水位进新版，未迁移的仍以原字符串作为只读续采依据。"""
        manifest: list[dict[str, Any]] = []
        legacy_readonly: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            item = {
                "id": row.get("id"),
                "水位编号": row.get("水位编号"),
                "目标源": row.get("目标源"),
                "增量字段名": row.get("增量字段名"),
                "上次采集时间": row.get("上次采集时间"),
                "采集顺序": row.get("采集顺序"),
            }
            if self.migration_status(row) in V2_READY_STATUSES:
                item.update(
                    {
                        "水位版本": self.watermark_version(row),
                        "当前水位值": row.get("当前水位值"),
                        "水位类型": row.get("水位类型"),
                        "迁移版本号": row.get("迁移版本号"),
                        "迁移时间": row.get("迁移时间"),
                        "回退水位": {
                            "原字符串水位": (row.get("回退水位快照") or {}).get("原字符串水位"),
                            "采集顺序": (row.get("回退水位快照") or {}).get("采集顺序"),
                            "迁移时间": (row.get("回退水位快照") or {}).get("迁移时间"),
                        },
                        "回退水位快照": row.get("回退水位快照"),
                        "只读": False,
                    }
                )
                manifest.append(item)
            else:
                item.update(
                    {
                        "水位版本": LEGACY_VERSION,
                        "当前水位值": row.get("原字符串水位", row.get("当前水位值")),
                        "水位类型": "字符串",
                        "只读": True,
                        "迁移状态": self.migration_status(row),
                    }
                )
                legacy_readonly.append(item)
        return {
            "module": MODULE,
            "version": SCHEMA_VERSION,
            "items": manifest,
            "legacy_readonly": legacy_readonly,
            "total": len(manifest) + len(legacy_readonly),
        }

    def crawl_sync(self, operator: str = "值班管理员") -> dict[str, Any]:
        """把续采结果回写采集执行侧清单（crawljob），另一业务清单同步变化。"""
        manifest = self.crawl_manifest()
        with _LOCK:
            jobs = store.rows(CRAWL_MODULE)
            changed = 0
            for item in manifest["items"] + manifest["legacy_readonly"]:
                job = next(
                    (
                        row
                        for row in jobs
                        if row.get("增量水位编号") == item.get("水位编号")
                        or str(row.get("关联目标源")) == str(item.get("目标源"))
                    ),
                    None,
                )
                if job is None:
                    continue
                job["当前水位值"] = item.get("当前水位值")
                job["水位版本"] = item.get("水位版本")
                job["水位类型"] = item.get("水位类型", "字符串")
                job["续采只读"] = bool(item.get("只读"))
                job["水位同步时间"] = _now()
                job["水位同步操作人"] = operator
                changed += 1
            return {"ok": True, "message": f"已同步 {changed} 条采集作业的续采水位", "changed": changed, "manifest": manifest}
