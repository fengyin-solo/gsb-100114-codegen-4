"""桩号桶巡线与定位状态栅栏。

桶索引按“道路方向 -> 连续桩号桶 -> 桶内病害”组织。会话只允许
“建立索引 -> 校验桩号 -> 切换定位”的顺序，跳级和倒序都在这里拒绝。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from datetime import date
from typing import Any

from app.store import store

STAKE_PATTERN = re.compile(r"[Kk]\s*(\d+)(?:\s*\+\s*(\d+(?:\.\d+)?))?")
DEFAULT_ROAD = "中山大道"
DEFAULT_DIRECTION = "由西向东（桩号递增）"
MODULE = "pavement"
STAGE_ORDER = ["indexed", "validated", "located"]
STAGE_LABELS = {
    "indexed": "桶索引已建立",
    "validated": "桩号已校验",
    "located": "定位已切换",
    "stale": "路线版本已过期",
}


class BucketProtocolError(Exception):
    """桩号桶协议错误：status_code 直接对应服务端拒绝原因。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def parse_stake(value: Any) -> float:
    """把 K12+345.6 解析成米；只接受现行道路养护常用桩号写法。"""
    text = str(value or "").strip().upper().replace("－", "-").replace("—", "-")
    match = STAKE_PATTERN.search(text)
    if not match:
        raise BucketProtocolError(422, f"桩号「{value}」格式无效，示例：K0+120")
    kilometers = float(match.group(1))
    meters = float(match.group(2) or 0)
    if meters >= 1000:
        raise BucketProtocolError(422, f"桩号「{value}」的米数必须小于 1000")
    return kilometers * 1000 + meters


def format_stake(position: float, bucket_size: int | None = None) -> str:
    rounded = int(position) if float(position).is_integer() else round(position, 2)
    kilometers, meters = divmod(rounded, 1000)
    meter_text = f"{meters:03.0f}" if float(meters).is_integer() else f"{meters:06.2f}"
    return f"K{kilometers}+{meter_text}"


def stake_label(position: float) -> str:
    return format_stake(position)


