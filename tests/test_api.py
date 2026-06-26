"""API integration tests against an in-memory repository."""

from __future__ import annotations

from supernote_task_service.models import TaskCreate
from tests.conftest import FakeRepository


def test_healthz_is_public(client) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_requires_auth(client) -> None:
    assert client.get("/v1/tasks").status_code == 401
    assert client.get("/v1/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_task_crud_flow(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "Write tests"}, headers=auth)
    assert created.status_code == 201
    task_id = created.json()["id"]

    got = client.get(f"/v1/tasks/{task_id}", headers=auth)
    assert got.status_code == 200
    assert got.json()["title"] == "Write tests"
    assert got.json()["status"] == "needsAction"

    patched = client.patch(f"/v1/tasks/{task_id}", json={"title": "Write more tests"}, headers=auth)
    assert patched.status_code == 200
    assert patched.json()["title"] == "Write more tests"

    completed = client.post(f"/v1/tasks/{task_id}/complete", headers=auth)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["completed"] is not None

    deleted = client.delete(f"/v1/tasks/{task_id}", headers=auth)
    assert deleted.status_code == 204
    assert client.get(f"/v1/tasks/{task_id}", headers=auth).status_code == 404


def test_invalid_task_id_rejected(client, auth) -> None:
    assert client.get("/v1/tasks/not-a-valid-id", headers=auth).status_code == 422


def test_update_requires_a_field(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "x"}, headers=auth)
    task_id = created.json()["id"]
    assert client.patch(f"/v1/tasks/{task_id}", json={}, headers=auth).status_code == 422


def test_create_task_with_unknown_list_404(client, auth) -> None:
    resp = client.post("/v1/tasks", json={"title": "x", "list_id": "0" * 32}, headers=auth)
    assert resp.status_code == 404


def test_list_crud_and_task_assignment(client, auth) -> None:
    created = client.post("/v1/lists", json={"title": "Work"}, headers=auth)
    assert created.status_code == 201
    list_id = created.json()["id"]

    task = client.post("/v1/tasks", json={"title": "Job", "list_id": list_id}, headers=auth)
    assert task.status_code == 201

    renamed = client.patch(f"/v1/lists/{list_id}", json={"title": "Job"}, headers=auth)
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Job"

    lists = client.get("/v1/lists", headers=auth).json()["lists"]
    titles = {item["title"] for item in lists}
    assert "Inbox" in titles and "Job" in titles

    assert client.delete(f"/v1/lists/{list_id}", headers=auth).status_code == 204


def test_incremental_sync_returns_changes_and_deletions(client, auth) -> None:
    first = client.get("/v1/tasks?since=0", headers=auth)
    cursor = first.json()["cursor"]

    created = client.post("/v1/tasks", json={"title": "Delta"}, headers=auth)
    task_id = created.json()["id"]

    delta = client.get(f"/v1/tasks?since={cursor}", headers=auth)
    ids = {t["id"] for t in delta.json()["tasks"]}
    assert task_id in ids
    new_cursor = delta.json()["cursor"]

    client.delete(f"/v1/tasks/{task_id}", headers=auth)
    after_delete = client.get(f"/v1/tasks?since={new_cursor}", headers=auth)
    deleted = [t for t in after_delete.json()["tasks"] if t["id"] == task_id]
    assert deleted and deleted[0]["is_deleted"] is True


def test_emoji_is_preserved(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "Shop \U0001f6d2"}, headers=auth)
    task_id = created.json()["id"]
    got = client.get(f"/v1/tasks/{task_id}", headers=auth)
    assert got.json()["title"] == "Shop \U0001f6d2"


def test_rate_limit_returns_429(low_rate_client, auth) -> None:
    # Limit is 5 per window in this fixture.
    statuses = [low_rate_client.get("/v1/lists", headers=auth).status_code for _ in range(6)]
    assert statuses[-1] == 429
    assert 200 in statuses


def test_unauthenticated_requests_are_rate_limited(low_rate_client) -> None:
    # Pre-auth per-IP limiting (default 30/window) eventually throttles even
    # invalid keys, so brute-forcing returns 429 rather than unlimited 401s.
    statuses = [
        low_rate_client.get("/v1/tasks", headers={"Authorization": "Bearer nope"}).status_code
        for _ in range(40)
    ]
    assert 401 in statuses
    assert 429 in statuses


