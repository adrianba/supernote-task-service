"""API integration tests against an in-memory repository."""

from __future__ import annotations


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
