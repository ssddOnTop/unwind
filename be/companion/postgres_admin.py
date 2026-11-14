"""Explicit operator commands. check is read-only; migrate requires a confirmation flag."""
import argparse
import asyncio
import json
import os
import socket
import psycopg
from psycopg.conninfo import conninfo_to_dict
from .contracts import ROOT
from .environment import load_runtime_environment
from .postgres import PostgresConfig
from .postgres_migrations import MIGRATION_V1


async def run(action, confirmed=False):
    load_runtime_environment(ROOT)
    dsn=os.environ.get('DATABASE_URL','')
    if not dsn:return {'configured':False,'connected':False}
    try:
        values=conninfo_to_dict(dsn)
        socket.getaddrinfo(values.get('host',''),int(values.get('port',5432)),type=socket.SOCK_STREAM)
    except Exception:return {'configured':True,'dns':'unavailable','connected':False}
    try:config=PostgresConfig(dsn)
    except ValueError:return {'configured':True,'dns':'resolved','tls_configuration':'rejected','connected':False}
    try:
        conn=await psycopg.AsyncConnection.connect(config.dsn,autocommit=True,sslmode='verify-full',
            sslrootcert=values.get('sslrootcert','system'),connect_timeout=3,
            options='-c default_transaction_read_only=on -c statement_timeout=3000')
        async with conn:
            if action=='migrate':
                if not confirmed:return {'connected':True,'migration':'confirmation_required'}
                await conn.execute('SET default_transaction_read_only=off')
                await conn.execute(MIGRATION_V1)
                return {'connected':True,'migration':'completed'}
            row=await (await conn.execute("SELECT (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user),to_regclass('companion_private.snapshots') IS NOT NULL")).fetchone()
            return {'configured':True,'dns':'resolved','connected':True,'verified_tls':True,'private_schema_ready':row[1],
                    'role_bypasses_rls':row[0],'read_only':True}
    except Exception:return {'configured':True,'dns':'resolved','connected':False,'read_only':action=='check'}


def main():
    p=argparse.ArgumentParser(description='Private monolith PostgreSQL diagnostics/operator migration; no data import')
    p.add_argument('action',choices=['check','migrate'])
    p.add_argument('--confirm-private-schema',action='store_true')
    args=p.parse_args()
    if args.action=='migrate' and not args.confirm_private_schema:p.error('Migration requires --confirm-private-schema')
    print(json.dumps(asyncio.run(run(args.action,args.confirm_private_schema))))

if __name__=='__main__':main()
