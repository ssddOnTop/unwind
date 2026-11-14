"""Sandbox webhook authentication only; no owner identity, persistence, or HTTP app.

The injected provider is async (kid: str) -> {"key": JWK}. It must use a fixed
Sandbox origin, credentials supplied by its caller, no redirects, and a streamed
response byte limit (MAX_KEY_RESPONSE_BYTES). This module also bounds the decoded
response, but cannot bound bytes already consumed inside somebody else's provider.
The existing PlaidSandboxClient adapter must use request(), not call().

Plaid reference: https://plaid.com/docs/api/webhooks/webhook-verification/
ES256, iat <= 300 seconds old, exact raw-body SHA256. Local hardening permits at
most 5 seconds of future clock skew. Key metadata is rechecked on every use;
cache TTL is 300 seconds, so upstream revocation visibility is bounded by TTL.
Unknown signed body fields are bounded and discarded, including user_id. Callers
must resolve item_id exclusively through their server-side registry, and handle
only supported webhook_type/webhook_code pairs. This verifier is not deduplication.

One verifier per event loop. Caller owns the provider/transport. aclose() drains
only verifier-owned key tasks, never closes the provider. Waiter cancellation
cannot cancel a shared lookup; provider cancellation itself remains cancellation.
No raw body, JWT, provider response, or underlying exception is logged/returned.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

MAX_BODY_BYTES = 65_536
MAX_TOKEN_BYTES = 8_192
MAX_KEY_RESPONSE_BYTES = 8_192
MAX_HEADERS = 100
MAX_HEADER_BYTES = 32_768
MAX_CACHE_KEYS = 16
MAX_INFLIGHT = 4
MAX_LOOKUPS_PER_MINUTE = 8
KEY_TTL_SECONDS = 300.0
FAILURE_COOLDOWN_SECONDS = 10.0
PROVIDER_TIMEOUT_SECONDS = 3.0
MAX_AGE_SECONDS = 300
FUTURE_SKEW_SECONDS = 5

_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_LABEL = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_B64 = re.compile(r"[A-Za-z0-9_-]+\Z")


class WebhookError(Exception):
    """Only safe error information for the HTTP adapter; never echo exceptions."""

    def __init__(self, status: int = 401):
        self.status = 503 if status == 503 else 401
        self.retryable = self.status == 503
        super().__init__("Webhook verification unavailable." if self.retryable
                         else "Webhook verification failed.")


@dataclass(frozen=True, slots=True)
class VerifiedPlaidEvent:
    item_id: str
    environment: str
    webhook_type: str
    webhook_code: str
    body_digest: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class _Key:
    public_key: ec.EllipticCurvePublicKey
    created_at: int
    expired_at: int | None
    cached_until: float


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _integer(value):
    if len(value) > 64:
        raise ValueError("integer budget")
    return int(value)


def _constant(_value):
    raise ValueError("non-finite JSON number")


def _bounded_tree(value, *, max_nodes=4096, max_depth=16, max_string=MAX_BODY_BYTES):
    pending = [(value, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("JSON budget")
        if type(node) is dict:
            if (len(node) + count > max_nodes
                    or any(type(k) is not str or len(k) > max_string for k in node)):
                raise ValueError("JSON object budget")
            pending.extend((v, depth + 1) for v in node.values())
        elif type(node) is list:
            if len(node) + count > max_nodes:
                raise ValueError("JSON array budget")
            pending.extend((v, depth + 1) for v in node)
        elif type(node) is float:
            if not math.isfinite(node):
                raise ValueError("non-finite number")
        elif type(node) not in (str, int, bool, type(None)):
            raise ValueError("JSON value type")
        if type(node) is str and len(node) > max_string:
            raise ValueError("JSON string budget")
        if type(node) is int and node.bit_length() > 213:
            raise ValueError("integer budget")


def _json(raw: bytes, limit: int) -> dict:
    if len(raw) > limit:
        raise ValueError("JSON byte budget")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                       parse_int=_integer, parse_constant=_constant)
    if type(value) is not dict:
        raise ValueError("JSON object required")
    _bounded_tree(value)
    return value


def _decode(segment: str, limit: int) -> bytes:
    if len(segment) > limit or not _B64.fullmatch(segment):
        raise ValueError("base64url budget or alphabet")
    decoded = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != segment:
        raise ValueError("noncanonical base64url")
    return decoded


def _timestamp(value):
    return type(value) is int and 0 <= value <= 253_402_300_799


class PlaidWebhookVerifier:
    def __init__(self, provider: Callable[[str], Awaitable[dict]], *,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic):
        self._provider = provider
        self._clock = clock
        self._monotonic = monotonic
        self._cache: OrderedDict[str, _Key] = OrderedDict()
        self._failures: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._lookups: deque[float] = deque()
        self._unavailable_until = 0.0
        self._closed = False

    async def aclose(self) -> None:
        """Cancel/drain owned lookups; never close the injected provider."""
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
        self._failures.clear()

    async def verify(self, raw_body: bytes,
                     raw_headers: list[tuple[bytes, bytes]]) -> VerifiedPlaidEvent:
        if self._closed:
            raise WebhookError(503)
        try:
            if type(raw_body) is not bytes or not 0 < len(raw_body) <= MAX_BODY_BYTES:
                raise ValueError("body budget")
            token = self._token(raw_headers)
            head, claims, signature = token.split(".")
            header = _json(_decode(head, 2048), 1536)
            payload = _json(_decode(claims, 4096), 3072)
            if (set(header) - {"alg", "kid", "typ"} or header.get("alg") != "ES256"
                    or ("typ" in header and header["typ"] != "JWT")):
                raise ValueError("JWT header")
            kid = header.get("kid")
            if type(kid) is not str or not _ID.fullmatch(kid):
                raise ValueError("key ID")
            if set(payload) != {"iat", "request_body_sha256"}:
                raise ValueError("JWT claims")
            issued = payload["iat"]
            digest = payload["request_body_sha256"]
            if not _timestamp(issued) or type(digest) is not str or not _HEX.fullmatch(digest):
                raise ValueError("JWT claim types")
            signature_bytes = _decode(signature, 86)
            if len(signature_bytes) != 64:
                raise ValueError("ES256 signature width")
            now = self._clock()
            self._check_age(issued, now)
            received_at = datetime.fromtimestamp(now, timezone.utc)
            key = await self._key(kid)
            now = self._clock()
            self._check_age(issued, now)
            self._check_key_time(key, now)
            if key.created_at > issued + FUTURE_SKEW_SECONDS:
                raise ValueError("key created after signature")
            # JOSE uses fixed-width r || s, whereas cryptography expects ASN.1 DER.
            r = int.from_bytes(signature_bytes[:32], "big")
            s = int.from_bytes(signature_bytes[32:], "big")
            key.public_key.verify(encode_dss_signature(r, s),
                                  (head + "." + claims).encode("ascii"),
                                  ec.ECDSA(hashes.SHA256()))
            actual_digest = hashlib.sha256(raw_body).hexdigest()
            if not hmac.compare_digest(actual_digest, digest):
                raise ValueError("body digest")
            # Do not parse, reserialize, or inspect unverified body fields above.
            body = _json(raw_body, MAX_BODY_BYTES)
            item_id = body.get("item_id")
            kind, code = body.get("webhook_type"), body.get("webhook_code")
            if (type(item_id) is not str or not _ID.fullmatch(item_id)
                    or body.get("environment") != "sandbox"
                    or type(kind) is not str or not _LABEL.fullmatch(kind)
                    or type(code) is not str or not _LABEL.fullmatch(code)):
                raise ValueError("webhook fields")
            return VerifiedPlaidEvent(item_id, "sandbox", kind, code, actual_digest, received_at)
        except WebhookError:
            raise
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError, InvalidSignature):
            raise WebhookError() from None

    @staticmethod
    def _token(headers):
        if type(headers) is not list or len(headers) > MAX_HEADERS:
            raise ValueError("header count")
        size = 0
        matches = []
        for pair in headers:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("header pair")
            name, value = pair
            if type(name) is not bytes or type(value) is not bytes:
                raise ValueError("header types")
            size += len(name) + len(value)
            if size > MAX_HEADER_BYTES:
                raise ValueError("header byte budget")
            if name.lower() == b"plaid-verification":
                if len(value) > MAX_TOKEN_BYTES:
                    raise ValueError("JWT budget")
                matches.append(value)
        if len(matches) != 1:
            raise ValueError("one verification header required")
        return matches[0].decode("ascii")

    @staticmethod
    def _check_age(issued, now):
        if not math.isfinite(now) or not -FUTURE_SKEW_SECONDS <= now - issued <= MAX_AGE_SECONDS:
            raise ValueError("JWT age")

    @staticmethod
    def _check_key_time(key, now):
        if key.created_at > now + FUTURE_SKEW_SECONDS or (
                key.expired_at is not None and key.expired_at <= now):
            raise WebhookError()

    async def _key(self, kid):
        tick = self._monotonic()
        key = self._cache.get(kid)
        if key is not None and tick < key.cached_until:
            self._check_key_time(key, self._clock())
            self._cache.move_to_end(kid)
            return key
        self._cache.pop(kid, None)
        failure = self._failures.get(kid)
        if failure is not None and tick < failure[0]:
            raise WebhookError(failure[1])
        self._failures.pop(kid, None)
        task = self._inflight.get(kid)
        if task is None:
            while self._lookups and tick - self._lookups[0] >= 60:
                self._lookups.popleft()
            if (tick < self._unavailable_until or len(self._inflight) >= MAX_INFLIGHT
                    or len(self._lookups) >= MAX_LOOKUPS_PER_MINUTE):
                raise WebhookError(503)
            self._lookups.append(tick)
            task = asyncio.create_task(self._fetch(kid))
            self._inflight[kid] = task
            task.add_done_callback(lambda done: self._done(kid, done))
        return await asyncio.shield(task)

    def _done(self, kid, task):
        if self._inflight.get(kid) is task:
            self._inflight.pop(kid, None)
        if not task.cancelled():
            task.exception()  # Retrieve failures even if every waiter was cancelled.

    async def _fetch(self, kid):
        try:
            try:
                response = await asyncio.wait_for(self._provider(kid), PROVIDER_TIMEOUT_SECONDS)
            except WebhookError as exc:
                raise WebhookError(exc.status) from None
            except Exception:
                raise WebhookError(503) from None
            try:
                # The provider is trusted infrastructure, but its response still has budgets.
                _bounded_tree(response, max_nodes=128, max_depth=6,
                              max_string=MAX_KEY_RESPONSE_BYTES)
                if type(response) is not dict or len(json.dumps(response)) > MAX_KEY_RESPONSE_BYTES:
                    raise ValueError("key response budget")
                jwk = response["key"]
                if (type(jwk) is not dict or jwk.get("kid") != kid
                        or jwk.get("alg") != "ES256" or jwk.get("kty") != "EC"
                        or jwk.get("crv") != "P-256" or "d" in jwk
                        or jwk.get("use", "sig") != "sig"
                        or jwk.get("key_ops", ["verify"]) != ["verify"]):
                    raise ValueError("key metadata")
                x, y = _decode(jwk["x"], 43), _decode(jwk["y"], 43)
                if len(x) != 32 or len(y) != 32:
                    raise ValueError("key coordinate width")
                created, expired = jwk["created_at"], jwk["expired_at"]
                if not _timestamp(created) or (expired is not None and not _timestamp(expired)):
                    raise ValueError("key time types")
                public = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"),
                            int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
                key = _Key(public, created, expired, self._monotonic() + KEY_TTL_SECONDS)
            except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
                raise WebhookError(503) from None
            self._check_key_time(key, self._clock())
            self._cache[kid] = key
            self._cache.move_to_end(kid)
            while len(self._cache) > MAX_CACHE_KEYS:
                self._cache.popitem(last=False)
            return key
        except WebhookError as exc:
            tick = self._monotonic()
            if exc.retryable:
                self._unavailable_until = tick + FAILURE_COOLDOWN_SECONDS
            self._failures[kid] = (tick + FAILURE_COOLDOWN_SECONDS, exc.status)
            self._failures.move_to_end(kid)
            while len(self._failures) > MAX_CACHE_KEYS:
                self._failures.popitem(last=False)
            raise
