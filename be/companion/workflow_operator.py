"""Private explicit-target enqueue only. No public cross-account API or direct model call."""
import argparse,asyncio,json
from datetime import datetime,timezone
from uuid import UUID
from .postgres import PostgresRuntime,PostgresConfig
from .workflow_contracts import WorkflowConfig
from .workflows import WorkflowQueue

async def run(args):
    account=str(UUID(args.account_id));request=str(UUID(args.request_id))
    now=datetime.now(timezone.utc)
    if args.confirm_enqueue_utc_date!=now.date().isoformat():raise ValueError('Confirm current UTC date')
    runtime=PostgresRuntime(PostgresConfig.from_env())
    try:
        await runtime.check()
        row=await runtime.identity(account_id=account)
        if row is None or (row[1],row[2])!=(args.issuer,args.subject):raise ValueError('Immutable account mapping mismatch')
        async with runtime.pool.connection() as c:
            role=await (await c.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')).fetchone()
            if not role or any(role):raise ValueError('Restricted private operator role required')
        queue=WorkflowQueue(runtime,WorkflowConfig(enabled=True))
        await queue.check()
        async with queue.scoped(account) as c:
            snapshot=await (await c.execute('SELECT body FROM companion_private.snapshots WHERE account_id=%s',(account,))).fetchone()
        if not snapshot:raise ValueError('No account state')
        epoch=snapshot[0]['state']['data_epoch']
        await queue.enqueue(account,request,epoch,'operator')
        print(json.dumps({'enqueued':True,'status':'queued_or_already_reserved','origin':'operator'}))
    finally:await runtime.close()

def main():
    p=argparse.ArgumentParser(description='Enqueue one consented account workflow using private operator DB authority')
    for flag in ('account-id','issuer','subject','request-id','confirm-enqueue-utc-date'):p.add_argument('--'+flag,required=True)
    args=p.parse_args()
    try:asyncio.run(run(args))
    except Exception:
        print('{"enqueued":false,"details":"Verify private authority, immutable account mapping, consent, schema and confirmation."}')
        raise SystemExit(1)
if __name__=='__main__':main()
