from decimal import Decimal

import pytest
from sqlalchemy import select

from beanosort.material import MaterialActions
from beanosort.models import Particle, Portion
from beanosort.population_sources import input_profile, source_snapshot
from beanosort.quantities import DomainError, kg
from beanosort.references import validate_recipe
from beanosort.models import Version
from beanosort.service import active_assignment, balance
from beanosort.simulation import current_settings
from test_material_actions import adjust_data, empty, move_data, state
from test_resorting import first_pass, generate, prepare


def test_split_and_combined_contents_keep_joint_photos_for_further_sorting(system,tmp_path):
    x=system;_,recipe,_,_,oldrun,outputs=first_pass(x,tmp_path)
    with x.db.read() as s:
        selected=active_assignment(s,portion_id=outputs['selected']).container_id
        passed=active_assignment(s,portion_id=outputs['pass_through']).container_id
        amount=balance(s,outputs['selected'])//2
        original=[(p.id,p.digest) for p in s.scalars(select(Particle).filter_by(run_id=oldrun))]
        allowed={stream:{p.payload['reference_bean']['bean_sequence'] for p in s.scalars(select(Particle).filter_by(run_id=oldrun,simulated_destination=stream))}
                 for stream in ('selected','pass_through')}
    MaterialActions(x.db).move_contents(x.uid,'move-selected-into-pass',move_data(x,selected,passed,moved_mass=kg(amount)))
    children=[state(x,cid)['portion_id'] for cid in (selected,passed)]
    for index,child in enumerate(children):
        with x.db.read() as s:
            portion=s.get(Portion,child)
            dataset=validate_recipe(s,s.get(Version,recipe).data)
            pool=source_snapshot(s,portion,dataset,current_settings(s),balance(s,child),force=True)
            assert pool['weights'] and pool['membership_evidence']=='inherited_estimate'
            assert {seq for seq,_ in pool['weights']}==(allowed['selected'] if index==0 else allowed['selected']|allowed['pass_through'])
        op,_=prepare(x,child,recipe,kg(state(x,(selected,passed)[index])['mass_mg']))
        newer=generate(x,op,count=50)
        with x.db.read() as s:
            assert input_profile(s,op).data['source_portion_id']==child
            assert all(p.payload['reference_bean']['dataset_id']==dataset.id for p in s.scalars(select(Particle).filter_by(run_id=newer)))
    with x.db.read() as s:
        assert original==[(p.id,p.digest) for p in s.scalars(select(Particle).filter_by(run_id=oldrun))]


def test_nonrepresentative_adjustment_blocks_false_population_replay_even_for_receipts(system,tmp_path):
    from test_references import reference
    x=system;_,recipe,_=reference(x,tmp_path);pid,cid,*_=x.receive('2')
    MaterialActions(x.db).reconcile(x.uid,'selective-sample',adjust_data(x,cid,weight='1.9',representative=False))
    with pytest.raises(DomainError,match='composition is unknown'):
        prepare(x,pid,recipe,'1.9')
