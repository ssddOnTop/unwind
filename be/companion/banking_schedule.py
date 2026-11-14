"""Durable per-owned-Item transaction-fetch admission, stored with the account snapshot."""
from datetime import timedelta
from .domain import instant,iso

MINIMUM=timedelta(hours=24)


def entry(doc,item_id):
    value=doc.get('meta',{}).get('banking_fetch_schedule',{}).get(item_id,{})
    return value if value.get('epoch')==doc['state']['data_epoch'] else {}


def next_due(doc,item,now):
    value=entry(doc,item['item_id'])
    if value.get('next_automatic_at'):return instant(value['next_automatic_at'])
    # On upgrade, respect an already committed legacy Item sync timestamp. It is
    # a conservative attempt floor, not invented proof of a successful import.
    if item.get('last_sync_at'):return instant(item['last_sync_at'])+MINIMUM
    return now


def reserve(doc,item_id,now):
    old=entry(doc,item_id)
    doc['meta'].setdefault('banking_fetch_schedule',{})[item_id]={
        'epoch':doc['state']['data_epoch'],'last_attempt_at':iso(now),
        'last_success_at':old.get('last_success_at'),'failures':old.get('failures',0),
        # Written BEFORE network I/O; crashes cannot create another automatic fetch.
        'next_automatic_at':iso(now+MINIMUM)}


def finish(doc,item_id,now,success):
    value=entry(doc,item_id)
    if not value:return
    failures=0 if success else min(3,value.get('failures',0)+1)
    value.update(failures=failures,next_automatic_at=iso(now+timedelta(hours=24*max(1,failures))))
    if success:value['last_success_at']=iso(now)


def last_full_success(doc):
    active={i['item_id'] for i in doc.get('meta',{}).get('banking_item_contexts',[])}
    if not active:return None
    stamps=[entry(doc,i).get('last_success_at') for i in active]
    return min(stamps) if all(stamps) else None
