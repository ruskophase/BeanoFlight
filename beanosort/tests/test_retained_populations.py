from copy import deepcopy
from decimal import Decimal
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from beanosort.capture import digest
from beanosort.handling import latest_profile
from beanosort.material import MaterialActions
from beanosort.models import Assignment, Audit, Container, Ledger, Particle, Portion, Profile
from beanosort.quantities import DomainError, kg
from beanosort.retained_populations import inspect_population, members, population_report, recover_profiles, validate
from beanosort.service import active_assignment, balance
from test_material_actions import adjust_data, move_data, state
from test_resorting import first_pass
from test_web import client_for


def combined(x,tmp_path):
    _,recipe,_,_,run,outputs=first_pass(x,tmp_path)
    with x.db.read() as s:
        cs=active_assignment(s,portion_id=outputs['selected']).container_id
        cd=active_assignment(s,portion_id=outputs['pass_through']).container_id
        quantity=balance(s,outputs['selected'])//2
        expected=(Decimal(quantity)*Decimal(latest_profile(s,outputs['selected']).data['count_per_kg'])+
                  Decimal(balance(s,outputs['pass_through']))*Decimal(latest_profile(s,outputs['pass_through']).data['count_per_kg']))/1000000
    MaterialActions(x.db).move_contents(x.uid,'known-population-combination',move_data(x,cs,cd,moved_mass=kg(quantity)))
    return state(x,cd)['portion_id'],cd,cs,run,expected


def test_combination_preserves_count_weighted_joint_records_and_photos_on_creation(system,tmp_path):
    x=system;pid,cid,_,run,expected=combined(x,tmp_path)
    with x.db.read() as s:
        data=latest_profile(s,pid).data;pool=data['retained_population'];validate(pool)
        assert abs(Decimal(pool['estimated_count'])-expected)<Decimal('1e-20')
        assert data['population_analysis_url']=='/portions/'+pid+'/populations'
        rows=members(s,pool);assert len(rows)==len({(row['dataset'].id,row['bean']['bean_sequence']) for row in rows})
        assert len(rows)>1 and all(row['bean']['photo_CamL'] and row['bean']['photo_CamR'] for row in rows)
        assert data['distributions'] and data['count_per_kg']
        report=population_report(s,pool)
        for feature,spec in report['definitions'].items():
            histogram=report['populations']['input']['histograms'][feature]
            for bucket,value in enumerate(histogram):
                selected=inspect_population(s,pool,feature=feature,bucket=bucket)
                assert abs(Decimal(selected['matching_count'])-Decimal(value))<Decimal('1e-20')


def test_portion_and_container_show_current_chart_link_and_readonly_interval_photos(system,tmp_path):
    x=system;pid,cid,_,_,_=combined(x,tmp_path);client=client_for(x)
    for path in ('/portions/'+pid,'/containers/'+cid):
        response=client.get(path)
        assert response.status_code==200 and f'href="/portions/{pid}/populations"' in response.text
    before=state(x,cid)
    page=client.get('/portions/'+pid+'/populations')
    assert page.status_code==200,page.text
    report=json.loads(page.text.split('<script id="population-data" type="application/json">')[1].split('</script>')[0])
    assert report['single_population'] and len(report['definitions'])==6
    for feature,histogram in report['populations']['input']['histograms'].items():
        for bucket,count in enumerate(histogram):
            if not Decimal(count):continue
            response=client.get('/portions/'+pid+'/populations/inspect',params={'feature':feature,'bucket':bucket})
            assert response.status_code==200,response.text
            data=response.json();assert data['retained'] and data['membership_evidence']=='inherited_estimate'
            assert abs(Decimal(data['matching_count'])-Decimal(count))<Decimal('1e-20')
            for item in data['items']:
                for camera,url in item['photos'].items():assert client.get(url).status_code==200
    assert state(x,cid)==before
    assert client.get('/portions/'+pid+'/populations/inspect',params={'feature':'lab_l','bucket':0,'basis':'simulated'}).status_code==409


def test_recovery_appends_evidence_only_is_idempotent_and_preserves_old_records(system,tmp_path):
    x=system;pid,cid,_,_,_=combined(x,tmp_path)
    with x.db.write() as s:
        previous=latest_profile(s,pid);data=deepcopy(previous.data)
        for key in ('retained_population','population_analysis_url','comparability_key'):data.pop(key,None)
        data.update(count_per_kg=None,estimated_count=None,distributions={})
        broken=Profile(id='profile-'+uuid4().hex,operation_id=previous.operation_id,portion_id=pid,stream='legacy_fixture',version=1,data=data)
        s.add(broken);broken_id=broken.id
    before=state(x,cid)
    with x.db.read() as s:
        old_profiles={p.id:deepcopy(p.data) for p in s.scalars(select(Profile))}
        ledger=[(row.id,row.delta_mg) for row in s.scalars(select(Ledger))]
        particles=[(row.id,row.digest) for row in s.scalars(select(Particle))]
    result=recover_profiles(x.db,x.uid,[pid],'Restore missing combined evidence')
    assert result[0]['reference_members']>1 and result[0]['profile_id']!=broken_id
    assert recover_profiles(x.db,x.uid,[pid],'Repeat recovery')[0]['already_recovered']
    after=state(x,cid);assert {k:v for k,v in before.items() if k!='profile_id'}=={k:v for k,v in after.items() if k!='profile_id'}
    with x.db.read() as s:
        assert all(s.get(Profile,key).data==data for key,data in old_profiles.items())
        assert len(list(s.scalars(select(Profile))))==len(old_profiles)+1
        assert [(row.id,row.delta_mg) for row in s.scalars(select(Ledger))]==ledger
        assert [(row.id,row.digest) for row in s.scalars(select(Particle))]==particles
        assert len(list(s.scalars(select(Audit).filter_by(action='population_evidence_recovered'))))==1


