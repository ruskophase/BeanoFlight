from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.models import Lineage, Operation, Output, Particle, Portion, Receipt, Run, SimulationAssumption
from beanosort.population_sources import input_profile
from beanosort.quantities import DomainError, kg
from beanosort.references import create_recipe
from beanosort.service import available, balance, trace
from beanosort.weighing import build_review, weighing_context
from test_references import reference
from test_weighing import change_settings


def prepare(x, portion, recipe, amount):
    bins=[x.workflow.create_container(x.uid,label='Re-sort '+uuid4().hex,location='Test floor',capacity='100',tare_mass='0.5') for _ in range(2)]
    data=dict(portion_id=portion,recipe_id=recipe,planned_mass=amount,station_id=x.stations['Training sorter'],
              selected_id=bins[0],pass_through_id=bins[1],ready=True,policy_id=x.versions['reconciliation_policy'])
    return x.workflow.prepare(x.uid,'re-sort-'+uuid4().hex,data),data


def generate(x, op, count=1000, seed='another-pass'):
    run=x.workflow.start(x.uid,op,count=count,seed=seed)
    while x.workflow.simulate_next(x.uid,run):pass
    return run


def reconcile(x, op):
    x.finish(op)
    with x.db.read() as s:
        context=weighing_context(s,s.get(Operation,op),'simulation');form={}
        for row in context['rows']:
            form.update({'gross_'+row['stream']:row['gross_value'],'weighed_'+row['stream']:'on','revision_'+row['stream']:str(row['container'].revision)})
        review=build_review(s,s.get(Operation,op),'simulation',form)
    x.workflow.reconcile(x.uid,'weights-'+op,op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Explicit simulation review',approve_discrepancy=True)
    with x.db.read() as s:return {o.stream:o.portion_id for o in s.scalars(select(Output).filter_by(operation_id=op))}


def first_pass(x,tmp_path,percent='100'):
    change_settings(x,percent)
    did,rid,_=reference(x,tmp_path);raw,*_=x.receive('2')
    op,_=prepare(x,raw,rid,'2');run=generate(x,op)
    return did,rid,raw,op,run,reconcile(x,op)


@pytest.mark.parametrize('source_stream',['selected','pass_through'])
def test_second_pass_samples_only_the_chosen_output_and_preserves_first_run(system,tmp_path,source_stream):
    x=system;did,rid,raw,first,run,outputs=first_pass(x,tmp_path)
    source=outputs[source_stream]
    area_recipe=create_recipe(x.db,x.uid,'resort-area-recipe','Area second pass',did,'area_mm2','above','0.5')
    with x.db.read() as s:
        mass=balance(s,source);before=[(p.id,p.digest,p.simulated_destination) for p in s.scalars(select(Particle).filter_by(run_id=run))]
        allowed={p.payload['reference_bean']['bean_sequence'] for p in s.scalars(select(Particle).filter_by(run_id=run,simulated_destination=source_stream))}
    second,data=prepare(x,source,area_recipe,kg(mass))
    with x.db.read() as s:
        pinned=input_profile(s,second);assert pinned and pinned.data['source_portion_id']==source
        assert {seq for seq,_ in pinned.data['weights']}==allowed
        assert available(s,s.get(Portion,source))==0
        mean=Decimal(pinned.data['mass_model']['input_mean_bean_mass_mg'])
        basis=s.scalar(select(SimulationAssumption).filter_by(operation_id=second)).data
        assert Decimal(basis['estimated_particles'])==Decimal(mass)/mean
    newer=generate(x,second,count=500)
    assert x.workflow.simulate_next(x.uid,newer,replay_first=True)==0
    with x.db.read() as s:
        particles=list(s.scalars(select(Particle).filter_by(run_id=newer)))
        assert len(particles)==500 and {p.payload['reference_bean']['bean_sequence'] for p in particles}<=allowed
        if source_stream=='selected':assert {p.payload['features']['lab_l'] for p in particles}=={10} and all(p.payload['decision']=='pass' for p in particles)
        assert before==[(p.id,p.digest,p.simulated_destination) for p in s.scalars(select(Particle).filter_by(run_id=run))]
    children=reconcile(x,second)
    with x.db.read() as s:
        assert balance(s,source)==0 and s.get(Portion,source).status=='consumed'
        assert all(s.scalar(select(Lineage).filter_by(parent_id=source,child_id=child,operation_id=second)) for child in children.values())
        assert s.scalar(select(func.count()).select_from(Receipt))==1
        descendant,_=trace(s,next(iter(children.values())))
        assert raw in {item.id for item in descendant}


def test_recapture_missed_targets_new_capture_trials_and_third_pass(system,tmp_path):
    x=system;_,rid,_,first,oldrun,outputs=first_pass(x,tmp_path,percent='0')
    assert 'selected' not in outputs
    change_settings(x,'100')
    with x.db.read() as s: mass=balance(s,outputs['pass_through'])
    second,_=prepare(x,outputs['pass_through'],rid,kg(mass));run=generate(x,second)
    with x.db.read() as s:
        particles=list(s.scalars(select(Particle).filter_by(run_id=run)))
        assert any(p.payload['decision']=='selected' for p in particles)
        assert all(p.simulated_destination=='selected' for p in particles if p.payload['decision']=='selected')
    outputs2=reconcile(x,second)
    change_settings(x,'50','0.3')
    with x.db.read() as s: third_mass=balance(s,outputs2['selected'])
    third,_=prepare(x,outputs2['selected'],rid,kg(third_mass))
    with x.db.read() as s:
        pinned=input_profile(s,third)
        assert str(pinned.data['mass_model']['reference_mean_mass_ug'])=='150000'
        basis=s.scalar(select(SimulationAssumption).filter_by(operation_id=third)).data
        assert Decimal(basis['mean_mass_g'])<Decimal('0.15')
    run3=generate(x,third,count=300)
    with x.db.read() as s:
        assert {p.payload['features']['lab_l'] for p in s.scalars(select(Particle).filter_by(run_id=run3))}=={10}
        assert s.get(Run,run3).source_versions['reference_population']['input_population_profile_id']==pinned.id
    reconcile(x,third)


def test_partial_second_pass_and_its_remainder_keep_source_composition(system,tmp_path):
    x=system;_,rid,_,_,_,out=first_pass(x,tmp_path)
    with x.db.read() as s:amount=balance(s,out['selected'])
    second,_=prepare(x,out['selected'],rid,kg(amount//2));generate(x,second,count=300)
    children=reconcile(x,second);assert 'remainder' in children
    with x.db.read() as s: remainder=balance(s,children['remainder'])
    third,_=prepare(x,children['remainder'],rid,kg(remainder));run=generate(x,third,count=250)
    with x.db.read() as s:
        assert {p.payload['features']['lab_l'] for p in s.scalars(select(Particle).filter_by(run_id=run))}=={10}
        assert input_profile(s,third).data['membership_evidence']=='inherited_estimate'


def test_representative_split_can_be_resorted_and_unrepresented_split_cannot(system,tmp_path):
    from test_handling import prepare as handling_prepare, execute, release, outputs
    x=system;_,rid,_,_,_,out=first_pass(x,tmp_path)
    with x.db.read() as s:amount=balance(s,out['selected'])
    op,handler,_=handling_prepare(x,'split',[out['selected']],withdrawn_mass=kg(amount//2))
    execute(x,handler,op,output_mass=kg(amount//2),remainder=kg(amount-amount//2));release(x,op)
    split=outputs(x,op);second,_=prepare(x,split['withdrawn'],rid,kg(amount//2));run=generate(x,second,count=250)
    with x.db.read() as s:assert {p.payload['features']['lab_l'] for p in s.scalars(select(Particle).filter_by(run_id=run))}=={10}
    op,handler,_=handling_prepare(x,'split',[out['pass_through']],withdrawn_mass='0.1')
    with x.db.read() as s:mass=balance(s,out['pass_through'])
    execute(x,handler,op,output_mass='0.1',remainder=kg(mass-100000),representative=False);release(x,op)
    with pytest.raises(DomainError,match='representative'):
        prepare(x,outputs(x,op)['withdrawn'],rid,'0.1')


def test_incompatible_dataset_or_held_source_is_not_silently_reset(system,tmp_path):
    from reference_fixture import bundle
    from beanosort.references import import_bundle
    x=system;_,rid,_,_,_,out=first_pass(x,tmp_path)
    # A second archive has a distinct manifest fingerprint, even with equal values.
    import json
    root=bundle(tmp_path/'other');manifest=json.loads((root/'manifest.json').read_text());manifest['provenance']['purpose']='different archive';(root/'manifest.json').write_text(json.dumps(manifest))
    did=import_bundle(x.db,x.uid,root,tmp_path/'other-snapshot',name='Other archive');other=create_recipe(x.db,x.uid,'other-recipe','Other reference',did)
    with x.db.read() as s:amount=kg(balance(s,out['selected']))
    with pytest.raises(DomainError,match='same reference'):
        prepare(x,out['selected'],other,amount)
    x.workflow.set_hold(x.uid,out['selected'],'Test quality hold')
    with pytest.raises(DomainError,match='released'):
        prepare(x,out['selected'],rid,amount)
