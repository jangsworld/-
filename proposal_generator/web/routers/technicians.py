"""Technician CRUD API endpoints."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.database import (
    get_all_technicians, add_technician, update_technician, delete_technician,
)
from web.schemas import TechnicianCreate, TechnicianUpdate

router = APIRouter(tags=["technicians"])


@router.get("/technicians")
def list_technicians():
    return get_all_technicians()


@router.post("/technicians", status_code=201)
def create_technician(body: TechnicianCreate):
    new_id = add_technician(
        body.name, body.qualification, body.grade, body.hire_date
    )
    return {"id": new_id, "message": "기술자가 추가되었습니다"}


@router.put("/technicians/{tech_id}")
def update_technician_route(tech_id: int, body: TechnicianUpdate):
    update_technician(
        tech_id,
        name=body.name,
        qualification=body.qualification,
        grade=body.grade,
        hire_date=body.hire_date,
        is_active=body.is_active,
    )
    return {"message": "수정되었습니다"}


@router.delete("/technicians/{tech_id}")
def delete_technician_route(tech_id: int):
    delete_technician(tech_id)
    return {"message": "삭제되었습니다"}
