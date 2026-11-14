"""Launcher selection tests: no environment file, server, or provider calls."""
import sys
import types
from pathlib import Path

import pytest

from companion import __main__ as launcher
from companion import environment
from companion.auth import AuthConfig


@pytest.fixture
def launch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "load_runtime_environment", lambda root: None)
    for key in ("CLERK_ISSUER", "CLERK_AUDIENCE", "CLERK_AUTHORIZED_PARTIES", "COMPANION_PLAID_SANDBOX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(launcher.os, "umask", lambda value: None)
    monkeypatch.setattr(launcher.uvicorn, "run", lambda app, **kwargs: calls.append(("server", app, kwargs)))
    def anonymous(path, **kwargs):
        calls.append(("anonymous", path, kwargs))
        return "anonymous-app"
    def authenticated(path, **kwargs):
        calls.append(("authenticated", path, kwargs))
        return "authenticated-app"
    monkeypatch.setattr(launcher, "create_app", anonymous)
    monkeypatch.setitem(sys.modules, "companion.auth_http", types.SimpleNamespace(create_authenticated_app=authenticated))
    def run(*args):
        monkeypatch.setattr(sys, "argv", ["companion", *args])
        launcher.main()
        return calls
    return run, calls, tmp_path


def test_auth_missing_configuration_never_falls_back(launch):
    run, calls, _ = launch
    with pytest.raises(SystemExit) as error:
        run("--auth")
    assert error.value.code == 2
    assert calls == []


def test_auth_defaults_to_separate_root_and_enables_experience(launch, monkeypatch):
    run, _, root = launch
    monkeypatch.setenv("CLERK_ISSUER", "https://accounts.example.com")
    calls = run("--auth", "--port", "8770", "--plaid-sandbox")
    name, path, kwargs = calls[0]
    assert name == "authenticated"
    assert path == root / "be" / ".data-auth"
    assert kwargs == {"auth_config": AuthConfig("https://accounts.example.com"),
                      "plaid_profile": True, "experience_profile": True}
    assert calls[1] == ("server", "authenticated-app", {
        "host": "127.0.0.1", "port": 8770, "workers": 1, "proxy_headers": False,
        "access_log": False, "log_level": "warning"})


def test_legacy_launcher_does_not_implicitly_enable_auth(launch, monkeypatch):
    run, _, root = launch
    monkeypatch.setenv("CLERK_ISSUER", "https://accounts.example.com")
    calls = run("--experience")
    assert calls[0] == ("anonymous", root / "be" / ".data", {
        "plaid_profile": False, "experience_profile": True})


def test_auth_explicit_root_is_forwarded_without_migration(launch, monkeypatch):
    run, _, root = launch
    monkeypatch.setenv("CLERK_ISSUER", "https://accounts.example.com")
    selected = root / "isolated-auth"
    calls = run("--auth", "--data-dir", str(selected))
    assert calls[0][1] == selected
    assert not selected.exists()


@pytest.mark.parametrize("issuer", ["", "http://accounts.example.com", "https://localhost", "https://accounts.example.com/path"])
def test_bad_issuer_never_launches(launch, monkeypatch, issuer):
    run, calls, _ = launch
    monkeypatch.setenv("CLERK_ISSUER", issuer)
    with pytest.raises(SystemExit):
        run("--auth")
    assert calls == []


def test_auth_data_root_outside_workspace_is_rejected(launch):
    run, calls, root = launch
    with pytest.raises(SystemExit):
        run("--auth", "--data-dir", str(root.parent / "outside-auth"))
    assert calls == []


@pytest.mark.parametrize("port", ["80", "65536"])
def test_auth_invalid_port_is_rejected(launch, port):
    run, calls, _ = launch
    with pytest.raises(SystemExit):
        run("--auth", "--port", port)
    assert calls == []


def test_postgres_requires_explicit_auth_and_never_falls_back(launch):
    run,calls,_=launch
    with pytest.raises(SystemExit):run('--persistence','postgres')
    assert not calls


@pytest.mark.parametrize('dsn',['','host=db.example.invalid sslmode=disable','host=db.example.invalid port=6543 sslmode=verify-full'])
def test_postgres_bad_configuration_does_not_launch_or_echo_secret(launch,monkeypatch,capsys,dsn):
    run,calls,_=launch
    monkeypatch.setenv('CLERK_ISSUER','https://accounts.example.com')
    monkeypatch.setenv('DATABASE_URL',dsn)
    with pytest.raises(SystemExit):run('--auth','--persistence','postgres')
    assert not calls
    if dsn:assert dsn not in capsys.readouterr().err


def test_postgres_is_explicit_and_passes_only_server_webhook_configuration(launch,monkeypatch):
    run,calls,root=launch
    monkeypatch.setenv('CLERK_ISSUER','https://accounts.example.com')
    monkeypatch.setenv('DATABASE_URL','host=db.example.invalid sslmode=verify-full')
    monkeypatch.setenv('PLAID_WEBHOOK_URL','https://receiver.example/webhooks/plaid')
    run('--auth','--persistence','postgres','--plaid-sandbox')
    assert calls[0][1]==root/'be'/'.data-auth-pg'
    assert calls[0][2]['postgres_runtime'] is not None
    assert calls[0][2]['webhook_url']=='https://receiver.example/webhooks/plaid'
    assert calls[1][2]['host']=='127.0.0.1'
    assert 'db.example.invalid' not in repr(calls[0][2]['postgres_runtime'].config)
