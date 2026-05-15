"""Project CRUD API endpoints."""
from fastapi import APIRouter, HTTPException

from core.database import (
    get_all_projects, add_project, update_project, delete_project,
)
from web.schemas import ProjectCreate, ProjectUpdate

router = APIRouter(tags=["projects"])


@router.get("/projects")
def list_projects():
    return get_all_projects()


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate):
    new_id = add_project(
        body.name, body.client, body.amount,
        body.contract_date, body.completion_date, body.certificate_path,
    )
    return {"id": new_id, "message": "실적이 추가되었습니다"}


@router.put("/projects/{project_id}")
def update_project_route(project_id: int, body: ProjectUpdate):
    update_project(
        project_id,
        name=body.name,
        client=body.client,
        amount=body.amount,
        contract_date=body.contract_date,
        completion_date=body.completion_date,
        certificate_path=body.certificate_path,
    )
    return {"message": "수정되었습니다"}


@router.delete("/projects/{project_id}")
def delete_project_route(project_id: int):
    delete_project(project_id)
    return {"message": "삭제되었습니다"}
