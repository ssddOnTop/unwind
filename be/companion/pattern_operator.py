"""Offline explicit operator loader. Never a public seed endpoint; no implicit env file reads."""
import argparse
import asyncio
import calendar
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL
import psycopg
from .postgres import PostgresConfig, lock_key
from .patterns import HealthSample, Transaction

DATASET='six-month-pattern-demo-v1'
SOURCE='Synthetic operator wearable — not Apple device provenance'


def six_month_start(now):
    month=now.year*12+now.month-1-6
    year,m=divmod(month,12)
    return now.replace(year=year,month=m+1,day=min(now.day,calendar.monthrange(year,m+1)[1]),hour=0,minute=0,second=0,microsecond=0)


def generate(now):
    """UTC synthetic normalized representations, not an Apple export JSON format."""
    now=now.astimezone(timezone.utc)
    start=six_month_start(now); samples=[];tx=[]; day=start
    def sample(hk_type,kind,a,b,suffix,**fields):
        samples.append(HealthSample(sample_id=f'{a.date()}:{suffix}',hk_type=hk_type,kind=kind,start_at=a,end_at=b,timezone='UTC',source_name=SOURCE,**fields).model_dump())
    while day<=now:
        n=(day-start).days;recent=(now-day).days<28;weekend=day.weekday()>=5
        end=min(day+timedelta(days=1),now)
        if n%17!=0 and end>day:
            sample('stepCount','dailyCumulative',day,end,'steps',value=float((3400 if recent else 9000)+(n%5)*160-(400 if weekend else 0)),unit='count')
            if day+timedelta(hours=8)<=now:
                hours=6.0 if recent else 8.0
                a=day+timedelta(hours=7-hours);b=day+timedelta(hours=7)
                sample('sleepAnalysis','category',a-timedelta(minutes=20),b+timedelta(minutes=15),'inbed',category='inBed')
                for index,(category,fraction) in enumerate([('asleepCore',.5),('asleepDeep',.25),('asleepREM',.25)]):
                    e=a+timedelta(hours=hours*fraction)
                    sample('sleepAnalysis','category',a,e,f'sleep{index}',category=category);a=e
                for hk in ('heartRate','restingHeartRate'):
                    point=day+timedelta(hours=8)
                    sample(hk,'quantity',point,point,hk,value=float(68+(n%4)+(4 if recent else 0)),unit='bpm')
        if day.weekday() in ([5] if recent else [1,3,5]) and day+timedelta(hours=19)<=now:
            a=day+timedelta(hours=18);b=a+timedelta(minutes=30)
            sample('workout','workout',a,b,'workout',workout_activity='walking',duration_seconds=1800.0,energy_kcal=120.0,distance_m=2200.0)
        if day+timedelta(hours=20)<=now:
            category='food_service' if day.weekday() in ([0,1,2,4,5] if recent else [5]) else 'groceries' if day.weekday()==3 else 'other'
            tx.append(Transaction(transaction_id=f'operator-{day.date()}',event_at=day+timedelta(hours=20),timezone='UTC',amount=18.5 if category=='food_service' else 35.0,currency='USD',category=category).model_dump())
        day+=timedelta(days=1)
    return start,samples,tx


async def load(conn, *, account_id, issuer, subject, now, remove=False, transactions=None):
    """All selectors must agree with an EXISTING immutable registry row; no account creation."""
    async with conn.transaction():
        row=await (await conn.execute('SELECT account_id FROM companion_private.accounts WHERE account_id=%s AND issuer=%s AND subject=%s',(str(UUID(account_id)),issuer,subject))).fetchone()
        if not row: raise ValueError('Account mapping not verified')
        account=str(row[0])
        locked=await (await conn.execute('SELECT pg_try_advisory_xact_lock(%s)',(lock_key(account),))).fetchone()
        if not locked[0]: raise ValueError('Stop the selected account runtime before operator load')
        await conn.execute("SELECT set_config('companion.account_id',%s,true)",(account,))
        batch=str(uuid5(NAMESPACE_URL,account+':'+DATASET))
        # Erase analyses before changing evidence; never show stale/corrected records as current.
        await conn.execute('DELETE FROM companion_private.pattern_runs WHERE account_id=%s',(account,)) if remove else None
        if remove:
            await conn.execute("DELETE FROM companion_private.pattern_batches WHERE account_id=%s AND dataset IN (%s,'provider-sandbox-import-v1')",(account,DATASET))
            return {'removed':True}
        existing=await (await conn.execute('SELECT 1 FROM companion_private.pattern_batches WHERE account_id=%s AND batch_id=%s',(account,batch))).fetchone()
        if existing:return {'loaded':True,'reused':True}
        start,samples,tx=generate(now)
        await conn.execute('INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at) VALUES(%s,%s,%s,\'synthetic\',%s,%s)',(account,batch,DATASET,start,now))
        columns=['sample_id','hk_type','kind','start_at','end_at','timezone','value','unit','category','workout_activity','duration_seconds','energy_kcal','distance_m','source_name','revision']
        async with conn.cursor() as cursor:
            await cursor.executemany('INSERT INTO companion_private.health_samples(account_id,batch_id,'+','.join(columns)+') VALUES('+','.join(['%s']*(len(columns)+2))+')',[(account,batch,*(s[k] for k in columns)) for s in samples])
        if transactions is not None:
            # A genuine provider-returned normalized file is a separate provenance batch, never relabeled synthetic Plaid.
            tx=[Transaction.model_validate_json(json.dumps(t)).model_dump() for t in transactions]
            txbatch=str(uuid5(NAMESPACE_URL,account+':provider-sandbox-import-v1'))
            await conn.execute("INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at) VALUES(%s,%s,'provider-sandbox-import-v1','providerSandbox',%s,%s)",(account,txbatch,start,now))
        else:txbatch=batch
        if any(t['event_at']<start or t['event_at']>now for t in tx):raise ValueError('Transaction outside selected dataset interval')
        columns=['transaction_id','event_at','timezone','amount','currency','category']
        async with conn.cursor() as cursor:
            await cursor.executemany('INSERT INTO companion_private.pattern_transactions(account_id,batch_id,'+','.join(columns)+') VALUES('+','.join(['%s']*8)+')',[(account,txbatch,*(t[k] for k in columns)) for t in tx])
        return {'loaded':True,'reused':False,'health_samples':len(samples),'transactions':len(tx)}


def main():
    p=argparse.ArgumentParser(description='Offline private pattern dataset operator; identifiers are never printed')
    p.add_argument('--account-id',required=True);p.add_argument('--issuer',required=True);p.add_argument('--subject',required=True)
    p.add_argument('--confirm-dataset',required=True,choices=[DATASET]);p.add_argument('--remove',action='store_true')
    p.add_argument('--provider-sandbox-transactions',type=Path,help='Only genuine Plaid-returned normalized records; provenance is asserted by the operator')
    a=p.parse_args()
    async def run():
        config=PostgresConfig(os.environ.get('DATABASE_URL',''))
        async with await psycopg.AsyncConnection.connect(config.dsn,autocommit=True) as conn:
            return await load(conn,account_id=a.account_id,issuer=a.issuer,subject=a.subject,now=datetime.now(timezone.utc),remove=a.remove,
                transactions=json.loads(a.provider_sandbox_transactions.read_text()) if a.provider_sandbox_transactions else None)
    try: print(json.dumps(asyncio.run(run())))
    except Exception: print('{"loaded":false,"error":"Operator load rejected; verify migration, account mapping and stopped runtime."}');raise SystemExit(1)

if __name__=='__main__': main()
