from decimal import Decimal
import json
import statistics

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from beanosort.capture import CAPTURE_DEFAULT, validate_particle
from beanosort.models import Audit, Container, Ledger, Operation, Particle, Portion, ReferenceDataset, User, Version
from beanosort.quantities import DomainError
from beanosort.references import (DEFAULT_RECIPE, create_recipe, features, import_bundle, load_dataset,
                                 matches, photo_path, population_report, recipe_data, sampled_particle,
                                 retire_recipe, retired_recipe_ids)
from beanosort.service import Workflow, balance
from reference_fixture import bundle
from test_web import client_for, token


def reference(x,tmp_path):
    original=bundle(tmp_path/'bundle')
    did=import_bundle(x.db,x.uid,original,tmp_path/'snapshots',name='Test photographed beans')
    rid=create_recipe(x.db,x.uid,'first-reference-recipe',DEFAULT_RECIPE,did)
    return did,rid,original


def job(x,rid,*,p='0.97',count=503):
    pid,*_=x.receive('2')
    old,data=x.prepare(pid,'2')
    x.workflow.transition(x.uid,old,'cancelled',reason='Use photographed reference')
    capture=x.workflow.create_version(x.uid,'capture_model','Reference test capture',{**CAPTURE_DEFAULT,'probability':p})
    op=x.workflow.prepare(x.uid,'prepare-reference-job',{**data,'recipe_id':rid,'capture_model_id':capture})
    run=x.workflow.start(x.uid,op,count=count,seed='photographed-beans')
    return pid,op,run


def test_reference_snapshot_and_recipe_are_immutable_and_repeatable(system,tmp_path):
    x=system; did,rid,original=reference(x,tmp_path)
    before=(original/'beans.jsonl').read_bytes()
    assert import_bundle(x.db,x.uid,original,tmp_path/'snapshots',name='Test photographed beans')==did
    assert create_recipe(x.db,x.uid,'first-reference-recipe',DEFAULT_RECIPE,did)==rid
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did); rows,_=load_dataset(dataset,verify=True)
        recipe=s.get(Version,rid).data
        assert dataset.metadata_json['bean_count']==10 and dataset.metadata_json['sample_count']==30
        assert dataset.metadata_json['representative_photo_count']==20
        assert recipe['statistics']['median']==statistics.median([r['combined_lab_l_mean'] for r in rows])
        assert recipe['statistics']['sigma']==statistics.pstdev([r['combined_lab_l_mean'] for r in rows])
        assert matches(10,recipe) is True and matches(26,recipe) is False
        assert matches(recipe['lower'],recipe) is False and matches(None,recipe) is None
        assert photo_path(dataset,1,'CamL').read_bytes()==(original/'photos/1-CamL.png').read_bytes()
    second=create_recipe(x.db,x.uid,'second-reference-recipe',DEFAULT_RECIPE,did,sigma='1')
    with x.db.read() as s:
        assert s.get(Version,rid).version==1 and s.get(Version,rid).data==recipe
        assert s.get(Version,second).version==2
    for table in ('reference_datasets','versions'):
        with pytest.raises(DBAPIError):
            with x.db.engine.begin() as c: c.execute(text(f'UPDATE {table} SET name=:name'),{'name':'rewritten'})
    assert (original/'beans.jsonl').read_bytes()==before


@pytest.mark.parametrize('p',['0','1','0.97'])
def test_measured_rows_keep_joint_properties_and_destination_through_replay(system,tmp_path,p):
    x=system; did,rid,_=reference(x,tmp_path); pid,op,run=job(x,rid,p=p)
    assert x.workflow.simulate_next(x.uid,run)==250
    restarted=Workflow(x.db)
    assert restarted.simulate_next(x.uid,run,replay_first=True)==0
    assert restarted.simulate_next(x.uid,run)==250
    assert restarted.simulate_next(x.uid,run)==3
    assert restarted.simulate_next(x.uid,run)==0
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did); rows,_=load_dataset(dataset)
        by_id={r['bean_id']:r for r in rows}; particles=list(s.scalars(select(Particle).filter_by(run_id=run)))
        assert len(particles)==503
        for particle in particles:
            payload=particle.payload; reference_row=by_id[payload['reference_bean']['bean_id']]
            assert payload['features']==features(reference_row)
            assert payload==sampled_particle(dataset,rows,s.get(Version,rid).data,run,'photographed-beans',particle.source_sequence)
            validate_particle(payload,'simulation')
            if payload['decision']=='pass' or p=='0': assert particle.simulated_destination=='pass_through'
            elif p=='1': assert particle.simulated_destination=='selected'
        profile=x.workflow.profile(s,s.get(Operation,op)); report=profile['reference_population']
        assert profile['counts']['physical_confirmed']==0
        targeted=profile['counts']['action_attempted']
        assert Decimal(report['populations']['expected_selected']['count'])==Decimal(p)*targeted
        assert sum(Decimal(report['populations'][name]['count']) for name in ('expected_selected','expected_pass_through','unresolved'))==503
        for name,pop in report['populations'].items():
            for feature,bins in pop['histograms'].items():
                assert sum(map(Decimal,bins))+Decimal(pop['unknown'][feature])==Decimal(pop['count'])
        assert balance(s,pid)==2000000 and s.scalar(select(func.count()).select_from(Ledger))==1


