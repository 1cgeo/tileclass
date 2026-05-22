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


# ---- Projects ---------------------------------------------------------------

class ProjectClassIn(BaseModel):
    id: int = Field(ge=1, le=254)
    name: str = Field(min_length=1, max_length=64)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class ProjectAttributeIn(BaseModel):
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=80)
    type: Literal["text", "number", "enum", "boolean"]
    required: bool = False
    options: Optional[list[str]] = None  # required for type=enum


class ProjectCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    kind: Literal["raster", "vector", "classification", "detection"] = "raster"
    topology_required: bool = False
    box_required: bool = False
    mask_complete_required: bool = True
    # Tile geometry (defaults match the historical 256×256 @ 2.5 m/px = 640 m
    # tile). Range validated by project_service._validate_tile_geometry too,
    # but Pydantic rejects out-of-range values with a structured 422 first.
    tile_px: int = Field(default=256, ge=1, le=4096)
    meters_per_pixel: float = Field(default=2.5, gt=0)
    primary_mbtiles: str = Field(min_length=1)
    secondary_mbtiles: Optional[str] = None
    tertiary_mbtiles: Optional[str] = None
    ref_mask_primary_mbtiles: Optional[str] = None
    ref_mask_secondary_mbtiles: Optional[str] = None
    # Mutual exclusion enforced server-side: raster wants `classes`,
    # vector wants `attributes`. Both lists optional at the schema layer
    # so the rejection is structured (not a Pydantic 422).
    classes: Optional[list[ProjectClassIn]] = None
    attributes: Optional[list[ProjectAttributeIn]] = None


class ProjectUpdateIn(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    mask_complete_required: Optional[bool] = None
    topology_required: Optional[bool] = None
    box_required: Optional[bool] = None
    # Editable only while the project has no tiles (mask bytes / bbox are
    # otherwise tied to the original geometry). Server returns 409
    # tile_geometry_locked if the project already has tiles.
    tile_px: Optional[int] = Field(default=None, ge=1, le=4096)
    meters_per_pixel: Optional[float] = Field(default=None, gt=0)
    active: Optional[bool] = None
    primary_mbtiles: Optional[str] = None
    secondary_mbtiles: Optional[str] = None
    tertiary_mbtiles: Optional[str] = None
    ref_mask_primary_mbtiles: Optional[str] = None
    ref_mask_secondary_mbtiles: Optional[str] = None


class ProjectClassesIn(BaseModel):
    classes: list[ProjectClassIn] = Field(min_length=1)


class ProjectAttributesIn(BaseModel):
    attributes: list[ProjectAttributeIn] = Field(min_length=0)


class ProjectMemberIn(BaseModel):
    user_id: int
    role: Literal["operator", "reviewer", "admin"]
