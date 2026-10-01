"""桩号桶定位器接口：服务端强制状态栅栏、版本栅栏和巡线同步。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from app.services.locator import LocatorError, locator_service

router = APIRouter(prefix="/api/pavement/locator", tags=["桩号桶定位器"])


class BuildIndexPayload(BaseModel):
    road: str
    expectedVersion: int | None = None


class ValidateStationPayload(BaseModel):
    road: str
    station: str
    expectedVersion: int | None = None


class LocatePayload(BaseModel):
    road: str
    diseaseId: int
    expectedVersion: int | None = None


class PreviewPayload(BaseModel):
    road: str
    diseaseCode: str
    expectedVersion: int | None = None


class AddDiseasePayload(BaseModel):
    expectedVersion: int | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class UndoDiseasePayload(BaseModel):
    expectedVersion: int | None = None


class RealignPayload(BaseModel):
    road: str
    expectedVersion: int
    newStart: str
    newEnd: str
    direction: str | None = None


class PatrolConclusionPayload(BaseModel):
    road: str
    expectedVersion: int | None = None
    conclusion: str
    patrolId: int | None = None
    bridgeLimit: str | None = None
    tunnelLimit: str | None = None


def _raise(error: LocatorError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/state")
def get_state(
    road: str = Query(description="路段编号或名称"),
    cursor: str | None = Query(default=None, description="现有浏览桶游标"),
) -> dict[str, Any]:
    """读取当前定位阶段、连续桶、现行病害、旧桩号快照和同步清单。"""
    try:
        return locator_service.state(road, cursor)
    except LocatorError as error:
        _raise(error)


@router.post("/build-index")
def build_index(payload: BuildIndexPayload) -> dict[str, Any]:
    """第一步：建立现行里程体系的桶索引；旧版本请求会被版本栅栏拒绝。"""
    try:
        return locator_service.build_index(payload.road, payload.expectedVersion)
    except LocatorError as error:
        _raise(error)


@router.post("/validate-station")
def validate_station(payload: ValidateStationPayload) -> dict[str, Any]:
    """第二步：校验桩号并定位到候选桶；未建索引或倒序调用都拒绝。"""
    try:
        return locator_service.validate_station(payload.road, payload.station, payload.expectedVersion)
    except LocatorError as error:
        _raise(error)


@router.post("/locate")
def locate(payload: LocatePayload) -> dict[str, Any]:
    """第三步：切换定位。服务端确认病害属于已校验桶后才允许进入巡线。"""
    try:
        return locator_service.locate(payload.road, payload.diseaseId, payload.expectedVersion)
    except LocatorError as error:
        _raise(error)


@router.post("/preview")
def preview_disease(payload: PreviewPayload) -> dict[str, Any]:
    """按编号预览相邻病害，不改动定位栅栏；旧桩号记录只读并明确标识。"""
    try:
        return locator_service.preview_disease(payload.road, payload.diseaseCode, payload.expectedVersion)
    except LocatorError as error:
        _raise(error)


@router.post("/defects")
def add_defect(payload: AddDiseasePayload) -> dict[str, Any]:
    """新增当前路线病害并即时入桶，重建索引时保证编号唯一且不漏项。"""
    try:
        result = locator_service.add_disease(payload.values | {"expectedVersion": payload.expectedVersion})
    except LocatorError as error:
        _raise(error)
    else:
        return {"ok": True, "message": "病害已加入桩号桶", **result}


@router.post("/defects/{disease_id}/undo")
def undo_defect(disease_id: int, payload: UndoDiseasePayload) -> dict[str, Any]:
    """撤销病害：台账删除记录，桶索引同步移除，避免重复和悬挂项。"""
    try:
        result = locator_service.undo_disease(disease_id, payload.expectedVersion)
    except LocatorError as error:
        _raise(error)
    else:
        return result


@router.post("/realign")
def realign_route(payload: RealignPayload) -> dict[str, Any]:
    """改线为新版本；旧病害保留快照，旧桶归档且不能覆盖现行路线。"""
    try:
        return locator_service.realign_route(
            payload.road,
            expected_version=payload.expectedVersion,
            new_start=payload.newStart,
            new_end=payload.newEnd,
            direction=payload.direction,
        )
    except LocatorError as error:
        _raise(error)


@router.post("/patrol-conclusion")
def sync_patrol_conclusion(payload: PatrolConclusionPayload) -> dict[str, Any]:
    """巡线结论同步到巡查待办、桥隧限载清单和病害台账。"""
    try:
        result = locator_service.sync_patrol_conclusion(
            payload.road,
            expected_version=payload.expectedVersion,
            conclusion=payload.conclusion,
            patrol_id=payload.patrolId,
            bridge_limit=payload.bridgeLimit,
            tunnel_limit=payload.tunnelLimit,
        )
    except LocatorError as error:
        _raise(error)
    else:
        return {"ok": True, "message": "巡线结论已同步到待办、桥隧限载清单和病害台账", **result}
