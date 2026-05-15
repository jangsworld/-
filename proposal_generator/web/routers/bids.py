"""Bid API endpoints."""
import json
from fastapi import APIRouter, HTTPException, Request

from core.database import (
    get_bid, update_bid,
    get_bid_technicians, set_bid_technicians,
    get_bid_projects, set_bid_projects,
    get_extra_items, add_extra_item, update_extra_item, delete_extra_item,
    get_all_technicians, get_all_projects,
)
from core.calculator import (
    calc_management_score, calc_experience_score,
    calc_technician_score, calc_reputation_score, calc_total_score,
)
from web.schemas import BidUpdate, BidTechniciansSet, BidProjectsSet, ExtraItemCreate, ExtraItemUpdate

router = APIRouter(tags=["bids"])


def _get_bid_or_404(bid_id: int):
    bid = get_bid(bid_id)
    if not bid:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다")
    return bid


@router.get("/bids/{bid_id}")
def get_bid_detail(bid_id: int, request: Request):
    bid = _get_bid_or_404(bid_id)
    ret_list = get_bid_technicians(bid_id, "retention")
    dep_list = get_bid_technicians(bid_id, "deployment")
    projects = get_bid_projects(bid_id)
    extra = get_extra_items(bid_id)
    scores = request.app.state.compute_scores(bid)
    return {
        "bid": bid,
        "retention_technicians": ret_list,
        "deployment_technicians": dep_list,
        "projects": projects,
        "extra_items": extra,
        "scores": scores,
    }


@router.put("/bids/{bid_id}")
def update_bid_route(bid_id: int, body: BidUpdate):
    _get_bid_or_404(bid_id)
    data = body.model_dump(exclude_none=True)
    # Serialize threshold lists to JSON strings if sent as lists
    for key in ("retention_thresholds", "deployment_thresholds"):
        if key in data and not isinstance(data[key], str):
            data[key] = json.dumps(data[key])
    update_bid(bid_id, **data)
    return {"message": "저장되었습니다"}


@router.get("/bids/{bid_id}/technicians")
def list_bid_technicians(bid_id: int, role: str = "retention"):
    _get_bid_or_404(bid_id)
    return get_bid_technicians(bid_id, role)


@router.post("/bids/{bid_id}/technicians")
def set_bid_technicians_route(bid_id: int, body: BidTechniciansSet):
    _get_bid_or_404(bid_id)
    set_bid_technicians(bid_id, body.role, body.ids)
    return {"message": "기술인력이 설정되었습니다"}


@router.get("/bids/{bid_id}/projects")
def list_bid_projects(bid_id: int):
    _get_bid_or_404(bid_id)
    return get_bid_projects(bid_id)


@router.post("/bids/{bid_id}/projects")
def set_bid_projects_route(bid_id: int, body: BidProjectsSet):
    _get_bid_or_404(bid_id)
    set_bid_projects(bid_id, body.ids)
    return {"message": "수행실적이 설정되었습니다"}


@router.get("/bids/{bid_id}/scores")
def get_bid_scores(bid_id: int, request: Request):
    bid = _get_bid_or_404(bid_id)
    return request.app.state.compute_scores(bid)


# ── Extra items ───────────────────────────────────────────────────────────────

@router.get("/bids/{bid_id}/extra_items")
def list_extra_items(bid_id: int):
    _get_bid_or_404(bid_id)
    return get_extra_items(bid_id)


@router.post("/bids/{bid_id}/extra_items", status_code=201)
def create_extra_item(bid_id: int, body: ExtraItemCreate):
    _get_bid_or_404(bid_id)
    new_id = add_extra_item(bid_id, body.name, body.max_score, body.actual_score, body.description)
    return {"id": new_id, "message": "항목이 추가되었습니다"}


@router.put("/bids/{bid_id}/extra_items/{item_id}")
def update_extra_item_route(bid_id: int, item_id: int, body: ExtraItemUpdate):
    _get_bid_or_404(bid_id)
    update_extra_item(
        item_id,
        name=body.name,
        max_score=body.max_score,
        actual_score=body.actual_score,
        description=body.description,
    )
    return {"message": "수정되었습니다"}


@router.delete("/bids/{bid_id}/extra_items/{item_id}")
def delete_extra_item_route(bid_id: int, item_id: int):
    _get_bid_or_404(bid_id)
    delete_extra_item(item_id)
    return {"message": "삭제되었습니다"}
