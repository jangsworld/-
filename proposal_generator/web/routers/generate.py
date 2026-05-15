"""PPT generation and download endpoint."""
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.database import get_bid, get_bid_technicians, get_bid_projects, get_extra_items
from core.ppt_generator import generate_ppt

router = APIRouter(tags=["generate"])


@router.get("/bids/{bid_id}/generate")
def generate_ppt_route(bid_id: int, request: Request):
    bid = get_bid(bid_id)
    if not bid:
        raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다")

    scores = request.app.state.compute_scores(bid)
    ret_list = get_bid_technicians(bid_id, "retention")
    dep_list = get_bid_technicians(bid_id, "deployment")
    projects = get_bid_projects(bid_id)
    extra_items = get_extra_items(bid_id)

    bid_data = {
        "bid_info": {
            "bid_name": bid.get("bid_name", ""),
            "bid_number": bid.get("bid_number", ""),
            "company_name": bid.get("company_name", ""),
            "client": bid.get("client", ""),
            "bid_date": bid.get("bid_date", ""),
            "bid_amount": bid.get("bid_amount", 0),
        },
        "management": {
            "rating": scores["management"]["rating"],
            "score": scores["management"]["score"],
            "max_score": scores["management"]["max_score"],
        },
        "experience": {
            "projects": projects,
            "total_amount": scores["experience"]["total_amount"],
            "percentage": scores["experience"]["percentage"],
            "score": scores["experience"]["score"],
            "max_score": scores["experience"]["max_score"],
        },
        "technician": {
            "retention_list": ret_list,
            "deployment_list": dep_list,
            "retention_raw": scores["technician"]["retention_raw"],
            "deployment_raw": scores["technician"]["deployment_raw"],
            "retention_score": scores["technician"]["retention_score"],
            "deployment_score": scores["technician"]["deployment_score"],
            "total_score": scores["technician"]["total_score"],
            "max_score": scores["technician"]["max_score"],
        },
        "reputation": {
            "status": scores["reputation"]["status"],
            "score": scores["reputation"]["score"],
            "max_score": scores["reputation"]["max_score"],
        },
        "extra_items": extra_items,
        "total_score": scores["grand_total"],
    }

    # Write to a temp file
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pptx", delete=False,
        prefix=f"proposal_{bid_id}_"
    )
    tmp.close()
    output_path = tmp.name

    try:
        generate_ppt(bid_data, output_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PPT 생성 오류: {e}")

    safe_name = bid.get("bid_name", "proposal").replace("/", "_").replace("\\", "_")
    filename = f"{safe_name}_정량평가.pptx"

    return FileResponse(
        path=output_path,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        background=None,  # file stays until response is sent
    )
