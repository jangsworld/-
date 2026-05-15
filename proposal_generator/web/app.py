"""FastAPI application factory and page routes."""
import os
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.database import (
    init_db, get_all_bids, create_bid, get_bid, delete_bid,
    get_all_technicians, get_bid_technicians, get_bid_projects,
    get_extra_items,
)
from core.calculator import (
    calc_management_score, calc_experience_score,
    calc_technician_score, calc_reputation_score, calc_total_score,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DOCUMENTS_ROOT = BASE_DIR / "documents"

# ── Sub-folders to guarantee on startup ──────────────────────────────────────
DOCUMENT_FOLDERS = [
    "신용평가등급",
    "실적증명서",
    "기술자",
    "재직증명서",
    "건강보험명부",
    "입찰참가자격등록증",
    "부정당업자확인서",
    "기타",
]

app = FastAPI(title="정량적 제안서 시스템")


def create_app() -> FastAPI:
    """Ensure folders exist and attach routers, then return the app."""
    _ensure_document_folders()
    _attach_routers()
    return app


def _ensure_document_folders():
    for folder in DOCUMENT_FOLDERS:
        (DOCUMENTS_ROOT / folder).mkdir(parents=True, exist_ok=True)


def _attach_routers():
    from web.routers import (
        technicians, projects, bids, documents, generate,
    )
    app.include_router(technicians.router, prefix="/api")
    app.include_router(projects.router, prefix="/api")
    app.include_router(bids.router, prefix="/api")
    app.include_router(documents.router, prefix="/api")
    app.include_router(generate.router, prefix="/api")


# ── Static files & templates ──────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# ── Utility: compute scores dict for a bid ───────────────────────────────────

def _compute_scores(bid: dict) -> dict:
    import json

    ret_list = get_bid_technicians(bid["id"], "retention")
    dep_list = get_bid_technicians(bid["id"], "deployment")
    projects = get_bid_projects(bid["id"])
    extra_items = get_extra_items(bid["id"])

    total_amount = sum(p.get("amount", 0) for p in projects)
    bid_amount = bid.get("bid_amount", 0)

    # thresholds
    try:
        ret_thresh = json.loads(bid.get("retention_thresholds") or "[]") or None
    except Exception:
        ret_thresh = None
    try:
        dep_thresh = json.loads(bid.get("deployment_thresholds") or "[]") or None
    except Exception:
        dep_thresh = None

    mgmt_score = calc_management_score(
        bid.get("credit_rating", "BBB+"), bid.get("score_management", 6.0)
    )
    exp_score, pct = calc_experience_score(
        total_amount, bid_amount, bid.get("score_experience", 6.0)
    )
    tech_result = calc_technician_score(
        ret_list, dep_list,
        bid.get("score_technician", 6.0),
        ret_thresh, dep_thresh,
    )
    rep_score = calc_reputation_score(
        bid.get("restriction_status", "none"),
        bid.get("score_reputation", 2.0),
    )
    grand_total = calc_total_score(
        mgmt_score, exp_score, tech_result["total_score"], rep_score, extra_items
    )

    return {
        "management": {
            "rating": bid.get("credit_rating", "BBB+"),
            "score": mgmt_score,
            "max_score": bid.get("score_management", 6.0),
        },
        "experience": {
            "total_amount": total_amount,
            "percentage": pct,
            "score": exp_score,
            "max_score": bid.get("score_experience", 6.0),
        },
        "technician": {
            "retention_raw": tech_result["retention_raw"],
            "deployment_raw": tech_result["deployment_raw"],
            "retention_score": tech_result["retention_score"],
            "deployment_score": tech_result["deployment_score"],
            "total_score": tech_result["total_score"],
            "max_score": bid.get("score_technician", 6.0),
            "retention_list": ret_list,
            "deployment_list": dep_list,
        },
        "reputation": {
            "status": bid.get("restriction_status", "none"),
            "score": rep_score,
            "max_score": bid.get("score_reputation", 2.0),
        },
        "extra": {
            "items": extra_items,
            "total": sum(i.get("actual_score", 0) for i in extra_items),
        },
        "grand_total": grand_total,
    }


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    bids = get_all_bids()
    # Attach grand_total to each bid for the table
    for b in bids:
        try:
            sc = _compute_scores(b)
            b["grand_total"] = sc["grand_total"]
        except Exception:
            b["grand_total"] = 0.0
    return templates.TemplateResponse(request, "index.html", {"bids": bids})


@app.post("/bids/new")
async def new_bid(bid_name: str = Form(...)):
    if not bid_name.strip():
        raise HTTPException(status_code=400, detail="공고명을 입력해주세요")
    new_id = create_bid(bid_name.strip())
    return RedirectResponse(url=f"/bids/{new_id}", status_code=303)


@app.get("/bids/{bid_id}", response_class=HTMLResponse)
async def bid_detail(request: Request, bid_id: int):
    bid = get_bid(bid_id)
    if not bid:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다")
    all_techs = get_all_technicians()
    ret_ids = {t["id"] for t in get_bid_technicians(bid_id, "retention")}
    dep_ids = {t["id"] for t in get_bid_technicians(bid_id, "deployment")}
    all_projects = __import__("core.database", fromlist=["get_all_projects"]).get_all_projects()
    selected_project_ids = {p["id"] for p in get_bid_projects(bid_id)}
    extra_items = get_extra_items(bid_id)
    scores = _compute_scores(bid)

    import json
    try:
        ret_thresh = json.loads(bid.get("retention_thresholds") or "[]")
    except Exception:
        ret_thresh = []
    try:
        dep_thresh = json.loads(bid.get("deployment_thresholds") or "[]")
    except Exception:
        dep_thresh = []

    return templates.TemplateResponse(request, "bid_detail.html", {
        "bid": bid,
        "all_techs": all_techs,
        "ret_ids": list(ret_ids),
        "dep_ids": list(dep_ids),
        "all_projects": all_projects,
        "selected_project_ids": list(selected_project_ids),
        "extra_items": extra_items,
        "scores": scores,
        "retention_thresholds": ret_thresh,
        "deployment_thresholds": dep_thresh,
        "documents_root": str(DOCUMENTS_ROOT),
    })


@app.post("/bids/{bid_id}/delete")
async def delete_bid_route(bid_id: int):
    bid = get_bid(bid_id)
    if not bid:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다")
    delete_bid(bid_id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/technicians", response_class=HTMLResponse)
async def technicians_page(request: Request):
    techs = get_all_technicians()
    return templates.TemplateResponse(request, "technicians.html", {"technicians": techs})


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    import core.database as db
    projs = db.get_all_projects()
    return templates.TemplateResponse(request, "projects.html", {"projects": projs})


# Make _compute_scores accessible to routers
app.state.compute_scores = _compute_scores
app.state.documents_root = DOCUMENTS_ROOT

# Run create_app() so folders/routers attach when this module is imported
create_app()
