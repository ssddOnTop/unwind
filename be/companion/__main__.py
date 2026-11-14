"""Supported local entry point; deliberately no configurable public bind address."""
import argparse
import os
from pathlib import Path

import uvicorn

from .contracts import ROOT
from .http import create_app


def main():
    parser = argparse.ArgumentParser(description="Local companion API (loopback only)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--auth", action="store_true", help="Require Clerk authentication and isolate storage per user")
    parser.add_argument('--persistence', choices=('sqlite','postgres'), default='sqlite', help='Explicit persistence selection; PostgreSQL requires --auth and a migrated private schema')
    parser.add_argument("--experience", action="store_true", help="Enable versioned backend experience/answer routes (legacy isolated-test mode)")
    parser.add_argument("--plaid-sandbox", action="store_true", help="Opt in to the frozen additive Sandbox state profile")
    parser.add_argument('--banking',action='store_true',help='Opt in to versioned multi-bank Sandbox management')
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    if args.persistence == 'postgres' and not args.auth:
        parser.error('PostgreSQL requires explicit authenticated mode; no anonymous fallback.')
    data_dir = (args.data_dir or ROOT / "be" / ('.data-auth-pg' if args.persistence == 'postgres' else ".data-auth" if args.auth else ".data")).resolve()
    if not data_dir.is_relative_to(ROOT):
        parser.error("--data-dir must stay under the workspace root")
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    os.umask(0o077)
    from .environment import load_runtime_environment
    load_runtime_environment(ROOT)
    # No reload, multiple workers, proxy headers, access payload logs, or public preview.
    plaid_profile = args.banking or args.plaid_sandbox or os.environ.get("COMPANION_PLAID_SANDBOX", "").lower() in {"1", "true"}
    if args.auth:
        from .auth import AuthConfig, AuthConfigurationError
        from .auth_http import create_authenticated_app
        try:
            config = AuthConfig.from_env()
        except AuthConfigurationError:
            parser.error("Clerk configuration is missing or invalid; authenticated mode cannot start.")
        runtime = None
        if args.persistence == 'postgres':
            from .postgres import PostgresConfig, PostgresRuntime
            try:
                runtime = PostgresRuntime(PostgresConfig.from_env())
            except ValueError:
                parser.error('PostgreSQL configuration is missing or invalid; verified TLS and DATABASE_URL are required.')
        webhook_url = os.environ.get('PLAID_WEBHOOK_URL') if runtime else None
        storage_options = {'postgres_runtime': runtime, 'webhook_url': webhook_url} if runtime else {}
        if args.banking:storage_options['banking_profile']=True
        app = create_authenticated_app(data_dir, auth_config=config,
                                       plaid_profile=plaid_profile, experience_profile=True, **storage_options)
    else:
        bank_options={'banking_profile':True} if args.banking else {}
        app = create_app(data_dir, plaid_profile=plaid_profile, experience_profile=args.experience, **bank_options)
    uvicorn.run(app, host="127.0.0.1", port=args.port,
                workers=1, proxy_headers=False, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
