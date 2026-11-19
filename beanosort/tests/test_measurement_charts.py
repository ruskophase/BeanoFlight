"""Retained machine records drive charts; inventory/evidence stay immutable."""
from copy import deepcopy
from html import unescape
import json
import re
from urllib.parse import urlsplit,parse_qsl
from uuid import uuid4

import pytest
from sqlalchemy import select

from beanosort.models import Assignment,Container,Destination,Ledger,MachineEvent,MachineJob,Operation,Output,Particle,Profile,Run,SortingCollection,identity
from beanosort.measurement_charts import context,population_report,profile_url
from beanosort.quantities import DomainError
from beanosort.weighing import weighing_context
from test_machine_jobs import prepared,integration
from test_web import client_for,token


def events():
    rows=[]
    for n,raw in enumerate(({'caml_lab_l_mean':21,'camr_lab_l_mean':23,'caml_lab_chroma_mean':8,
                            'camr_lab_chroma_mean':10,'caml_ellipse_major_mm':6,'camr_ellipse_major_mm':8,
                            'projected_area_geomean_mm2':30,'rotational_ellipsoid_volume_proxy_mm3':125,
                            'caml_mean_r':100,'caml_mean_g':80,'caml_mean_b':40},
                           {'caml_lab_l_mean':29,'camr_lab_l_mean':31,'rotational_ellipsoid_volume_proxy_mm3':250},{})):
        rows.append({'sequence':n+1,'kind':'bean','created_at':'2026-11-18T08:00:00+00:00',
            'data':{'bean_sequence':n+100,'ordinal':n,'features':raw,'sample_count':(2,3,0)[n],
                    'measurement_status':'measured' if raw else 'missing'}})
    return rows


def started(x, *, source='live', destination=None, image_mode='none'):
    op=prepared(x,destination,source=source,image_mode=image_mode);adapter=integration(x)
    adapter.enqueue(x.uid,op,'measurement-start-'+uuid4().hex,'start');adapter.tick()
    return op,adapter


def finished(x, *, source='live', destination=None, destination_gross='1.100', image_mode='none', extra_events=()):
    op,adapter=started(x,source=source,destination=destination,image_mode=image_mode)
    adapter.transport.events=events()+list(extra_events);adapter.transport.state='completed';adapter.tick()
    client=client_for(x);page=client.get('/jobs/'+op+'/weigh');form={}
    with x.db.read() as s:ctx=weighing_context(s,s.get(Operation,op),x.db.environment)
    for row in ctx['rows']:
        code=row['stream'];form.update({'gross_'+code:'0.900' if code=='remainder' else destination_gross,
            'revision_'+code:str(row['container'].revision),'weighed_'+code:'on'})
    page=client.post('/jobs/'+op+'/weigh/review',data={**form,'csrf':token(page),'key':token(page,'key')})
    assert page.status_code==200,page.text
    page=client.post('/jobs/'+op+'/weigh/accept',data={'csrf':token(page),'review_token':token(page,'review_token'),
        'accept_review':'on','reason':'Isolated machine-chart fixture'})
    assert page.status_code==200,page.text
    with x.db.read() as s:
        portion=s.scalar(select(Output.portion_id).where(Output.operation_id==op,Output.stream=='pass_through'))
        profile=s.scalar(select(Profile).where(Profile.portion_id==portion))
        cid=s.scalar(select(Assignment.container_id).where(Assignment.portion_id==portion,Assignment.active==True))
        url=profile_url(s,profile)
    return op,client,portion,cid,url


def report(page):
    return json.loads(re.search(r'<script id="population-data"[^>]*>(.*?)</script>',page.text,re.S).group(1))