def test_representative_reweighing_keeps_evidence_and_selective_adjustment_does_not_invent_it(system,tmp_path):
    x=system;pid,cid,_,_,_=combined(x,tmp_path);before=state(x,cid)
    MaterialActions(x.db).reconcile(x.uid,'known-reweigh',adjust_data(x,cid,weight=kg(before['mass_mg']//2)))
    with x.db.read() as s:
        population=latest_profile(s,pid).data['retained_population']
        assert population['net_mass_mg']==before['mass_mg']//2
    MaterialActions(x.db).reconcile(x.uid,'selective-removal',adjust_data(x,cid,weight=kg(before['mass_mg']//3),representative=False))
    with x.db.read() as s:
        assert latest_profile(s,pid).data['material_population_unknown']
        assert 'retained_population' not in latest_profile(s,pid).data
    with pytest.raises(DomainError):recover_profiles(x.db,x.uid,[pid],'Cannot recover unobserved selective membership')


def test_different_archives_keep_dataset_scoped_ids_instead_of_collapsing_equal_sequences(system,tmp_path):
    x=system;pid,_,_,_,_=combined(x,tmp_path)
    from beanosort.references import import_bundle
    from reference_fixture import bundle
    import hashlib
    root=bundle(tmp_path/'other-reference')
    rows=[json.loads(line) for line in (root/'beans.jsonl').read_text().splitlines()]
    for row in rows:row['bean_id']='other/'+row['bean_id']
    raw=''.join(json.dumps(row)+'\n' for row in rows).encode();(root/'beans.jsonl').write_bytes(raw)
    manifest=json.loads((root/'manifest.json').read_text());manifest['files'][-1]['sha256']=hashlib.sha256(raw).hexdigest()
    (root/'manifest.json').write_text(json.dumps(manifest))
    did=import_bundle(x.db,x.uid,root,tmp_path/'other-snapshots',name='Other archive')
    with x.db.read() as s:
        original=deepcopy(latest_profile(s,pid).data['retained_population'])
        second=deepcopy(original['pools'][0]);second.update(dataset_id=did)
        from beanosort.models import ReferenceDataset
        second['dataset_fingerprint']=s.get(ReferenceDataset,did).fingerprint
        original['pools'].append(second);original['estimated_count']=str(2*Decimal(original['estimated_count']));original['net_mass_mg']*=2
        original['fingerprint']=digest({key:value for key,value in original.items() if key!='fingerprint'})
        items=members(s,original)
        assert len(items)==2*len(original['pools'][0]['weights'])
        assert len(items)==len({(row['dataset'].id,row['bean']['bean_sequence']) for row in items})
        bad=deepcopy(original);bad['pools'][0]['weights'][0][1]+=1
        with pytest.raises(DomainError,match='fingerprint'):members(s,bad)


def test_returned_count_sample_updates_estimated_counts_without_losing_joint_evidence(system,tmp_path):
    from beanosort.handling import MaterialHandling
    x=system;pid,cid,_,_,_=combined(x,tmp_path);before=state(x,cid)
    MaterialHandling(x.db).count_sample(x.uid,'retained-count-sample',pid,10,'0.001',{
        'station_id':x.stations['Receiving desk'],'evidence':x.evidence,'representative':True,'sample_returned':True})
    with x.db.read() as s:
        data=latest_profile(s,pid).data
        expected=Decimal(before['mass_mg'])/100
        assert abs(Decimal(data['retained_population']['estimated_count'])-expected)<Decimal('1e-20')
        assert data['population_analysis_url']=='/portions/'+pid+'/populations'
        assert data['count_density_evidence']['sample_count']==10
        assert members(s,data['retained_population'])


def test_complete_known_populations_keep_evidence_without_weighing_or_sampling_assumption(system,tmp_path):
    x=system;_,cid,source,_,_=combined(x,tmp_path)
    before_source=state(x,source);before_destination=state(x,cid)
    op=MaterialActions(x.db).move_contents(x.uid,'whole-known-populations',move_data(
        x,source,cid,empty_source=True,moved_mass='',representative=False))
    result=state(x,cid)
    assert result['mass_mg']==before_source['mass_mg']+before_destination['mass_mg']
    assert state(x,source)['portion_id'] is None
    with x.db.read() as s:
        data=latest_profile(s,result['portion_id']).data
        assert data['retained_population'] and data['population_analysis_url']
        assert members(s,data['retained_population'])
        from beanosort.models import Measurement
        assert not list(s.scalars(select(Measurement).filter_by(operation_id=op)))


@pytest.mark.parametrize('source_known,destination_known',[(True,False),(False,True),(False,False)])
def test_unknown_contributor_leaves_combined_population_unknown(system,tmp_path,source_known,destination_known):
    x=system
    if source_known or destination_known:
        _,known_cid,_,_,_=combined(x,tmp_path)
    _,raw,*_=x.receive('1')
    source=known_cid if source_known else raw
    if destination_known:destination=known_cid
    elif source_known:destination=raw
    else:_,destination,*_=x.receive('2')
    MaterialActions(x.db).move_contents(x.uid,'whole-mixed-evidence',move_data(
        x,source,destination,empty_source=True,moved_mass='',representative=False))
    with x.db.read() as s:
        pid=active_assignment(s,container_id=destination).portion_id
        data=latest_profile(s,pid).data
        assert data['composition_evidence']=='unknown'
        assert not data.get('retained_population') and not data.get('population_analysis_url')
        assert not data.get('distributions') and data.get('count_per_kg') is None
        from beanosort.models import Lineage
        assert len(list(s.scalars(select(Lineage).filter_by(child_id=pid))))==2
