"""Read-only retained measurements and saved-crop references; no hardware IO."""
import math
import statistics

from sqlalchemy import select

from .models import Assignment, Container, MachineJob, Operation, Particle, Profile, Run, SortingCollection, Version
from .quantities import DomainError

METRICS = {
    'lab_l': {'title':'Lightness','unit':'CIE L*','step':1},
    'lab_chroma': {'title':'Chroma','unit':'CIE chroma','step':1},
    'size_mm': {'title':'Major axis (camera mean)','unit':'mm','step':.5},
    'area_mm2': {'title':'Projected area (two-view geometric mean)','unit':'mm²','step':2},
    'volume_proxy_mm3': {'title':'Ellipsoid volume proxy','unit':'mm³ proxy, not mass','step':10},
}


def profile_url(session, profile):
    """Derive links for old immutable profiles; do not backfill evidence rows."""
    if not session.get(MachineJob, profile.operation_id):return None
    data=profile.data
    if profile.stream in {'remainder','retained'}:return None
    stream=data.get('logical_stream') or data.get('applies_to') or profile.stream
    if stream not in {'run','pass_through'}:return None
    return f'/jobs/{profile.operation_id}/measurements?population={"input" if stream=="run" else "pass_through"}&profile_id={profile.id}'


def context(session, operation_id, profile_id=''):
    operation=session.get(Operation,operation_id);job=session.get(MachineJob,operation_id)
    run=session.scalar(select(Run).where(Run.operation_id==operation_id))
    if not operation or not job or not run:raise DomainError('This job has no retained machine measurements')
    profile=session.get(Profile,profile_id) if profile_id else None
    if profile_id and (not profile or profile.operation_id!=operation_id or not profile_url(session,profile)):
        raise DomainError('Choose a measured profile from this job; remainder composition is unknown')
    query=select(Particle.id,Particle.source_key,Particle.source_sequence,Particle.payload['features'],
                 Particle.payload['decision'].as_string(),Particle.payload['action'].as_string()).where(Particle.run_id==run.id)
    collection=None
    if profile:
        # This is the server's evidence-receipt cutoff, not a physical event time.
        # Later ingestion must never silently alter a released profile's charts.
        query=query.where(Particle.created_at<=profile.created_at)
        collection=session.scalar(select(SortingCollection).where(SortingCollection.operation_id==operation_id,
                                                                  SortingCollection.code==profile.stream))
        if collection:
            from .segments import particle_filter
            condition=particle_filter(session,collection)
            if condition is None:raise DomainError('Collection association is unknown; a per-bin chart cannot be established')
            query=query.where(condition)
    rows=[{'id':pid,'key':key,'sequence':seq,'features':features,'passed':choice=='pass' and action=='not_issued'}
          for pid,key,seq,features,choice,action in session.execute(query.order_by(Particle.source_sequence))]
    frozen_count=profile.data.get('counts',{}).get('tracked') if profile else None
    if frozen_count is not None and len(rows)!=frozen_count:
        raise DomainError('Retained records do not match this historical profile; its evidence has not been replaced')
    report=population_report(rows)
    linked=None
    if profile and profile.portion_id:
        assignment=session.scalar(select(Assignment).where(Assignment.portion_id==profile.portion_id,Assignment.active==True))
        linked=session.get(Container,assignment.container_id) if assignment else None
    return {'operation':operation,'machine_job':job,'run':run,'profile':profile,'collection':collection,
            'container':linked,'recipe':session.get(Version,operation.recipe_id),'rows':rows,'report':report}


def value(row, key):
    number=row['features'].get(key)
    if number is None or isinstance(number,bool):return None
    try:number=float(number)
    except (TypeError,ValueError):return None
    return number if math.isfinite(number) else None


def population_report(rows):
    definitions={}
    for key,spec in METRICS.items():
        known=[v for row in rows if (v:=value(row,key)) is not None]
        step=spec['step'];lower=math.floor(min(known)/step)*step if known else 0
        width=max(step,math.ceil((max(known)-lower+step)/80/step)*step) if known else step
        count=math.floor((max(known)-lower)/width)+1 if known else 1
        definitions[key]={**spec,'edges':[round(lower+n*width,8) for n in range(count+1)],
            'count':len(known),'unknown':len(rows)-len(known),'median':statistics.median(known) if known else None,
            'mean':statistics.mean(known) if known else None,'sigma':statistics.pstdev(known) if known else None,
            'method':'one retained feature aggregate per tracked bean; count weighting; bins [lower, upper)'}
    populations={}
    for name,subset in (('input',rows),('observed_pass_through',[row for row in rows if row['passed']])):
        histograms={};unknown={}
        for key,spec in definitions.items():
            histograms[key]=[0]*(len(spec['edges'])-1);unknown[key]=0
            for row in subset:
                v=value(row,key)
                bucket=next((i for i,(lo,hi) in enumerate(zip(spec['edges'],spec['edges'][1:])) if v is not None and lo<=v<hi),None)
                if bucket is None:unknown[key]+=1
                else:histograms[key][bucket]+=1
        populations[name]={'count':len(subset),'histograms':histograms,'unknown':unknown}
    return {'schema':'beanosort-machine-measurement-charts/v1','measurement_evidence':True,
        'definitions':definitions,'populations':populations,'recipe':{'relation':'pass_all'},
        'series':[{'key':'input','title':'Tracked run observations','colour':'#64748b'},
                  {'key':'pass_through','title':'Intended Pass-through','colour':'#bc6414'}],
        'basis':'image-derived per-track aggregates; all tracked records in the denominator; missing features remain unknown',
        'physical_confirmation':False,'physical_census':False,'unresolved_routing':sum(not row['passed'] for row in rows)}


