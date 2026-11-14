"""Offline host configuration checks: mock JWKS, scratch stores, no providers/DB."""
import httpx
import pytest

from companion.container import public_api_hosts
from companion.http import LOOPBACK_HOSTS, allowed_host, validated_hosts
from test_auth_http import Harness, key  # shared offline RSA/JWKS fixture


def test_public_origin_adds_only_exact_host():
    assert public_api_hosts(None) == LOOPBACK_HOSTS
    assert public_api_hosts("https://api.example.com:8443/") == LOOPBACK_HOSTS | {"api.example.com"}


@pytest.mark.parametrize("value", ["", "http://api.example.com", "https://api.example.com/v1",
    "https://user:pass@api.example.com", "https://api.example.com?", "https://api.example.com#",
    "https://*.example.com", "https://api.example.com:0", "https://api.example.com:65536",
    "https://api.example.com:", "https://api.example.com:/", "https://api.example.com\\evil",
    "https://api%2eexample.com", " https://api.example.com", "https://api.example.com\n"])
def test_invalid_public_origin_fails_closed(value):
    with pytest.raises(ValueError, match="PUBLIC_API_URL"):
        public_api_hosts(value)


def test_host_allowlist_cannot_be_wildcard_or_forwarded():
    with pytest.raises(ValueError):
        validated_hosts({"*"})
    hosts = public_api_hosts("https://api.example.com")
    assert allowed_host({"headers": [(b"host", b"api.example.com:443")]}, hosts)
    for headers in [[(b"host", b"evil.example"), (b"x-forwarded-host", b"api.example.com")],
                    [(b"host", b"api.example.com"), (b"host", b"api.example.com")],
                    [(b"host", b"api.example.com.evil")], [(b"host", b"api.example.com:0")]]:
        assert not allowed_host({"headers": headers}, hosts)


@pytest.mark.asyncio
async def test_public_host_reaches_admission_and_default_child_boundary(tmp_path, key):
    hosts = public_api_hosts("https://api.example.com")
    h = Harness(tmp_path, key, service_factory=None, allowed_hosts=hosts, experience_profile=True)
    async with h.app.router.lifespan_context(h.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="https://api.example.com") as client:
            for path in ["/health", "/auth/v1/session", "/state", "/experience/v1"]:
                response = await client.get(path, headers=h.headers())
                assert response.status_code == 200, (path, response.status_code)
            assert (await client.get("/state")).status_code == 401
            for host in ["evil.example", "api.example.com.evil", "clerk.example.com"]:
                response = await client.get("/state", headers={**h.headers(), "Host": host, "X-Forwarded-Host": "api.example.com"})
                assert response.status_code == 400
            for host in ["127.0.0.1:8000", "localhost"]:
                assert (await client.get("/health", headers={"Host": host})).status_code == 200


@pytest.mark.asyncio
async def test_default_factory_still_rejects_public_host(tmp_path, key):
    h = Harness(tmp_path, key)
    async with h.app.router.lifespan_context(h.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="https://api.example.com") as client:
            assert (await client.get("/health")).status_code == 400
            assert h.jwks_calls == 0