def test_reference_ingestion_rejects_changed_rows_and_source_files(system,tmp_path):
    x=system; did,rid,_=reference(x,tmp_path); pid,op,run=job(x,rid)
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did);rows,_=load_dataset(dataset); recipe=s.get(Version,rid).data
        row=sampled_particle(dataset,rows,recipe,run,'photographed-beans',0)
    row['features']['lab_l']=99
    with pytest.raises(DomainError,match='differs'):
        x.workflow.ingest(x.uid,run,'tampered-reference',[row])
    with x.db.read() as s:
        assert s.scalar(select(func.count()).select_from(Particle))==0
    from pathlib import Path
    path=Path(dataset.directory)/'beans.jsonl'; path.write_text(path.read_text()+'\n')
    with pytest.raises(DomainError,match='changed'):
        x.workflow.simulate_next(x.uid,run)


def test_reference_import_rejects_bad_hash_and_unsafe_photo(system,tmp_path):
    x=system; root=bundle(tmp_path/'bundle')
    manifest=json.loads((root/'manifest.json').read_text()); manifest['files'][-1]['sha256']='wrong'
    (root/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(DomainError,match='hash'):
        import_bundle(x.db,x.uid,root,tmp_path/'snapshots',name='Bad reference')
    from beanosort.references import inside
    for path in ('../secret','/etc/passwd'):
        with pytest.raises(DomainError): inside(root,path)


def test_missing_reference_property_stays_unresolved(system,tmp_path):
    import hashlib
    x=system;root=bundle(tmp_path/'unknown-bundle')
    rows=[json.loads(line) for line in (root/'beans.jsonl').read_text().splitlines()]
    rows[0]['combined_lab_l_mean']=None
    raw=''.join(json.dumps(row)+'\n' for row in rows).encode();(root/'beans.jsonl').write_bytes(raw)
    manifest=json.loads((root/'manifest.json').read_text());manifest['files'][-1]['sha256']=hashlib.sha256(raw).hexdigest()
    (root/'manifest.json').write_text(json.dumps(manifest))
    did=import_bundle(x.db,x.uid,root,tmp_path/'unknown-snapshots',name='Unknown lightness fixture')
    rid=create_recipe(x.db,x.uid,'unknown-reference-recipe','Unknown reference test',did)
    client=client_for(x)
    assert client.get('/recipes/'+rid,params={'population':'input'}).status_code==200
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did);recipe=s.get(Version,rid).data
        report=population_report([{'payload':{'features':features(rows[0]),'decision':'unknown','action':'not_issued'}}],dataset,recipe,CAPTURE_DEFAULT)
        assert report['populations']['unresolved']['count']=='1'
        assert report['populations']['input']['unknown']['lab_l']=='1'
        assert report['populations']['expected_selected']['count']=='0'
        assert report['populations']['expected_pass_through']['count']=='0'
    client.close()


def test_reference_recipes_are_simulation_only(factory,tmp_path):
    x=factory('real');did,rid,_=reference(x,tmp_path)
    pid,*_=x.receive('2');old,data=x.prepare(pid,'2')
    x.workflow.transition(x.uid,old,'cancelled',reason='Check reference isolation')
    with pytest.raises(DomainError,match='isolated simulation'):
        x.workflow.prepare(x.uid,'real-reference-prohibited',{**data,'recipe_id':rid})
    with x.db.read() as s:
        assert s.get(Portion,pid).status=='stock' and balance(s,pid)==2000000


def test_recipe_catalogue_preview_photos_and_run_charts(system,tmp_path):
    x=system;did,rid,_=reference(x,tmp_path);client=client_for(x)
    page=client.get('/recipes'); assert page.status_code==200 and DEFAULT_RECIPE in page.text
    preview=client.get('/recipes/'+rid);assert preview.status_code==200 and 'Population comparison' in preview.text
    assert 'archived source photographs' in preview.text
    assert client.get(f'/reference-datasets/{did}/photos/1/CamL').headers['content-type']=='image/png'
    assert client.get('/jobs/new',params={'recipe_id':rid}).text.count('value="'+rid+'" selected')==1
    data=dict(csrf=token(page),key=token(page,'key'),name='Above median',dataset_id=did,feature='lab_l',relation='above',sigma='1')
    assert client.post('/recipes',data={**data,'csrf':'bad'}).status_code==403
    created=client.post('/recipes',data=data);assert created.status_code==200 and 'Above median' in created.text
    assert client.post('/recipes',data=data).url.path==created.url.path
    pid,op,run=job(x,rid);x.workflow.simulate_next(x.uid,run)
    assert '/jobs/'+op+'/populations' in client.get('/jobs/'+op).text
    for population in ('input','targeted','selected','pass_through'):
        result=client.get('/jobs/'+op+'/populations',params={'population':population})
        assert result.status_code==200 and 'population-data' in result.text
        assert 'Simulated destination outcomes' in result.text
    with x.db.write() as s: s.get(User,x.uid).role='operator'
    assert client.post('/recipes',data={**data,'key':'operator-recipe'}).status_code==409
    client.close()


