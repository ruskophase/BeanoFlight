"""Operation captions retain their original material/container boundary."""
from html import escape

from sqlalchemy import select

from beanosort.models import Assignment, Container
from beanosort.operation_labels import describe_operation
from test_handling import execute, prepare
from test_occupied_destinations import empty, prepare_into
from test_portion_labels import sorted_stock
from test_web import client_for


def test_caption_keeps_original_recipe_and_vessels_after_transfers(system):
    x = system; _, source, _, op, name, data, children = sorted_stock(x)
    with x.db.read() as s:
        original = describe_operation(s, op)
        assert original['recipe'] == name+' (v1)'
        assert dict(original['roles'])['Source'] == s.get(Container, source).label
        assert original['final_masses_mg'] == {'Source':2000000, 'Selected':10000000, 'Pass-through':8000000}
    # Reusing these vessels/portions must not change a past job's source/output.
    for portion in (children['remainder'], children['selected']):
        transfer, handler, _ = prepare(x, 'transfer', [portion])
        execute(x, handler, transfer)
    x.workflow.create_version(x.uid, 'recipe', name, {**data, 'note':'Later version'})
    with x.db.read() as s:
        assert describe_operation(s, op) == original
        selected_container = s.scalar(select(Assignment.container_id).where(Assignment.operation_id == op,
            Assignment.portion_id == children['selected']))
        assert dict(original['roles'])['Selected'] == s.get(Container, selected_container).label
    client = client_for(x)
    for path in ('/', '/jobs/'+op, '/portions/'+children['selected']):
        page = client.get(path)
        assert page.status_code == 200
        assert 'data-operation-caption="'+op+'"' in page.text
        assert escape(name)+' (v1)' in page.text
        for role, label in original['roles']:
            assert '<b>'+role+':</b> '+escape(label) in page.text
        assert '(2 kg remaining)' in page.text and '(10 kg)' in page.text and '(8 kg)' in page.text
    with x.db.read() as s:
        assert describe_operation(s, transfer)['recipe'] == 'No sorting recipe'
    client.close()


def test_occupied_destination_contents_are_not_described_as_sorter_source(system):
    x = system; source, source_bin, *_ = x.receive('2')
    _, selected, *_ = x.receive('5'); passed = empty(x)
    op, _ = prepare_into(x, source, selected, passed)
    with x.db.read() as s:
        expected = describe_operation(s, op)
        assert dict(expected['roles']) == {'Source':s.get(Container, source_bin).label,
            'Selected':s.get(Container, selected).label, 'Pass-through':s.get(Container, passed).label}
        assert expected['final_masses_mg'] == {}
    x.start(op, 1);x.finish(op);x.reconcile(op, selected='6', pass_through='1')
    with x.db.read() as s:
        assert describe_operation(s, op) == expected
    x.workflow.release(x.uid, op, accept_estimates=True, reason='Reviewed complete combined totals')
    with x.db.read() as s:
        assert describe_operation(s, op)['final_masses_mg'] == {'Source':0, 'Selected':6000000, 'Pass-through':1000000}