def test_delta_pagination_drains_same_millisecond_group() -> None:
    # More rows than the page limit share one last_modified millisecond. The
    # page-cursor must drain the whole group and advance past it so a syncing
    # client makes progress instead of looping on the same timestamp forever.
    repo = FakeRepository()
    ids = [repo.create_task(TaskCreate(title=f"t{i}", list_id=None)) for i in range(3)]
    for tid in ids:
        repo._tasks[tid]["last_modified"] = 1000

    page, cursor, has_more = repo.list_tasks(since=500, limit=2)
    assert {t.id for t in page} == set(ids)
    assert cursor == 1001
    assert has_more is False

    nxt, _, nxt_more = repo.list_tasks(since=cursor, limit=2)
    assert nxt == []
    assert nxt_more is False


# ----- P1: importance ------------------------------------------------------


def test_importance_round_trip(client, auth) -> None:
    for value in (1, 3, 5, None):
        created = client.post("/v1/tasks", json={"title": "p", "importance": value}, headers=auth)
        assert created.status_code == 201
        assert created.json()["importance"] == value
        tid = created.json()["id"]
        assert client.get(f"/v1/tasks/{tid}", headers=auth).json()["importance"] == value


def test_importance_out_of_range_rejected(client, auth) -> None:
    low = client.post("/v1/tasks", json={"title": "p", "importance": 0}, headers=auth)
    high = client.post("/v1/tasks", json={"title": "p", "importance": 6}, headers=auth)
    assert low.status_code == 422
    assert high.status_code == 422


def test_importance_patch_change_clear_and_omit(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "p", "importance": 2}, headers=auth)
    tid = created.json()["id"]

    changed = client.patch(f"/v1/tasks/{tid}", json={"importance": 4}, headers=auth)
    assert changed.json()["importance"] == 4

    # Omitting importance leaves it unchanged.
    other = client.patch(f"/v1/tasks/{tid}", json={"title": "p2"}, headers=auth)
    assert other.json()["importance"] == 4

    # Explicit null clears it.
    cleared = client.patch(f"/v1/tasks/{tid}", json={"importance": None}, headers=auth)
    assert cleared.json()["importance"] is None


# ----- P2: POST returns the full resource ----------------------------------


def test_post_task_returns_full_resource(client, auth) -> None:
    resp = client.post("/v1/tasks", json={"title": "full"}, headers=auth)
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "full"
    assert "id" in body and "last_modified" in body and body["last_modified"] > 0
    assert body["status"] == "needsAction"


def test_post_list_returns_full_resource(client, auth) -> None:
    resp = client.post("/v1/lists", json={"title": "Groceries"}, headers=auth)
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Groceries"
    assert "id" in body and body["last_modified"] > 0


# ----- P3: has_more --------------------------------------------------------


def test_has_more_paging(client, repo, auth) -> None:
    ids = [
        client.post("/v1/tasks", json={"title": f"t{i}"}, headers=auth).json()["id"]
        for i in range(3)
    ]
    for i, tid in enumerate(ids):
        repo._tasks[tid]["last_modified"] = 1000 + i  # distinct timestamps

    first = client.get("/v1/tasks?since=0&limit=2", headers=auth)
    assert first.json()["has_more"] is True
    cursor = first.json()["cursor"]
    # Resume strictly after the boundary to avoid re-delivering the boundary row.
    second = client.get(f"/v1/tasks?since={cursor + 1}&limit=2", headers=auth)
    assert second.json()["has_more"] is False


# ----- P4: optimistic concurrency ------------------------------------------