@pytest.mark.parametrize('relation',['above','below'])
def test_area_recipes_pin_projected_area_statistics_and_preserve_samples(system,tmp_path,relation):
    x=system;did,_,_=reference(x,tmp_path)
    rid=create_recipe(x.db,x.uid,'area-'+relation,'Area '+relation,did,'area_mm2',relation,'2')
    with x.db.read() as s:
        dataset=s.get(ReferenceDataset,did);rows,_=load_dataset(dataset);recipe=s.get(Version,rid).data
        values=[r['projected_area_geomean_mm2_median'] for r in rows]
        assert recipe['statistics']['field']=='projected_area_geomean_mm2_median'
        assert recipe['statistics']['unit']=='mm²'
        assert recipe['statistics']['median']==statistics.median(values)
        assert recipe['statistics']['sigma']==statistics.pstdev(values)
        assert matches(recipe['upper'] if relation=='above' else recipe['lower'],recipe) is False
        for i in range(50):
            row=sampled_particle(dataset,rows,recipe,'area-run','seed',i)
            assert row['features']['area_mm2']==2*row['features']['lab_l']
            assert (row['decision']=='selected')==matches(row['features']['area_mm2'],recipe)
    client=client_for(x)
    page=client.get('/recipes/'+rid)
    assert page.status_code==200 and 'data-population-chart="area_mm2"' in page.text
    pid,op,run=job(x,rid,count=10);x.workflow.simulate_next(x.uid,run)
    assert client.get('/jobs/'+op+'/populations').status_code==200
    client.close()


def test_existing_five_feature_snapshots_keep_old_rows_when_area_is_added(system,tmp_path):
    from copy import deepcopy
    from types import SimpleNamespace
    x=system;did,rid,_=reference(x,tmp_path)
    with x.db.read() as s:
        stored=s.get(ReferenceDataset,did);metadata=deepcopy(stored.metadata_json)
        metadata['metrics'].pop('area_mm2')
        dataset=SimpleNamespace(id=stored.id,fingerprint=stored.fingerprint,directory=stored.directory,metadata_json=metadata)
        rows,_=load_dataset(dataset);old_recipe=s.get(Version,rid).data
        old=sampled_particle(dataset,rows,old_recipe,'old-run','seed',1)
        assert 'area_mm2' not in old['features']
        raw=next(r for r in rows if r['bean_id']==old['reference_bean']['bean_id'])
        assert old['features']=={k:v for k,v in features(raw).items() if k!='area_mm2'}
        area=recipe_data(dataset,'area_mm2','above','2')
        updated=sampled_particle(dataset,rows,area,'old-run','seed',1)
        assert updated['reference_bean']==old['reference_bean']
        assert updated['features']['area_mm2']==raw['projected_area_geomean_mm2_median']
        assert 'area_mm2' not in dataset.metadata_json['metrics']
        assert sampled_particle(dataset,rows,old_recipe,'old-run','seed',1)==old
        report=population_report([{'payload':updated}],dataset,area,CAPTURE_DEFAULT)
        assert 'area_mm2' in report['definitions']


def test_retired_recipe_is_removed_from_new_jobs_without_erasing_history(system,tmp_path):
    x=system;did,rid,_=reference(x,tmp_path)
    pid,*_=x.receive('2');op,data=x.prepare(pid,'2');legacy=data['recipe_id']
    with x.db.read() as s:original=s.get(Version,legacy).data
    for _ in range(2):
        assert retire_recipe(x.db,x.uid,'remove-legacy',legacy,'Operator requested removal')==legacy
    client=client_for(x)
    assert legacy not in client.get('/recipes').text
    assert 'value="'+legacy+'"' not in client.get('/jobs/new').text
    assert client.get('/jobs/new',params={'recipe_id':legacy}).status_code==409
    historical=client.get('/recipes/'+legacy)
    assert historical.status_code==200 and 'Retired recipe' in historical.text
    assert 'Prepare sorting with this recipe' not in historical.text
    assert 'existing job retains its pinned version' in client.get('/jobs/'+op).text
    editor=client.get('/jobs/'+op+'/edit')
    assert 'Choose a current recipe' in editor.text and 'value="'+legacy+'"' not in editor.text
    x.workflow.transition(x.uid,op,'cancelled',reason='Check retired recipe guard')
    with pytest.raises(DomainError,match='retired'):
        x.workflow.prepare(x.uid,'new-legacy-forbidden',data)
    replacement=x.workflow.prepare(x.uid,'active-recipe-permitted',{**data,'recipe_id':rid})
    with x.db.read() as s:
        assert s.get(Version,legacy).data==original
        assert s.get(Operation,op).recipe_id==legacy and s.get(Operation,replacement).recipe_id==rid
        assert legacy in retired_recipe_ids(s)
        assert s.scalar(select(func.count()).select_from(Audit).where(Audit.action=='recipe_retired'))==1
        assert balance(s,pid)==2000000
    client.close()
