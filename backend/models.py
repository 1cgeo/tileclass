"""Pydantic schemas for request/response bodies."""
from pydantic import BaseModel, Field
from typing import Optional, Literal


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str


class ClassOut(BaseModel):
    id: int
    name: str
    color: str


class TileOut(BaseModel):
    id: int
    name: str
    bbox_west: float
    bbox_south: float
    bbox_east: float
    bbox_north: float
    status: str
    assigned_to: Optional[int]
    assigned_to_username: Optional[str] = None
    classified_by: Optional[int]
    classified_by_username: Optional[str] = None
    reviewed_by: Optional[int]
    reviewed_by_username: Optional[str] = None
    version: int = 1
    filled_pixels: int = 0
    paused_at: Optional[str] = None
    blocked_from: Optional[str] = None


class ReportProblemIn(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class BulkTileIdsIn(BaseModel):
    ids: list[int]
    reason: Optional[str] = Field(default=None, max_length=500)


class BulkReportProblemIn(BaseModel):
    ids: list[int] = Field(min_length=1)
    note: str = Field(min_length=1, max_length=2000)


class ResetReasonIn(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class CreateUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6)
    role: Literal["operator", "admin"] = "operator"


class DashboardOut(BaseModel):
    totals_by_status: dict
    total_tiles: int
    completion_percent: float
    daily_completed: list
    per_operator: list
    avg_classify_seconds: float = 0.0
    avg_review_seconds: float = 0.0
    rate_per_day: float
    eta_days: Optional[float]
    paused_count: int = 0
    paused_by_status: dict = {}


class SetActiveIn(BaseModel):
    active: bool


class SetCanReviewIn(BaseModel):
    can_review: bool


class SetRoleIn(BaseModel):
    role: Literal["operator", "admin"]


class AssignTileIn(BaseModel):
    user_id: int
    reason: str | None = None


class BulkAssignIn(BaseModel):
    tile_ids: list[int] = Field(min_length=1)
    user_id: int
    reason: Optional[str] = Field(default=None, max_length=500)
