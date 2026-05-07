"""Reviewer-side `request_changes` flow.

The reviewer can kick a tile back to the pending queue with a note instead
of approving. Mask is preserved so the classifier can refine in place; the
note surfaces via /api/tiles/{id}/review-note when the tile is picked up
again.
"""
import numpy as np
from tests.conftest import token


def h(t):
    return {"Authorization": f"Bearer {t}"}


def _classify_first_tile(client, op, raw_class: int = 1) -> int:
    op_tok = token(client, op["username"], op["password"])
    r = client.get("/api/tiles/next?project_id=1", headers=h(op_tok))
    assert r.status_code == 200, f"/next returned {r.status_code}: {r.text!r}"
    nxt = r.json()
    raw = bytes([raw_class]) * 65536
    r = client.post(
        f"/api/tiles/{nxt['id']}/classify",
        headers={**h(op_tok), "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert r.status_code == 200, r.text
    return nxt["id"]


def test_request_changes_returns_tile_to_pending(client, admin_user, operators, tiles):
    """op1 classifies; op2 picks up the review queue and requests changes;
    the tile lands back in pending with the note attached."""
    tile_id = _classify_first_tile(client, operators[0])

    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    review = client.get("/api/tiles/next?project_id=1", headers=h(op2_tok)).json()
    assert review["id"] == tile_id and review["status"] == "in_review"

    r = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "Limites de água imprecisos no canto NE."},
        headers=h(op2_tok),
    )
    assert r.status_code == 200, r.text

    # Status returned to pending; assigned_to cleared.
    from backend.database import connect
    conn = connect()
    try:
        row = conn.execute(
            "SELECT status, assigned_to, classified_by, data_png FROM tiles WHERE id=?",
            (tile_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row["status"] == "pending"
    assert row["assigned_to"] is None
    assert row["classified_by"] == operators[0]["id"]  # original classifier preserved
    # Mask NOT wiped — request_changes preserves work, unlike report_problem.
    assert row["data_png"] is not None and len(row["data_png"]) > 100


def test_review_note_surfaces_on_next_load(client, admin_user, operators, tiles):
    """When the classifier picks up the kicked-back tile, /review-note serves
    the most recent note."""
    tile_id = _classify_first_tile(client, operators[0])
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    msg = "Reverificar área cultivada vs floresta."
    client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": msg},
        headers=h(op2_tok),
    )

    op1_tok = token(client, operators[0]["username"], operators[0]["password"])
    r = client.get(f"/api/tiles/{tile_id}/review-note", headers=h(op1_tok))
    assert r.status_code == 200
    body = r.json()
    assert body["note"] == msg
    assert body["by_username"] == operators[1]["username"]


def test_review_note_204_when_none(client, admin_user, operators, tiles):
    """Tiles never kicked back have no note — endpoint returns 204."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    r = client.get(f"/api/tiles/{nxt['id']}/review-note", headers=h(op_tok))
    assert r.status_code == 204


def test_request_changes_requires_review_state(client, admin_user, operators, tiles):
    """Cannot kick back a tile that's not in review (e.g. mid-classification)."""
    op_tok = token(client, operators[0]["username"], operators[0]["password"])
    nxt = client.get("/api/tiles/next?project_id=1", headers=h(op_tok)).json()
    # Tile is in_progress, not in_review — should be rejected.
    r = client.post(
        f"/api/tiles/{nxt['id']}/request-changes",
        json={"note": "x"},
        headers=h(op_tok),
    )
    assert r.status_code == 409


def test_request_changes_rejects_empty_note(client, admin_user, operators, tiles):
    """A note is required — empty/whitespace-only is rejected."""
    tile_id = _classify_first_tile(client, operators[0])
    op2_tok = token(client, operators[1]["username"], operators[1]["password"])
    client.get("/api/tiles/next?project_id=1", headers=h(op2_tok))
    r = client.post(
        f"/api/tiles/{tile_id}/request-changes",
        json={"note": "   "},
        headers=h(op2_tok),
    )
    assert r.status_code == 422 or r.status_code == 400
