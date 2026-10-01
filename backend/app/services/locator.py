"""桩号桶定位器：用状态栅栏保证“建索引、校验桩号、切换定位”的顺序。

定位数据仍保存在各业务模块自己的内存表里；这里维护按现行里程建立的桶索引、
游标映射和改线版本。旧路线的桶只归档用于旧游标兼容，不能再写回现行路线。
"""
from __future__ import annotations

import re
import threading
from datetime import date
from typing import Any

from app.store import store

PAVEMENT_MODULE = "pavement"
PATROL_MODULE = "patrol"
BRIDGE_INFO_MODULE = "bridge_info"
TUNNEL_MODULE = "tunnel"
ROAD_SECTION_MODULE = "road_section"

BUCKET_SIZE = 100
STAGE_INDEXED = "indexed"
STAGE_VALIDATED = "validated"
STAGE_LOCATED = "located"
STAGE_LABELS = {
    STAGE_INDEXED: "已建索引",
    STAGE_VALIDATED: "已校验桩号",
    STAGE_LOCATED: "已切换定位",
}
STAGE_ORDER = [STAGE_INDEXED, STAGE_VALIDATED, STAGE_LOCATED]


class LocatorError(Exception):
    """定位器业务错误，status_code 供路由层映射成 HTTP 状态。"""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def parse_station(value: Any) -> float | None:
    """把 K1+234、1234.5 等桩号解析成米；无法可靠解析时返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace("K", "K")
    matched = re.search(r"K?\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)", text)
    if matched:
        return int(matched.group(1)) * 1000 + float(matched.group(2))
    range_text = re.split(r"\s*(?:至|~|—|–|-|—)\s*", text, maxsplit=1)[0].strip()
    matched = re.fullmatch(r"\d+(?:\.\d+)?", range_text)
    return float(range_text) if matched else None


def format_station(meter: float) -> str:
    """按内部演示口径格式化为三位米数的桩号。"""
    kilometer = int(meter // 1000)
    meter_part = int(round(meter - kilometer * 1000))
    if meter_part == 1000:
        kilometer += 1
        meter_part = 0
    return f"K{kilometer}+{meter_part:03d}"


def _as_int(value: Any, default: int = 1) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class PavementLocatorService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._indexes: dict[str, dict[str, Any]] = {}
        self._archived_indexes: dict[str, dict[int, dict[str, Any]]] = {}

    def state(self, road: str, cursor: str | None = None) -> dict[str, Any]:
        with self._lock:
            road = self._road_key(road)
            index = self._indexes.get(road)
            if index is None:
                section = self._road_section(road)
                return {
                    "road": road,
                    "roadName": section.get("路段名称", road),
                    "stageCode": "idle",
                    "stage": "待建索引",
                    "version": _as_int(section.get("里程版本")),
                    "direction": section.get("道路方向") or "顺桩",
                    "buckets": [],
                    "defects": [],
                    "snapshots": [],
                    "patrolTodos": self._patrol_todos(road),
                    "loadRestrictions": self._load_restrictions(road),
                }
            cursor_info = self.resolve_cursor(road, cursor)
            return {
                "road": road,
                "roadName": index["roadName"],
                "stageCode": index["stage"],
                "stage": STAGE_LABELS[index["stage"]],
                "version": index["version"],
                "direction": index["direction"],
                "startStation": format_station(index["startMeter"]),
                "endStation": format_station(index["endMeter"]),
                "cursor": cursor_info["cursor"],
                "cursorMigrated": cursor_info["migrated"],
                "validatedStation": index.get("validatedStation"),
                "currentDiseaseId": index.get("currentDiseaseId"),
                "currentBucket": self._bucket_payload(index.get("currentBucketKey"), index),
                "buckets": [self._bucket_payload(key, index) for key in index["sequence"]],
                "defects": self._current_defects(index),
                "snapshots": self._snapshot_defects(road, index["version"]),
                "patrolTodos": self._patrol_todos(road),
                "loadRestrictions": self._load_restrictions(road),
                "lastSync": index.get("lastSync"),
            }

    def build_index(self, road: str, expected_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            section = self._road_section(road)
            road = self._road_key(road)
            version = _as_int(section.get("里程版本"))
            old = self._indexes.get(road)
            if expected_version is not None and expected_version != version:
                raise LocatorError(
                    f"道路已使用里程版本 v{version}，旧桶 v{expected_version} 不能重建当前桶索引",
                    status_code=409,
                )
            index = self._make_index(section, preserve=old)
            self._indexes[road] = index
            return self.state(road)

    def validate_station(self, road: str, station_text: str, expected_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            index = self._require_index(road, expected_version)
            if index["stage"] != STAGE_INDEXED:
                raise LocatorError("定位状态栅栏拦截：必须停留在“已建索引”才能校验桩号，不能倒序切换")
            meter = parse_station(station_text)
            if meter is None:
                raise LocatorError(f"桩号「{station_text}」无法解析，请使用 K0+100 这类格式", status_code=422)
            if meter < index["startMeter"] or meter > index["endMeter"]:
                raise LocatorError(
                    f"桩号 {format_station(meter)} 不在现行里程 {format_station(index['startMeter'])} 至 "
                    f"{format_station(index['endMeter'])} 内",
                    status_code=422,
                )
            bucket_key = self._bucket_key_for_meter(index, meter)
            index["stage"] = STAGE_VALIDATED
            index["validatedMeter"] = meter
            index["validatedStation"] = format_station(meter)
            index["validatedBucketKey"] = bucket_key
            return self.state(road, bucket_key)

    def locate(self, road: str, disease_id: int, expected_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            index = self._require_index(road, expected_version)
            if index["stage"] != STAGE_VALIDATED:
                expected_label = STAGE_LABELS.get(index["stage"], index["stage"])
                raise LocatorError(f"定位状态栅栏拦截：当前为“{expected_label}”，不能从该阶段直接切换定位")
            bucket_key = index["diseaseMap"].get(int(disease_id))
            if bucket_key != index.get("validatedBucketKey"):
                raise LocatorError("病害不在刚校验通过的桩号桶内，请重新按顺序校验", status_code=422)
            disease = store.find(PAVEMENT_MODULE, int(disease_id))
            meter = parse_station(disease.get("桩号快照") or disease.get("起止桩号")) if disease else None
            if meter is None or not self._meter_in_bucket(index, bucket_key, meter):
                raise LocatorError("病害桩号与桶边界不一致，已拒绝切换定位", status_code=422)
            index["stage"] = STAGE_LOCATED
            index["currentBucketKey"] = bucket_key
            index["currentDiseaseId"] = int(disease_id)
            return self.state(road, bucket_key)

    def preview_disease(self, road: str, disease_code: str, expected_version: int | None = None) -> dict[str, Any]:
        """按病害编号预览；只返回相邻记录，不改变定位状态和当前桶。"""
        with self._lock:
            road = self._road_key(road)
            disease = self._find_pavement_by_code(disease_code)
            if disease is None:
                raise LocatorError(f"病害编号「{disease_code}」不存在", status_code=404)
            version = _as_int(disease.get("桩号版本"), self._current_version(road))
            index = self._indexes.get(road)
            if index and version == index["version"]:
                self._require_version(index, expected_version)
                ordered = self._ordered_defects(index)
                position = next((i for i, row in enumerate(ordered) if int(row["id"]) == int(disease["id"])), -1)
                if position < 0:
                    raise LocatorError("该病害不在现行桶索引内", status_code=404)
                bucket_key = index["diseaseMap"][int(disease["id"])]
                return {
                    "disease": disease,
                    "bucket": self._bucket_payload(bucket_key, index),
                    "previous": ordered[position - 1] if position > 0 else None,
                    "next": ordered[position + 1] if position + 1 < len(ordered) else None,
                    "originBucket": self._bucket_payload(index.get("currentBucketKey"), index),
                    "stale": False,
                }
            archived = self._archived_indexes.get(road, {}).get(version)
            bucket_key = archived["diseaseMap"].get(int(disease["id"])) if archived else None
            return {
                "disease": disease,
                "bucket": self._bucket_payload(bucket_key, archived) if archived and bucket_key else None,
                "previous": None,
                "next": None,
                "originBucket": self._bucket_payload(index.get("currentBucketKey"), index) if index else None,
                "stale": True,
                "message": "该病害属于旧桩号快照，仅可预览，不能切换到现行定位",
            }

    def add_disease(self, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            road = self._road_key(str(values.get("所属路段") or "").strip())
            self._require_index(road, _as_int(values.get("expectedVersion"), None) if values.get("expectedVersion") else None)
            code = str(values.get("病害编号") or "").strip()
            disease_type = str(values.get("病害类型") or "").strip()
            station_text = str(values.get("桩号") or values.get("起止桩号") or "").strip()
            missing = [name for name, value in (("病害编号", code), ("病害类型", disease_type), ("桩号", station_text)) if not value]
            if missing:
                raise LocatorError(f"缺少必填字段：{'、'.join(missing)}", status_code=422)
            if self._find_pavement_by_code(code) is not None:
                raise LocatorError(f"病害编号「{code}」已存在，不能重复入桶", status_code=409)
            meter = parse_station(station_text)
            index = self._indexes[road]
            if meter is None or meter < index["startMeter"] or meter > index["endMeter"]:
                raise LocatorError("新增病害桩号不在现行里程范围内", status_code=422)
            rows = store.rows(PAVEMENT_MODULE)
            entry = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "病害编号": code,
                "所属路段": road,
                "病害类型": disease_type,
                "严重程度": str(values.get("严重程度") or "一般"),
                "起止桩号": station_text,
                "桩号快照": format_station(meter),
                "面积": str(values.get("面积") or "—"),
                "发现日期": str(values.get("发现日期") or date.today().isoformat()),
                "病害状态": "待修复",
                "status": "待修复",
                "pending": True,
                "abnormal": False,
                "桩号版本": index["version"],
            }
            rows.append(entry)
            self._rebuild_current(road)
            refreshed = self._indexes[road]
            return {"entry": entry, "bucket": self._bucket_payload(refreshed["diseaseMap"][entry["id"]], refreshed), "state": self.state(road)}

    def undo_disease(self, disease_id: int, expected_version: int | None = None) -> dict[str, Any]:
        with self._lock:
            entry = store.find(PAVEMENT_MODULE, int(disease_id))
            if entry is None:
                raise LocatorError(f"病害记录 {disease_id} 不存在或已撤销", status_code=404)
            road = self._road_key(str(entry.get("所属路段") or ""))
            if road in self._indexes:
                self._require_version(self._indexes[road], expected_version)
            rows = store.rows(PAVEMENT_MODULE)
            rows[:] = [row for row in rows if int(row.get("id", 0)) != int(disease_id)]
            for index in list(self._indexes.values()):
                if index["road"] == road:
                    self._rebuild_current(road)
            for archive in self._archived_indexes.get(road, {}).values():
                self._populate_defects(archive)
            return {"ok": True, "message": f"病害 {entry.get('病害编号')} 已撤销并从桶索引移除", "state": self.state(road)}

    def realign_route(
        self,
        road: str,
        *,
        expected_version: int,
        new_start: str,
        new_end: str,
        direction: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            section = self._road_section(road)
            road = self._road_key(road)
            current_version = _as_int(section.get("里程版本"))
            if expected_version != current_version:
                raise LocatorError(
                    f"改线版本栅栏拦截：当前为 v{current_version}，旧桶 v{expected_version} 不能覆盖新路线",
                    status_code=409,
                )
            start_meter = parse_station(new_start)
            end_meter = parse_station(new_end)
            if start_meter is None or end_meter is None or start_meter >= end_meter:
                raise LocatorError("新里程体系的起终点无效，终点必须大于起点", status_code=422)

            old = self._indexes.get(road)
            if old:
                self._archived_indexes.setdefault(road, {})[old["version"]] = old
            for disease in self._road_pavements(road):
                if _as_int(disease.get("桩号版本"), current_version) == current_version:
                    disease["桩号版本"] = current_version
                    disease["桩号快照"] = disease.get("桩号快照") or disease.get("起止桩号")
                    disease["旧桩号说明"] = f"改线前 v{current_version} 快照，不随现行里程重算"

            new_version = current_version + 1
            section["起止桩号"] = f"{format_station(start_meter)}-{format_station(end_meter)}"
            section["里程版本"] = new_version
            if direction:
                section["道路方向"] = "逆桩" if "逆" in direction else "顺桩"
            section["改线说明"] = f"v{current_version} 改为 v{new_version}，旧病害维持桩号快照"

            index = self._make_index(section, preserve=None)
            self._indexes[road] = index
            return self.state(road)

    def sync_patrol_conclusion(
        self,
        road: str,
        *,
        expected_version: int | None,
        conclusion: str,
        patrol_id: int | None = None,
        bridge_limit: str | None = None,
        tunnel_limit: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            index = self._require_index(road, expected_version)
            if index["stage"] != STAGE_LOCATED:
                raise LocatorError("巡线结论只能在“已切换定位”后同步，避免结论挂到错误桶位")
            bucket_key = index["currentBucketKey"]
            bucket = index["buckets"][bucket_key]
            diseases = [store.find(PAVEMENT_MODULE, disease_id) for disease_id in bucket["diseaseIds"]]
            diseases = [disease for disease in diseases if disease]
            codes = [str(disease.get("病害编号")) for disease in diseases]
            today = date.today().isoformat()

            patrol = store.find(PATROL_MODULE, int(patrol_id)) if patrol_id else None
            if patrol is not None and str(patrol.get("巡查路段") or "") not in self._road_aliases(road):
                raise LocatorError("指定巡查记录不属于当前道路，不能写入本桶巡线结论", status_code=422)
            if patrol is None:
                patrol = self._find_open_patrol(road)
            if patrol is None:
                rows = store.rows(PATROL_MODULE)
                patrol = {
                    "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                    "巡查编号": f"PATR-BUCKET-{today.replace('-', '')}-{road}",
                    "巡查路段": index["roadName"],
                    "巡查日期": today,
                    "status": "待巡查",
                    "pending": True,
                    "abnormal": True,
                }
                rows.append(patrol)
            patrol.update(
                {
                    "pending": True,
                    "巡线结论": conclusion,
                    "关联桶位": bucket["label"],
                    "关联病害": "、".join(codes),
                    "里程版本": f"v{index['version']}",
                    "待办来源": "桩号桶巡线",
                }
            )
            patrol.setdefault("发现问题", conclusion)

            bridge_tip = bridge_limit or f"巡线提示：{conclusion}"
            tunnel_tip = tunnel_limit or f"巡线提示：{conclusion}"
            bridges = self._update_structure_tips(BRIDGE_INFO_MODULE, road, bucket, bridge_tip)
            tunnels = self._update_structure_tips(TUNNEL_MODULE, road, bucket, tunnel_tip)

            for disease in diseases:
                disease["巡线结论"] = conclusion
                disease["台账更新时间"] = today
                disease["同步状态"] = "已同步巡查待办与桥隧限载清单"
                disease["台账桶位"] = bucket["label"]

            index["lastSync"] = {
                "conclusion": conclusion,
                "patrolId": patrol["id"],
                "diseaseIds": [disease["id"] for disease in diseases],
                "bridgeIds": [row["id"] for row in bridges],
                "tunnelIds": [row["id"] for row in tunnels],
                "bucket": bucket["label"],
                "syncedAt": today,
            }
            return {"ok": True, "sync": index["lastSync"], "state": self.state(road, bucket_key)}

    def resolve_cursor(self, road: str, cursor: str | None) -> dict[str, Any]:
        index = self._indexes.get(road)
        if index is None:
            return {"cursor": cursor, "migrated": False}
        if cursor and cursor in index["buckets"]:
            return {"cursor": cursor, "migrated": False}
        if cursor:
            parsed = re.search(r":v(\d+):(\d+)$", cursor)
            if parsed:
                old_version = int(parsed.group(1))
                old_head = float(parsed.group(2))
                target_head = self._nearest_head(index, old_head)
                target_key = self._bucket_key(index, target_head)
                return {"cursor": target_key, "migrated": old_version != index["version"]}
        return {"cursor": index["sequence"][0], "migrated": False}

    def _require_index(self, road: str, expected_version: int | None) -> dict[str, Any]:
        road = self._road_key(road)
        index = self._indexes.get(road)
        if index is None:
            raise LocatorError("尚未建立桶索引，请先建立索引后再校验桩号", status_code=409)
        self._require_version(index, expected_version)
        return index

    def _require_version(self, index: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and int(expected_version) != int(index["version"]):
            raise LocatorError(
                f"当前道路为 v{index['version']}，请求使用旧桶 v{expected_version}，已拒绝",
                status_code=409,
            )

    def _make_index(self, section: dict[str, Any], *, preserve: dict[str, Any] | None) -> dict[str, Any]:
        road = str(section.get("路段编号"))
        start_text, end_text = self._section_range(section.get("起止桩号"))
        start_meter = parse_station(start_text)
        end_meter = parse_station(end_text)
        if start_meter is None or end_meter is None or start_meter >= end_meter:
            raise LocatorError("现行里程体系的起止桩号无效，无法形成连续桶", status_code=422)
        direction = section.get("道路方向") or "顺桩"
        direction = "逆桩" if "逆" in str(direction) else "顺桩"
        version = _as_int(section.get("里程版本"))
        heads: list[float] = []
        head = start_meter
        while head < end_meter:
            heads.append(head)
            head += BUCKET_SIZE
        buckets = {self._bucket_key_from_parts(road, version, value): {
            "road": road,
            "version": version,
            "head": value,
            "end": min(value + BUCKET_SIZE, end_meter),
            "label": f"{format_station(value)} 桶",
            "diseaseIds": [],
        } for value in heads}
        ascending = list(buckets)
        index = {
            "road": road,
            "roadName": str(section.get("路段名称") or road),
            "version": version,
            "direction": direction,
            "startMeter": start_meter,
            "endMeter": end_meter,
            "stage": STAGE_INDEXED,
            "buckets": buckets,
            "sequence": ascending if direction == "顺桩" else list(reversed(ascending)),
            "diseaseMap": {},
            "currentBucketKey": None,
            "currentDiseaseId": None,
            "cursorMap": {},
        }
        self._populate_defects(index)
        if preserve and preserve["version"] == version:
            index["stage"] = preserve["stage"]
            validated_key = None
            current_key = None
            meter = preserve.get("validatedMeter")
            if meter is not None:
                validated_key = self._bucket_key_for_meter(index, meter)
            if validated_key is None and preserve.get("validatedBucketKey"):
                old_parsed = re.search(r":v\d+:(\d+)$", str(preserve["validatedBucketKey"]))
                if old_parsed:
                    validated_key = self._bucket_key_for_meter(index, float(old_parsed.group(1)))
            if validated_key:
                index["validatedMeter"] = meter
                index["validatedStation"] = preserve.get("validatedStation")
                index["validatedBucketKey"] = validated_key
            else:
                index["stage"] = STAGE_INDEXED

            current_disease_id = preserve.get("currentDiseaseId")
            if index["stage"] == STAGE_LOCATED and current_disease_id in index["diseaseMap"]:
                current_key = index["diseaseMap"][int(current_disease_id)]
                index["currentDiseaseId"] = current_disease_id
            elif index["stage"] == STAGE_LOCATED:
                index["stage"] = STAGE_VALIDATED
                current_key = validated_key
            elif index["stage"] == STAGE_VALIDATED:
                current_key = validated_key
            index["currentBucketKey"] = current_key

            for old_key, old_bucket in preserve["buckets"].items():
                mapped = self._bucket_key_for_meter(index, old_bucket["head"])
                if mapped:
                    index["cursorMap"][old_key] = mapped
            old_cursor = preserve.get("currentBucketKey") or preserve.get("validatedBucketKey")
            if old_cursor:
                index["cursorMap"][old_cursor] = current_key or index["sequence"][0]
            index["lastSync"] = preserve.get("lastSync")
        return index

    def _rebuild_current(self, road: str) -> None:
        current = self._indexes.get(road)
        if current is None:
            return
        section = self._road_section(road)
        rebuilt = self._make_index(section, preserve=current)
        self._indexes[road] = rebuilt

    def _populate_defects(self, index: dict[str, Any]) -> None:
        for bucket in index["buckets"].values():
            bucket["diseaseIds"] = []
        index["diseaseMap"] = {}
        defects = []
        for disease in self._road_pavements(index["road"]):
            if _as_int(disease.get("桩号版本"), index["version"]) != index["version"]:
                continue
            meter = parse_station(disease.get("桩号快照") or disease.get("起止桩号"))
            if meter is None:
                continue
            key = self._bucket_key_for_meter(index, meter)
            if key is None:
                continue
            defects.append((meter, int(disease["id"]), disease))
        for meter, disease_id, disease in sorted(defects, key=lambda item: (item[0], item[1])):
            key = index["diseaseMap"].setdefault(disease_id, self._bucket_key_for_meter(index, meter))
            ids = index["buckets"][key]["diseaseIds"]
            if disease_id not in ids:
                ids.append(disease_id)

    def _road_pavements(self, road: str) -> list[dict[str, Any]]:
        aliases = self._road_aliases(road)
        return [row for row in store.rows(PAVEMENT_MODULE) if str(row.get("所属路段") or "") in aliases]

    def _snapshot_defects(self, road: str, current_version: int) -> list[dict[str, Any]]:
        return [
            row
            for row in self._road_pavements(road)
            if _as_int(row.get("桩号版本"), current_version) != current_version
        ]

    def _current_defects(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        return self._ordered_defects(index)

    def _ordered_defects(self, index: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for key in index["sequence"]:
            for disease_id in index["buckets"][key]["diseaseIds"]:
                disease = store.find(PAVEMENT_MODULE, disease_id)
                if disease:
                    rows.append(disease)
        return rows if index["direction"] == "顺桩" else list(reversed(rows))

    def _bucket_payload(self, key: str | None, index: dict[str, Any] | None) -> dict[str, Any] | None:
        if not key or index is None or key not in index["buckets"]:
            return None
        bucket = index["buckets"][key]
        defects = [store.find(PAVEMENT_MODULE, disease_id) for disease_id in bucket["diseaseIds"]]
        return {
            "cursor": key,
            "head": format_station(bucket["head"]),
            "end": format_station(bucket["end"]),
            "label": bucket["label"],
            "direction": index["direction"],
            "diseaseIds": bucket["diseaseIds"],
            "defects": [disease for disease in defects if disease],
        }

    def _patrol_todos(self, road: str) -> list[dict[str, Any]]:
        aliases = self._road_aliases(road)
        return [
            row
            for row in store.rows(PATROL_MODULE)
            if str(row.get("巡查路段") or "") in aliases and (row.get("pending") or row.get("巡线结论"))
        ]

    def _load_restrictions(self, road: str) -> list[dict[str, Any]]:
        aliases = self._road_aliases(road)
        result = []
        for module, kind in ((BRIDGE_INFO_MODULE, "桥梁"), (TUNNEL_MODULE, "隧道")):
            for row in store.rows(module):
                if str(row.get("所属路段") or "") not in aliases:
                    continue
                if row.get("限载提示") or row.get("status") in {"限载", "限速", "维修", "封闭"}:
                    item = dict(row)
                    item["结构类型"] = kind
                    result.append(item)
        return result

    def _update_structure_tips(
        self,
        module: str,
        road: str,
        bucket: dict[str, Any],
        tip: str,
    ) -> list[dict[str, Any]]:
        aliases = self._road_aliases(road)
        matched = []
        for row in store.rows(module):
            if str(row.get("所属路段") or "") not in aliases:
                continue
            meter = parse_station(row.get("桩号位置"))
            if meter is not None and bucket["head"] <= meter < bucket["end"]:
                row["限载提示"] = tip
                row["关联桶位"] = bucket["label"]
                matched.append(row)
        return matched

    def _find_open_patrol(self, road: str) -> dict[str, Any] | None:
        aliases = self._road_aliases(road)
        for row in store.rows(PATROL_MODULE):
            if str(row.get("巡查路段") or "") in aliases and row.get("pending"):
                return row
        return None

    def _find_pavement_by_code(self, code: str) -> dict[str, Any] | None:
        text = code.strip()
        for row in store.rows(PAVEMENT_MODULE):
            if str(row.get("病害编号") or "").strip() == text:
                return row
        return None

    def _road_section(self, road: str) -> dict[str, Any]:
        for row in store.rows(ROAD_SECTION_MODULE):
            if row.get("路段编号") == road or row.get("路段名称") == road:
                return row
        raise LocatorError(f"道路「{road}」不存在", status_code=404)

    def _road_key(self, road: str) -> str:
        road = str(road or "").strip()
        if not road:
            raise LocatorError("缺少所属路段", status_code=422)
        for row in store.rows(ROAD_SECTION_MODULE):
            if road == str(row.get("路段编号") or "") or road == str(row.get("路段名称") or ""):
                return str(row["路段编号"])
        return road

    def _road_aliases(self, road: str) -> set[str]:
        aliases = {road}
        for row in store.rows(ROAD_SECTION_MODULE):
            if road == str(row.get("路段编号") or "") or road == str(row.get("路段名称") or ""):
                aliases.update({str(row.get("路段编号")), str(row.get("路段名称"))})
        return aliases

    def _current_version(self, road: str) -> int:
        try:
            return _as_int(self._road_section(road).get("里程版本"))
        except LocatorError:
            return 1

    def _section_range(self, value: Any) -> tuple[str, str]:
        text = str(value or "")
        for separator in ("至", "—", "–", "~", "-"):
            if separator in text:
                left, right = text.split(separator, 1)
                return left.strip(), right.strip()
        return text.strip(), text.strip()

    def _bucket_key_for_meter(self, index: dict[str, Any], meter: float) -> str | None:
        if meter < index["startMeter"] or meter > index["endMeter"]:
            return None
        head = index["startMeter"] + ((meter - index["startMeter"]) // BUCKET_SIZE) * BUCKET_SIZE
        if head >= index["endMeter"]:
            head = index["startMeter"] + (((index["endMeter"] - index["startMeter"]) - 1) // BUCKET_SIZE) * BUCKET_SIZE
        return self._bucket_key(index, head)

    def _meter_in_bucket(self, index: dict[str, Any], key: str, meter: float) -> bool:
        bucket = index["buckets"].get(key)
        return bool(bucket and bucket["head"] <= meter < bucket["end"])

    def _nearest_bucket_key(self, index: dict[str, Any], meter: float | None) -> str | None:
        if meter is None:
            return index["sequence"][0]
        key = self._bucket_key_for_meter(index, meter)
        return key

    def _nearest_head(self, index: dict[str, Any], old_head: float) -> float:
        heads = [bucket["head"] for bucket in index["buckets"].values()]
        return min(heads, key=lambda head: abs(head - old_head))

    def _bucket_key(self, index: dict[str, Any], head: float) -> str:
        return self._bucket_key_from_parts(index["road"], index["version"], head)

    def _bucket_key_from_parts(self, road: str, version: int, head: float) -> str:
        return f"{road}:v{version}:{int(head)}"


locator_service = PavementLocatorService()