class StakeBucketService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.routes: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.sync_keys: set[str] = set()

    # ------------------------------------------------------------------
    # 路线与桶索引
    # ------------------------------------------------------------------
    def list_routes(self) -> list[dict[str, Any]]:
        with self._lock:
            candidates: dict[str, dict[str, Any]] = {}
            for row in store.rows("road_section"):
                try:
                    start, end = self._range_from_row(row)
                except BucketProtocolError:
                    continue
                name = str(row.get("路段名称") or "").strip()
                if not name:
                    continue
                candidates[name] = {
                    "road": name,
                    "direction": str(row.get("道路方向") or DEFAULT_DIRECTION),
                    "start": start,
                    "end": end,
                    "bucket_size": int(row.get("桶长") or 100),
                }
            if not candidates:
                candidates[DEFAULT_ROAD] = {
                    "road": DEFAULT_ROAD,
                    "direction": DEFAULT_DIRECTION,
                    "start": 0,
                    "end": 2000,
                    "bucket_size": 100,
                }

            result = []
            for road, candidate in candidates.items():
                current = self.routes.get(road)
                if current:
                    item = dict(current)
                    item["indexed"] = True
                else:
                    item = {**candidate, "version": 1, "index_revision": 0, "indexed": False}
                item["bucket_count"] = self._bucket_count(item)
                result.append(item)
            return sorted(result, key=lambda item: item["road"])

    def build_index(
        self,
        road: str,
        *,
        expected_version: int | None = None,
        bucket_size: int | None = None,
    ) -> dict[str, Any]:
        road = road.strip()
        with self._lock:
            existing = self.routes.get(road)
            if existing and expected_version is not None and int(expected_version) != int(existing["version"]):
                raise BucketProtocolError(409, "路线版本已变化，请刷新现行里程体系后重建桶索引")
            if existing:
                if bucket_size:
                    self._validate_bucket_size(bucket_size)
                    existing["bucket_size"] = bucket_size
                existing["index_revision"] = int(existing["index_revision"]) + 1
                route = existing
            else:
                candidate = self._candidate_route(road)
                size = self._validate_bucket_size(bucket_size or candidate["bucket_size"])
                route = {
                    "road": road,
                    "direction": candidate["direction"],
                    "start": candidate["start"],
                    "end": candidate["end"],
                    "bucket_size": size,
                    "version": 1,
                    "index_revision": 1,
                }
                self.routes[road] = route
            self._stamp_current_disease(route)
            buckets = self._buckets(route)
            return {"route": self._route_payload(route), "buckets": buckets}

    def rebuild_index(self, road: str, *, expected_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            route = self._built_route(road)
            if expected_version is not None and int(expected_version) != int(route["version"]):
                raise BucketProtocolError(409, "旧版本索引不能覆盖现行路线，请基于新版本重建")
            route["index_revision"] = int(route["index_revision"]) + 1
            return {"route": self._route_payload(route), "buckets": self._buckets(route)}

    def reroute(
        self,
        road: str,
        *,
        expected_version: int,
        start: Any,
        end: Any,
        direction: str | None = None,
        bucket_size: int | None = None,
    ) -> dict[str, Any]:
        road = road.strip()
        with self._lock:
            current = self._built_route(road)
            if int(expected_version) != int(current["version"]):
                raise BucketProtocolError(409, "并发改线冲突：旧桶不能覆盖新路线")
            start_m = parse_stake(start)
            end_m = parse_stake(end)
            if end_m <= start_m:
                raise BucketProtocolError(422, "改线终点桩号必须大于起点桩号")
            size = self._validate_bucket_size(bucket_size or current["bucket_size"])

            for row in store.rows(MODULE):
                if row.get("所属路段") != road:
                    continue
                if "桩号版本" not in row:
                    row["桩号版本"] = current["version"]
                    row["路线版本"] = current["version"]
                    row["桩号快照"] = row.get("起止桩号")
                if int(row.get("桩号版本", current["version"])) == int(current["version"]):
                    row["现行体系状态"] = "旧线快照"
                    row["历史桩号快照"] = row.get("桩号快照") or row.get("起止桩号")

            current.update({
                "start": start_m,
                "end": end_m,
                "bucket_size": size,
                "direction": direction.strip() if direction and direction.strip() else current["direction"],
                "version": int(current["version"]) + 1,
                "index_revision": 1,
            })
            for session in self.sessions.values():
                if session.get("road") == road and int(session.get("route_version", 0)) != int(current["version"]):
                    session["stage"] = "stale"
                    session["stale_reason"] = "路线已改线，旧桶会话不可继续切换"
            return {"route": self._route_payload(current), "buckets": self._buckets(current)}

    def list_buckets(self, road: str, cursor: str | None = None) -> dict[str, Any]:
        with self._lock:
            route = self._built_route(road)
            decoded = self._decode_cursor(route, cursor) if cursor else None
            initial_bucket = decoded["bucket"] if decoded else 0
            buckets = self._buckets(route)
            canonical = self._make_cursor(route, initial_bucket)
            return {
                "route": self._route_payload(route),
                "cursor": canonical,
                "replaced_legacy_cursor": bool(decoded and decoded.get("legacy")),
                "initial_bucket": initial_bucket,
                "buckets": buckets,
            }

    # ------------------------------------------------------------------
    # 三阶段定位协议
    # ------------------------------------------------------------------
    def start_session(self, road: str, *, cursor: str | None = None) -> dict[str, Any]:
        with self._lock:
            route = self._built_route(road)
            decoded = self._decode_cursor(route, cursor) if cursor else None
            session_id = str(uuid.uuid4())
            session = {
                "session_id": session_id,
                "road": road,
                "route_version": route["version"],
                "index_revision": route["index_revision"],
                "stage": "indexed",
                "stage_label": STAGE_LABELS["indexed"],
                "cursor": self._make_cursor(route, decoded["bucket"] if decoded else 0),
                "origin_bucket": decoded["bucket"] if decoded else 0,
                "current_bucket": decoded["bucket"] if decoded else 0,
                "selected_bucket": None,
                "disease_id": None,
            }
            self.sessions[session_id] = session
            return {"session": dict(session), "route": self._route_payload(route)}

    def validate_stake(
        self,
        session_id: str,
        *,
        stake: Any,
        disease_id: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            self._require_stage(session, "indexed")
            route = self._session_route(session)
            position = parse_stake(stake)
            self._ensure_in_route(route, position)
            bucket_index = self._bucket_index(route, position)
            disease = self._disease(disease_id) if disease_id is not None else None
            if disease:
                disease_position = parse_stake(disease.get("起止桩号"))
                if disease.get("所属路段") != route["road"]:
                    raise BucketProtocolError(422, "病害所属路段与巡线道路不一致")
                if int(disease.get("桩号版本", route["version"])) != int(route["version"]):
                    raise BucketProtocolError(409, "该病害属于旧桩号快照，不能按现行里程定位")
                if self._bucket_index(route, disease_position) != bucket_index:
                    raise BucketProtocolError(422, "病害桩号不在本次校验的桩号桶内")
            session.update({
                "stage": "validated",
                "stage_label": STAGE_LABELS["validated"],
                "stake": stake_label(position),
                "stake_position": position,
                "current_bucket": bucket_index,
                "selected_bucket": bucket_index,
                "disease_id": disease_id if disease else None,
            })
            return {"session": dict(session), "bucket": self._bucket(route, bucket_index)}

    def locate(
        self,
        session_id: str,
        *,
        disease_id: int | None = None,
        origin_bucket: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            self._require_stage(session, "validated")
            route = self._session_route(session)
            validated_bucket = int(session["current_bucket"])
            origin = validated_bucket
            if origin_bucket is not None:
                origin = max(0, min(int(origin_bucket), self._bucket_count(route) - 1))
            disease = self._disease(disease_id) if disease_id is not None else self._disease(int(session["disease_id"])) if session.get("disease_id") else None
            if disease:
                position = parse_stake(disease.get("起止桩号"))
                if disease.get("所属路段") != route["road"]:
                    raise BucketProtocolError(422, "病害所属路段与定位道路不一致")
                if int(disease.get("桩号版本", route["version"])) != int(route["version"]):
                    raise BucketProtocolError(409, "旧病害只能维持桩号快照，不能切换到现行桶")
                bucket_index = self._bucket_index(route, position)
                if bucket_index != validated_bucket:
                    raise BucketProtocolError(422, "定位病害不在已校验桩号桶内")
            else:
                bucket_index = validated_bucket
            session.update({
                "stage": "located",
                "stage_label": STAGE_LABELS["located"],
                "origin_bucket": origin,
                "current_bucket": bucket_index,
                "selected_bucket": bucket_index,
                "disease_id": disease["id"] if disease else None,
            })
            return self._position_payload(session, route)

    def return_to_origin(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            self._require_stage(session, "located")
            route = self._session_route(session)
            bucket_index = int(session["origin_bucket"])
            session["current_bucket"] = bucket_index
            session["selected_bucket"] = bucket_index
            return {"session": dict(session), "bucket": self._bucket(route, bucket_index)}

    def neighbors(self, session_id: str, disease_id: int) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            if session["stage"] == "stale":
                raise BucketProtocolError(409, session.get("stale_reason", "会话版本已过期"))
            route = self._session_route(session)
            records = self._current_disease(route)
            index = next((idx for idx, item in enumerate(records) if int(item["id"]) == int(disease_id)), None)
            if index is None:
                raise BucketProtocolError(404, f"现行桶索引中没有病害 {disease_id}")
            return {
                "previous": records[index - 1] if index > 0 else None,
                "current": records[index],
                "next": records[index + 1] if index + 1 < len(records) else None,
            }

    def conclude(
        self,
        session_id: str,
        *,
        conclusion: str,
        action: str = "纳入待办",
        target_type: str | None = None,
        target_id: int | None = None,
        inspector: str | None = None,
    ) -> dict[str, Any]:
        conclusion = str(conclusion or "").strip()
        if not conclusion:
            raise BucketProtocolError(422, "巡线结论不能为空")
        with self._lock:
            session = self._session(session_id)
            self._require_stage(session, "located")
            route = self._session_route(session)
            disease_id = session.get("disease_id")
            if not disease_id:
                raise BucketProtocolError(422, "请先定位具体病害，再同步巡线结论")
            disease = self._disease(int(disease_id))
            sync_key = f"{session_id}:{disease_id}"
            if sync_key in self.sync_keys:
                raise BucketProtocolError(409, "该巡线结论已经同步，不能重复写入台账")

            bucket = self._bucket(route, int(session["current_bucket"]))
            patrol = self._sync_patrol(
                session_id=session_id,
                route=route,
                bucket=bucket,
                disease=disease,
                conclusion=conclusion,
                inspector=inspector,
            )
            load_limit = self._sync_load_limit(
                target_type=target_type,
                target_id=target_id,
                route=route,
                bucket=bucket,
                disease=disease,
                conclusion=conclusion,
            )

            disease["status"] = "修复中"
            disease["病害状态"] = "修复中"
            disease["pending"] = True
            disease["abnormal"] = True
            disease["巡线结论"] = conclusion
            disease["处置要求"] = action
            disease["巡线会话"] = session_id
            disease["最近巡线日期"] = date.today().isoformat()
            self.sync_keys.add(sync_key)
            return {
                "ok": True,
                "message": "巡线结论已同步到巡查待办、桥隧限载清单和病害台账",
                "patrol": patrol,
                "load_limit": load_limit,
                "disease": dict(disease),
            }

    # ------------------------------------------------------------------
    # 同步与定位面板
    # ------------------------------------------------------------------
    def _position_payload(self, session: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        bucket = self._bucket(route, int(session["current_bucket"]))
        disease = None
        if session.get("disease_id"):
            disease = self._disease(int(session["disease_id"]))
        road_diseases = [item for item in self._current_disease(route)]
        patrols = self._patrol_panel(route, disease)
        load_limits = self._load_limits(route, disease)
        return {
            "session": dict(session),
            "route": self._route_payload(route),
            "bucket": bucket,
            "disease": disease,
            "disease_list": road_diseases,
            "patrols": patrols,
            "load_limits": load_limits,
        }

    def _patrol_panel(self, route: dict[str, Any], disease: dict[str, Any] | None) -> list[dict[str, Any]]:
        code = disease.get("病害编号") if disease else None
        rows = []
        for row in store.rows("patrol"):
            if row.get("巡查路段") == route["road"] or (code and row.get("关联病害") == code):
                rows.append(dict(row))
        return sorted(rows, key=lambda item: int(item.get("id", 0)), reverse=True)[:10]

    def _load_limits(self, route: dict[str, Any], disease: dict[str, Any] | None) -> list[dict[str, Any]]:
        code = disease.get("病害编号") if disease else None
        result = []
        for row in store.rows("bridge_info"):
            if row.get("status") == "限载" and (row.get("关联路段") == route["road"] or row.get("关联病害") == code):
                result.append({
                    "kind": "桥梁",
                    "id": row["id"],
                    "编号": row.get("桥梁编号"),
                    "名称": row.get("桥梁名称"),
                    "状态": row.get("status"),
                    "限制": row.get("限载要求") or row.get("设计荷载"),
                    "提示": row.get("限载提示") or row.get("巡线结论"),
                    "关联桩号": row.get("关联桩号"),
                })
        for row in store.rows("tunnel"):
            if row.get("status") in {"限速", "限载"} and (row.get("关联路段") == route["road"] or row.get("关联病害") == code):
                result.append({
                    "kind": "隧道",
                    "id": row["id"],
                    "编号": row.get("隧道编号"),
                    "名称": row.get("隧道名称"),
                    "状态": row.get("status"),
                    "限制": row.get("限速要求") or row.get("限高要求"),
                    "提示": row.get("限载提示") or row.get("巡线结论"),
                    "关联桩号": row.get("关联桩号"),
                })
        for notice in store.rows("load_limit_notices"):
            if notice.get("关联路段") == route["road"] or notice.get("关联病害") == code:
                result.append({
                    "kind": str(notice.get("kind") or "限载提示"),
                    "id": notice.get("id"),
                    "编号": notice.get("编号"),
                    "名称": notice.get("名称"),
                    "状态": notice.get("状态"),
                    "限制": notice.get("限制"),
                    "提示": notice.get("提示"),
                    "关联桩号": notice.get("关联桩号"),
                })
        deduped: dict[tuple[Any, Any], dict[str, Any]] = {}
        for item in result:
            deduped[(item["kind"], item["id"])] = item
        return list(deduped.values())

    def _sync_patrol(
        self,
        *,
        session_id: str,
        route: dict[str, Any],
        bucket: dict[str, Any],
        disease: dict[str, Any],
        conclusion: str,
        inspector: str | None,
    ) -> dict[str, Any]:
        rows = store.rows("patrol")
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "待办",
            "pending": True,
            "abnormal": True,
            "巡查编号": f"TODO-{date.today().strftime('%m%d')}-{len(rows) + 1:03d}",
            "巡查路段": route["road"],
            "巡查日期": date.today().isoformat(),
            "巡查人员": inspector or "桶巡线值班员",
            "巡查车辆": "巡查车",
            "发现问题": f"{disease.get('病害编号')} {disease.get('病害类型')}（{disease.get('起止桩号')}）",
            "处置措施": conclusion,
            "巡查状态": "待办",
            "桩号桶巡线": bucket["label"],
            "关联病害": disease.get("病害编号"),
            "巡线会话": session_id,
        }
        rows.append(entry)
        return entry

    def _sync_load_limit(
        self,
        *,
        target_type: str | None,
        target_id: int | None,
        route: dict[str, Any],
        bucket: dict[str, Any],
        disease: dict[str, Any],
        conclusion: str,
    ) -> dict[str, Any]:
        linked_target = None
        if target_type and target_id is not None:
            kind = "桥梁" if target_type in {"bridge", "bridge_info", "桥梁"} else "隧道"
            module = "bridge_info" if kind == "桥梁" else "tunnel"
            linked_target = store.find(module, target_id)
            if linked_target is None:
                raise BucketProtocolError(404, f"{kind} {target_id} 不存在")
            linked_target["status"] = "限载" if kind == "桥梁" else "限速"
            linked_target["pending"] = True
            linked_target["abnormal"] = True
            linked_target["关联路段"] = route["road"]
            linked_target["关联桩号"] = bucket["label"]
            linked_target["关联病害"] = disease.get("病害编号")
            linked_target["限载提示"] = conclusion
            linked_target["巡线结论"] = conclusion
            if kind == "桥梁":
                linked_target["桥梁状态"] = "限载"
                linked_target["限载要求"] = linked_target.get("限载要求") or "按最近评定荷载限载通行"
            else:
                linked_target["隧道状态"] = "限速"
                linked_target["限速要求"] = linked_target.get("限速要求") or "限速通过，必要时限高绕行"

        notices = store.rows("load_limit_notices")
        notice = {
            "id": max((int(row.get("id", 0)) for row in notices), default=0) + 1,
            "status": "生效中",
            "pending": True,
            "abnormal": True,
            "kind": "桥梁限载" if linked_target is not None and linked_target.get("桥梁编号") else "隧道限速" if linked_target is not None else "桥隧提示",
            "编号": linked_target.get("桥梁编号") if linked_target else f"LIMIT-{disease.get('病害编号')}",
            "名称": (linked_target.get("桥梁名称") or linked_target.get("隧道名称")) if linked_target else disease.get("病害编号"),
            "状态": linked_target.get("status") if linked_target else "提示",
            "限制": linked_target.get("限载要求") or linked_target.get("限速要求") if linked_target else "巡查发现通行风险，复核桥隧限载",
            "提示": conclusion,
            "关联路段": route["road"],
            "关联桩号": bucket["label"],
            "关联病害": disease.get("病害编号"),
        }
        notices.append(notice)
        return notice

    # ------------------------------------------------------------------
    # 数据与游标
    # ------------------------------------------------------------------
    def validate_disease_stake(self, road: str, stake_text: str) -> float:
        """新增病害时校验桩号；索引未建立时只校验格式，不提前固化版本。"""
        position = parse_stake(stake_text)
        route = self.routes.get(road)
        if route:
            self._ensure_in_route(route, position)
        return position

    def stamp_disease(self, entry: dict[str, Any]) -> dict[str, Any]:
        road = str(entry.get("所属路段") or "")
        route = self.routes.get(road)
        if not route:
            return entry
        position = parse_stake(entry.get("起止桩号"))
        self._ensure_in_route(route, position)
        entry["桩号版本"] = route["version"]
        entry["路线版本"] = route["version"]
        entry["桩号快照"] = entry.get("起止桩号")
        entry["现行体系状态"] = "现行"
        return entry

    def _stamp_current_disease(self, route: dict[str, Any]) -> None:
        for row in store.rows(MODULE):
            if row.get("所属路段") != route["road"]:
                continue
            if "桩号版本" in row:
                continue
            try:
                position = parse_stake(row.get("起止桩号"))
                self._ensure_in_route(route, position)
            except BucketProtocolError:
                continue
            row["桩号版本"] = route["version"]
            row["路线版本"] = route["version"]
            row["桩号快照"] = row.get("起止桩号")
            row["现行体系状态"] = "现行"

    def _current_disease(self, route: dict[str, Any]) -> list[dict[str, Any]]:
        records = []
        for row in store.rows(MODULE):
            if row.get("所属路段") != route["road"]:
                continue
            if int(row.get("桩号版本", route["version"])) != int(route["version"]):
                continue
            try:
                position = parse_stake(row.get("起止桩号"))
                self._ensure_in_route(route, position)
            except BucketProtocolError:
                continue
            item = dict(row)
            item["stake_position"] = position
            item["bucket_index"] = self._bucket_index(route, position)
            records.append(item)
        return sorted(records, key=lambda item: (item["stake_position"], str(item.get("病害编号"))))

    def _buckets(self, route: dict[str, Any]) -> list[dict[str, Any]]:
        count = self._bucket_count(route)
        records = self._current_disease(route)
        grouped: dict[int, list[dict[str, Any]]] = {index: [] for index in range(count)}
        for item in records:
            grouped[int(item["bucket_index"])].append({
                "id": item["id"],
                "病害编号": item.get("病害编号"),
                "病害类型": item.get("病害类型"),
                "严重程度": item.get("严重程度"),
                "桩号": item.get("起止桩号"),
                "状态": item.get("status"),
            })
        size = int(route["bucket_size"])
        buckets = []
        for index in range(count):
            start = float(route["start"]) + index * size
            end = min(float(route["end"]), start + size)
            end_label_position = end if index == count - 1 else end - 0.001
            buckets.append({
                "index": index,
                "start": stake_label(start),
                "end": stake_label(end_label_position),
                "label": f"{stake_label(start)}-{stake_label(end_label_position)}",
                "cursor": self._make_cursor(route, index),
                "disease_count": len(grouped[index]),
                "diseases": grouped[index],
            })
        return buckets

    def _bucket(self, route: dict[str, Any], index: int) -> dict[str, Any]:
        buckets = self._buckets(route)
        if not buckets:
            raise BucketProtocolError(409, "当前路线没有可用桩号桶")
        return buckets[max(0, min(index, len(buckets) - 1))]

    def _candidate_route(self, road: str) -> dict[str, Any]:
        for row in store.rows("road_section"):
            if row.get("路段名称") != road:
                continue
            start, end = self._range_from_row(row)
            return {
                "road": road,
                "direction": str(row.get("道路方向") or DEFAULT_DIRECTION),
                "start": start,
                "end": end,
                "bucket_size": int(row.get("桶长") or 100),
            }
        if road == DEFAULT_ROAD:
            return {"road": road, "direction": DEFAULT_DIRECTION, "start": 0.0, "end": 2000.0, "bucket_size": 100}
        raise BucketProtocolError(404, f"道路「{road}」不在管养路段清单中")

    def _range_from_row(self, row: dict[str, Any]) -> tuple[float, float]:
        matches = STAKE_PATTERN.findall(str(row.get("起止桩号") or ""))
        if len(matches) < 2:
            raise BucketProtocolError(422, "路段起止桩号必须包含起点和终点，例如 K0+000-K2+000")
        values = []
        for kilometers, meters in matches[:2]:
            values.append(float(kilometers) * 1000 + float(meters or 0))
        if values[1] <= values[0]:
            raise BucketProtocolError(422, "路段终点桩号必须大于起点桩号")
        return values[0], values[1]

    def _built_route(self, road: str) -> dict[str, Any]:
        route = self.routes.get(road)
        if route is None:
            raise BucketProtocolError(409, "请先建立桩号桶索引，再校验桩号或定位")
        return route

    def _session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise BucketProtocolError(404, "定位会话不存在或已失效")
        return session

    def _session_route(self, session: dict[str, Any]) -> dict[str, Any]:
        route = self._built_route(str(session["road"]))
        if int(session.get("route_version", -1)) != int(route["version"]):
            session["stage"] = "stale"
            raise BucketProtocolError(409, "路线已经改线，旧桶会话必须基于新版本重新开始")
        session["index_revision"] = route["index_revision"]
        return route

    def _require_stage(self, session: dict[str, Any], expected: str) -> None:
        actual = str(session.get("stage"))
        if actual == "stale":
            raise BucketProtocolError(409, session.get("stale_reason", "会话版本已过期"))
        if actual != expected:
            raise BucketProtocolError(
                409,
                f"定位状态栅栏拒绝：当前为「{STAGE_LABELS.get(actual, actual)}」，"
                f"不能执行「{STAGE_LABELS[expected]}」阶段操作",
            )

    def _disease(self, disease_id: int) -> dict[str, Any]:
        disease = store.find(MODULE, int(disease_id))
        if disease is None:
            raise BucketProtocolError(404, f"病害记录 {disease_id} 不存在")
        return disease

    def _ensure_in_route(self, route: dict[str, Any], position: float) -> None:
        if position < float(route["start"]) or position > float(route["end"]):
            raise BucketProtocolError(
                422,
                f"桩号 {stake_label(position)} 超出现行路线范围 "
                f"{stake_label(float(route['start']))}-{stake_label(float(route['end']))}",
            )

    def _bucket_index(self, route: dict[str, Any], position: float) -> int:
        size = int(route["bucket_size"])
        index = int((position - float(route["start"])) // size)
        return max(0, min(index, self._bucket_count(route) - 1))

    def _bucket_count(self, route: dict[str, Any]) -> int:
        size = int(route["bucket_size"])
        length = float(route["end"]) - float(route["start"])
        return max(1, int(length // size) + (1 if length % size else 0))

    def _validate_bucket_size(self, value: Any) -> int:
        try:
            size = int(value)
        except (TypeError, ValueError):
            raise BucketProtocolError(422, "桶长必须是 20-1000 米之间的整数") from None
        if size < 20 or size > 1000:
            raise BucketProtocolError(422, "桶长必须在 20-1000 米之间")
        return size

    def _route_payload(self, route: dict[str, Any]) -> dict[str, Any]:
        item = dict(route)
        item["start_label"] = stake_label(float(route["start"]))
        item["end_label"] = stake_label(float(route["end"]))
        item["bucket_count"] = self._bucket_count(route)
        item["indexed"] = True
        return item

    def _make_cursor(self, route: dict[str, Any], bucket_index: int, disease_id: int | None = None) -> str:
        payload = {
            "road": route["road"],
            "v": route["version"],
            "r": route["index_revision"],
            "b": max(0, min(bucket_index, self._bucket_count(route) - 1)),
            "d": disease_id,
        }
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _decode_cursor(self, route: dict[str, Any], cursor: str) -> dict[str, Any]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
            if payload.get("road") != route["road"]:
                raise ValueError
            legacy = int(payload.get("r", 0)) != int(route["index_revision"]) or int(payload.get("v", 0)) != int(route["version"])
            bucket = max(0, min(int(payload.get("b", 0)), self._bucket_count(route) - 1))
            return {"bucket": bucket, "disease_id": payload.get("d"), "legacy": legacy}
        except (ValueError, TypeError, json.JSONDecodeError):
            try:
                legacy_bucket = max(0, min(int(cursor), self._bucket_count(route) - 1))
            except ValueError:
                raise BucketProtocolError(422, "浏览游标无效，无法兼容到现行桶索引") from None
            return {"bucket": legacy_bucket, "disease_id": None, "legacy": True}


stake_bucket_service = StakeBucketService()
