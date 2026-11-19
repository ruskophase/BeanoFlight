"""Operator-entered optional USB recording plan; no camera activation here."""
import math
from .quantities import DomainError


def number(data,key,low,high,*,integer=False):
    try:value=float(data.get('fastcam_'+key,''))
    except (TypeError,ValueError):raise DomainError('Enter a valid FastCamUSB '+key.replace('_',' ')) from None
    if not math.isfinite(value) or not low<=value<=high or integer and value!=int(value):
        raise DomainError('FastCamUSB '+key.replace('_',' ')+' is outside its supported range')
    return int(value) if integer else value


def plan(data,source,duration,cameras=None):
    if data.get('fastcam_enabled')!='on':return None
    if source!='live':raise DomainError('FastCamUSB recording is available only for live sorting jobs')
    start=number(data,'start_seconds',0,duration);end=number(data,'end_seconds',0,duration)
    if end<=start:raise DomainError('FastCamUSB end must be after start and within the job duration')
    width=number(data,'width',640,1280,integer=True)
    if width not in {640,1280}:raise DomainError('Choose a supported FastCamUSB resolution')
    output=str(data.get('fastcam_output_folder','')).strip();device=str(data.get('fastcam_device','')).strip()
    if not output or len(output)>4096 or len(device)>4096:raise DomainError('Enter a valid FastCamUSB output folder/device')
    settings={'device':device or None,'width':width,'fps':number(data,'fps',1,200),
        'exposure_ms':number(data,'exposure_us',100,100000)/1000,
        'preview_fps':number(data,'preview_fps',1,60,integer=True),'output_folder':output,
        **{key:data.get('fastcam_'+key)=='on' for key in ('external_trigger','disable_preview','disable_preview_during_recording')}}
    trigger=None
    if settings['external_trigger']:
        mode=data.get('fastcam_trigger_mode')
        if mode not in {'multiple','fixed'}:raise DomainError('Choose a FastCamUSB trigger mode')
        trigger={'mode':mode,'multiplier':number(data,'trigger_multiplier',1,16,integer=True),
            'fixed_frequency_hz':number(data,'trigger_fixed_frequency_hz',1,199.999),
            'pulse_us':number(data,'trigger_pulse_us',1,1000000,integer=True),
            'illumination':data.get('fastcam_trigger_illumination')=='on'}
        if mode=='fixed' and trigger['illumination']:raise DomainError('Shared illumination requires a multiple trigger rate')
        if cameras:
            period=round(1000000/cameras['frame_rate_hz'])
            slot=period//trigger['multiplier'] if mode=='multiple' else round(1000000/trigger['fixed_frequency_hz'])
            width=cameras.get('strobe_lead_us',0)+max(cameras.get('trigger_low_us',0),trigger['pulse_us'])+cameras.get('strobe_lag_us',0)
            if slot<=5000 or width>=slot:raise DomainError('FastCamUSB trigger pulses must fit and remain below 200 Hz')
    return {'schema':'beano-fastcam-plan/v1','time_basis':'elapsed_since_first_feed',
        'start_seconds':start,'end_seconds':end,'settings':settings,'trigger':trigger,
        'settings_evidence':'operator entry; defaults supplied by the current Waveform Generator / FastCamUSB listener'}


def register_routes(app,machine,user_for):
    from fastapi import Request
    from fastapi.responses import JSONResponse
    @app.get('/machine-fastcam/defaults')
    def defaults(request:Request):
        user_for(request)
        result=machine.transport.call(machine.endpoint(),'/v1/fastcam-defaults')
        return JSONResponse(result,headers={'Cache-Control':'no-store'})
