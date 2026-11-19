"""Container swaps preserve attribution, stock, retained readings and replay."""
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func,select,text
from sqlalchemy.exc import DBAPIError

from beanosort.capture import fixture_particle
from beanosort.models import (Assignment,Container,Destination,Ledger,Operation,Output,Particle,Portion,Profile,
                             SortingCollection,SortingSegment)
from beanosort.quantities import DomainError
from beanosort.segments import change_containers,collections,current,particle_filter,record_weight
from beanosort.service import available,balance
from beanosort.weighing import build_review,weighing_context
from test_occupied_destinations import empty
from test_web import client_for,token
from test_references import reference


def ready(x):
    source,source_bin,*_=x.receive('20')
    op,data=x.prepare(source)
    run=x.start(op,1000)
    x.workflow.simulate_next(x.uid,run)
    return source,source_bin,op,data,run


def swap(x,op,data,**changes):
    return change_containers(x.workflow,x.uid,'swap-'+uuid4().hex,op,
        {**data,'feeding_stopped':True,'in_flight_clear':True,'ready':True,'reason':'Full output bin',**changes})


def reviewed(x,op,weights):
    with x.db.read() as s:
        context=weighing_context(s,s.get(Operation,op),x.db.environment);form={}
        for row in context['rows']:
            code=row['stream'];gross=weights.get(code,row['gross_value'])
            form.update({'gross_'+code:gross,'revision_'+code:str(row['container'].revision),'weighed_'+code:'on'})
        return build_review(s,s.get(Operation,op),x.db.environment,form),form


def test_selected_swap_keeps_pass_collection_and_reconciles_four_vessels(system):
    x=system;source,source_bin,op,data,run=ready(x);replacement=empty(x)
    with x.db.read() as s:before=[(p.id,p.digest,p.simulated_destination) for p in s.scalars(select(Particle).filter_by(run_id=run))]
    x.workflow.transition(x.uid,op,'paused',reason='Selected full')
    swap(x,op,data,selected_id=replacement)
    with x.db.read() as s:
        items=collections(s,s.get(Operation,op));assert len(items)==3
        assert len([item for item in items if item.stream=='pass_through'])==1
        first=next(item for item in items if item.code=='selected')
        assert current(s,s.get(Operation,op)).first_sequence==250
    record_weight(x.workflow,x.uid,'early-closed-weight',first.id,{'gross':'3.5','revision':'1','weighed':True})
    location=x.workflow.create_location(x.uid,'segment-new-location','Closed output shelf')
    x.workflow.move_container(x.uid,'move-closed-collection',first.container_id,location_id=location,revision='1')
    client=client_for(x)
    located=client.get('/locations/'+location)
    assert located.status_code==200 and 'href="/collections/'+first.id+'"' in located.text
    assert '3 kg recorded' in located.text and 'wip · awaiting reconciliation' in located.text
    assert '<td data-label="Available">0 kg</td>' in located.text
    client.close()
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    assert x.workflow.simulate_next(x.uid,run,replay_first=True)==0
    x.finish(op)
    review,form=reviewed(x,op,{'selected_2':'13.5','pass_through':'4.5','remainder':'0.5'})
    assert form['gross_selected']=='3.500'
    assert len(review['rows'])==4 and review['discrepancy_mg']==0
    rid=x.workflow.reconcile(x.uid,'segmented-weights',op,review['data'])
    assert x.workflow.reconcile(x.uid,'segmented-weights',op,review['data'])==rid
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed all four containers')
    with x.db.read() as s:
        assert {o.stream:o.mass_mg for o in s.scalars(select(Output).filter_by(operation_id=op))}=={'selected':3000000,'selected_2':13000000,'pass_through':4000000}
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==20000000
        profiles={p.stream:p.data for p in s.scalars(select(Profile).filter_by(operation_id=op))}
        assert Decimal(profiles['selected']['expected']['selected'])+Decimal(profiles['selected_2']['expected']['selected'])==Decimal(profiles['run']['expected']['selected'])
        assert profiles['pass_through']['counts']['tracked']==1000
        assert sorted((p.id,p.digest,p.simulated_destination) for p in s.scalars(select(Particle).where(Particle.id.in_([p[0] for p in before]))))==sorted(before)
        assert all(available(s,s.get(Portion,o.portion_id))==o.mass_mg for o in s.scalars(select(Output).filter_by(operation_id=op)))


