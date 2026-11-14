"""Crash-boundary simulations through the public persistence extension interface."""
from fastapi.testclient import TestClient
import httpx
import pytest

from companion.bootstrap import UnavailableSynthesis
from companion.connections.plaid import PlaidSandboxClient, SandboxCredentials
from companion.extensions.persistence import PersistenceExtension
from companion.extensions.plaid import PlaidExtension
from companion.http import create_app
from test_backend import NOW
from test_plaid import Provider, PUBLIC, ACCESS, call, connect, get_state, page, transaction


class FailingFinalCommit:
    id, version, status = 'persistence','1','available'
    def __init__(self,path):
        self.inner=PersistenceExtension(path)
        self.armed=False
    async def handle(self,operation,payload):
        if operation=='commit' and self.armed and payload['document']['meta'].get('plaid_inflight') is None:
            self.armed=False
            raise RuntimeError('Synthetic failure before final SQLite transaction')
        return await self.inner.handle(operation,payload)


def app_with(tmp_path,provider,storage=None):
    replacements={'synthesis':UnavailableSynthesis(), 'plaid':PlaidExtension(tmp_path,PlaidSandboxClient(
        SandboxCredentials('synthetic','synthetic'),httpx.MockTransport(provider.handle)))}
    if storage:
        replacements['persistence']=storage
    return create_app(tmp_path,plaid_profile=True,replacements=replacements,clock=lambda:NOW)


def test_final_sync_commit_failure_then_restart_reuses_original_cursor(tmp_path):
    provider=Provider();storage=FailingFinalCommit(tmp_path/'state.sqlite')
    with TestClient(app_with(tmp_path,provider,storage),base_url='http://127.0.0.1') as c:
        connect(c);call(c,'sync')
        provider.pages=[page([transaction('second')],cursor='cursor-two')]
        storage.armed=True
        r=call(c,'sync',rid='failed-final')
        assert r.status_code==500
        assert len(get_state(c)['observations'])==1
    provider.pages=[page([transaction('second')],cursor='cursor-two')]
    with TestClient(app_with(tmp_path,provider),base_url='http://127.0.0.1') as c:
        r=call(c,'sync',rid='failed-final')
        assert r.status_code==200 and r.json()['result']['added']==1
        assert len(get_state(c)['observations'])==2
        calls=[body for path,body in provider.requests if path=='/transactions/sync']
        assert calls[-1]['cursor']==calls[-2]['cursor']=='cursor-one'


def test_exchanged_token_durable_staging_avoids_one_use_exchange_after_restart(tmp_path):
    provider=Provider();storage=FailingFinalCommit(tmp_path/'state.sqlite')
    with TestClient(app_with(tmp_path,provider,storage),base_url='http://127.0.0.1') as c:
        call(c,'link_token',consent_sandbox=True)
        storage.armed=True
        assert call(c,'exchange',rid='exchange',public_token=PUBLIC).status_code==500
        assert (tmp_path/'plaid-pending.vault').exists()
        assert ACCESS.encode() not in (tmp_path/'plaid-pending.vault').read_bytes()
        assert not get_state(c)['observations']
    with TestClient(app_with(tmp_path,provider),base_url='http://127.0.0.1') as c:
        r=call(c,'exchange',rid='exchange',public_token=PUBLIC)
        assert r.status_code==200 and r.json()['result']['status']=='connected'
        assert sum(p=='/item/public_token/exchange' for p,_ in provider.requests)==1
        assert not (tmp_path/'plaid-pending.vault').exists()
        assert call(c,'sync').json()['result']['added']==1