def test_conditional_update_matching_version(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "x"}, headers=auth)
    tid = created.json()["id"]
    lm = created.json()["last_modified"]
    resp = client.patch(
        f"/v1/tasks/{tid}",
        json={"title": "y"},
        headers={**auth, "If-Unmodified-Since": str(lm)},
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "y"


def test_conditional_update_stale_version_conflicts(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "x"}, headers=auth)
    tid = created.json()["id"]
    resp = client.patch(
        f"/v1/tasks/{tid}",
        json={"title": "y"},
        headers={**auth, "If-Unmodified-Since": "1"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"
    # No write happened.
    assert client.get(f"/v1/tasks/{tid}", headers=auth).json()["title"] == "x"


def test_conditional_delete_and_complete(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "x"}, headers=auth)
    tid = created.json()["id"]
    stale = {**auth, "If-Unmodified-Since": "1"}
    assert client.post(f"/v1/tasks/{tid}/complete", headers=stale).status_code == 409
    assert client.delete(f"/v1/tasks/{tid}", headers=stale).status_code == 409


def test_conditional_missing_row_is_404(client, auth) -> None:
    resp = client.patch(
        f"/v1/tasks/{'0' * 32}",
        json={"title": "y"},
        headers={**auth, "If-Unmodified-Since": "1"},
    )
    assert resp.status_code == 404


# ----- P5: idempotent list creation ----------------------------------------


def test_create_list_is_idempotent_by_title(client, auth) -> None:
    first = client.post("/v1/lists", json={"title": "Shared"}, headers=auth)
    assert first.status_code == 201
    again = client.post("/v1/lists", json={"title": "Shared"}, headers=auth)
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]


def test_create_list_after_soft_delete_creates_new(client, auth) -> None:
    first = client.post("/v1/lists", json={"title": "Temp"}, headers=auth)
    lid = first.json()["id"]
    assert client.delete(f"/v1/lists/{lid}", headers=auth).status_code == 204
    again = client.post("/v1/lists", json={"title": "Temp"}, headers=auth)
    assert again.status_code == 201
    assert again.json()["id"] != lid


def test_get_list_by_title_query(client, auth) -> None:
    created = client.post("/v1/lists", json={"title": "Findable"}, headers=auth)
    found = client.get("/v1/lists?title=Findable", headers=auth)
    assert found.status_code == 200
    assert found.json()["lists"][0]["id"] == created.json()["id"]
    assert client.get("/v1/lists?title=Missing", headers=auth).status_code == 404


# ----- P6: date / timezone semantics ---------------------------------------


def test_due_date_only_is_midnight_utc(client, auth) -> None:
    created = client.post("/v1/tasks", json={"title": "d", "due": "2026-06-25"}, headers=auth)
    assert created.status_code == 201
    # Date.UTC(2026, 5, 25) in ms.
    assert created.json()["due"] == "2026-06-25T00:00:00Z"


def test_due_tz_aware_round_trip(client, auth) -> None:
    created = client.post(
        "/v1/tasks", json={"title": "d", "due": "2026-06-25T12:00:00+02:00"}, headers=auth
    )
    assert created.json()["due"] == "2026-06-25T10:00:00Z"


# ----- P8: version + error codes -------------------------------------------


def test_version_endpoint_is_public(client) -> None:
    from supernote_task_service import __version__

    resp = client.get("/v1/version")
    assert resp.status_code == 200
    assert resp.json() == {"version": __version__}


def test_error_codes_present(client, auth) -> None:
    assert client.get("/v1/tasks").json()["code"] == "unauthorized"
    assert client.get(f"/v1/tasks/{'0' * 32}", headers=auth).json()["code"] == "not_found"
    assert client.get("/v1/tasks/bad", headers=auth).json()["code"] == "validation_error"


# ----- P9: cursor expiry ---------------------------------------------------


def test_cursor_expiry_disabled_by_default(client, auth) -> None:
    assert client.get("/v1/tasks?since=1", headers=auth).status_code == 200


def test_cursor_expiry_when_enabled(repo) -> None:
    from fastapi.testclient import TestClient

    from supernote_task_service.config import Settings
    from supernote_task_service.deps import get_repository
    from supernote_task_service.main import create_app

    settings = Settings(
        SUPERNOTE_DB_PASSWORD="unused",
        API_KEYS="test-key",
        CURSOR_MAX_AGE_MS=1000,
    )
    app = create_app(settings)
    app.dependency_overrides[get_repository] = lambda: repo
    with TestClient(app) as c:
        auth = {"Authorization": "Bearer test-key"}
        resp = c.get("/v1/tasks?since=1", headers=auth)
        assert resp.status_code == 410
        assert resp.json()["code"] == "cursor_expired"
