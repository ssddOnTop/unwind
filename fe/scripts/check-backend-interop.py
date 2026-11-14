#!/usr/bin/env python3
"""ASGI interop with the sibling backend, using Swift-encoded synthetic templates.
Not an iOS runtime or URLSession end-to-end test. No server port or production DB is used.
"""
import copy, json, sys, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'be'))
from fastapi.testclient import TestClient
from companion.http import create_app
from jsonschema import Draft202012Validator, FormatChecker
schema=json.loads((root/'contracts/state.schema.json').read_text())
cases=json.loads((root/'fe/evidence/encoded-contracts.json').read_text())
validator=Draft202012Validator(schema,format_checker=FormatChecker())
checks=[]; snapshots=[]
def check(name, condition):
    assert condition,name
    checks.append(name)
def template(definition, index=0):
    return copy.deepcopy([x['payload'] for x in cases if x['definition']==definition][index])
def state_response(r):
    check('HTTP 200 '+str(r.request.url.path),r.status_code==200)
    state=r.json(); validator.validate(state); snapshots.append(state); return state
with tempfile.TemporaryDirectory(dir=root/'fe/evidence',prefix='interop-') as directory:
    with TestClient(create_app(Path(directory)),base_url='http://127.0.0.1') as client:
        state=state_response(client.get('/state'))
        seed=template('demo_request'); seed['data_epoch']=state['data_epoch']
        state=state_response(client.post('/demo',json=seed))
        check('explicit synthetic seed yields six records',len(state['observations'])==6)
        replay=state_response(client.post('/demo',json=seed))
        check('identical demo retry returns same state',replay==state)
        batch=template('observation_batch'); batch['data_epoch']=state['data_epoch']
        response=client.post('/observations',json=batch)
        check('Swift observation batch accepted as six duplicate revisions',response.status_code==200 and response.json()['ignored']==6)
        for index in [0,1,2,3]:
            f=template('feedback_request',index); f['data_epoch']=state['data_epoch']
            state=state_response(client.post('/feedback',json=f))
        check('scenario stop persists in preferences','purchase_recurrence' in state['preferences']['suppressed_scenario_ids'])
        check('feedback retains immutable original attribution',next(x for x in state['observations'] if x['id']=='demo-purchase-1')['household_attribution']=='self')
        p=template('preferences_request'); p['data_epoch']=state['data_epoch']; p['preferences']=copy.deepcopy(state['preferences']); p['preferences']['no_spending']=True
        state=state_response(client.put('/preferences',json=p)); check('full preferences persisted',state['preferences']['no_spending'])
        process=template('process_request'); process['data_epoch']=state['data_epoch']
        state=state_response(client.post('/process',json=process)); check('deterministic processing without AI',state['process']['synthesis']['mode']=='disabled')
        old_epoch=state['data_epoch']; deletion=template('delete_request'); deletion['data_epoch']=old_epoch
        state=state_response(client.request('DELETE','/data',json=deletion))
        check('delete rotates epoch and erases evidence',state['data_epoch']!=old_epoch and not state['observations'] and not state['feedback'] and not state['notification_intents'])
        repeated=state_response(client.request('DELETE','/data',json=deletion)); check('delete exact retry is safe',repeated==state)
        stale=template('feedback_request'); stale['data_epoch']=old_epoch; stale['request_id']='never-retagged-old-request'
        r=client.post('/feedback',json=stale); check('old epoch rejected rather than relabeled',r.status_code==409 and r.json()['error']['code']=='epoch_mismatch')
report={'kind':'Swift-encoded templates against real backend ASGI; synthetic only, not iOS runtime','passed':len(checks),'checks':checks}
(root/'fe/evidence/backend-interop.json').write_text(json.dumps(report,indent=2)+'\n')
(root/'fe/evidence/backend-state-responses.json').write_text(json.dumps(snapshots,indent=2)+'\n')
print(json.dumps(report,indent=2))
