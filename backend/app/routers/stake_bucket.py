"""桩号桶定位器接口：桶索引、状态栅栏、巡线结论与改线版本控制。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import (
    BucketConclusionPayload,
    BucketIndexPayload,
    BucketLocatePayload,
    BucketReroutePayload,
    BucketSessionPayload,
    BucketStakePayload,
)
from app.services.stake_bucket import BucketProtocolError, stake_bucket_service

router = APIRouter(prefix="/api/stake-buckets", tags=["桩号桶定位器"])


def _raise_protocol(error: BucketProtocolError) -> None:
    raise HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/routes")
def list_routes() -> dict[str, object]:
    """列出道路方向与现行桶索引版本。"""
    return {"items": stake_bucket_service.list_routes()}


@router.post("/indexes")
def build_index(payload: BucketIndexPayload) -> dict[str, object]:
    """第一步：建立桶索引；重复建立会生成兼容旧游标的新索引版本。"""
    try:
        return stake_bucket_service.build_index(
            payload.road,
            expected_version=payload.expected_version,
            bucket_size=payload.bucket_size,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/rebuild-index")
def rebuild_index(payload: BucketIndexPayload) -> dict[str, object]:
    """在不改变现行里程的前提下重建桶索引，并兼容既有浏览游标。"""
    try:
        return stake_bucket_service.rebuild_index(
            payload.road,
            expected_version=payload.expected_version,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/reroutes")
def reroute(road: str, payload: BucketReroutePayload) -> dict[str, object]:
    """改线：只允许新的现行版本覆盖路线，旧病害保留桩号快照。"""
    try:
        return stake_bucket_service.reroute(
            road,
            expected_version=payload.expected_version,
            start=payload.start,
            end=payload.end,
            direction=payload.direction,
            bucket_size=payload.bucket_size,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.get("/routes/{road}/buckets")
def list_buckets(road: str, cursor: str | None = Query(default=None)) -> dict[str, object]:
    """读取按道路方向形成的连续桶，游标可来自旧索引。"""
    try:
        return stake_bucket_service.list_buckets(road, cursor)
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/routes/{road}/sessions")
def start_session(road: str, payload: BucketSessionPayload) -> dict[str, object]:
    """建立索引后开启定位会话，初始阶段只允许校验桩号。"""
    try:
        return stake_bucket_service.start_session(road, cursor=payload.cursor)
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/sessions/{session_id}/validate-stake")
def validate_stake(session_id: str, payload: BucketStakePayload) -> dict[str, object]:
    """第二步：校验桩号，未建立索引或倒序重放会被拒绝。"""
    try:
        return stake_bucket_service.validate_stake(
            session_id,
            stake=payload.stake,
            disease_id=payload.disease_id,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/sessions/{session_id}/locate")
def locate(session_id: str, payload: BucketLocatePayload) -> dict[str, object]:
    """第三步：切换定位，并同时返回病害列表、巡查详情和桥隧限载提示。"""
    try:
        return stake_bucket_service.locate(
            session_id,
            disease_id=payload.disease_id,
            origin_bucket=payload.origin_bucket,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/sessions/{session_id}/return")
def return_to_origin(session_id: str) -> dict[str, object]:
    """滚桶预览后跳回建立定位时记录的原桶位置。"""
    try:
        return stake_bucket_service.return_to_origin(session_id)
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.get("/sessions/{session_id}/diseases/{disease_id}/neighbors")
def neighbors(session_id: str, disease_id: int) -> dict[str, object]:
    """按病害编号预览相邻桶记录。"""
    try:
        return stake_bucket_service.neighbors(session_id, disease_id)
    except BucketProtocolError as error:
        _raise_protocol(error)


@router.post("/sessions/{session_id}/conclusion")
def conclude(session_id: str, payload: BucketConclusionPayload) -> dict[str, object]:
    """巡线结论一次性同步到三个业务清单，并防止重复提交。"""
    try:
        return stake_bucket_service.conclude(
            session_id,
            conclusion=payload.conclusion,
            action=payload.action or "纳入待办",
            target_type=payload.target_type,
            target_id=payload.target_id,
            inspector=payload.inspector,
        )
    except BucketProtocolError as error:
        _raise_protocol(error)