def test_guards_prevent_unpaused_unconfirmed_reused_and_competing_destinations(system):
    x=system;_,_,op,data,run=ready(x);replacement=empty(x)
    with pytest.raises(DomainError):swap(x,op,data,selected_id=replacement)
    x.workflow.transition(x.uid,op,'paused',reason='Change bins')
    with pytest.raises(DomainError):swap(x,op,data,selected_id=replacement,in_flight_clear=False)
    swap(x,op,data,selected_id=replacement)
    with pytest.raises(DomainError):swap(x,op,data,selected_id=data['selected_id'])
    with pytest.raises(DomainError):x.workflow.simulate_next(x.uid,run)
    with pytest.raises(DomainError):
        x.workflow.ingest(x.uid,run,'late-synthetic-pre-swap',[fixture_particle(250)],check_generation_segment=True,generation_segment=None)
    source,*_=x.receive('1')
    other,otherdata=x.prepare(source,'1');x.workflow.delete_prepared_sorting(x.uid,'release-other-guard',other,confirm_unstarted=True)
    with pytest.raises(DomainError):x.workflow.prepare(x.uid,'conflicting-full-bin',{**otherdata,'selected_id':data['selected_id']})
    with x.db.read() as s:
        assert len(collections(s,s.get(Operation,op)))==3
        assert s.scalar(select(func.count()).select_from(Particle).where(Particle.run_id==run))==250
    with pytest.raises(DBAPIError):
        with x.db.write() as s:s.execute(text("UPDATE sorting_segments SET first_sequence=1"))


def test_web_pause_swap_and_dynamic_review(system):
    x=system;_,_,op,data,run=ready(x);replacement=empty(x)
    client=client_for(x);page=client.get('/jobs/'+op)
    page=client.post('/jobs/'+op+'/pause-for-change',data={'csrf':token(page)})
    assert page.status_code==200 and 'Change output containers' in page.text
    page=client.post('/jobs/'+op+'/change-containers',data={'csrf':token(page),'key':token(page,'key'),
        'selected_id':replacement,'pass_through_id':data['pass_through_id'],'feeding_stopped':'on','in_flight_clear':'on','ready':'on','reason':'Selected full'})
    assert page.status_code==200 and 'Container collection history' in page.text
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op)
    page=client.get('/jobs/'+op+'/weigh');assert page.status_code==200 and 'gross_selected_2' in page.text
    review,form=reviewed(x,op,{'selected':'3.5','selected_2':'13.5','pass_through':'4.5','remainder':'0.5'})
    page=client.post('/jobs/'+op+'/weigh/review',data={**form,'csrf':token(page),'key':token(page,'key')})
    assert page.status_code==200,page.text
    page=client.post('/jobs/'+op+'/weigh/accept',data={'csrf':token(page),'review_token':token(page,'review_token'),'accept_review':'on','reason':'Reviewed four containers'})
    assert page.status_code==200 and 'Completed' in page.text,page.text
    with x.db.read() as s:
        label_id=next(c.id for c in collections(s,s.get(Operation,op)) if c.code=='selected')
    label=client.get('/labels/collection/'+label_id)
    assert label.status_code==200 and '/collections/'+label_id in label.text
    assert client.get('/qr/collection/'+label_id).headers['content-type']=='image/svg+xml'
    scan=client.get('/scan',params={'code':'http://example.test/collections/'+label_id})
    assert scan.status_code==200 and '/jobs/'+op in str(scan.url) and label_id in scan.text
    client.close()


def ref_ready(x,tmp_path):
    _,recipe,_=reference(x,tmp_path)
    source,*_=x.receive('3');unused,data=x.prepare(source,'1')
    x.workflow.delete_prepared_sorting(x.uid,'cancel-ref-segment-fixture',unused,confirm_unstarted=True)
    data.pop('capture_model_id');data['recipe_id']=recipe
    op=x.workflow.prepare(x.uid,'reference-segment-job',data)
    run=x.workflow.start(x.uid,op,count=1000,seed='reference-segments')
    x.workflow.simulate_next(x.uid,run)
    return source,op,data,run


