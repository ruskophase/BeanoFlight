from copy import deepcopy
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from beanosort.handling import latest_profile
from beanosort.material import MaterialActions
from beanosort.models import (Audit, Container, HandlingPlan, Input, Ledger, Measurement, Operation,
                             Output, Pack, Portion, ProductLot, Profile, Station)
from beanosort.operation_labels import describe_operation
from beanosort.quantities import DomainError, kg
from beanosort.service import active_assignment, balance
from test_material_actions import adjust_data, record, state
from test_material_web import values
from test_retained_populations import combined
from test_web import client_for


@pytest.mark.parametrize('environment', ['simulation', 'real'])
@pytest.mark.parametrize('basis', ['gross', 'net', 'removed'])
@pytest.mark.parametrize('remaining', ['0', '1.25'])
def test_packaging_removes_only_product_and_retains_or_closes_source(factory, environment, basis, remaining):
    x = factory(environment)
    pid, cid, *_ = x.receive('2')
    after = int(Decimal(remaining) * 1000000)
    reading = kg(2000000 - after if basis == 'removed' else after + (500000 if basis == 'gross' else 0))
    data = adjust_data(x, cid, weight=reading, weighing_basis=basis, reason='')
    action = MaterialActions(x.db)
    op = action.package_product(x.uid, 'final-product-packaging', data)
    assert action.package_product(x.uid, 'final-product-packaging', data) == op
    result = record(x, op)
    assert result['packaged_mg'] == 2000000 - after
    assert result['before_mg'] == result['after_mg'] + result['packaged_mg']
    assert result['disposition'] == 'final_product'
    assert result['mass_basis'] == ('measured removed net mass' if basis == 'removed' else 'recorded contents less measured remaining net mass')
    if basis == 'removed':
        assert result['outputs'][0]['mass_basis'] == 'calculated remainder; not separately weighed'
    with x.db.read() as s:
        operation = s.get(Operation, op)
        assert (operation.kind, operation.status, operation.release_status) == ('product_packaging', 'completed', 'released')
        assert s.get(Station, operation.station_id).name == 'Packaging desk'
        assert s.get(Station, operation.station_id).kind == 'packaging'
        assert operation.execution_method == ('simulated' if environment == 'simulation' else 'manual')
        assert balance(s, pid) == after
        assert s.scalar(select(func.sum(Ledger.delta_mg))) == after
        postings = list(s.scalars(select(Ledger).filter_by(operation_id=op)))
        assert len(postings) == 1 and postings[0].delta_mg == -(2000000 - after)
        assert s.scalar(select(Input.mass_mg).filter_by(operation_id=op)) == 2000000
        assert s.scalar(select(func.count()).select_from(Pack)) == 0
        assert s.scalar(select(func.count()).select_from(ProductLot)) == 0
        assert s.scalar(select(func.count()).select_from(Portion)) == 1
        measurement = s.scalar(select(Measurement).filter_by(operation_id=op, role='packaged_net' if basis == 'removed' else 'remaining_net'))
        assert measurement.mass_mg == (2000000 - after if basis == 'removed' else after)
        assert measurement.provenance['kind'] == ('simulation' if environment == 'simulation' else 'direct_measurement')
        if basis == 'gross':
            assert measurement.provenance['tare_mg'] == 500000
            assert s.scalar(select(Measurement.mass_mg).filter_by(operation_id=op, role='remaining_gross')) == after + 500000
        else:
            assert s.scalar(select(func.count()).select_from(Measurement).filter_by(operation_id=op)) == 1
        if basis == 'removed':
            assert measurement.provenance['tare_basis'] == 'removed contents net after tare'
            assert s.scalar(select(func.count()).select_from(Measurement).where(
                Measurement.operation_id == op, Measurement.role.in_(['remaining_net', 'remaining_gross']))) == 0
        assignment = active_assignment(s, container_id=cid)
        if after:
            assert assignment.portion_id == pid and s.get(Portion, pid).status == 'stock'
            assert s.scalar(select(Output.mass_mg).filter_by(operation_id=op)) == after
        else:
            assert assignment is None and s.get(Portion, pid).status == 'consumed'
            assert s.scalar(select(func.count()).select_from(Output).filter_by(operation_id=op)) == 0
        assert s.scalar(select(func.count()).select_from(Audit).filter_by(operation_id=op, action='product_packaging')) == 1
        assert describe_operation(s, op)['final_masses_mg']['Final product'] == 2000000 - after


@pytest.mark.parametrize('change', [
    {'weight': '2'}, {'weight': '3'}, {'weight': '-1'}, {'weight': 'NaN'}, {'weight': ''},
    {'weight': '0.0000001'}, {'weighing_basis': 'invalid'}, {'confirmed': False},
    {'weighing_basis': 'gross', 'weight': '0.4'},
    {'weighing_basis': 'removed', 'weight': '0'},
    {'weighing_basis': 'removed', 'weight': '2.000001'},
])
def test_invalid_packaging_does_not_change_stock(system, change):
    x = system
    _, cid, *_ = x.receive('2')
    before = state(x, cid)
    with pytest.raises(DomainError):
        MaterialActions(x.db).package_product(x.uid, 'invalid-product-packaging', adjust_data(x, cid, **change))
    assert state(x, cid) == before
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(HandlingPlan)) == 0


