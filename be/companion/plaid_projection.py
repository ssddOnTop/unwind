"""Count-only projection of a staged Sandbox delta; raw provider fields never persist."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from .contracts import RULES
from .domain import key, ref
from .connections.plaid import PlaidFailure


def category(value):
    if not isinstance(value, dict):
        return None
    primary, detailed = value.get("primary"), value.get("detailed")
    if not isinstance(primary, str) or not isinstance(detailed, str) or not detailed.startswith(primary + "_"):
        return None
    if primary == "FOOD_AND_DRINK":
        if detailed in {"FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK_FAST_FOOD"}:
            return "restaurant"
        if detailed == "FOOD_AND_DRINK_GROCERIES":
            return "grocery"
    return "other"


def project(state, connection, staged):
    mapping = deepcopy(connection.get("records", {}))
    observations = deepcopy(state["observations"])
    latest = {}
    for o in observations:
        if o["id"] not in latest or o["revision"] > latest[o["id"]]["revision"]:
            latest[o["id"]] = o
    counts = {"added": 0, "modified": 0, "removed": 0, "skipped": 0}
    cutoff = (date.fromisoformat(state["evaluation_date"]) - timedelta(days=RULES["retention"]["observation_days"] - 1)).isoformat()
    entries = {}
    fields = ("transaction_id", "pending_transaction_id", "pending", "authorized_date", "date", "merchant_name", "personal_finance_category", "amount")
    for row in staged["added"] + staged["modified"]:
        identifier = row.get("transaction_id")
        if not isinstance(identifier, str) or not identifier:
            counts["skipped"] += 1
            continue
        canonical = {k: row.get(k) for k in fields}
        if identifier in entries:
            if entries[identifier] != canonical:
                raise PlaidFailure()
            counts["skipped"] += 1
        entries[identifier] = canonical
    removed_ids = {r.get("transaction_id") for r in staged["removed"] if isinstance(r, dict)}
    linked_pending = {r.get("pending_transaction_id") for r in entries.values() if not r.get("pending")}
    # Allocate all IDs first so cross-page pending references do not depend on order.
    for identifier in entries:
        mapping.setdefault(identifier, str(uuid4()))
    ordered = sorted(entries.items(), key=lambda pair: (not bool(pair[1].get("pending")), pair[0]))
    for identifier, row in ordered:
        try:
            local_date = row.get("authorized_date") or row.get("date")
            date.fromisoformat(local_date)
            posted_date = row.get("date")
            if posted_date is not None:
                date.fromisoformat(posted_date)
            if local_date < cutoff:
                counts["skipped"] += 1
                continue
            amount = Decimal(str(row.get("amount")))
            if not amount.is_finite():
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            counts["skipped"] += 1
            continue
        classified = category(row.get("personal_finance_category"))
        if amount == 0 or (amount < 0 and classified not in {"restaurant", "grocery"}):
            counts["skipped"] += 1
            continue
        app_id = mapping[identifier]
        previous = latest.get(app_id)
        merchant = row.get("merchant_name")
        o = {"id": app_id, "revision": 1, "source_id": "plaid", "mode": "fixture", "operation": "upsert",
             "kind": "purchase", "local_date": local_date, "timezone": state["preferences"]["timezone"],
             "time_precision": "date", "occurred_at": None, "lifecycle": "pending" if row.get("pending") else "posted",
             "supersedes": None, "household_attribution": "unknown",
             "provenance": {"adapter": "plaid_sandbox", "adapter_version": "1", "record_id": app_id,
                            "method": "transactions_sync", "coverage": "partial"},
             "purchase": {"category": classified, "merchant": merchant[:120] if isinstance(merchant, str) else None,
                          "authorized_date": row.get("authorized_date"), "posted_date": None if row.get("pending") else posted_date,
                          "is_refund": amount < 0}, "activity": None}
        if previous:
            # Existing date-only evidence is never silently rebucketed on timezone preference changes.
            o["timezone"] = previous["timezone"]
            semantic = lambda v: {k: val for k, val in v.items() if k not in {"revision", "supersedes"}}
            if semantic(previous) == semantic(o):
                counts["skipped"] += 1
                continue
            o.update(revision=previous["revision"] + 1, supersedes=ref(previous))
        elif not row.get("pending") and row.get("pending_transaction_id") in mapping:
            pending = latest.get(mapping[row["pending_transaction_id"]])
            if pending is not None:
                if pending["lifecycle"] not in {"pending", "removed"}:
                    raise PlaidFailure()
                o["supersedes"] = ref(pending)
        observations.append(o)
        latest[app_id] = o
        counts["modified" if previous else "added"] += 1
    for identifier in removed_ids:
        if identifier in linked_pending:
            counts["skipped"] += 1
            continue
        if identifier in entries:
            raise PlaidFailure()
        previous = latest.get(mapping.get(identifier))
        if previous is None or previous["operation"] == "remove":
            counts["skipped"] += 1
            continue
        o = deepcopy(previous)
        o.update(revision=previous["revision"] + 1, supersedes=ref(previous), operation="remove", lifecycle="removed", purchase=None, activity=None)
        observations.append(o)
        latest[o["id"]] = o
        counts["removed"] += 1
    connection = deepcopy(connection)
    connection["records"] = {provider_id: app_id for provider_id, app_id in mapping.items() if app_id in latest}
    connection["cursor"] = staged["cursor"]
    return {"observations": sorted(observations, key=key), "connection": connection, "counts": counts}