def test_reference_bin_profiles_photos_and_further_sort_are_scoped_to_windows(system,tmp_path):
    x=system;source,op,data,run=ref_ready(x,tmp_path)
    new_selected,new_pass=empty(x),empty(x)
    x.workflow.transition(x.uid,op,'paused',reason='Both bins full')
    swap(x,op,data,selected_id=new_selected,pass_through_id=new_pass)
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op)
    review,_=reviewed(x,op,{'selected':'0.55','pass_through':'0.7','selected_2':'0.65','pass_through_2':'1.1','remainder':'2.5'})
    assert len(review['rows'])==5 and review['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'reference-window-weights',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Reviewed five weighed vessels')
    with x.db.read() as s:
        child=s.scalar(select(Output.portion_id).filter_by(operation_id=op,stream='selected_2'))
        profile=s.scalar(select(Profile).filter_by(portion_id=child)).data
        collection=s.get(SortingCollection,profile['collection_id'])
        expected_members=set(s.scalars(select(Particle.payload['reference_bean']['bean_sequence'].as_integer())
            .where(Particle.run_id==run,Particle.simulated_destination=='selected',particle_filter(s,collection))))
        assert profile['counts']['tracked']==750 and expected_members
        url=profile['population_analysis_url']
    client=client_for(x);page=client.get(url)
    assert page.status_code==200 and 'Charts cover the particle windows' in page.text
    import json,re
    report=json.loads(re.search(r'<script id="population-data"[^>]*>(.*?)</script>',page.text,re.S).group(1))
    assert report['populations']['input']['count']=='750'
    histogram=report['populations']['expected_selected']['histograms']['lab_l']
    bucket=next(i for i,count in enumerate(histogram) if Decimal(count)>0)
    inspected=client.get('/jobs/'+op+'/populations/inspect',params={'collection_id':collection.id,'feature':'lab_l','bucket':bucket,'population':'selected'})
    assert inspected.status_code==200
    assert Decimal(inspected.json()['matching_count'])==Decimal(histogram[bucket])
    assert inspected.json()['physical_confirmation'] is False
    client.close()
    next_op,next_data=x.prepare(child,'0.15')
    x.workflow.delete_prepared_sorting(x.uid,'replace-next-reference-pass',next_op,confirm_unstarted=True)
    next_data.pop('capture_model_id');next_data['recipe_id']=data['recipe_id']
    next_op=x.workflow.prepare(x.uid,'next-reference-pass',next_data)
    next_run=x.workflow.start(x.uid,next_op,count=50,seed='next-window-run');x.workflow.simulate_next(x.uid,next_run)
    with x.db.read() as s:
        assert set(s.scalars(select(Particle.payload['reference_bean']['bean_sequence'].as_integer()).where(Particle.run_id==next_run))) <= expected_members


def test_manual_unknown_boundary_does_not_invent_per_bin_composition(factory):
    x=factory('real');source,*_=x.receive('20');op,data=x.prepare(source)
    x.start(op);x.workflow.transition(x.uid,op,'paused',reason='Physical feeding stopped manually')
    swap(x,op,data,selected_id=empty(x))
    x.workflow.transition(x.uid,op,'running');x.finish(op)
    review,_=reviewed(x,op,{'selected':'3.5','pass_through':'4.5','selected_2':'13.5','remainder':'0.5'})
    assert not review['model']['available']
    x.workflow.reconcile(x.uid,'manual-segment-weights',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Accept unknown composition with independently entered masses')
    with x.db.read() as s:
        assert current(s,s.get(Operation,op)).first_sequence is None
        profiles=list(s.scalars(select(Profile).where(Profile.operation_id==op,Profile.portion_id.is_not(None))))
        assert all(p.data['composition_evidence']=='unknown' and p.data['estimated_count'] is None for p in profiles)


def test_shared_source_hopper_swap_still_weighs_source_only_once(system,tmp_path):
    from test_hopper_emptying import hopper
    x=system;op,source,source_bin,data,_=hopper(x,tmp_path,planned='2')
    run=x.workflow.start(x.uid,op,count=1000,seed='shared-segments');x.workflow.simulate_next(x.uid,run)
    x.workflow.transition(x.uid,op,'paused',reason='Return vessel full')
    swap(x,op,data,pass_through_id=empty(x))
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op)
    review,_=reviewed(x,op,{'pass_through':'9.5','pass_through_2':'1.5'})
    assert len(review['rows'])==2 and review['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'shared-segment-weights',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Shared source included in its collection total')
    with x.db.read() as s:
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==10000000
        assert {o.stream:o.mass_mg for o in s.scalars(select(Output).filter_by(operation_id=op))}=={'pass_through':9000000,'pass_through_2':1000000}


