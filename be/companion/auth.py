"""Framework-independent Clerk native session verification. No secrets or dotenv reads.

The public construction path for Principal is AuthVerifier.authenticate_request().
A verifier/cache belongs to exactly one trusted issuer. Close it during shutdown.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import ipaddress
import json
import math
import os
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_MAX_AUTH = 16 * 1024
_MAX_JSON_HEADER = 2048
_MAX_JWKS = 256 * 1024
_B64 = re.compile(r"^[A-Za-z0-9_-]+$")
_VERIFIED = object()


class AuthError(Exception):
    """Safe public failure: never includes token, claims, URL, or vendor errors."""

    def __init__(self, code: str = "unauthorized"):
        self.code = code
        self.status_code = {"unauthorized": 401, "session_pending": 403,
                            "auth_unavailable": 503}[code]
        self.retryable = code == "auth_unavailable"
        self.headers = {"WWW-Authenticate": "Bearer"} if self.status_code == 401 else {}
        super().__init__(code)


class AuthConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True, init=False)
class Principal:
    issuer: str
    subject: str
    session_id: str

    def __init__(self, issuer: str, subject: str, session_id: str, *, _proof=None):
        if _proof is not _VERIFIED:
            raise TypeError("Principal must be obtained from verified authentication")
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "session_id", session_id)


def _public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
        # IPv4-mapped addresses must not bypass IPv4 policy; deny transition forms.
        return (ip.is_global and not ip.is_multicast and not ip.is_unspecified
                and not (isinstance(ip, ipaddress.IPv6Address)
                         and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)))
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class AuthConfig:
    issuer: str
    audience: str | None = None
    authorized_parties: tuple[str, ...] = ()

    def __post_init__(self):
        try:
            u = urlsplit(self.issuer)
            host = u.hostname
            valid = (isinstance(self.issuer, str) and len(self.issuer) <= 2048
                     and self.issuer.isascii() and u.scheme == "https" and host
                     and not u.username and not u.password and not u.path
                     and not u.query and not u.fragment and u.port in (None, 443)
                     and self.issuer == f"https://{u.netloc}"
                     and not any(c.isspace() for c in self.issuer)
                     and not any(c in self.issuer for c in "\\?#"))
            if not valid or host.endswith("."):
                raise ValueError
            try:
                ipaddress.ip_address(host)
            except ValueError:
                labels = host.split(".")
                if (len(labels) < 2 or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", p)
                                           for p in labels)
                        or labels[-1].lower() in {"local", "localhost", "internal", "test", "invalid"}):
                    raise ValueError
            else:
                if not _public_ip(host):
                    raise ValueError
            if self.audience is not None and not _bounded_string(self.audience, 2048):
                raise ValueError
            parties = tuple(self.authorized_parties)
            if (isinstance(self.authorized_parties, str) or len(parties) > 64
                    or any(not _bounded_string(p, 2048) for p in parties)):
                raise ValueError
            object.__setattr__(self, "authorized_parties", parties)
        except (ValueError, TypeError, AttributeError):
            raise AuthConfigurationError("Invalid Clerk authentication configuration") from None

    @property
    def jwks_url(self) -> str:
        return self.issuer + "/.well-known/jwks.json"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AuthConfig:
        """Only explicit supplied mapping or process environment; never reads files.

        CLERK_AUTHORIZED_PARTIES is a JSON array (not a guessed CSV/origin).
        Empty/malformed present settings fail startup instead of disabling checks.
        """
        env = os.environ if environ is None else environ
        try:
            parties = _json(env.get("CLERK_AUTHORIZED_PARTIES", "[]").encode())
            if not isinstance(parties, list):
                raise ValueError
            return cls(env["CLERK_ISSUER"], env.get("CLERK_AUDIENCE"), tuple(parties))
        except (KeyError, ValueError, TypeError, AttributeError):
            raise AuthConfigurationError("Invalid Clerk authentication configuration") from None


def _bounded_string(value, limit: int) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _json(raw: bytes):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError("duplicate JSON member")
            result[name] = value
        return result

    def invalid_constant(_):
        raise ValueError("invalid JSON constant")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("invalid JSON number")
        return number

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=invalid_constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid JSON") from None


def _decode(segment: str, limit: int) -> bytes:
    if not segment or len(segment) > (limit * 4 + 2) // 3 or not _B64.fullmatch(segment):
        raise ValueError
    value = base64.b64decode(segment + "=" * (-len(segment) % 4), altchars=b"-_", validate=True)
    if len(value) > limit or base64.urlsafe_b64encode(value).rstrip(b"=").decode() != segment:
        raise ValueError
    return value


async def _resolve(host: str, port: int) -> Sequence[str]:
    rows = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(row[4][0] for row in rows))


class JWKSCache:
    """Bounded set TTL; one refresh gate including failures and signature rotation.

    DNS is revalidated for every fetch, then the connection is pinned to one
    validated public address while preserving TLS SNI/certificate hostname and Host.
    Custom transport/resolver are trusted dependency-injection hooks for tests.
    No proxy environment, redirect, credential header, discovery or stale-key grace.
    """

    def __init__(self, config: AuthConfig, *, transport: httpx.AsyncBaseTransport | None = None,
                 resolver: Callable[[str, int], Awaitable[Sequence[str]]] = _resolve,
                 monotonic: Callable[[], float] = time.monotonic, ttl: float = 300):
        if not 0 < ttl <= 300:
            raise AuthConfigurationError("JWKS TTL must be in (0, 300]")
        self.config = config
        self._resolver, self._clock, self._ttl = resolver, monotonic, ttl
        self._keys: Mapping[str, rsa.RSAPublicKey] = MappingProxyType({})
        self._expires = float("-inf")
        self._last_attempt = float("-inf")
        self._failed = False
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._closed = False
        self._client = httpx.AsyncClient(transport=transport, trust_env=False, verify=True,
                                        follow_redirects=False, timeout=5,
                                        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))

    async def start(self) -> None:
        """Optional eager startup validation/fetch; failure keeps profile unavailable."""
        await self.refresh()
        if not self._usable():
            raise AuthError("auth_unavailable")

    async def aclose(self) -> None:
        self._closed = True
        async with self._lock:
            self._keys = MappingProxyType({})
            await self._client.aclose()
        if self._refresh_task is not None:
            await asyncio.gather(self._refresh_task, return_exceptions=True)

    def _usable(self) -> bool:
        return not self._closed and bool(self._keys) and self._clock() < self._expires

    async def refresh(self) -> None:
        if self._closed:
            raise AuthError("auth_unavailable")
        task = self._refresh_task
        if task is None or task.done():
            if self._clock() - self._last_attempt < 30:
                return
            self._last_attempt = self._clock()  # Includes DNS, network and parse failures.
            task = asyncio.create_task(self._refresh_once())
            self._refresh_task = task
            task.add_done_callback(self._refresh_done)
        # A disconnected caller cannot cancel the issuer's shared refresh and
        # force every other caller to endure an empty-cache cooldown.
        await asyncio.shield(task)

    @staticmethod
    def _refresh_done(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()

    async def _refresh_once(self) -> None:
        async with self._lock:
            if self._closed:
                raise AuthError("auth_unavailable")
            try:
                async with asyncio.timeout(5):
                    raw = await self._fetch()
                    keys = self._parse_keys(raw)
                self._keys = MappingProxyType(keys)  # Atomic replacement, never merge.
                self._expires = self._clock() + self._ttl
                self._failed = False
            except asyncio.CancelledError:
                self._failed = True
                raise
            except Exception:
                self._failed = True  # Deliberately retain only still-unexpired previous set.

    async def _fetch(self) -> bytes:
        u = urlsplit(self.config.issuer)
        addresses = await self._resolver(u.hostname, 443)
        if not addresses or len(addresses) > 32 or any(not _public_ip(ip) for ip in addresses):
            raise ValueError("unsafe JWKS destination")
        address = addresses[0]
        authority = f"[{address}]" if ":" in address else address
        # httpcore's sni_hostname extension preserves hostname verification on IP URL.
        url = f"https://{authority}/.well-known/jwks.json"
        async with self._client.stream("GET", url, headers={"Host": u.netloc,
                "Accept": "application/json", "Accept-Encoding": "identity"},
                extensions={"sni_hostname": u.hostname}) as response:
            if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("invalid JWKS response")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > _MAX_JWKS):
                raise ValueError("oversized JWKS")
            chunks, size = [], 0
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > _MAX_JWKS:
                    raise ValueError("oversized JWKS")
                chunks.append(chunk)
            return b"".join(chunks)

    @staticmethod
    def _parse_keys(raw: bytes) -> dict[str, rsa.RSAPublicKey]:
        if len(raw) > _MAX_JWKS:
            raise ValueError
        document = _json(raw)
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise ValueError
        entries = document["keys"]
        if not 1 <= len(entries) <= 32:
            raise ValueError
        keys, seen = {}, set()
        for entry in entries:
            if not isinstance(entry, dict) or not _bounded_string(entry.get("kid"), 128):
                raise ValueError
            kid = entry["kid"]
            if kid in seen:
                raise ValueError
            seen.add(kid)
            # Ignore non-signing keys, but never select an unsuitable key.
            if (entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig"
                    or entry.get("alg", "RS256") != "RS256"
                    or ("key_ops" in entry and entry["key_ops"] != ["verify"])):
                continue
            if any(field in entry for field in ("d", "p", "q", "dp", "dq", "qi", "oth")):
                raise ValueError
            n = int.from_bytes(_decode(entry.get("n"), 1024), "big")
            e = int.from_bytes(_decode(entry.get("e"), 4), "big")
            if not 2048 <= n.bit_length() <= 8192 or e < 3 or e % 2 == 0:
                raise ValueError
            keys[kid] = rsa.RSAPublicNumbers(e, n).public_key()
        if not keys:
            raise ValueError
        return keys

    async def key(self, kid: str) -> rsa.RSAPublicKey:
        if not self._usable() or kid not in self._keys:
            await self.refresh()
        if not self._usable():
            raise AuthError("auth_unavailable")
        key = self._keys.get(kid)
        if key is None:
            raise AuthError("auth_unavailable" if self._failed else "unauthorized")
        return key


class AuthVerifier:
    def __init__(self, config: AuthConfig, *, cache: JWKSCache | None = None,
                 clock: Callable[[], float] = time.time):
        if cache is not None and cache.config != config:
            raise AuthConfigurationError("JWKS cache issuer/policy mismatch")
        self.config, self.cache, self._clock = config, cache or JWKSCache(config), clock

    async def start(self) -> None:
        await self.cache.start()

    async def aclose(self) -> None:
        await self.cache.aclose()

    async def authenticate_request(self, request) -> Principal:
        """Accept an ASGI Request (scope['headers']) or raw byte-header sequence.

        Use ASGI raw headers, never a collapsed mapping: duplicate Authorization
        must remain observable. This performs no domain/storage side effects.
        """
        headers = request.scope["headers"] if hasattr(request, "scope") else request
        try:
            values = [v for k, v in headers if k.lower() == b"authorization"]
            if len(values) != 1 or not isinstance(values[0], bytes) or len(values[0]) > _MAX_AUTH:
                raise ValueError
            value = values[0].decode("ascii")
            if not re.fullmatch(r"(?i:Bearer) [A-Za-z0-9_.-]+", value):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise AuthError() from None
        return await self.verify_token(value[7:])

    async def verify_token(self, token: str) -> Principal:
        """Verify a raw JWT; HTTP callers should use authenticate_request instead."""
        try:
            if not isinstance(token, str) or len(token) > _MAX_AUTH - 7:
                raise ValueError
            segments = token.split(".")
            if len(segments) != 3:
                raise ValueError
            header = _json(_decode(segments[0], _MAX_JSON_HEADER))
            claims = _json(_decode(segments[1], _MAX_AUTH))
            signature = _decode(segments[2], 1024)
            if (not isinstance(header, dict) or not isinstance(claims, dict)
                    or header.get("alg") != "RS256" or not _bounded_string(header.get("kid"), 128)
                    or any(k in header for k in ("jku", "jwk", "x5u", "crit", "b64"))):
                raise ValueError
            message = (segments[0] + "." + segments[1]).encode("ascii")
        except (ValueError, TypeError, UnicodeError):
            raise AuthError() from None
        key = await self.cache.key(header["kid"])
        try:
            key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            # Same-kid rotation: shared gate, at most one refresh and one retry.
            await self.cache.refresh()
            key = await self.cache.key(header["kid"])
            try:
                key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
            except InvalidSignature:
                # A failed gated refresh cannot distinguish same-kid rotation
                # during outage from a bad signature; never prompt refresh loops.
                raise AuthError("auth_unavailable" if self.cache._failed else "unauthorized") from None
        self._validate_claims(claims)
        return Principal(claims["iss"], claims["sub"], claims["sid"], _proof=_VERIFIED)

    def _validate_claims(self, c: dict) -> None:
        now = self._clock()
        if (c.get("iss") != self.config.issuer
                or not _bounded_string(c.get("sub"), 256) or not _bounded_string(c.get("sid"), 256)
                or type(c.get("v")) is not int or c["v"] != 2 or "act" in c
                or any(type(c.get(t)) is not int or not 0 <= c[t] <= 253402300799 for t in ("exp", "nbf", "iat"))):
            raise AuthError()
        if (now >= c["exp"] + 5 or c["nbf"] > now + 5 or c["iat"] > now + 5
                or c["exp"] <= c["iat"] or c["exp"] <= c["nbf"]):
            raise AuthError()
        if "azp" in c and (not _bounded_string(c["azp"], 2048) or c["azp"] not in self.config.authorized_parties):
            raise AuthError()
        if self.config.audience is None:
            if "aud" in c:
                raise AuthError()
        else:
            audience = c.get("aud")
            values = [audience] if isinstance(audience, str) else audience
            if (not isinstance(values, list) or not 1 <= len(values) <= 64
                    or any(not _bounded_string(a, 2048) for a in values)
                    or self.config.audience not in values):
                raise AuthError()
        if "sts" in c and c["sts"] != "active":
            raise AuthError("session_pending" if c["sts"] == "pending" else "unauthorized")


def verifier_from_env(environ: Mapping[str, str] | None = None, **kwargs) -> AuthVerifier:
    """Explicit auth-profile factory; invalid/missing config never becomes anonymous."""
    return AuthVerifier(AuthConfig.from_env(environ), **kwargs)
