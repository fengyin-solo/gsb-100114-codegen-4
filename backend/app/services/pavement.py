"""路面病害业务规则：状态流转、字段校验、桩号版本与筛选口径。"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.stake_bucket import BucketProtocolError, stake_bucket_service
from app.store import store

MODULE = "pavement"
REQUIRED_FIELDS = ["病害编号", "所属路段", "病害类型", "起止桩号"]
OPTIONAL_FIELDS = ["严重程度", "面积", "发现日期", "病害状态"]
STATUS_ORDER = ["待修复", "修复中", "已修复", "已验收"]
ACTION_RULES = {"派发修复": "修复中", "标记修复": "已修复", "验收通过": "已验收"}
NEGATIVE_ACTIONS = []


class PavementService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        road: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("病害编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if road:
            rows = [row for row in rows if row.get("所属路段") == road]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str] | str]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        disease_code = str(values.get("病害编号") or "").strip()
        if any(str(row.get("病害编号") or "").strip() == disease_code for row in rows):
            return None, f"病害编号「{disease_code}」已存在，重建索引后也不能重复登记"

        road = str(values.get("所属路段") or "").strip()
        stake = str(values.get("起止桩号") or "").strip()
        try:
            stake_bucket_service.validate_disease_stake(road, stake)
        except BucketProtocolError as error:
            return None, error.message

        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({field: values.get(field) for field in OPTIONAL_FIELDS if values.get(field) is not None})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry.setdefault("发现日期", date.today().isoformat())
        stake_bucket_service.stamp_disease(entry)
        rows.append(entry)
        return entry, []

    def delete_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        rows = store.rows(MODULE)
        index = next((idx for idx, row in enumerate(rows) if int(row.get("id", 0)) == entry_id), None)
        if index is None:
            return None, f"病害记录 {entry_id} 不存在或已归档"
        entry = rows.pop(index)
        return entry, f"病害记录 {entry.get('病害编号')} 已撤销，桶索引浏览不会重复或漏项"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"病害记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于路面病害可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["病害状态"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"病害记录已{action}"
