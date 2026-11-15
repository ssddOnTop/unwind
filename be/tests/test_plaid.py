"""Sandbox profile, transport, atomic sync, revocation and vault regressions; no ambient keys."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest

from companion.bootstrap import UnavailableSynthesis
from companion.connections.plaid import PlaidSandboxClient, SandboxCredentials
from companion.contracts import DomainError, validate
from sample_data import EXAMPLE
from companion.domain import iso
from companion.environment import load_runtime_environment
from companion.extensions.plaid import PlaidExtension
from companion.http import create_app
from companion.plaid_contracts import validate_profile
from companion.plaid_projection import category
from companion.vault import PrivateVault
from test_backend import NOW


PUBLIC = "public-sandbox-synthetic-test-only"
ACCESS = "access-sandbox-synthetic-test-only"
LINK = "link-sandbox-synthetic-test-only"


def transaction(identifier="provider-one", **changes):
    row = {"transaction_id": identifier, "date": "2026-11-10", "authorized_date": "2026-11-09",
           "pending": False, "pending_transaction_id": None, "amount": 12.345,
           "merchant_name": "Synthetic Merchant", "name": "PRIVATE_RAW_DESCRIPTION",
           "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT"},
           "iso_currency_code": "USD"}
    row.update(changes)
    return row


def page(added=None, modified=None, removed=None, cursor="cursor-one", has_more=False, status="HISTORICAL_UPDATE_COMPLETE"):
    return {"added": added or [], "modified": modified or [], "removed": removed or [], "next_cursor": cursor,
            "has_more": has_more, "transactions_update_status": status}


class Provider:
    def __init__(self):
        self.requests = []
        self.pages = [page([transaction()])]
        self.fail_remove = False
        self.fail_exchange = False
        self.sync_entered = None
        self.sync_release = None
        self.exchange_entered = None
        self.exchange_release = None

    async def handle(self, request):
        assert request.url.host == "sandbox.plaid.com"
        body = json.loads(request.content)
        self.requests.append((request.url.path, body))
        if request.url.path == "/link/token/create":
            assert body["products"] == ["transactions"] and body["country_codes"] == ["US"]
            return httpx.Response(200, json={"link_token": LINK, "expiration": "2026-11-15T12:00:00Z"})
        if request.url.path == "/item/public_token/exchange":
            if self.exchange_entered:
                self.exchange_entered.set()
                await self.exchange_release.wait()
            if self.fail_exchange:
                raise httpx.ReadTimeout("Synthetic ambiguous exchange")
            return httpx.Response(200, json={"access_token": ACCESS, "item_id": "PRIVATE_ITEM_ID"})
        if request.url.path == "/transactions/sync":
            if self.sync_entered:
                self.sync_entered.set()
                await self.sync_release.wait()
            response = self.pages.pop(0) if self.pages else page()
            if isinstance(response, Exception):
                raise response
            if response.get("error_code"):
                return httpx.Response(400, json=response)
            return httpx.Response(200, json=response)
        if request.url.path == "/item/remove":
            if self.fail_remove:
                return httpx.Response(500, json={"error_code": "INTERNAL_SERVER_ERROR", "error_message": "PRIVATE_ERROR"})
            return httpx.Response(200, json={"request_id": "PRIVATE_PROVIDER_REQUEST"})
        raise AssertionError("Unexpected Sandbox endpoint")


def app_for(tmp_path, provider, clock=lambda: NOW):
    extension = PlaidExtension(tmp_path, PlaidSandboxClient(SandboxCredentials("synthetic-client", "synthetic-secret"), httpx.MockTransport(provider.handle)))
    return create_app(tmp_path, plaid_profile=True, replacements={"plaid": extension, "synthesis": UnavailableSynthesis()}, clock=clock)


def get_state(client):
    r = client.get("/state")
    assert r.status_code == 200, r.text
    validate_profile("state", r.json())
    return r.json()


def call(client, action, rid=None, epoch=None, **extra):
    body = {"schema_version": "1", "request_id": rid or str(uuid4()), "data_epoch": epoch or get_state(client)["data_epoch"], "action": action, **extra}
    r = client.post("/connections/plaid", json=body)
    if r.status_code == 200:
        validate_profile("response", r.json())
    else:
        validate("error", r.json())
    return r


def connect(client):
    assert call(client, "link_token", consent_sandbox=True).status_code == 200
    r = call(client, "exchange", public_token=PUBLIC)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def client(tmp_path, provider):
    with TestClient(app_for(tmp_path, provider), base_url="http://127.0.0.1") as c:
        yield c


def test_profile_explicit_and_ninth_typed_discriminated_route(client, tmp_path):
    spec = client.get("/openapi.json").json()
    assert len(spec["paths"]) == 11
    request_schema = spec["paths"]["/connections/plaid"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert request_schema["discriminator"]["propertyName"] == "action"
    assert get_state(client)["sources"][2]["capability"] == "available"
    with TestClient(create_app(tmp_path / "base", replacements={"synthesis": UnavailableSynthesis()}), base_url="http://127.0.0.1") as base:
        assert len(base.get("/openapi.json").json()["paths"]) == 10
        assert base.post("/connections/plaid", json={}).status_code == 404
        validate("state", base.get("/state").json())


def test_link_exchange_sync_real_shape_and_secret_exclusion(client, tmp_path, provider):
    connect(client)
    s = get_state(client)
    assert s["sources"][2]["status"] == "syncing" and not s["observations"]
    r = call(client, "sync")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"status": "complete", "added": 1, "modified": 0, "removed": 0, "skipped": 0, "retry_after_seconds": None}
    s = r.json()["state"]
    o = s["observations"][0]
    assert o["source_id"] == "plaid" and o["mode"] == "fixture"
    assert o["household_attribution"] == "unknown" and o["occurred_at"] is None
    assert o["local_date"] == "2026-11-09" and o["provenance"]["adapter"] == "plaid_sandbox"
    assert s["ledger"][0]["purchase_count"] == 1 and s["ledger"][0]["suggestion"] is None
    assert set(o["purchase"]) == {"category", "merchant", "authorized_date", "posted_date", "is_refund"}
    text = json.dumps(s)
    for sentinel in (ACCESS, PUBLIC, LINK, "PRIVATE_RAW_DESCRIPTION", "PRIVATE_ITEM_ID", "provider-one", "cursor-one"):
        assert sentinel not in text
    db_bytes = (tmp_path / "state.sqlite").read_bytes()
    assert ACCESS.encode() not in db_bytes and PUBLIC.encode() not in db_bytes and LINK.encode() not in db_bytes
    assert (tmp_path / "plaid.key").stat().st_mode & 0o777 == 0o600


def test_exchange_idempotent_one_use_and_link_receipt_encrypted(client, provider):
    first = call(client, "link_token", rid="link", consent_sandbox=True)
    assert call(client, "link_token", rid="link", consent_sandbox=True).json() == first.json()
    exchange = call(client, "exchange", rid="exchange", public_token=PUBLIC)
    assert call(client, "exchange", rid="exchange", public_token=PUBLIC).json() == exchange.json()
    assert sum(path == "/item/public_token/exchange" for path, _ in provider.requests) == 1
    assert call(client, "exchange", rid="exchange", public_token=PUBLIC + "changed").status_code == 409


@pytest.mark.parametrize("change", [{"action": "unknown"}, {"action": "link_token", "consent_sandbox": False},
    {"action": "exchange", "public_token": "public-production-not-allowed"}, {"action": "sync", "cursor": "forbidden"},
    {"action": "sync", "access_token": ACCESS}, {"action": "sync", "schema_version": "2"}])
def test_strict_request_rejection(client, change):
    b = {"schema_version": "1", "request_id": "invalid", "data_epoch": get_state(client)["data_epoch"], "action": "sync", **change}
    assert client.post("/connections/plaid", json=b).status_code == 422


def test_consent_unsolicited_exchange_and_manual_enable(client):
    assert call(client, "exchange", public_token=PUBLIC).json()["error"]["code"] == "consent_required"
    assert call(client, "sync").json()["error"]["code"] == "consent_required"
    s = get_state(client)
    s["preferences"]["enabled_source_ids"].append("plaid")
    r = client.put("/preferences", json={"schema_version": "1", "request_id": "forged", "data_epoch": s["data_epoch"], "preferences": s["preferences"]})
    assert r.status_code == 409


def test_staged_pages_modified_removed_and_no_duplicate_revision(client, provider):
    connect(client)
    call(client, "sync")
    original = get_state(client)["observations"][0]
    provider.pages = [page(modified=[transaction(merchant_name="Corrected synthetic merchant")], cursor="c2")]
    r = call(client, "sync")
    assert r.json()["result"]["modified"] == 1
    latest = r.json()["state"]["observations"][-1]
    assert latest["revision"] == 2 and latest["supersedes"] == {"observation_id": original["id"], "revision": 1}
    provider.pages = [page(modified=[transaction(merchant_name="Corrected synthetic merchant")], cursor="c3")]
    assert call(client, "sync").json()["result"]["skipped"] == 1
    provider.pages = [page(removed=[{"transaction_id": "provider-one"}], cursor="c4")]
    r = call(client, "sync")
    assert r.json()["result"]["removed"] == 1
    assert r.json()["state"]["ledger"][0]["purchase_count"] == 0
    assert len(r.json()["state"]["observations"]) == 3


def test_pending_to_posted_across_pages_suppresses_tombstone(client, provider):
    connect(client)
    pending = transaction("pending-id", pending=True)
    posted = transaction("posted-id", pending_transaction_id="pending-id")
    provider.pages = [page([posted], cursor="middle", has_more=True), page([pending], removed=[{"transaction_id": "pending-id"}], cursor="final")]
    r = call(client, "sync")
    assert r.status_code == 200, r.text
    obs = r.json()["state"]["observations"]
    assert len(obs) == 2 and not any(o["operation"] == "remove" for o in obs)
    assert r.json()["state"]["ledger"][0]["purchase_count"] == 1
    assert next(o for o in obs if o["lifecycle"] == "posted")["supersedes"] is not None


def test_pagination_mutation_restarts_original_cursor(client, provider):
    connect(client)
    provider.pages = [page([transaction("discard")], cursor="middle", has_more=True),
                      {"error_code": "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION"}, page([transaction("keep")], cursor="complete")]
    r = call(client, "sync")
    assert r.json()["result"]["added"] == 1
    calls = [b for p, b in provider.requests if p == "/transactions/sync"]
    assert "cursor" not in calls[0] and calls[1]["cursor"] == "middle" and "cursor" not in calls[2]


def test_page_failure_commits_no_evidence_or_cursor(client, provider):
    connect(client)
    provider.pages = [page([transaction()], cursor="middle", has_more=True), httpx.ReadTimeout("synthetic timeout")]
    r = call(client, "sync")
    assert r.json()["result"]["status"] == "error" and not r.json()["state"]["observations"]
    provider.pages = [page([transaction()], cursor="retry")]
    assert call(client, "sync").json()["result"]["added"] == 1
    assert "cursor" not in [b for p,b in provider.requests if p == "/transactions/sync"][-1]


@pytest.mark.parametrize("status,want", [("NOT_READY", "waiting"), ("INITIAL_UPDATE_COMPLETE", "partial"), ("HISTORICAL_UPDATE_COMPLETE", "complete"), ("TRANSACTIONS_UPDATE_STATUS_UNKNOWN", "waiting")])
def test_readiness_not_inferred_from_has_more_false(client, provider, status, want):
    connect(client)
    provider.pages = [page(status=status)]
    r = call(client, "sync")
    assert r.json()["result"]["status"] == want
    assert not r.json()["state"]["observations"]
    assert r.json()["state"]["sources"][2]["coverage"] == "unknown"


@pytest.mark.parametrize("pfc,want", [(None,None), ({"primary":"FOOD_AND_DRINK","detailed":"FOOD_AND_DRINK_COFFEE"},"other"),
    ({"primary":"GENERAL_MERCHANDISE","detailed":"FOOD_AND_DRINK_RESTAURANT"},None),
    ({"primary":"FOOD_AND_DRINK","detailed":"FOOD_AND_DRINK_FAST_FOOD"},"restaurant")])
def test_category_whitelist(pfc, want):
    assert category(pfc) == want


def test_amount_sign_dates_category_and_raw_description_exclusions(client, provider):
    connect(client)
    provider.pages = [page([transaction("zero", amount=0), transaction("deposit", amount=-200, personal_finance_category=None),
        transaction("refund", amount=-2), transaction("unknown-date", date=None, authorized_date=None),
        transaction("old", date="2020-01-01", authorized_date=None), transaction("merchant-missing", merchant_name=None)])]
    r = call(client, "sync")
    assert r.json()["result"]["added"] == 2 and r.json()["result"]["skipped"] == 4
    obs = r.json()["state"]["observations"]
    assert sum(o["purchase"]["is_refund"] for o in obs) == 1
    assert any(o["purchase"]["merchant"] is None for o in obs)


def test_disconnect_failure_retains_only_encrypted_revocation_and_retry(client, provider):
    connect(client)
    call(client, "sync")
    provider.fail_remove = True
    r = call(client, "disconnect", rid="disconnect")
    assert r.status_code == 200 and r.json()["result"]["status"] == "removal_pending"
    assert r.json()["state"]["sources"][2]["consent"] == "revoked"
    assert call(client, "link_token", consent_sandbox=True).status_code == 409
    provider.fail_remove = False
    assert call(client, "disconnect").json()["result"]["status"] == "disconnected"


def test_delete_clears_tokens_key_receipts_and_stale_link(client, provider, tmp_path):
    link = call(client, "link_token", rid="link", consent_sandbox=True)
    old_epoch = link.json()["data_epoch"]
    call(client, "exchange", public_token=PUBLIC)
    call(client, "sync")
    provider.fail_remove = True
    r = client.request("DELETE", "/data", json={"schema_version":"1", "request_id":"delete", "data_epoch":old_epoch, "confirm":"delete_all"})
    assert r.status_code == 200, r.text
    assert not r.json()["observations"] and "Remote Sandbox Item removal unconfirmed." in r.json()["limitations"]
    assert not (tmp_path / "plaid.key").exists() and not (tmp_path / "plaid-pending.vault").exists()
    assert call(client, "link_token", rid="link", epoch=old_epoch, consent_sandbox=True).status_code == 409


def test_restart_restores_cursor_and_observations(tmp_path, provider):
    with TestClient(app_for(tmp_path, provider), base_url="http://127.0.0.1") as c:
        connect(c); call(c, "sync")
    provider.pages = [page()]
    with TestClient(app_for(tmp_path, provider), base_url="http://127.0.0.1") as c:
        assert call(c, "sync").status_code == 200
        assert [b for p,b in provider.requests if p == "/transactions/sync"][-1]["cursor"] == "cursor-one"
        assert len(get_state(c)["observations"]) == 1


def test_runtime_loader_only_explicit_and_permissions(tmp_path, monkeypatch):
    monkeypatch.delenv("SYNTHETIC_DOTENV_TEST", raising=False)
    p = tmp_path / ".env"
    p.write_text("SYNTHETIC_DOTENV_TEST=synthetic-value\n")
    p.chmod(0o600)
    import os
    assert "SYNTHETIC_DOTENV_TEST" not in os.environ
    load_runtime_environment(tmp_path)
    assert os.environ["SYNTHETIC_DOTENV_TEST"] == "synthetic-value"
    monkeypatch.delenv("SYNTHETIC_DOTENV_TEST")
    p.chmod(0o644)
    with pytest.raises(RuntimeError):
        load_runtime_environment(tmp_path)


async def test_delete_during_exchange_removes_late_item_without_revival(tmp_path, provider):
    provider.exchange_entered, provider.exchange_release = asyncio.Event(), asyncio.Event()
    app = app_for(tmp_path, provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            s = (await c.get('/state')).json()
            base={"schema_version":"1", "data_epoch":s['data_epoch']}
            assert (await c.post('/connections/plaid',json={**base,'request_id':'link','action':'link_token','consent_sandbox':True})).status_code == 200
            exchange=asyncio.create_task(c.post('/connections/plaid',json={**base,'request_id':'exchange','action':'exchange','public_token':PUBLIC}))
            await provider.exchange_entered.wait()
            deleted=await c.request('DELETE','/data',json={**base,'request_id':'delete','confirm':'delete_all'})
            assert deleted.status_code == 200
            provider.exchange_release.set()
            assert (await exchange).status_code == 409
            final=(await c.get('/state')).json()
            assert not final['observations'] and final['sources'][2]['consent']=='not_requested'
            assert any(path=='/item/remove' for path,_ in provider.requests)
            assert not (tmp_path/'plaid.key').exists()