def test_packaging_rejects_held_locked_and_stale_contents(system):
    x = system
    pid, cid, *_ = x.receive('2')
    action = MaterialActions(x.db)
    x.workflow.set_hold(x.uid, pid, 'Review before sale')
    with pytest.raises(DomainError, match='quality hold'):
        action.package_product(x.uid, 'held-product-packaging', adjust_data(x, cid))
    # A new released source can still be reserved or have its form become stale.
    pid, cid, *_ = x.receive('2')
    op, _ = x.prepare(pid, '1')
    with pytest.raises(DomainError, match='active jobs'):
        action.package_product(x.uid, 'reserved-product-packaging', adjust_data(x, cid))
    x.workflow.delete_prepared_sorting(x.uid, 'release-packaging-source', op, confirm_unstarted=True)
    old = adjust_data(x, cid)
    action.package_product(x.uid, 'first-product-removal', old)
    with pytest.raises(DomainError, match='changed'):
        action.package_product(x.uid, 'second-product-removal', old)
    with pytest.raises(DomainError, match='different content'):
        action.package_product(x.uid, 'first-product-removal', {**old, 'weight': '0'})
    assert state(x, cid)['mass_mg'] == 1250000


@pytest.mark.parametrize('representative', [True, False])
@pytest.mark.parametrize('basis', ['net', 'removed'])
def test_partial_packaging_preserves_population_evidence_only_under_accepted_assumption(system, tmp_path, representative, basis):
    x = system
    pid, cid, *_ = combined(x, tmp_path)
    before = state(x, cid)
    with x.db.read() as s:
        original = deepcopy(latest_profile(s, pid).data)
    after = before['mass_mg'] // 2
    MaterialActions(x.db).package_product(x.uid, 'package-known-population', adjust_data(
        x, cid, weight=kg(before['mass_mg'] - after if basis == 'removed' else after),
        weighing_basis=basis, representative=representative))
    assert state(x, cid)['portion_id'] == pid
    with x.db.read() as s:
        data = latest_profile(s, pid).data
        assert s.get(Profile, before['profile_id']).data == original
        if representative:
            assert data['retained_population']['net_mass_mg'] == after
            assert data['retained_population']['pools'][0]['weights'] == original['retained_population']['pools'][0]['weights']
            expected = Decimal(original['retained_population']['estimated_count']) * after / before['mass_mg']
            assert abs(Decimal(data['retained_population']['estimated_count']) - expected) < Decimal('1e-20')
            assert data['population_analysis_url'] == '/portions/' + pid + '/populations'
        else:
            assert data['material_population_unknown'] and 'retained_population' not in data


def test_web_packages_product_with_signed_source_and_without_packet_tracking(system):
    x = system
    pid, cid, *_ = x.receive('2', label='Beans for sale')
    client = client_for(x)
    page = client.get('/handling/package?container_id=' + cid)
    assert page.headers['cache-control'] == 'no-store'
    assert '2.000 kg contents' in page.text and 'nominal capacity 1000.000 kg' in page.text
    data = {**values(page), 'container_id': cid, 'weight': '1.75', 'weighing_basis': 'gross'}
    assert client.post('/handling/package', data={**data, 'csrf': 'wrong'}).status_code == 403
    assert client.post('/handling/package', data={**data, 'state_' + cid: 'invalid'}).status_code == 409
    response = client.post('/handling/package', data=data)
    assert response.status_code == 200, response.text
    assert '<h1>Final product recorded</h1>' in response.text
    assert '0.75 kg packaged as Final product' in response.text
    assert client.post('/handling/package', data=data).url == response.url
    assert state(x, cid)['portion_id'] == pid and state(x, cid)['mass_mg'] == 1250000
    history = client.get('/handling')
    assert 'Final product removed' in history.text and '0.75 kg' in history.text
    assert client.get('/operations/' + response.url.path.rsplit('/', 1)[-1]).url == response.url
    page = client.get('/handling/package')
    response = client.post('/handling/package', data={**values(page), 'container_id': cid, 'weight': '0', 'weighing_basis': 'net'})
    assert response.status_code == 200 and 'Container recorded empty and available for reuse' in response.text
    assert state(x, cid)['portion_id'] is None


def test_web_packages_directly_weighed_contents_and_labels_remainder_as_calculated(system):
    x = system
    pid, cid, *_ = x.receive('2', label='Large container')
    client = client_for(x)
    page = client.get('/handling/package?container_id=' + cid)
    assert 'Scale reading reflects<select' in page.text
    assert '<option value="removed">Mass of contents removed</option>' in page.text
    reading_input = page.text.split('<label>Scale reading<input', 1)[1].split('>', 1)[0]
    assert 'placeholder=' not in reading_input
    data = {**values(page), 'container_id': cid, 'weight': '0.75', 'weighing_basis': 'removed'}
    response = client.post('/handling/package', data=data)
    assert response.status_code == 200, response.text
    assert '0.75 kg packaged as Final product' in response.text
    assert 'Calculated contents remaining' in response.text and 'Measured contents remaining' not in response.text
    assert 'source container was not reweighed' in response.text
    assert client.post('/handling/package', data=data).url == response.url
    assert state(x, cid)['portion_id'] == pid and state(x, cid)['mass_mg'] == 1250000
    page = client.get('/handling/package')
    response = client.post('/handling/package', data={**values(page), 'container_id': cid, 'weight': '1.25', 'weighing_basis': 'removed'})
    assert response.status_code == 200 and 'Container recorded empty and available for reuse' in response.text
    assert state(x, cid)['portion_id'] is None