def inspect(session, ctx, *, feature, bucket, population, basis, page):
    if feature not in METRICS or population not in {'input','pass_through'} or basis!='observed' or page<1:
        raise DomainError('Choose a recorded measurement interval and population')
    spec=ctx['report']['definitions'][feature]
    if not 0<=bucket<len(spec['edges'])-1:raise DomainError('Unknown chart interval')
    lo,hi=spec['edges'][bucket:bucket+2]
    matches=[row for row in ctx['rows'] if (population=='input' or row['passed']) and
             (v:=value(row,feature)) is not None and lo<=v<hi]
    pages=max(1,math.ceil(len(matches)/24));page=min(page,pages);items=[]
    for row in matches[(page-1)*24:page*24]:
        particle=session.get(Particle,row['id']);machine=particle.payload.get('machine_evidence',{})
        raw=machine.get('features',{});colours={}
        for camera,prefix in (('CamL','caml'),('CamR','camr')):
            rgb=[raw.get(prefix+'_mean_'+channel) for channel in 'rgb']
            colours[camera]=[round(v) for v in rgb] if all(type(v) in (float,int) and math.isfinite(v) and 0<=v<=255 for v in rgb) else None
        items.append({'bean_sequence':machine.get('bean_sequence',row['sequence']),'bean_id':row['key'],'count':1,'recorded_passages':1,
            'feature_value':value(row,feature),'photos':{'CamL':None,'CamR':None},'colours':colours,
            'sample_count':machine.get('sample_count'),'missed_target_passages':0})
    from .machine_photos import references
    photos=references(session,ctx['machine_job'],[item['bean_sequence'] for item in items])
    for item in items:
        if ref:=photos.get(item['bean_sequence']):
            item.update(photos=ref['photos'],photo_frame_index=ref['frame_index'])
    live=ctx['machine_job'].request.get('source')=='live'
    return {'measurement_evidence':True,'population':population,'basis':basis,'title':spec['title'],'unit':spec['unit'],
        'matching_count':len(matches),'reference_bean_count':len(matches),'count_basis':'tracked records','count_unit':'tracked record',
        'page':page,'pages':pages,'items':items,'run_id':ctx['run'].id,'physical_confirmation':False,
        'evidence_label':('Image-derived aggregates from live camera observations. ' if live else 'Image-derived aggregates from recorded replay; not a present physical passage. ')+
            'Pass-through membership is intended, not chute-verified. Colour swatches use recorded per-camera RGB statistics. '+
            'Available photos show the earliest saved crop pair for each track; chart values may aggregate several frames. Photos do not verify container arrival.'}


def register_routes(app, db, user_for, render):
    from fastapi import Request
    from fastapi.responses import HTMLResponse,JSONResponse

    @app.get('/jobs/{key}/measurements',response_class=HTMLResponse)
    def charts(request:Request,key:str,population:str='pass_through',profile_id:str=''):
        user=user_for(request)
        if population not in {'input','pass_through'}:raise DomainError('Choose input observations or intended Pass-through')
        with db.read() as session:
            ctx=context(session,key,profile_id)
            return render(request,'measurement_charts.html',db_session=session,user=user,population=population,
                inspection_url=f'/jobs/{key}/measurements/inspect'+(f'?profile_id={profile_id}' if profile_id else ''),**ctx)

    @app.get('/jobs/{key}/measurements/inspect')
    def inspection(request:Request,key:str,feature:str,bucket:int,population:str='pass_through',basis:str='observed',page:int=1,profile_id:str=''):
        user_for(request)
        with db.read() as session:
            ctx=context(session,key,profile_id)
            return JSONResponse(inspect(session,ctx,feature=feature,bucket=bucket,population=population,basis=basis,page=page),
                                headers={'Cache-Control':'no-store'})
