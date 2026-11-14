#!/usr/bin/env python3
"""Independent HTTP integration gate for an EXCLUSIVE, EMPTY local demo server.

From project root (dependency setup: scripts/verify_contract.py --help):
  .venv-verify/bin/python -B scripts/verify_http.py --base-url http://127.0.0.1:8000 --allow-reset
  .venv-verify/bin/python -B scripts/verify_http.py --allow-reset --with-scripted --output evidence/http-check.json

No server launch, polling, credentials, backend imports, proxies or redirects.
Without --allow-reset only GET preflight runs; mutation verification is refused.
Use a separate empty database with no other clients and no provider credentials.
The live-mode negative test retains cloud_consent=false, never enabling cloud AI.
An interrupted/failed run may leave ONLY its synthetic test data: inspect the
report and retire that isolated database; never automatically reset an existing one.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import ipaddress
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

sys.dont_write_bytecode = True
from jsonschema import Draft202012Validator, FormatChecker
from verify_contract import ROOT, SCHEMA, SCENARIOS, fixture, schema_errors, state_issues, write_report

# These are the eight routes in contracts/v1.md, not guessed backend internals.
ROUTES = {
    ("GET", "/health"): (None, "health"),
    ("GET", "/state"): (None, "state"),
    ("POST", "/observations"): ("observation_batch", "ingestion_response"),
    ("POST", "/feedback"): ("feedback_request", "state"),
    ("PUT", "/preferences"): ("preferences_request", "state"),
    ("POST", "/process"): ("process_request", "state"),
    ("POST", "/demo"): ("demo_request", "state"),
    ("DELETE", "/data"): ("delete_request", "state"),
}


class StopVerification(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_url(value):
    url = urlsplit(value if "://" in value else "http://" + value)
    if (url.scheme != "http" or url.username is not None or url.password is not None
            or url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("only plain HTTP loopback origin without credentials/path/query is allowed")
    host = url.hostname
    # Pin localhost to a literal: no DNS/rebinding or proxy-dependent destination.
    host = "127.0.0.1" if host == "localhost" else host
    address = ipaddress.ip_address(host)
    if not address.is_loopback or "%" in host:
        raise ValueError("non-loopback destination refused")
    port = url.port if url.port is not None else 8000
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return f"http://{'[' + host + ']' if address.version == 6 else host}:{port}"


class Harness:
    def __init__(self, base_url):
        self.base_url = base_url
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.results = []
        self.routes = set()
        self.requests = 0
        self.mutations = 0
        self.state = None
        self.openapi = None

    def check(self, name, expected, actual, fatal=False):
        passed = expected == actual
        self.results.append({"check": name, "passed": passed, "expected": expected, "actual": actual})
        if not passed and fatal:
            raise StopVerification()

    def request(self, name, method, path, body=None, status=200, code=None, invalid=False):
        if body is not None and not invalid:
            self.check(name + ".request_schema", [], schema_errors(ROUTES[method, path][0], body), True)
        if method != "GET":
            # Detect intervening writes before each mutation, and never claim to
            # make a shared server safe. The operator must provide isolation.
            previous = self.state
            current = self.request(name + ".prewrite", "GET", "/state")
            self.check(name + ".exclusive_snapshot", True, current == previous, True)
            self.check(name + ".demo_only", True, self.demo_only(current), True)
        data = None if body is None else json.dumps(body, sort_keys=True).encode()
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
        self.requests += 1
        if method != "GET":
            self.mutations += 1
        try:
            response = self.opener.open(request, timeout=35)
        except HTTPError as error:
            response = error
        except (URLError, OSError, TimeoutError) as error:
            self.check(name + ".transport", "reachable loopback server", type(error).__name__, True)
        with response:
            actual_status = response.code
            raw = response.read(2 * 1024 * 1024 + 1)
            content_type = response.headers.get_content_type()
        self.check(name + ".http", status, actual_status, True)
        self.check(name + ".bounded_response", True, len(raw) <= 2 * 1024 * 1024, True)
        if path == "/docs":
            self.check(name + ".swagger_ui", True, b"SwaggerUIBundle" in raw and b"openapi.json" in raw)
            return None
        self.check(name + ".content_type", "application/json", content_type, True)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            self.check(name + ".json", True, False, True)
        if path == "/openapi.json":
            return value
        self.routes.add((method, path))
        definition = ROUTES[method, path][1] if status == 200 else "error"
        self.check(name + ".response_schema", [], schema_errors(definition, value), True)
        if self.openapi:
            response_schema = self.openapi["paths"][path][method.lower()]["responses"][str(status)]["content"]["application/json"]["schema"]
            validator = Draft202012Validator({**response_schema, "components": self.openapi.get("components", {})}, format_checker=FormatChecker())
            self.check(name + ".openapi_response", True, validator.is_valid(value))
        if status != 200:
            self.check(name + ".error_code", code, value["error"]["code"])
            self.check(name + ".not_retryable", False, value["error"]["retryable"])
            self.check(name + ".request_id", True, value["error"]["request_id"] == body["request_id"])
        elif definition in ("state", "ingestion_response"):
            state = value if definition == "state" else value["state"]
            # Validate structure first, but never write private records/counts to evidence.
            self.check(name + ".semantics", [], state_issues(state))
            if self.state is not None and state["data_epoch"] == self.state["data_epoch"]:
                self.check(name + ".revision_monotonic", True, state["revision"] >= self.state["revision"])
            self.state = state
            if definition == "ingestion_response":
                self.check(name + ".counts_conserved", True, value["accepted"] + value["ignored"] == len(body["observations"]))
        return value

    @staticmethod
    def demo_only(state):
        return (all(o["source_id"] == "demo" and o["mode"] == "fixture" for o in state["observations"])
                and state["preferences"]["enabled_source_ids"] == ["demo"]
                and not state["preferences"]["cloud_consent"]
                and all(s["id"] == "demo" or (not s["enabled"] and s["consent"] == "not_requested") for s in state["sources"]))

    def body(self, **fields):
        return {"schema_version": "1", "request_id": "verify-" + uuid4().hex,
                "data_epoch": self.state["data_epoch"], **fields}

    def unchanged(self, name, previous):
        self.check(name, True, self.request(name + ".read", "GET", "/state") == previous)

    def reject(self, name, method, path, body, status, code, invalid=False):
        previous = deepcopy(self.state)
        self.request(name, method, path, body, status, code, invalid)
        self.unchanged(name + ".atomic_no_write", previous)

    def eligible(self, cid):
        card = next(c for c in self.state["ledger"] if c["id"] == cid)
        return card["suggestion"] is not None and card["suggestion"]["status"] == "eligible"

    def preferences(self, name, preferences):
        self.request(name, "PUT", "/preferences", self.body(preferences=preferences))
        self.check(name + ".replacement_exact", True, self.state["preferences"] == preferences)

    def feedback(self, kind, value, scope="card", event_ref=None, attribution=None):
        previous = deepcopy(self.state)
        body = self.body(card_id="purchase_recurrence", kind=kind, value=value, scope=scope,
                         event_ref=event_ref, attribution=attribution, note="")
        name = "feedback." + kind + "." + scope + "." + value
        self.request(name, "POST", "/feedback", body)
        self.check(name + ".immutable_evidence", True, self.state["observations"] == previous["observations"])
        self.check(name + ".audit_appended", True, len(self.state["feedback"]) == len(previous["feedback"]) + 1)
        self.check(name + ".audit_preserved", True, all(f in self.state["feedback"] for f in previous["feedback"]))
        fields = ("card_id", "kind", "value", "scope", "event_ref", "attribution", "note")
        self.check(name + ".feedback_exact", True, any(all(f[k] == body[k] for k in fields) for f in self.state["feedback"] if f not in previous["feedback"]))
        self.check(name + ".eligibility", value in ("correct", "useful"), self.eligible("purchase_recurrence"))

    def documentation(self):
        document = self.request("openapi", "GET", "/openapi.json")
        self.check("openapi.version", True, str(document.get("openapi", "")).startswith("3.1."), True)
        methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
        found = {(m.upper(), p) for p, item in document.get("paths", {}).items() for m in item if m in methods}
        self.check("openapi.exact_eight_routes", True, found == set(ROUTES), True)

        def local_refs(node):
            if isinstance(node, dict):
                for key, val in node.items():
                    if key == "$ref":
                        if not isinstance(val, str) or not val.startswith("#/"):
                            return False
                        target = document
                        try:
                            for part in val[2:].split("/"):
                                target = target[part.replace("~1", "/").replace("~0", "~")]
                        except (KeyError, TypeError):
                            return False
                    elif not local_refs(val):
                        return False
            elif isinstance(node, list):
                return all(local_refs(v) for v in node)
            return True

        self.check("openapi.local_resolving_refs", True, local_refs(document), True)
        for (method, path), (request_def, response_def) in ROUTES.items():
            operation = document["paths"][path][method.lower()]
            name = "openapi." + method + path
            self.check(name + ".operation_id", True, bool(operation.get("operationId")))
            for status in (["200", "409", "422"] if request_def else ["200"]):
                model = operation.get("responses", {}).get(status, {}).get("content", {}).get("application/json", {}).get("schema")
                self.check(name + ".typed_response_" + status, True, bool(model), True)
            if request_def:
                request_body = operation.get("requestBody", {})
                model = request_body.get("content", {}).get("application/json", {}).get("schema", {})
                self.check(name + ".required_typed_body", True, request_body.get("required") is True and bool(model), True)
                seen = set()
                while "$ref" in model:
                    self.check(name + ".acyclic_model_ref", False, model["$ref"] in seen, True)
                    seen.add(model["$ref"])
                    target = document
                    for part in model["$ref"][2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                    model = target
                self.check(name + ".exact_request_fields", True,
                           set(model.get("properties", {})) == set(SCHEMA["$defs"][request_def]["properties"])
                           and set(model.get("required", [])) == set(SCHEMA["$defs"][request_def]["required"])
                           and model.get("additionalProperties") is False)
        self.openapi = document
        self.request("docs", "GET", "/docs")
        self.request("health.openapi", "GET", "/health")

    def run(self, allow_reset, with_scripted):
        health = self.request("health", "GET", "/health")
        self.check("health.ok", "ok", health["status"])
        self.check("health.all_capabilities", True,
                   sorted(c["id"] for c in health["capabilities"]) == sorted(["persistence", "scenarios", "feedback", "demo", "healthkit", "plaid", "email", "synthesis"]))
        initial = self.request("state.empty", "GET", "/state")
        defaults = deepcopy(fixture("state.example.json")["preferences"])
        defaults.update(no_kitchen=False, no_spending=False, movement_restriction=False, notes="")
        pristine = (not initial["observations"] and not initial["feedback"] and not initial["notification_intents"]
                    and all(s["consent"] == "not_requested" for s in initial["sources"])
                    and initial["preferences"] == defaults and initial["process"]["status"] == "idle"
                    and not initial["process"]["synthesis"]["drafts"] and self.demo_only(initial))
        self.check("safety.empty_unconsented_default_instance", True, pristine, True)
        self.unchanged("state.read_does_not_mutate", initial)
        self.documentation()
        self.check("safety.allow_reset_required", True, allow_reset, True)
        sample = fixture("state.example.json")
        demo = {"action": "seed", "fixture_set": SCENARIOS["demo"]["fixture_set"], "consent_demo": True}
        seed = self.body(**demo)
        seeded = self.request("demo.seed", "POST", "/demo", seed)
        self.check("demo.fixture_observations", True, seeded["observations"] == sample["observations"])
        self.check("demo.fixture_preferences", True, seeded["preferences"] == sample["preferences"])
        self.check("demo.fixture_clock", True, seeded["evaluation_date"] == SCENARIOS["demo"]["evaluation_date"])
        self.check("demo.seed_exact_replay", True, self.request("demo.seed_replay", "POST", "/demo", seed) == seeded)
        self.check("demo.new_seed_noop", True, self.request("demo.seed_noop", "POST", "/demo", self.body(**demo)) == seeded)
        batch = fixture("observation-batch.example.json")
        batch.update(self.body())
        replay = self.request("observations.replay", "POST", "/observations", batch)
        self.check("observations.all_ignored", True, replay["accepted"] == 0 and replay["ignored"] == len(batch["observations"]))
        self.check("observations.noop_state", True, self.state == seeded)
        self.check("observations.exact_retry", True, self.request("observations.retry", "POST", "/observations", batch) == replay)
        conflict = deepcopy(batch)
        conflict["observations"] = []
        self.reject("idempotency_conflict", "POST", "/observations", conflict, 409, "idempotency_conflict")
        valid = deepcopy(batch["observations"][0])
        valid["id"] = "verify-synthetic-extra"
        valid["provenance"]["record_id"] = "verify-synthetic-extra"
        invalid = deepcopy(batch["observations"][1])
        invalid["local_date"] = "2026-02-30"
        self.reject("atomic_invalid_batch", "POST", "/observations", self.body(observations=[valid, invalid], source_updates=[]), 422, "invalid_request", True)
        invalid = deepcopy(batch["observations"][1])
        invalid["purchase"]["merchant"] = "Synthetic changed merchant"
        self.reject("atomic_revision_conflict", "POST", "/observations", self.body(observations=[valid, invalid], source_updates=[]), 409, "revision_conflict")
        event = {"observation_id": batch["observations"][0]["id"], "revision": 1}
        self.feedback("correctness", "partial", "event", event, "shared")
        self.feedback("correctness", "correct", "event", event, "self")
        self.feedback("correctness", "incorrect")
        self.feedback("correctness", "correct")
        self.feedback("usefulness", "not_useful")
        self.feedback("usefulness", "useful")
        self.feedback("suppress", "stop", "scenario")
        self.check("suppression.durable_preference", True, "purchase_recurrence" in self.state["preferences"]["suppressed_scenario_ids"])
        previous = deepcopy(self.state)
        self.check("seed.preserves_feedback_preferences", True, self.request("demo.seed_after_feedback", "POST", "/demo", self.body(**demo)) == previous)
        preferences = deepcopy(defaults)
        self.preferences("preferences.unsuppress_replace", preferences)
        self.check("preferences.both_eligible", True, self.eligible("purchase_recurrence") and self.eligible("recorded_steps"))
        preferences.update(food_suggestions=False, no_kitchen=True, no_spending=True, movement_restriction=True)
        self.preferences("preferences.constraints", preferences)
        self.check("preferences.blocked", False, self.eligible("purchase_recurrence") or self.eligible("recorded_steps"))
        preferences["food_suggestions"] = True
        self.preferences("preferences.compatible_free_planning", preferences)
        self.check("preferences.planning_needs_no_resources", True, self.eligible("purchase_recurrence"))
        partial = deepcopy(preferences)
        del partial["notes"]
        self.reject("preferences.not_patch", "PUT", "/preferences", self.body(preferences=partial), 422, "invalid_request", True)
        for source in ("plaid", "email"):
            unavailable = deepcopy(preferences)
            unavailable["enabled_source_ids"].append(source)
            self.reject("preferences." + source, "PUT", "/preferences", self.body(preferences=unavailable), 409, "not_configured")
        for mode in (["disabled", "live", "scripted"] if with_scripted else ["disabled", "live"]):
            previous = deepcopy(self.state)
            self.check("process.no_cloud_consent", False, previous["preferences"]["cloud_consent"], True)
            self.request("process." + mode, "POST", "/process", self.body(synthesis_mode=mode))
            process = self.state["process"]
            self.check("process." + mode + ".completed", True, process["status"] == "completed" and process["ledger_status"] == "completed")
            synthesis = process["synthesis"]
            self.check("process." + mode + ".mode", True, synthesis["mode"] == mode)
            self.check("process." + mode + ".result",
                       {"status": "completed" if mode == "scripted" else "skipped",
                        "reason": {"disabled": "disabled", "live": "no_credentials", "scripted": None}[mode]},
                       {"status": synthesis["status"], "reason": synthesis["reason"]})
            self.check("process." + mode + ".domain_immutable", True, all(self.state[k] == previous[k] for k in ("observations", "preferences", "feedback", "ledger", "notification_intents")))
            if mode == "scripted":
                self.check("process.scripted.drafts_present", True, bool(synthesis["drafts"]))
        accepted_body = self.body(observations=[valid], source_updates=[])
        accepted = self.request("observations.accept_new", "POST", "/observations", accepted_body)
        self.check("observations.accept_counts", True, accepted["accepted"] == 1 and accepted["ignored"] == 0)
        self.check("observations.accepted_retry_counts", True, self.request("observations.accept_retry", "POST", "/observations", accepted_body) == accepted)
        duplicate = self.request("observations.new_request_duplicate", "POST", "/observations", self.body(observations=[valid], source_updates=[]))
        self.check("observations.duplicate_counts", True, duplicate["accepted"] == 0 and duplicate["ignored"] == 1)
        self.check("observations.duplicate_noop", True, duplicate["state"] == accepted["state"])
        self.reject("demo.modified_fixture_conflict", "POST", "/demo", self.body(**demo), 409, "demo_conflict")
        reset = self.body(**{**demo, "action": "reset"})
        old_epoch = self.state["data_epoch"]
        reset_state = self.request("demo.reset", "POST", "/demo", reset)
        self.check("reset.epoch_rotated", True, reset_state["data_epoch"] != old_epoch)
        self.check("reset.fixture_restored", True, reset_state["observations"] == sample["observations"] and reset_state["preferences"] == sample["preferences"] and not reset_state["feedback"])
        self.check("reset.exact_replay", True, self.request("demo.reset_replay", "POST", "/demo", reset) == reset_state)
        stale = {**self.body(**demo), "data_epoch": old_epoch}
        self.reject("reset.stale_epoch", "POST", "/demo", stale, 409, "epoch_mismatch")
        deletion = self.body(confirm="delete_all")
        old_epoch = self.state["data_epoch"]
        clean = self.request("delete", "DELETE", "/data", deletion)
        self.check("delete.epoch_rotated", True, clean["data_epoch"] != old_epoch)
        self.check("delete.clean_defaults", True, clean["preferences"] == defaults and not clean["observations"] and not clean["feedback"] and not clean["notification_intents"]
                   and clean["process"]["status"] == "idle" and not clean["process"]["synthesis"]["drafts"]
                   and all(s["consent"] == "not_requested" for s in clean["sources"]))
        self.check("delete.exact_replay", True, self.request("delete.replay", "DELETE", "/data", deletion) == clean)
        stale = {**batch, "request_id": "verify-stale-" + uuid4().hex, "data_epoch": old_epoch}
        self.reject("delete.stale_upload", "POST", "/observations", stale, 409, "epoch_mismatch")
        self.check("coverage.eight_real_routes", 8, len(self.routes))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="HTTP literal loopback only (localhost is pinned); default %(default)s")
    parser.add_argument("--output", default="evidence/http-check.json", help="Sanitized evidence under project root; default %(default)s")
    parser.add_argument("--allow-reset", action="store_true", help="Authorize destructive synthetic tests ONLY on an exclusive empty instance; existing data/consent always refused")
    parser.add_argument("--with-scripted", action="store_true", help="Require configured actual SDK scripted integration; response checks alone do not prove SDK internals")
    args = parser.parse_args()
    harness = Harness("")
    completed = False
    try:
        harness.base_url = loopback_url(args.base_url)
        # Validate output boundary before sending any traffic.
        output = (ROOT / args.output).resolve()
        if not output.is_relative_to(ROOT):
            raise ValueError("output boundary")
        harness.run(args.allow_reset, args.with_scripted)
        completed = True
    except StopVerification:
        pass
    except Exception as error:
        # No exception messages, response bodies, notes, URLs or credentials in reports.
        harness.check("harness.runtime_or_safety", "successful safe execution", type(error).__name__)
    failed = sum(not r["passed"] for r in harness.results)
    summary = {"status": "passed" if completed and not failed else "failed", "completed": completed,
               "checks": len(harness.results), "failed": failed, "passed": len(harness.results) - failed,
               "requests": harness.requests, "mutation_requests": harness.mutations, "domain_routes_exercised": len(harness.routes),
               "scripted_requested": args.with_scripted}
    report = {"summary": summary, "generated_at": datetime.now(timezone.utc).isoformat(),
              "scope": "Real HTTP contract checks, not proof of SDK internals, secure disk erasure, iOS or live provider access.",
              "pending": ([] if completed and not failed else ["Complete passing isolated HTTP runtime verification"])
                         + ([] if args.with_scripted and completed and not failed else ["Scripted SDK runtime verification"]),
              "checks": harness.results}
    try:
        write_report(args.output, report)
    except (OSError, ValueError):
        summary["status"] = "failed"
        summary["evidence_written"] = False
    print(json.dumps(summary))
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
