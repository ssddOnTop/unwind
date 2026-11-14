"""Owned Sandbox sync delta -> normalized private transactions, in the snapshot transaction."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from uuid import uuid5,NAMESPACE_URL
from .plaid_projection import category


def normalize_delta(staged,connection_id,bank_account_id,timezone,now):
    records=[];cutoff=(date.fromisoformat(now[:10])-timedelta(days=190))
    for r in staged['added']+staged['modified']:
        identifier=r.get('transaction_id')
        if not isinstance(identifier,str) or not 1<=len(identifier)<=160:continue
        try:
            event=date.fromisoformat(r.get('authorized_date') or r['date'])
            amount=Decimal(str(r['amount']))
            currency=r.get('iso_currency_code')
            if not amount.is_finite() or abs(amount)>=Decimal('1000000000000'):raise ValueError()
            if not isinstance(currency,str) or len(currency)!=3 or not currency.isascii() or not currency.isupper():raise ValueError()
            if event<cutoff or event>date.fromisoformat(now[:10]):raise ValueError()
        except (KeyError,ValueError,TypeError,InvalidOperation):
            # A malformed/now-out-of-window modification must not leave an older version actionable.
            records.append({'transaction_id':identifier,'deleted':True})
            continue
        classified=category(r.get('personal_finance_category'))
        records.append({'transaction_id':identifier,'event_at':datetime.combine(event,time.min,ZoneInfo(timezone)).isoformat(),'timezone':timezone,
            'amount':str(amount),'currency':currency,'category':{'restaurant':'food_service','grocery':'groceries'}.get(classified,'other'),
            'deleted':bool(r.get('pending'))})
        if r.get('pending_transaction_id'):
            records.append({'transaction_id':r['pending_transaction_id'],'deleted':True})
    for r in staged['removed']:
        if isinstance(r.get('transaction_id'),str):records.append({'transaction_id':r['transaction_id'],'deleted':True})
    return {'connection_id':connection_id,'bank_account_id':bank_account_id,'records':records}


async def persist_bank_patterns(conn,account,doc,changes):
    # Optional additive deployment: base/v1 runtimes never run DDL or synthesize a fallback.
    ready=await (await conn.execute("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='companion_private' AND table_name='pattern_batches' AND column_name='origin')")).fetchone()
    if not ready[0]:return
    contexts=doc.get('meta',{}).get('pattern_bank_accounts',[])
    enabled={(c['connection_id'],c['bank_account_id']):c['active'] for c in contexts}
    for delta in changes:
        pair=(delta['connection_id'],delta['bank_account_id'])
        if pair not in enabled:raise ValueError('Unowned normalized bank delta')
        batch=str(uuid5(NAMESPACE_URL,account+':plaid:'+':'.join(pair)))
        dataset='plaid-'+batch
        at=doc['state']['generated_at']
        await conn.execute("INSERT INTO companion_private.pattern_batches(account_id,batch_id,dataset,provenance,start_at,end_at,origin,connection_id,bank_account_id,active) VALUES(%s,%s,%s,'providerSandbox',%s,%s,'plaid',%s,%s,%s) ON CONFLICT(account_id,batch_id) DO UPDATE SET end_at=excluded.end_at,active=excluded.active",(account,batch,dataset,at,at,*pair,enabled[pair]))
        for r in delta['records']:
            if 'event_at' not in r:
                await conn.execute('UPDATE companion_private.pattern_transactions SET deleted=true WHERE account_id=%s AND batch_id=%s AND transaction_id=%s',(account,batch,r['transaction_id']))
            else:
                await conn.execute('INSERT INTO companion_private.pattern_transactions(account_id,batch_id,transaction_id,event_at,timezone,amount,currency,category,deleted) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,batch_id,transaction_id) DO UPDATE SET event_at=excluded.event_at,timezone=excluded.timezone,amount=excluded.amount,currency=excluded.currency,category=excluded.category,deleted=excluded.deleted,ingested_at=now()',
                    (account,batch,r['transaction_id'],r['event_at'],r['timezone'],r['amount'],r['currency'],r['category'],r['deleted']))
    rows=await (await conn.execute("SELECT batch_id,connection_id,bank_account_id FROM companion_private.pattern_batches WHERE account_id=%s AND origin='plaid'",(account,))).fetchall()
    for batch,cid,aid in rows:
        await conn.execute('UPDATE companion_private.pattern_batches SET active=%s WHERE account_id=%s AND batch_id=%s',(enabled.get((cid,aid),False),account,batch))
    health_enabled=any(s['id']=='healthkit' and s['enabled'] and s['consent']=='granted' for s in doc['state'].get('sources',[]))
    await conn.execute("UPDATE companion_private.pattern_batches SET active=%s WHERE account_id=%s AND origin='healthkit'",(health_enabled,account))
