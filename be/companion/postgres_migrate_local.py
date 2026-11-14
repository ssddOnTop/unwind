"""Explicit one-off trusted SQLite registry import; never an HTTP identity/seed API.

The caller quiesces the source before apply. Secrets come only from the environment
(or the explicitly selected mode-0600 secrets root). Output is counts/status only.
"""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from .environment import load_runtime_environment
from .plaid_contracts import validate_profile
from .postgres import PostgresConfig
from .vault import PrivateVault

TABLES=('accounts','snapshots','receipts','plaid_owners','webhook_receipts','webhook_jobs')


class Refused(Exception):pass


def require(ok):
    if not ok:raise Refused()


def regular(path):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1)


def read_db(path):
    regular(path)
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    require(conn.execute('PRAGMA quick_check').fetchone()==('ok',))
    return conn


def private_mkdir(path):
    path.mkdir(mode=0o700,parents=True,exist_ok=False)
    path.chmod(0o700)


def copy_private(source,target,*,residual=False):
    """SQLite backup folds WAL into each DB. No source chmod/write/renaming."""
    private_mkdir(target)
    files=0
    for path in source.iterdir():
        info=path.lstat()
        require(info.st_uid==os.getuid() and not path.is_symlink())
        if stat.S_ISDIR(info.st_mode):
            files+=copy_private(path,target/path.name,residual=residual);continue
        regular(path)
        if path.name.endswith(('-wal','-shm','-journal','.lock')):continue
        if residual and path.name=='state.sqlite':continue
        destination=target/path.name
        if path.suffix=='.sqlite':
            with closing(read_db(path)) as original,closing(sqlite3.connect(destination)) as backup:original.backup(backup)
        else:shutil.copyfile(path,destination)
        destination.chmod(0o600);files+=1
    return files


def source_records(root):
    require(root.is_dir() and not root.is_symlink() and (root/'.auth-root').read_bytes()==b'companion-auth-root-v1\n')
    with closing(read_db(root/'identities.sqlite')) as conn:
        identities=conn.execute('SELECT account_id,issuer,subject FROM identities ORDER BY account_id').fetchall()
    seen=set();owners={};records=[]
    for identifier,issuer,subject in identities:
        require(str(UUID(identifier))==identifier and isinstance(issuer,str) and issuer and isinstance(subject,str) and subject)
        require(identifier not in seen);seen.add(identifier)
        path=root/'users'/identifier
        require(not path.is_symlink())
        document=None;receipts=[];items={}
        if (path/'state.sqlite').exists():
            with closing(read_db(path/'state.sqlite')) as conn:
                row=conn.execute('SELECT body FROM snapshot WHERE id=1').fetchone()
                document=json.loads(row[0]) if row else None
                receipts=[(*r[:4],json.loads(r[4]) if r[4] is not None else None,r[5]) for r in conn.execute('SELECT epoch,route,request_id,digest,response,created_at FROM receipts')]
        if document:
            validate_profile('state',document['state'])
            meta=document['meta'];epoch=document['state']['data_epoch'];vault=PrivateVault(path)
            def remember(item_id,active,generation,item_epoch=epoch):
                require(isinstance(item_id,str) and 1<=len(item_id)<=256 and type(generation) is int and generation>=0)
                require(item_id not in owners or owners[item_id]==identifier)
                owners[item_id]=identifier
                if item_id not in items or active:items[item_id]=(item_id,item_epoch,generation,bool(active))
            if meta.get('banking_vault'):
                private=vault.open(meta['banking_vault']);require(private.get('version')==2)
                for item in private['items'].values():remember(item['item_id'],item['connected'],item['generation'])
            else:
                private=vault.open(meta.get('plaid_vault'));item_id=private.get('item_id') or meta.get('plaid_item_id')
                if item_id:
                    require(not meta.get('plaid_item_id') or meta['plaid_item_id']==item_id)
                    remember(item_id,meta.get('plaid_connected',False),meta.get('plaid_generation',1))
                require(not meta.get('plaid_connected') or item_id)
            pending=vault.pending();item=pending.get('item') or pending.get('connection')
            if item and item.get('item_id'):remember(item['item_id'],False,0,pending.get('epoch',epoch))
            for r in receipts:
                require(isinstance(r[0],str) and isinstance(r[3],str));datetime.fromisoformat(r[5])
                if isinstance(r[4],dict):
                    for field in ('plaid_receipt','banking_receipt'):
                        if field in r[4]:require(bool(vault.open(r[4][field])))
        else:require(not receipts)
        records.append({'id':identifier,'issuer':issuer,'subject':subject,'path':path,'document':document,'receipts':receipts,'items':list(items.values())})
    if (root/'users').exists():
        require(all(p.is_dir() and not p.is_symlink() and p.name in seen for p in (root/'users').iterdir()))
    return records


