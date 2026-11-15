"""Synthetic authenticated HTTP journey against disposable local PostgreSQL.

JWT verifier/identity-provider and HealthKit are test doubles. The authenticated
routes, durable backend publication and account isolation are real local code.
No Clerk signup, OS permission, Sandbox provider, installed app or live data.
"""
from copy import deepcopy
from uuid import uuid4

from test_postgres import local_pg_cluster, pg_dsn, runtime
from test_pattern_bridges import health_app, consent, health_pages, send


async def overview(client, h, who="a"):
    response = await client.get("/onboarding/v1", headers=h.headers(who))
    assert response.status_code == 200, response.text
    return response.json()


async def decide(client, h, view, action, who="a"):
    body = {"schema_version": "1", "request_id": str(uuid4()),
            "data_epoch": view["data_epoch"], "expected_revision": view["revision"],
            "step": view["current_step"], "action": action}
    response = await client.post("/onboarding/v1/actions", headers=h.headers(who), json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


async def test_new_account_permission_upload_resume_completion_and_other_account_denial(health_app):
    h = health_app
    async with h.client() as client:
        assert (await client.get("/onboarding/v1")).status_code == 401
        start = await overview(client, h)
        assert start["current_step"] == "health" and not start["legacy"]
        state = (await client.get("/state", headers=h.headers())).json()
        page = health_pages(state["data_epoch"])[0]
        page.update(page_count=2, samples=page["samples"][:2])
        # Setup progress is not authority to ingest. Existing permission gate still applies.
        assert (await send(client, h, page)).status_code == 409
        await consent(client, h)
        assert (await send(client, h, page)).status_code == 200
        service = (await h.handle("a")).service.app.state.service
        async with service.pattern_coordinator.transaction() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM companion_private.health_samples")).fetchone())[0] == 0
        # Simulated interruption/lost response: reload setup and replay exact pending page.
        assert await overview(client, h) == start
        assert (await send(client, h, page)).status_code == 200
        terminal = {**page, "request_id": str(uuid4()), "page_index": 1, "samples": []}
        assert (await send(client, h, terminal)).status_code == 200
        assert (await send(client, h, terminal)).status_code == 200
        async with service.pattern_coordinator.transaction() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM companion_private.health_samples WHERE NOT deleted")).fetchone())[0] == 2
        view, _ = await decide(client, h, start, "continue")
        view, _ = await decide(client, h, view, "skip")
        view, _ = await decide(client, h, view, "unavailable")
        finished, request = await decide(client, h, view, "finish")
        assert finished["status"] == "complete" and finished["initial_sync"]["status"] == "records_available"
        assert finished["initial_sync"]["available_records"] >= 2
        # Lost completion response is retry-safe; later sign-in gets completed state.
        assert (await client.post("/onboarding/v1/actions", headers=h.headers(), json=request)).json() == finished
        assert await overview(client, h) == finished

        other = await overview(client, h, "b")
        assert other["current_step"] == "health" and not other["legacy"]
        state_b = (await client.get("/state", headers=h.headers("b"))).json()
        # Neither an account-A payload nor a denied decision authorizes account-B upload.
        assert (await send(client, h, page, "b")).status_code == 409
        other, _ = await decide(client, h, other, "denied", "b")
        unauthorized_page = deepcopy(page)
        unauthorized_page.update(data_epoch=state_b["data_epoch"], request_id=str(uuid4()))
        assert (await send(client, h, unauthorized_page, "b")).status_code == 409
        other, _ = await decide(client, h, other, "skip", "b")
        other, _ = await decide(client, h, other, "unavailable", "b")
        other, _ = await decide(client, h, other, "finish", "b")
        assert other["initial_sync"] == {"status": "no_data", "available_records": 0}
        assert await overview(client, h) == finished
        async with service.pattern_coordinator.transaction() as conn:
            assert (await (await conn.execute("SELECT count(*) FROM companion_private.health_samples WHERE NOT deleted")).fetchone())[0] == 2
