"""Portion captions follow pinned material history, including unsorted remainders."""
from html import escape

from sqlalchemy import select

from beanosort.models import Assignment, Portion, Version
from beanosort.portion_labels import describe_portion
from test_handling import execute, outputs, prepare, release
from test_web import client_for


def sorted_stock(x):
    raw, source, *_ = x.receive('20')
    with x.db.read() as s:
        data = dict(s.get(Version, x.versions['recipe']).data)
        receipt = s.get(Portion, raw).receipt_id
    name = 'Dark outliers: L* < median minus 2 sigma'
    recipe = x.workflow.create_version(x.uid, 'recipe', name, data)
    unused, settings = x.prepare(raw, '18')
    x.workflow.delete_prepared_sorting(x.uid, 'unused-caption-job', unused, confirm_unstarted=True)
    op = x.workflow.prepare(x.uid, 'caption-job', {**settings, 'recipe_id': recipe})
    x.start(op, 1); x.finish(op)
    x.reconcile(op, selected='10', pass_through='8', remainder='2')
    release(x, op)
    return raw, source, receipt, op, name, data, outputs(x, op)


def test_pinned_recipe_stream_and_historical_identity_captions(system):
    x = system; raw, source, receipt, op, name, data, children = sorted_stock(x)
    x.workflow.create_version(x.uid, 'recipe', name, {**data, 'note': 'New recipe version'})
    with x.db.read() as s:
        assert describe_portion(s, children['selected']) == name+' (v1) · Selected'
        assert describe_portion(s, children['pass_through']) == name+' (v1) · Pass-through'
        assert describe_portion(s, children['remainder']) == name+' (v1) · Unprocessed remainder'
        assert describe_portion(s, raw) == 'Received material · No sorting recipe'
    client = client_for(x)
    with x.db.read() as s:
        destination = s.scalar(select(Assignment.container_id).filter_by(portion_id=children['selected'], active=True))
        from beanosort.models import Container
        location = s.get(Container, destination).location_id
    for url in ('/', '/containers', '/containers/'+destination, '/portions/'+children['selected'],
                '/labels/portion/'+children['selected'], '/locations/'+location):
        page = client.get(url)
        assert page.status_code == 200, page.text
        assert 'data-portion-origin="'+children['selected']+'"' in page.text
        assert escape(name)+' (v1) · Selected' in page.text
    history = client.get('/containers/'+source)
    assert 'Received material · No sorting recipe' in history.text
    assert client.get('/receipts/'+receipt).status_code == 200
    client.close()


def test_split_and_transfer_retain_recipe_without_assigning_one_recipe_to_blend(system):
    x = system; _, _, _, _, name, _, children = sorted_stock(x)
    transfer, handler, _ = prepare(x, 'transfer', [children['selected']])
    execute(x, handler, transfer)
    with x.db.read() as s:
        assert describe_portion(s, children['selected']) == name+' (v1) · Selected'
    split, handler, _ = prepare(x, 'split', [children['selected']], withdrawn_mass='3')
    execute(x, handler, split, output_mass='3', remainder='7'); release(x, split)
    pieces = outputs(x, split)
    with x.db.read() as s:
        assert describe_portion(s, pieces['withdrawn']) == name+' (v1) · Selected · Split portion'
        assert describe_portion(s, pieces['remainder']) == name+' (v1) · Selected · Split remainder'
    second, *_ = x.receive('1')
    blend, handler, _ = prepare(x, 'blend', [pieces['withdrawn'], second])
    execute(x, handler, blend, output_mass='4', actual_inputs={pieces['withdrawn']:'3', second:'1'})
    blended = outputs(x, blend)['blend']
    with x.db.read() as s:
        assert describe_portion(s, blended) == 'Blend · See contributing population histories'