def counts(records):
    return {'users':len(records),'snapshots':sum(bool(r['document']) for r in records),
        'observations':sum(len(r['document']['state']['observations']) for r in records if r['document']),
        'receipts':sum(len(r['receipts']) for r in records),'items':sum(len(r['items']) for r in records),
        'experience_answers':sum(len(r['document']['meta'].get('experience_answers',[])) for r in records if r['document'])}


def connect():
    config=PostgresConfig.from_env();values=conninfo_to_dict(config.dsn)
    return psycopg.connect(config.dsn,autocommit=True,sslmode='verify-full',sslrootcert=values.get('sslrootcert','system'),
        connect_timeout=8,options='-c statement_timeout=15000 -c lock_timeout=3000')


def target_status(conn):
    ready=conn.execute("SELECT to_regclass('companion_private.schema_version') IS NOT NULL").fetchone()[0]
    if ready:
        require(conn.execute('SELECT version FROM companion_private.schema_version').fetchall()==[(1,)])
        total=sum(conn.execute('SELECT count(*) FROM companion_private.'+table).fetchone()[0] for table in TABLES)
    else:total=0
    return ready,total==0


def verify(conn,records,residual):
    for record in records:
        identifier=record['id'];doc=record['document']
        conn.execute("SELECT set_config('companion.account_id',%s,true)",(identifier,))
        identity=conn.execute('SELECT issuer,subject FROM companion_private.accounts WHERE account_id=%s',(identifier,)).fetchone()
        require(identity==(record['issuer'],record['subject']))
        row=conn.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(identifier,)).fetchone()
        require((row[0] if row else None)==doc)
        stored=conn.execute('SELECT epoch,route,request_id,digest,response,created_at FROM companion_private.receipts WHERE account_id=%s',(identifier,)).fetchall()
        canonical=lambda rows:sorted((r[0],r[1],r[2],r[3],json.dumps(r[4],sort_keys=True),datetime.fromisoformat(r[5]) if isinstance(r[5],str) else r[5]) for r in rows)
        require(canonical(stored)==canonical(record['receipts']))
        items=conn.execute('SELECT item_id,epoch,generation,active FROM companion_private.plaid_owners WHERE account_id=%s',(identifier,)).fetchall()
        require(sorted(items)==sorted(record['items']))
        destination=residual/'users'/identifier
        for path in record['path'].rglob('*') if record['path'].exists() else []:
            if not path.is_file() or path.name.endswith(('-wal','-shm','-journal','.lock')) or path.name=='state.sqlite':continue
            copied=destination/path.relative_to(record['path'])
            regular(copied);require(copied.stat().st_mode & 0o077==0)
            if path.suffix=='.sqlite':
                with closing(read_db(path)) as original,closing(read_db(copied)) as backup:require(list(original.iterdump())==list(backup.iterdump()))
            else:require(path.read_bytes()==copied.read_bytes())


