"""Container-only authenticated PostgreSQL monolith; never loads dotenv or runs DDL."""
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

import uvicorn
from psycopg.conninfo import make_conninfo

from .auth import AuthConfig
from .auth_http import create_authenticated_app
from .connections.plaid import SandboxCredentials
from .postgres import PostgresConfig, PostgresRuntime


DATA_ROOT = Path("/data")
CA_FILE = Path("/run/certs/supabase-ca.crt")


def create_container_app():
    os.umask(0o077)
    # Do not allow ambient tracing to export private financial context.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    os.environ.setdefault("COMPANION_DAILY_SYNTHESIS_MODE", "disabled")
    auth = AuthConfig.from_env()
    config = PostgresConfig.from_env()
    if not CA_FILE.is_file():
        raise ValueError("Mount the PostgreSQL CA certificate read-only.")
    config = PostgresConfig(make_conninfo(config.dsn, sslrootcert=str(CA_FILE)))
    if SandboxCredentials.from_environment() is None:
        raise ValueError("Plaid Sandbox credentials are required.")
    webhook_url = os.environ.get("PLAID_WEBHOOK_URL", "")
    url = urlsplit(webhook_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("A server-only HTTPS Plaid webhook URL is required.")
    # Refuse a missing/fresh volume rather than silently replacing migrated state.
    info = DATA_ROOT.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("The residual directory must be owned by the container user, mode 0700.")
    marker = DATA_ROOT / ".postgres-root"
    if marker.is_symlink() or marker.read_bytes() != b"companion-postgres-residual-v1\n":
        raise ValueError("Mount the prepared PostgreSQL residual root, not a SQLite root.")
    return create_authenticated_app(
        DATA_ROOT, auth_config=auth, postgres_runtime=PostgresRuntime(config),
        plaid_profile=True, experience_profile=True, banking_profile=True,
        webhook_url=webhook_url,
    )


def main():
    try:
        app = create_container_app()
    except Exception:
        # Never print a DSN, credential, private path or underlying exception.
        raise SystemExit("Container configuration invalid; check auth, PostgreSQL TLS, Sandbox and private mounts.") from None
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1,
                proxy_headers=False, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