def test_occupied_replacement_combines_ancestry_and_rejects_stale_saved_reading(system):
    from beanosort.models import Lineage
    x=system;source,_,op,data,run=ready(x)
    previous,occupied,*_=x.receive('5')
    full,full_bin,*_=x.receive('1000')
    x.workflow.transition(x.uid,op,'paused',reason='Selected full')
    with pytest.raises(DomainError,match='capacity'):
        swap(x,op,data,selected_id=full_bin)
    swap(x,op,data,selected_id=occupied)
    with x.db.read() as s:first=next(c for c in collections(s,s.get(Operation,op)) if c.code=='selected')
    record_weight(x.workflow,x.uid,'removed-v1',first.id,{'gross':'3.5','revision':'1','weighed':True})
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op)
    weights={'selected_2':'18.5','pass_through':'4.5','remainder':'0.5'}
    review,_=reviewed(x,op,weights)
    record_weight(x.workflow,x.uid,'removed-v2',first.id,{'gross':'3.5','revision':'1','weighed':True})
    with pytest.raises(DomainError):x.workflow.reconcile(x.uid,'stale-segment-reading',op,review['data'])
    review,_=reviewed(x,op,weights)
    assert review['input_total_mg']==25000000 and review['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'occupied-segment-final',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='Actual additions and previous stock accounted')
    with x.db.read() as s:
        child=s.scalar(select(Output.portion_id).filter_by(operation_id=op,stream='selected_2'))
        assert {l.parent_id for l in s.scalars(select(Lineage).filter_by(child_id=child))}=={source,previous}
        assert balance(s,previous)==0 and balance(s,child)==18000000
        assert balance(s,full)==1000000000
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==1025000000


def test_source_first_used_as_hopper_destination_after_pause_is_weighed_once(system,tmp_path):
    from test_hopper_emptying import hopper
    x=system;op,source,source_bin,data,_=hopper(x,tmp_path,same=False,planned='2')
    run=x.workflow.start(x.uid,op,count=1000,seed='source-return-later');x.workflow.simulate_next(x.uid,run)
    x.workflow.transition(x.uid,op,'paused',reason='Return remaining beans to source')
    swap(x,op,data,pass_through_id=source_bin)
    x.workflow.transition(x.uid,op,'running')
    while x.workflow.simulate_next(x.uid,run):pass
    x.finish(op)
    review,_=reviewed(x,op,{'pass_through':'1','pass_through_2':'10'})
    assert len(review['rows'])==2 and review['discrepancy_mg']==0
    x.workflow.reconcile(x.uid,'source-return-later-weights',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,reason='One weight per physical vessel')
    with x.db.read() as s:
        assert s.scalar(select(func.sum(Ledger.delta_mg)))==10000000
        assert s.scalar(select(func.count()).select_from(Assignment).where(Assignment.active==True))==2


def test_interrupted_segmented_run_keeps_remainder_retained_loss_and_signed_discrepancy(system):
    x=system;_,_,op,data,run=ready(x)
    x.workflow.transition(x.uid,op,'paused',reason='Selected full')
    swap(x,op,data,selected_id=empty(x))
    x.workflow.transition(x.uid,op,'running');x.workflow.simulate_next(x.uid,run)
    x.workflow.transition(x.uid,op,'failed',reason='Manual feeder interruption')
    x.finish(op)
    review,form=reviewed(x,op,{'selected':'1.5','selected_2':'3.5','pass_through':'5.5','remainder':'10.5'})
    with x.db.read() as s:
        review=build_review(s,s.get(Operation,op),x.db.environment,{**form,'retained':'0.5','loss':'0.3'})
    assert review['discrepancy_mg']==200000
    x.workflow.reconcile(x.uid,'interrupted-segment-final',op,review['data'])
    x.workflow.release(x.uid,op,accept_estimates=True,approve_discrepancy=True,reason='Reviewed retention, loss and unexplained difference')
    with x.db.read() as s:
        amounts={o.stream:o.mass_mg for o in s.scalars(select(Output).filter_by(operation_id=op))}
        assert amounts=={'selected':1000000,'selected_2':3000000,'pass_through':5000000,'remainder':10000000,'retained':500000}
        retained=s.get(Portion,s.scalar(select(Output.portion_id).filter_by(operation_id=op,stream='retained')))
        assert retained.status=='wip' and available(s,retained)==0
