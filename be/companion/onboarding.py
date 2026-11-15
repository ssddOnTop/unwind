"""Resumable optional setup, not an OS authorization or ingestion authority.

The snapshot owns both progress and at most four successful action receipts. They
commit together and survive ordinary receipt retention; account erasure removes
both. No provider, permission, registration, or upload is invoked here.
"""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import DomainError
from .domain import active


TOKEN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
META_KEY = "onboarding_v1"
StepID = Literal["health", "bank", "notifications"]
Decision = Literal["pending", "continue", "skip", "denied", "unavailable"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OnboardingErrorDetail(Strict):
    field: str
    message: str = Field(max_length=1000)


class OnboardingErrorBody(Strict):
    code: Literal[
        "invalid_request", "unsupported_version", "revision_conflict",
        "idempotency_conflict", "epoch_mismatch", "consent_required",
        "not_configured", "not_found", "demo_conflict", "busy", "internal_error",
        "stale_onboarding", "onboarding_complete", "unauthorized", "session_pending",
        "auth_unavailable", "accounts_unavailable",
    ]
    message: str = Field(max_length=1000)
    retryable: bool
    request_id: str | None = Field(pattern=TOKEN)
    details: list[OnboardingErrorDetail]


class OnboardingError(Strict):
    """Additive error contract; the existing frozen State error enum is unchanged."""
    error: OnboardingErrorBody


class OnboardingStep(Strict):
    id: StepID
    title: str
    purpose: str
    optional: Literal[True] = True
    decision: Decision = "pending"


class InitialSync(Strict):
    status: Literal["pending", "records_available", "no_data", "deferred"] = "pending"
    available_records: int | None = Field(default=None, ge=0)


class Onboarding(Strict):
    schema_version: Literal["1"] = "1"
    data_epoch: str = Field(pattern=TOKEN)
    revision: int = Field(ge=0)
    status: Literal["active", "complete"]
    current_step: Literal["health", "bank", "notifications", "initial_sync", "complete"]
    legacy: bool
    steps: list[OnboardingStep]
    initial_sync: InitialSync


class OnboardingAction(Strict):
    schema_version: Literal["1"]
    request_id: str = Field(pattern=TOKEN)
    data_epoch: str = Field(pattern=TOKEN)
    expected_revision: int = Field(ge=0)
    step: Literal["health", "bank", "notifications", "initial_sync"]
    action: Literal["continue", "skip", "denied", "unavailable", "finish", "defer"]

    @model_validator(mode="after")
    def step_action(self):
        if (self.step == "initial_sync") != (self.action in {"finish", "defer"}):
            raise ValueError("Initial sync accepts finish or defer; optional steps accept a decision.")
        return self


STEPS = (
    ("health", "Apple Health", "Share recorded activity and sleep to add context to your reports. "
     "This is optional. Continuing is a decision to try setup, not proof of device permission or readable data."),
    ("bank", "Sandbox bank", "Add Sandbox transactions to explore spending patterns alongside your records. "
     "This is optional test data, not live finances. Continuing does not establish a bank connection."),
    ("notifications", "Report notifications", "Choose whether to set up alerts when a report is ready. "
     "This is optional. Continuing does not authorize OS notifications or register this device."),
)


def legacy_evidence(doc):
    """Explicit prior source decisions/links/records, never age, revision or capability."""
    state, meta = doc["state"], doc["meta"]
    return bool(
        set(state["preferences"]["enabled_source_ids"]) - {"demo"}
        or any(s["id"] != "demo" and (s["enabled"] or s["consent"] != "not_requested")
               for s in state["sources"])
        or meta.get("plaid_connected")
        or meta.get("banking_item_contexts")
        or any(o["source_id"] != "demo" for o in state["observations"])
    )


async def pattern_storage_available(conn):
    """The pattern coordinator can exist on the base PostgreSQL profile.

    Absence of the entire optional schema means it contains no stored records.
    A partial schema or failed probe is not evidence of an empty account.
    """
    tables = await (await conn.execute("""
        SELECT to_regclass('companion_private.health_samples') IS NOT NULL,
               to_regclass('companion_private.pattern_transactions') IS NOT NULL,
               to_regclass('companion_private.pattern_batches') IS NOT NULL
        """)).fetchone()
    if any(tables) and not all(tables):
        raise DomainError("internal_error", 503, "Stored-record availability is temporarily unavailable.")
    return all(tables)


async def available_records(service, doc):
    """Count stored, nonremoved records, not permission, import completion or delivery.

    Normalized storage is read through the existing account-scoped transaction.
    Only counts leave it; no records or identifiers enter the onboarding DTO.
    The snapshot and normalized tables may contain projections of the same event:
    this is a count of stored records, not distinct real-world events.
    """
    count = len([o for o in active(service.eligible_state(doc)["observations"])
                 if o["source_id"] != "demo"])
    coordinator = getattr(service, "pattern_coordinator", None)
    if coordinator is not None:
        async with coordinator.transaction() as conn:
            if not await pattern_storage_available(conn):
                return count
            row = await (await conn.execute("""
                SELECT
                  (SELECT count(*) FROM companion_private.health_samples h
                   JOIN companion_private.pattern_batches b USING(account_id,batch_id)
                   WHERE h.account_id=%s AND b.active AND NOT h.deleted
                     AND h.end_at>=now()-interval '190 days') +
                  (SELECT count(*) FROM companion_private.pattern_transactions t
                   JOIN companion_private.pattern_batches b USING(account_id,batch_id)
                   WHERE t.account_id=%s AND b.active AND NOT t.deleted
                     AND t.event_at>=now()-interval '190 days')
                """, (coordinator.store.account_id, coordinator.store.account_id))).fetchone()
            count += row[0]
    return count


async def initialize(service, doc):
    """Caller holds the service lock and commits the returned document atomically.

    Called before startup retention so a legacy account's last stored observation
    cannot disappear before the migration decision. Never reclassify an existing
    onboarding session after its own preference changes or first upload.
    """
    if META_KEY in doc["meta"]:
        return
    legacy = legacy_evidence(doc)
    coordinator = getattr(service, "pattern_coordinator", None)
    if not legacy and coordinator is not None:
        # Historical/deleted/inactive records are evidence of prior use too;
        # completion's available-record count deliberately has stricter filters.
        async with coordinator.transaction() as conn:
            if await pattern_storage_available(conn):
                row = await (await conn.execute("""
                    SELECT EXISTS(SELECT 1 FROM companion_private.health_samples WHERE account_id=%s)
                        OR EXISTS(SELECT 1 FROM companion_private.pattern_transactions WHERE account_id=%s)
                    """, (coordinator.store.account_id, coordinator.store.account_id))).fetchone()
                legacy = bool(row[0])
    view = Onboarding(
        data_epoch=doc["state"]["data_epoch"], revision=0,
        status="complete" if legacy else "active",
        current_step="complete" if legacy else "health", legacy=legacy,
        steps=[OnboardingStep(id=id, title=title, purpose=purpose) for id, title, purpose in STEPS],
        # Legacy bypass is not a claim that an initial sync ever ran.
        initial_sync=InitialSync(status="deferred" if legacy else "pending"),
    )
    doc["meta"][META_KEY] = {"view": view.model_dump(), "receipts": {}}


async def get(service):
    async with service.lock:
        if META_KEY not in service.document["meta"]:
            doc = deepcopy(service.document)
            await initialize(service, doc)
            await service.commit(doc, bump=False)
        return deepcopy(service.document["meta"][META_KEY]["view"])


async def act(service, body: OnboardingAction):
    async with service.lock:
        # Fence BEFORE looking up a receipt: erased account epochs never replay.
        if body.data_epoch != service.document["state"]["data_epoch"]:
            raise DomainError("epoch_mismatch", 409, "Refresh onboarding before submitting a decision.")
        receipt = service.receipt("/onboarding/v1/actions", body.model_dump())
        doc = deepcopy(service.document)
        await initialize(service, doc)
        saved = doc["meta"][META_KEY]
        cached = saved["receipts"].get(body.request_id)
        if cached is not None:
            if cached["digest"] != receipt["digest"]:
                raise DomainError("idempotency_conflict", 409)
            return deepcopy(cached["response"])
        view = saved["view"]
        if body.expected_revision != view["revision"]:
            raise DomainError("stale_onboarding", 409, "Onboarding changed. Refresh before deciding.")
        if view["status"] == "complete":
            raise DomainError("onboarding_complete", 409, "Onboarding is already complete.")
        if body.step != view["current_step"]:
            raise DomainError("invalid_request", 409, "Decide the current onboarding step first.")
        if body.step == "initial_sync":
            sync = InitialSync(status="deferred")
            if body.action == "finish":
                try:
                    count = await available_records(service, doc)
                except Exception:
                    # A failed check is not the user's explicit deferral decision.
                    # No progress or receipt is committed, so this request can retry.
                    # Cancellation still propagates and leaves progress untouched.
                    raise DomainError("internal_error", 503,
                                      "Record availability could not be checked. Retry this request.") from None
                else:
                    sync = InitialSync(status="records_available" if count else "no_data", available_records=count)
            view.update(status="complete", current_step="complete", initial_sync=sync.model_dump())
        else:
            index = next(i for i, step in enumerate(view["steps"]) if step["id"] == body.step)
            view["steps"][index]["decision"] = body.action
            view["current_step"] = view["steps"][index + 1]["id"] if index < 2 else "initial_sync"
        view["revision"] += 1
        response = Onboarding.model_validate(view).model_dump()
        saved["receipts"][body.request_id] = {"digest": receipt["digest"], "response": deepcopy(response)}
        # A bounded four-decision journal in the SAME snapshot, not expiring generic
        # receipts: no pending reservation, external effect or partial advancement.
        await service.commit(doc, bump=False)
        return response