@pytest.mark.parametrize('environment',['simulation','real'])
def test_completed_machine_population_has_charts_and_preserves_evidence(factory,environment):
    x=factory(environment);op,client,portion,cid,url=finished(x)
    with x.db.read() as s:
        before=[(p.id,deepcopy(p.data)) for p in s.scalars(select(Profile))]
        ledger=[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
    for path in ('/jobs/'+op,'/portions/'+portion,'/containers/'+cid):
        page=client.get(path)
        assert page.status_code==200 and 'Population charts & measurement evidence' in unescape(page.text)
    page=client.get(url);assert page.status_code==200,page.text
    assert 'No bean photographs were saved' in page.text and 'not independently verified' in page.text
    data=report(page);assert data['measurement_evidence'] and data['physical_confirmation'] is False
    assert data['definitions']['lab_l']['count']==2 and data['definitions']['lab_l']['unknown']==1
    assert data['definitions']['lab_l']['mean']==26
    for pop in data['populations'].values():
        assert pop['count']==3  # sample_count=2/3 is never counted as extra beans
        for feature,hist in pop['histograms'].items():assert sum(hist)+pop['unknown'][feature]==3
    bucket=next(i for i,n in enumerate(data['populations']['input']['histograms']['lab_l']) if n)
    args=dict(parse_qsl(urlsplit(url).query));args.update(feature='lab_l',bucket=bucket,basis='observed',population='pass_through')
    inspection=client.get('/jobs/'+op+'/measurements/inspect',params=args)
    assert inspection.status_code==200,inspection.text
    bean=inspection.json()['items'][0]
    assert bean['colours']['CamL']==[100,80,40] and bean['photos']=={'CamL':None,'CamR':None}
    assert bean['feature_value']==22 and inspection.json()['physical_confirmation'] is False
    with x.db.read() as s:
        assert before==[(p.id,p.data) for p in s.scalars(select(Profile))]
        assert ledger==[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
        remainder=s.scalar(select(Profile).where(Profile.operation_id==op,Profile.stream=='remainder'))
        assert profile_url(s,remainder) is None
    assert client.get('/jobs/'+op+'/measurements/inspect',params={**args,'basis':'simulated'}).status_code==409
    client.close()


def test_historical_profile_excludes_later_observations_and_wrong_profile(system):
    x=system;op,adapter=started(x)
    adapter.transport.events=events()[:1];adapter.tick()
    with x.db.write() as s:
        profile=Profile(id=identity('profile'),operation_id=op,stream='run',version=1,data=x.workflow.profile(s,s.get(Operation,op)))
        s.add(profile);pid=profile.id
    adapter.transport.events=events()[:2];adapter.tick()
    client=client_for(x)
    frozen=report(client.get('/jobs/'+op+'/measurements',params={'profile_id':pid}))
    current=report(client.get('/jobs/'+op+'/measurements'))
    assert frozen['populations']['input']['count']==1 and current['populations']['input']['count']==2
    other,_=started(x)
    assert client.get('/jobs/'+other+'/measurements',params={'profile_id':pid}).status_code==409
    assert client.get('/jobs/'+op+'/measurements/inspect',params={'feature':'moisture','bucket':0}).status_code==409
    client.close()


def test_existing_destination_shows_only_run_contribution_and_replay_is_labelled(system):
    x=system;_,destination,*_=x.receive('2')
    op,client,portion,cid,url=finished(x,source='recorded-fixture',destination=destination,destination_gross='3.100')
    page=client.get(url)
    assert page.status_code==200 and 'Recorded replay measurements' in page.text
    assert "Charts describe this run's contribution" in page.text and 'do not observe the present contents' in page.text
    assert report(page)['populations']['input']['count']==3
    assert report(page)['populations']['observed_previous']['count']==0
    assert 'Earlier contents have no' in page.text
    client.close()


def test_collection_without_an_association_boundary_cannot_claim_full_run(system):
    x=system;op,adapter=started(x)
    adapter.transport.events=events();adapter.tick()
    adapter.enqueue(x.uid,op,'chart-pause','pause');adapter.tick()
    with x.db.write() as s:
        dest=s.scalar(select(Destination).where(Destination.operation_id==op))
        collection=SortingCollection(id=identity('collection'),operation_id=op,destination_id=dest.id,
            container_id=dest.container_id,stream='pass_through',code='pass_through',ordinal=1)
        s.add(collection);s.flush()
        p=Profile(id=identity('profile'),operation_id=op,stream='pass_through',version=1,
            data={**x.workflow.profile(s,s.get(Operation,op)),'logical_stream':'pass_through'})
        s.add(p);pid=p.id
    with x.db.read() as s:
        with pytest.raises(DomainError,match='association is unknown'):context(s,op,pid)


def test_saved_crops_follow_bean_identity_and_leave_colour_and_history_intact(system):
    from fastapi.testclient import TestClient
    x=system;op,adapter=started(x,image_mode='stereo')
    image=lambda frame,paths:{'sequence':frame,'kind':'images','created_at':'2026-11-18T08:00:01+00:00',
        'data':{'bean_sequence':100,'frame_index':frame,'paths':paths}}
    adapter.transport.events=events()+[
        image(4,['stereo/100/00000004-CamL.png','stereo/100/00000004-CamR.png']),
        image(5,['stereo/100/00000005-CamL.png','stereo/100/00000005-CamR.png']),
        {'sequence':6,'kind':'images','data':{'bean_sequence':101,'frame_index':6,'paths':['../../outside.png']}}]
    adapter.transport.state='completed';adapter.tick()
    client=client_for(x);calls=[]
    class CropTransport:
        fail=False
        def crop(self,*args):
            calls.append(args)
            if self.fail:raise DomainError('Machine is offline')
            return b'\x89PNG\r\n\x1a\nfixture'
    transport=CropTransport();client.app.state.machine_jobs.transport=transport
    with x.db.read() as s:
        job=s.get(MachineJob,op);before=[(p.id,p.payload) for p in s.scalars(select(Particle))]
        original_events=[(e.sequence,e.data) for e in s.scalars(select(MachineEvent))]
    data=report(client.get('/jobs/'+op+'/measurements'));bucket=next(i for i,n in enumerate(data['populations']['input']['histograms']['lab_l']) if n)
    inspection=client.get('/jobs/'+op+'/measurements/inspect',params={'feature':'lab_l','bucket':bucket}).json()
    bean=inspection['items'][0]
    assert bean['bean_sequence']==100 and bean['photo_frame_index']==4
    assert bean['colours']['CamL']==[100,80,40] and not inspection['physical_confirmation']
    assert calls==[]  # listing intervals does not fetch/upload image bytes
    for camera,url in bean['photos'].items():
        response=client.get(url)
        assert response.status_code==200 and response.content.startswith(b'\x89PNG')
        assert response.headers['content-type']=='image/png' and response.headers['cache-control'].startswith('private')
        assert calls[-1]==(job.endpoint,job.remote_id,4,camera)
    # Authentication, job binding, event identity, camera and terminal-state checks precede network IO.
    photo=bean['photos']['CamL'];other=prepared(x,image_mode='stereo')
    count=len(calls)
    assert client.get(photo.replace(op,other)).status_code==404
    assert client.get(photo.replace('/4/','/6/')).status_code==404
    assert client.get(photo.replace('CamL','Other')).status_code==404
    assert len(calls)==count
    guest=TestClient(client.app);assert guest.get(photo,follow_redirects=False).status_code==303;guest.close()
    transport.fail=True;assert client.get(photo).status_code==503
    with x.db.read() as s:
        assert before==[(p.id,p.payload) for p in s.scalars(select(Particle))]
        assert original_events==[(e.sequence,e.data) for e in s.scalars(select(MachineEvent))]
    client.close()


def test_combined_history_keeps_earlier_photos_when_latest_addition_saves_none(system):
    x=system
    first,c1,p1,cid,url1=finished(x)
    crop={'sequence':4,'kind':'images','created_at':'2026-11-18T08:00:01+00:00',
          'data':{'bean_sequence':100,'frame_index':20,'paths':['stereo/100/00000020-CamL.png','stereo/100/00000020-CamR.png']}}
    middle,c2,p2,_,url2=finished(x,destination=cid,destination_gross='1.700',image_mode='stereo',extra_events=[crop])
    latest,client,p3,_,url=finished(x,destination=cid,destination_gross='2.300')
    with x.db.read() as s:
        profiles=[(p.id,deepcopy(p.data)) for p in s.scalars(select(Profile))]
        ledger=[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
        particles=[(p.id,deepcopy(p.payload)) for p in s.scalars(select(Particle))]
        middle_job=s.get(MachineJob,middle)
    page=client.get(url);assert page.status_code==200,page.text
    data=report(page)
    assert data['record_count']==9 and data['combined_contribution_evidence']
    assert [source['operation_id'] for source in data['contributions']]==[first,middle,latest]
    assert data['populations']['input']['count']==3
    assert data['populations']['observed_previous']['count']==6
    assert data['populations']['observed_pass_through']['count']==9
    for key,hist in data['populations']['observed_pass_through']['histograms'].items():
        assert sum(hist)+data['populations']['observed_pass_through']['unknown'][key]==9
    assert 'does not remove earlier saved photographs' in page.text
    args=dict(parse_qsl(urlsplit(url).query));args.update(feature='lab_l',bucket=0,basis='observed',population='pass_through')
    inspected=client.get('/jobs/'+latest+'/measurements/inspect',params=args).json()
    assert inspected['matching_count']==3 and inspected['photographed_count']==1
    assert inspected['missing_photo_reference_count']==2 and inspected['photo_ordering']=='saved-photo records first'
    beans=inspected['items'];assert len(beans)==3
    assert beans[0]['origin_operation_id']==middle and beans[0]['photos']['CamL'].startswith('/jobs/'+middle+'/')
    assert beans[0]['photo_frame_index']==20 and beans[0]['colours']['CamL']==[100,80,40]
    assert all(not b['photos']['CamL'] and not b['photos']['CamR'] for b in beans[1:])
    assert {b['origin_operation_id'] for b in beans}=={first,middle,latest}  # same bean_sequence, different runs
    calls=[]
    class Images:
        def crop(self,*args):calls.append(args);return b'\x89PNG\r\n\x1a\nfixture'
    client.app.state.machine_jobs.transport=Images()
    assert client.get(beans[0]['photos']['CamL']).status_code==200
    assert calls==[(middle_job.endpoint,middle_job.remote_id,4,'CamL')]
    # Single-run views and older immutable profiles keep their own cutoff and scope.
    single=report(client.get(url+'&scope=run'))
    assert single['record_count']==3 and not single['combined_contribution_evidence']
    assert report(client.get(url2))['record_count']==6
    assert report(client.get(url1))['record_count']==3
    previous=client.get('/jobs/'+latest+'/measurements/inspect',params={**args,'population':'previous'}).json()
    assert previous['matching_count']==2 and previous['photographed_count']==1
    latest_only=client.get('/jobs/'+latest+'/measurements/inspect',params={**args,'population':'input'}).json()
    assert latest_only['matching_count']==1 and latest_only['photographed_count']==0
    assert latest_only['items'][0]['origin_operation_id']==latest and latest_only['items'][0]['photos']['CamL'] is None
    with x.db.read() as s:
        assert profiles==[(p.id,p.data) for p in s.scalars(select(Profile))]
        assert ledger==[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
        assert particles==[(p.id,p.payload) for p in s.scalars(select(Particle))]
    for c in (c1,c2,client):c.close()


def test_cyclic_pinned_contribution_history_is_rejected(system):
    x=system;op,adapter=started(x)
    adapter.transport.events=events();adapter.tick()
    key=identity('profile')
    with x.db.write() as s:
        s.add(Profile(id=key,operation_id=op,stream='pass_through',version=1,
            data={'destination_combination':{'previous_profile_id':key,'previous_portion_id':None}}))
    with x.db.read() as s:
        with pytest.raises(DomainError,match='Cyclic'):context(s,op,key)
