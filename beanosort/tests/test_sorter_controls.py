import json
from urllib.error import URLError

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from beanosort.models import Audit, Ledger, User
from beanosort.quantities import DomainError
from beanosort.sorter_controls import Connection, MachineHTTP, RemoteUnavailable, connection_for, validate_address
from fake_sorter import FakeMachine, machine_server
from test_web import client_for, token


def controls(x):
    client = client_for(x)
    machine = FakeMachine(); client.app.state.sorter_transport = machine
    page = client.get('/sorter-controls')
    return client, machine, {'X-CSRF-Token': token(page), 'X-Sorter-Connection': token(page, 'connection_id'),
        'X-Sorter-Client': 'browser-control-1234'}


def post(client, headers, action, **body):
    return client.post('/sorter-controls/api/'+action, headers=headers, json=body)


def test_opening_controls_does_not_acquire_or_change_outputs(system):
    client, machine, headers = controls(system)
    assert 'Sorter Controls' in client.get('/').text
    assert 'physical machine' in client.get('/sorter-controls').text
    panel = client.get('/sorter-controls/panel')
    assert panel.status_code == 200 and 'no-store' in panel.headers['cache-control']
    assert 'Test valve channels' in panel.text and 'Load Candidate 3' in panel.text
    assert machine.calls == []
    state = client.get('/sorter-controls/api/state', headers=headers)
    assert state.status_code == 200 and not state.json()['lease']['active']
    assert machine.calls == [('state', None)]
    assert post(client, headers, 'conveyor/enabled', enabled=True).status_code == 409
    assert not machine.state['conveyor']['enabled']
    with system.db.read() as s: assert s.scalar(select(func.count()).select_from(Ledger)) == 0


def test_control_lease_is_bound_to_login_and_cannot_be_stolen(system):
    client, machine, headers = controls(system)
    acquired = post(client, headers, 'control/acquire', operator_name='Spoofed operator')
    assert acquired.status_code == 200
    assert acquired.json()['lease']['client_id'] == headers['X-Sorter-Client']
    assert acquired.json()['lease']['operator_name'] == 'admin'
    machine_id = machine.state['lease']['client_id']
    assert machine_id.startswith('beanosort-') and machine_id != headers['X-Sorter-Client']
    other, _, other_headers = controls(system)
    other.app.state.sorter_transport = machine
    assert post(other, other_headers, 'control/acquire', client_id=machine_id).status_code == 409
    assert post(other, other_headers, 'conveyor/enabled', client_id=machine_id, enabled=True).status_code == 409
    assert not machine.state['conveyor']['enabled']
    assert post(client, headers, 'conveyor/config', speed_sps=20, acceleration_sps2=10, direction='reverse').status_code == 200
    assert post(client, headers, 'conveyor/enabled', enabled=True).status_code == 200
    assert post(client, headers, 'control/release').status_code == 200
    assert not machine.state['conveyor']['enabled'] and not machine.state['lease']['active']


def test_heartbeats_are_not_accounting_writes_and_errors_are_not_retried(system):
    client, machine, headers = controls(system)
    post(client, headers, 'control/acquire')
    with system.db.read() as s: before = s.scalar(select(func.count()).select_from(Audit))
    assert post(client, headers, 'control/heartbeat').status_code == 200
    with system.db.read() as s: assert s.scalar(select(func.count()).select_from(Audit)) == before
    response = post(client, headers, 'vibration/config', frequency_hz=4000, duty_percent=101, direction='clockwise')
    assert response.status_code == 422 and 'Duty' in response.json()['detail']
    assert len([c for c in machine.calls if c[0] == 'vibration/config']) == 1
    machine.offline = True
    assert post(client, headers, 'triggers/pulse', count=1).status_code == 503
    assert len([c for c in machine.calls if c[0] == 'triggers/pulse']) == 1
    with system.db.read() as s:
        record = s.scalar(select(Audit).where(Audit.action == 'sorter_control_command').order_by(Audit.created_at.desc()))
        assert record.data['command'] == 'triggers/pulse' and not record.data['acknowledged']
        assert s.scalar(select(func.count()).select_from(Ledger)) == 0


def test_stops_need_authentication_and_csrf_but_no_control_lease(system):
    client, machine, headers = controls(system)
    unauthenticated = TestClient(client.app)
    assert unauthenticated.post('/sorter-controls/api/stop', follow_redirects=False).status_code == 303
    assert client.post('/sorter-controls/api/stop', headers={**headers, 'X-CSRF-Token': 'wrong'}, json={}).status_code == 403
    assert client.get('/sorter-controls/api/stop', headers=headers).status_code == 405
    assert post(client, headers, 'arbitrary/path').status_code == 404
    assert post(client, {**headers, 'X-Sorter-Connection': 'stale'}, 'stop').status_code == 409
    assert machine.calls == []
    assert post(client, headers, 'stop').status_code == 200
    assert post(client, headers, 'valves/stop').status_code == 200


def test_connection_is_saved_and_old_pages_cannot_retarget_commands(system):
    client, machine, headers = controls(system)
    result = client.post('/sorter-controls/connection', data={'csrf': headers['X-CSRF-Token'],
        'connection_id': headers['X-Sorter-Connection'], 'host': '192.168.0.42', 'port': '8001'})
    assert result.status_code == 200 and '192.168.0.42' in result.text
    assert machine.calls == [('state', None)]
    assert post(client, headers, 'stop').status_code == 409
    fresh = client.get('/sorter-controls')
    new_id = token(fresh, 'connection_id')
    assert new_id != 'default'
    headers['X-Sorter-Connection'] = new_id
    assert post(client, headers, 'control/acquire').status_code == 200
    refused = client.post('/sorter-controls/connection', data={'csrf': headers['X-CSRF-Token'],
        'connection_id': new_id, 'host': '192.168.0.43', 'port': '8000'})
    assert refused.status_code == 409 and 'Release' in refused.text
    assert '192.168.0.42' in client.get('/sorter-controls').text
    with system.db.read() as s: assert s.scalar(select(func.count()).select_from(Ledger)) == 0


def test_operator_can_control_but_cannot_change_connection(system):
    client, machine, headers = controls(system)
    with system.db.write() as s: s.get(User, system.uid).role = 'operator'
    assert post(client, headers, 'control/acquire').status_code == 200
    result = client.post('/sorter-controls/connection', data={'csrf': headers['X-CSRF-Token'],
        'connection_id': headers['X-Sorter-Connection'], 'host': '192.168.0.42', 'port': '8001'})
    assert result.status_code == 409


@pytest.mark.parametrize('host,port', [('http://127.0.0.1',8000),('user@host',8000),('example/a',8000),
    ('bad..host',8000),('0.0.0.0',8000),('224.0.0.1',8000),('localhost',0),('localhost',65536),('localhost','8.5')])
def test_invalid_machine_addresses_are_rejected(host,port):
    with pytest.raises(DomainError): validate_address(host,port)


def test_http_transport_against_local_fixture_and_failed_connection(monkeypatch):
    machine = FakeMachine()
    with machine_server(machine) as port:
        assert MachineHTTP().request(Connection('fixture','127.0.0.1',port),'state')[1]['connection']['connected']
        assert MachineHTTP().request(Connection('fixture','127.0.0.1',port),'conveyor/enabled',{'client_id':'nobody','enabled':True})[0] == 409
    class Broken:
        def open(self,*args,**kwargs): raise URLError('offline')
    monkeypatch.setattr('beanosort.sorter_controls.build_opener', lambda *args: Broken())
    with pytest.raises(RemoteUnavailable): MachineHTTP().request(Connection('fixture','127.0.0.1',port),'state')