def run(args,progress):
    os.umask(0o077)
    source,residual,backup=args.source_root,args.residual_root,args.backup_root
    require(all(p.is_absolute() and not p.is_symlink() for p in (source,residual,backup)))
    require(not any(a==b or a in b.parents or b in a.parents for a,b in ((source,residual),(source,backup),(backup,residual))))
    if args.secrets_root:load_runtime_environment(args.secrets_root)
    progress['phase']='read_source';records=source_records(source);summary=counts(records)
    with closing(connect()) as conn:
        # A session pooler may terminate TLS before its upstream PG connection;
        # verify this client's actual libpq TLS session, not pg_stat_ssl upstream.
        require(conn.pgconn.ssl_in_use);progress['verified_tls']=True
        if args.mode=='preflight':
            conn.execute('SET default_transaction_read_only=on');ready,empty=target_status(conn);require(empty)
            progress['phase']='private_backup'
            if not backup.exists():private_mkdir(backup)
            require(backup.is_dir() and backup.stat().st_mode & 0o077==0)
            files=copy_private(source,backup/'preflight')
            return {'status':'preflight_ready','verified_tls':True,'schema_ready':ready,'target_empty':empty,'private_backup_ready':True,'backup_files':files,**summary}
        if args.mode=='verify':
            conn.execute('SET default_transaction_read_only=on')
            with conn.transaction():verify(conn,records,residual)
            return {'status':'verified_exact','verified_tls':True,'identity_snapshot_receipt_owner_match':True,'private_residual_match':True,**summary}
        require(args.confirm_source_stopped and args.confirm_empty_target)
        require((backup/'preflight').is_dir() and not residual.exists())
        progress['phase']='acquire_source_locks'
        with ExitStack() as locks:
            for path in [source/'.registry.lock',*[r['path']/'state.sqlite.lock' for r in records if (r['path']/'state.sqlite').exists()]]:
                regular(path);f=locks.enter_context(path.open('rb'));fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            records=source_records(source);summary=counts(records)
            progress['phase']='quiesced_backup';copy_private(source,backup/'quiesced')
            progress['phase']='private_residual_copy';private_mkdir(residual);private_mkdir(residual/'users')
            for r in records:
                if r['path'].exists():copy_private(r['path'],residual/'users'/r['id'],residual=True)
                else:private_mkdir(residual/'users'/r['id'])
            progress['phase']='transactional_transfer'
            with conn.transaction():
                conn.execute('SELECT pg_advisory_xact_lock(686459532118)')
                conn.execute('LOCK TABLE '+','.join('companion_private.'+t for t in TABLES)+' IN ACCESS EXCLUSIVE MODE')
                ready,empty=target_status(conn);require(ready and empty)
                for r in records:
                    identifier=r['id'];doc=r['document']
                    conn.execute('INSERT INTO companion_private.accounts(account_id,issuer,subject,generation) VALUES(%s,%s,%s,%s)',(identifier,r['issuer'],r['subject'],0))
                    conn.execute("SELECT set_config('companion.account_id',%s,true)",(identifier,))
                    if doc:conn.execute('INSERT INTO companion_private.snapshots(account_id,body) VALUES(%s,%s)',(identifier,Jsonb(doc)))
                    for epoch,route,request_id,digest,response,created_at in r['receipts']:
                        conn.execute('INSERT INTO companion_private.receipts(account_id,epoch,route,request_id,digest,response,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)',
                            (identifier,epoch,route,request_id,digest,Jsonb(response) if response is not None else None,created_at))
                    for item_id,epoch,generation,active in r['items']:
                        conn.execute("INSERT INTO companion_private.plaid_owners(environment,item_id,account_id,epoch,generation,active) VALUES('sandbox',%s,%s,%s,%s,%s)",(item_id,identifier,epoch,generation,active))
                verify(conn,records,residual)
            progress['remote_transfer_committed']=True
            marker=residual/'.postgres-root';marker.write_bytes(b'companion-postgres-residual-v1\n');marker.chmod(0o600)
            return {'status':'migrated','verified_tls':True,'transaction_committed':True,'exact_match':True,'source_preserved':True,**summary}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['preflight','apply','verify'])
    for flag in ('source-root','residual-root','backup-root'):p.add_argument('--'+flag,type=Path,required=True)
    p.add_argument('--secrets-root',type=Path)
    p.add_argument('--confirm-source-stopped',action='store_true');p.add_argument('--confirm-empty-target',action='store_true')
    args=p.parse_args();progress={'phase':'configuration','remote_transfer_committed':False}
    try:result=run(args,progress)
    except Exception:
        print(json.dumps({'status':'refused_or_failed','source_preserved':True,**progress}));raise SystemExit(1) from None
    print(json.dumps(result))


if __name__=='__main__':main()
