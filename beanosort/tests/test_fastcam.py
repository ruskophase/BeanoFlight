from copy import deepcopy
from dataclasses import replace
import pytest
from sqlalchemy import select

from beanosort.fastcam import plan
from beanosort.machine_jobs import install_recipe
from beanosort.models import MachineCommand,MachineJob,Operation
from beanosort.quantities import DomainError
from test_web import client_for,token


def fields():
    return {'fastcam_enabled':'on','fastcam_start_seconds':'10','fastcam_end_seconds':'40',
        'fastcam_width':'1280','fastcam_fps':'180','fastcam_exposure_us':'5000','fastcam_preview_fps':'30',
        'fastcam_device':'','fastcam_output_folder':'/test/recordings','fastcam_external_trigger':'on',
        'fastcam_disable_preview_during_recording':'on','fastcam_trigger_mode':'multiple',
        'fastcam_trigger_multiplier':'3','fastcam_trigger_fixed_frequency_hz':'180','fastcam_trigger_pulse_us':'50',
        'fastcam_trigger_illumination':'on'}


def test_optional_plan_validates_window_settings_units_and_live_scope():
    data=fields();p=plan(data,'live',60)
    assert p['start_seconds']==10 and p['end_seconds']==40 and p['settings']['exposure_ms']==5
    assert p['settings']['external_trigger'] is True and p['time_basis']=='elapsed_since_first_feed'
    assert plan({**data,'fastcam_enabled':''},'recorded',60) is None
    for changed in ({'fastcam_start_seconds':'40'},{'fastcam_end_seconds':'61'},
                    {'fastcam_fps':'nan'},{'fastcam_exposure_us':'0'}, {'fastcam_preview_fps':'30.5'},
                    {'fastcam_output_folder':''},{'fastcam_trigger_multiplier':'17'}):
        with pytest.raises(DomainError):plan({**data,**changed},'live',60)
    with pytest.raises(DomainError):plan(data,'replay',60)
    assert plan({**data,'fastcam_external_trigger':''},'live',60)['trigger'] is None


def test_preparation_pins_optional_settings_without_camera_commands(system):
    x=system;recipe=install_recipe(x.db);portion,_,*_ = x.receive('1')
    receiver=x.workflow.create_container(x.uid,label='USB test receiver',location='Test',capacity='10',tare_mass='.5')
    client=client_for(x);machine=client.app.state.machine_jobs
    machine.settings=replace(machine.settings,machine_jobs_token_file='fake-no-network')
    catalog={'machine_id':'test-machine','sources':[{'id':'live','name':'Live cameras','physical':True}],
        'maximum_duration_seconds':1800,'maximum_pause_seconds':600,'fastcam_capture_supported':True,
        'settings':[{'id':'a'*64,'name':'Test bundle','values':{'name':'Test','conveyor':{'speed_steps_per_second':20},
            'vibration_motor':{'duty_percent':38},'cameras':{'frame_rate_hz':59.5}}}]}
    machine.catalog=lambda:deepcopy(catalog);machine.endpoint=lambda:'http://fake:8091'
    calls=[]
    class Reader:
        def call(self,endpoint,path,body=None):
            calls.append((path,body));assert path=='/v1/fastcam-defaults' and body is None
            return {'settings':{'fps':180,'external_trigger':True},'trigger':{},'source':'test saved settings'}
    machine.transport=Reader()
    page=client.get('/jobs/new',params={'recipe_id':recipe})
    assert page.status_code==200 and '<details data-fastcam-options>' in page.text
    assert '<details data-fastcam-options open' not in page.text and calls==[]
    defaults=client.get('/machine-fastcam/defaults')
    assert defaults.status_code==200 and defaults.headers['cache-control']=='no-store'
    payload={**fields(),'csrf':token(page),'key':token(page,'key'),'portion_id':portion,'planned_mass':'1',
        'station_id':x.stations['Training sorter'],'recipe_id':recipe,'pass_through_id':receiver,'ready':'on',
        'policy_id':x.versions['reconciliation_policy'],'capture_model_id':x.versions['capture_model'],
        'machine_settings':'a'*64,'machine_source':'live','duration_seconds':'60','image_mode':'stereo'}
    result=client.post('/jobs',data=payload);assert result.status_code==200,result.text
    op=result.url.path.split('/')[-1]
    with x.db.read() as s:
        job=s.get(MachineJob,op);assert job.request['fastcam']==plan(fields(),'live',60)
        assert job.state=='prepared' and s.get(Operation,op).status=='prepared'
        assert list(s.scalars(select(MachineCommand)))==[]
    assert calls==[('/v1/fastcam-defaults',None)] and 'Optional FastCamUSB recording' in result.text
    client.close()
