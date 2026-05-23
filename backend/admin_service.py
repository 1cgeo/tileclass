"""Public facade — implementation lives in `backend/admin/{dashboard,
tiles_query, tiles_mutations, users, thumbnails}.py`. Re-exports keep the
existing import paths (`from .. import admin_service`, tests, routers) working
without touching call sites."""
from .admin.dashboard import (
    dashboard, class_distribution, feature_distribution, tile_class_distribution,
    detection_distribution, projects_stats,
)
from .admin.tiles_query import (
    list_tiles,
    count_tiles,
    list_tiles_map,
    list_problems,
)
from .admin.tiles_mutations import (
    reset_many,
    reset_tile,
    report_problem_many,
    re_review_many,
    re_review_tile,
    assign_many,
    assign_operator,
    delete_tile,
    admin_pause_tile,
    unassign_operator,
    unassign_many,
    block_many,
    unblock_many,
    block_tile,
    unblock_tile,
)
from .admin.users import (
    list_users,
    create_user,
    set_user_role,
    set_user_active,
)
from .admin.thumbnails import (
    tile_thumbnail,
    tile_satellite_thumbnail,
)

__all__ = [
    "dashboard", "class_distribution", "feature_distribution",
    "tile_class_distribution", "detection_distribution", "projects_stats",
    "list_tiles", "count_tiles", "list_tiles_map", "list_problems",
    "reset_many", "reset_tile",
    "report_problem_many",
    "re_review_many", "re_review_tile",
    "assign_many", "assign_operator",
    "delete_tile",
    "admin_pause_tile",
    "unassign_operator", "unassign_many",
    "block_many", "unblock_many", "block_tile", "unblock_tile",
    "list_users", "create_user", "set_user_role",
    "set_user_active",
    "tile_thumbnail", "tile_satellite_thumbnail",
]
