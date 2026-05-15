"""Pydantic schemas for the FastAPI web layer."""
from typing import List, Optional, Any
from pydantic import BaseModel


# ---- Technician ----

class TechnicianCreate(BaseModel):
    name: str
    qualification: str = "정보통신기술자"
    grade: str  # 특급 | 고급 | 중급 | 초급
    hire_date: str = ""


class TechnicianUpdate(BaseModel):
    name: Optional[str] = None
    qualification: Optional[str] = None
    grade: Optional[str] = None
    hire_date: Optional[str] = None
    is_active: Optional[int] = None


# ---- Project ----

class ProjectCreate(BaseModel):
    name: str
    client: str = ""
    amount: int = 0
    contract_date: str = ""
    completion_date: str = ""
    certificate_path: str = ""


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    client: Optional[str] = None
    amount: Optional[int] = None
    contract_date: Optional[str] = None
    completion_date: Optional[str] = None
    certificate_path: Optional[str] = None


# ---- Bid ----

class BidCreate(BaseModel):
    bid_name: str
    bid_number: str = ""
    client: str = ""
    bid_date: str = ""
    company_name: str = ""
    bid_amount: int = 0
    score_management: float = 6.0
    score_experience: float = 6.0
    score_technician: float = 6.0
    score_reputation: float = 2.0


class BidUpdate(BaseModel):
    bid_name: Optional[str] = None
    bid_number: Optional[str] = None
    client: Optional[str] = None
    bid_date: Optional[str] = None
    company_name: Optional[str] = None
    bid_amount: Optional[int] = None
    score_management: Optional[float] = None
    score_experience: Optional[float] = None
    score_technician: Optional[float] = None
    score_reputation: Optional[float] = None
    credit_rating: Optional[str] = None
    restriction_status: Optional[str] = None
    retention_thresholds: Optional[Any] = None
    deployment_thresholds: Optional[Any] = None


# ---- Bid technicians / projects ----

class BidTechniciansSet(BaseModel):
    role: str  # retention | deployment
    ids: List[int]


class BidProjectsSet(BaseModel):
    ids: List[int]


# ---- Extra items ----

class ExtraItemCreate(BaseModel):
    name: str
    max_score: float = 0.0
    actual_score: float = 0.0
    description: str = ""


class ExtraItemUpdate(BaseModel):
    name: Optional[str] = None
    max_score: Optional[float] = None
    actual_score: Optional[float] = None
    description: Optional[str] = None
