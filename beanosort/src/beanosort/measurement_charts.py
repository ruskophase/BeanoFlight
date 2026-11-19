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


def _run_context(session, operation_id, profile_id=''):
    operation=session.get(Operation,operation_id);job=session.get(MachineJob,operation_id)
    run=session.scalar(select(Run).where(Run.operation_id==operation_id))
    if not operation or not job or not run:raise DomainError('This job has no retained machine measurements')
    profile=session.get(Profile,profile_id) if profile_id else None
    if profile_id and (not profile or profile.operation_id!=operation_id or not profile_url(session,profile)):
        raise DomainError('Choose a measured profile from this job; remainder composition is unknown')
    query=select(Particle.id,Particle.source_key,Particle.source_sequence,Particle.payload['features'],
                 Particle.payload['decision'].as_string(),Particle.payload['action'].as_string(),
                 Particle.payload['machine_evidence']['bean_sequence'].as_integer()).where(Particle.run_id==run.id)
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
    rows=[{'id':pid,'key':key,'sequence':seq,'features':features,'passed':choice=='pass' and action=='not_issued',
           'bean_sequence':bean,'operation_id':operation_id,'run_id':run.id}
          for pid,key,seq,features,choice,action,bean in session.execute(query.order_by(Particle.source_sequence))]
    frozen_count=profile.data.get('counts',{}).get('tracked') if profile else None
    if frozen_count is not None and len(rows)!=frozen_count:
        raise DomainError('Retained records do not match this historical profile; its evidence has not been replaced')
    linked=None
    if profile and profile.portion_id:
        assignment=session.scalar(select(Assignment).where(Assignment.portion_id==profile.portion_id,Assignment.active==True))
        linked=session.get(Container,assignment.container_id) if assignment else None
    return {'operation':operation,'machine_job':job,'run':run,'profile':profile,'collection':collection,
            'container':linked,'recipe':session.get(Version,operation.recipe_id),'rows':rows}


def context(session, operation_id, profile_id='', scope='population'):
    if scope not in {'population','run'}:raise DomainError('Choose population evidence or this run only')
    ctx=_run_context(session,operation_id,profile_id);sources=[ctx];limitations=[];seen={profile_id} if profile_id else set()
    profile=ctx['profile']
    combined=bool(scope=='population' and profile and profile.data.get('destination_combination'))
    while combined and profile.data.get('destination_combination'):
        before=profile.data['destination_combination'];prior_id=before.get('previous_profile_id')
        if not prior_id:
            limitations.append({'portion_id':before.get('previous_portion_id'),'reason':'Earlier contents have no pinned population profile'})
            break
        if prior_id in seen:raise DomainError('Cyclic contribution profile history; observations cannot be pooled')
        seen.add(prior_id);previous=session.get(Profile,prior_id)
        if not previous or previous.portion_id!=before.get('previous_portion_id') or previous.created_at>profile.created_at:
            raise DomainError('The pinned earlier contents profile does not match this contribution history')
        if not profile_url(session,previous):
            limitations.append({'portion_id':previous.portion_id,'reason':'Earlier contents have no independently scoped machine observations'})
            break
        try:prior=_run_context(session,previous.operation_id,previous.id)
        except DomainError as exc:
            limitations.append({'portion_id':previous.portion_id,'reason':str(exc)});break
        sources.insert(0,prior);profile=previous
    previous_rows=[];used={row['id'] for row in ctx['rows']}
    for source in sources[:-1]:
        for row in source['rows']:
            if row['passed'] and row['id'] not in used:previous_rows.append(row);used.add(row['id'])
    latest=ctx['rows'];rows=previous_rows+latest
    report=population_report(latest,previous_rows=previous_rows if combined else None)
    contexts=[]
    for source in sources:
        p=source['profile'];job=source['machine_job']
        contexts.append({'operation':source['operation'],'run':source['run'],'profile':p,'job':job,
            'record_count':len(source['rows']),'image_mode':job.request.get('image_mode'),
            'source':job.request.get('source'),
            'url':f'/jobs/{job.operation_id}/measurements?scope=run&population=pass_through'+(f'&profile_id={p.id}' if p else '')})
    report['contributions']=[{'operation_id':source['operation'].id,'run_id':source['run'].id,
        'profile_id':source['profile'].id if source['profile'] else None,'record_count':source['record_count'],
        'source':source['source'],'image_mode':source['image_mode'],
        'settings_sha256':source['job'].request.get('settings_sha256'),
        'calibration_id':(source['job'].request.get('configuration') or {}).get('id')} for source in contexts]
    signatures={(source['machine_job'].request.get('machine_id'),
                 (source['machine_job'].request.get('configuration') or {}).get('id'),
                 source['machine_job'].request.get('settings_sha256'),source['machine_job'].request.get('source')) for source in sources}
    return {**ctx,'rows':rows,'report':report,'evidence_sources':contexts,'combined_evidence':combined,
        'evidence_limitations':limitations,'scope':scope,'mixed_configuration':len(signatures)>1,
        'scope_ids':{'input':{row['id'] for row in latest},'pass_through':{row['id'] for row in rows if row['passed']},
                     'previous':{row['id'] for row in previous_rows}}}


def value(row, key):
    number=row['features'].get(key)
    if number is None or isinstance(number,bool):return None
    try:number=float(number)
    except (TypeError,ValueError):return None
    return number if math.isfinite(number) else None


def population_report(rows, *, previous_rows=None):
    universe=(previous_rows or [])+rows
    definitions={}
    for key,spec in METRICS.items():
        known=[v for row in universe if (v:=value(row,key)) is not None]
        step=spec['step'];lower=math.floor(min(known)/step)*step if known else 0
        width=max(step,math.ceil((max(known)-lower+step)/80/step)*step) if known else step
        count=math.floor((max(known)-lower)/width)+1 if known else 1
        definitions[key]={**spec,'edges':[round(lower+n*width,8) for n in range(count+1)],
            'count':len(known),'unknown':len(universe)-len(known),'median':statistics.median(known) if known else None,
            'mean':statistics.mean(known) if known else None,'sigma':statistics.pstdev(known) if known else None,
            'method':'one retained feature aggregate per tracked bean within its source run; count weighting; bins [lower, upper)'}
    populations={}
    subsets=[('input',rows),('observed_pass_through',[row for row in universe if row['passed']])]
    if previous_rows is not None:subsets.append(('observed_previous',previous_rows))
    for name,subset in subsets:
        histograms={};unknown={}
        for key,spec in definitions.items():
            histograms[key]=[0]*(len(spec['edges'])-1);unknown[key]=0
            for row in subset:
                v=value(row,key)
                bucket=next((i for i,(lo,hi) in enumerate(zip(spec['edges'],spec['edges'][1:])) if v is not None and lo<=v<hi),None)
                if bucket is None:unknown[key]+=1
                else:histograms[key][bucket]+=1
        populations[name]={'count':len(subset),'histograms':histograms,'unknown':unknown}
    series=[{'key':'input','title':'Tracked run observations','colour':'#64748b'},
            {'key':'pass_through','title':'Intended Pass-through','colour':'#bc6414'}]
    if previous_rows is not None:series=[
        {'key':'input','title':'Latest addition observations','colour':'#64748b'},
        {'key':'previous','title':'Earlier contents observations','colour':'#206a55'},
        {'key':'pass_through','title':'Combined retained observations','colour':'#bc6414'}]
    return {'schema':'beanosort-machine-measurement-charts/v2' if previous_rows is not None else 'beanosort-machine-measurement-charts/v1',
        'measurement_evidence':True,'record_count':len(universe),
        'definitions':definitions,'populations':populations,'recipe':{'relation':'pass_all'},
        'series':series,'combined_contribution_evidence':previous_rows is not None,
        'basis':'image-derived per-track aggregates; all tracked records in the denominator; missing features remain unknown',
        'physical_confirmation':False,'physical_census':False,'unresolved_routing':sum(not row['passed'] for row in rows)}


def inspect(session, ctx, *, feature, bucket, population, basis, page):
    if feature not in METRICS or population not in {item['key'] for item in ctx['report']['series']} or basis!='observed' or page<1:
        raise DomainError('Choose a recorded measurement interval and population')
    spec=ctx['report']['definitions'][feature]
    if not 0<=bucket<len(spec['edges'])-1:raise DomainError('Unknown chart interval')
    lo,hi=spec['edges'][bucket:bucket+2]
    matches=[row for row in ctx['rows'] if row['id'] in ctx['scope_ids'][population] and
             (v:=value(row,feature)) is not None and lo<=v<hi]
    from .machine_photos import references
    jobs={source['job'].operation_id:source['job'] for source in ctx['evidence_sources']}
    sequences={op:[] for op in jobs}
    for row in matches:sequences[row['operation_id']].append(row['bean_sequence'])
    photos={op:references(session,jobs[op],beans) for op,beans in sequences.items()}
    def photo(row):return photos[row['operation_id']].get(row['bean_sequence'])
    photographed=sum(bool(photo(row)) for row in matches)
    if ctx['combined_evidence']:matches.sort(key=lambda row:not bool(photo(row)))
    pages=max(1,math.ceil(len(matches)/24));page=min(page,pages);items=[]
    for row in matches[(page-1)*24:page*24]:
        particle=session.get(Particle,row['id']);machine=particle.payload.get('machine_evidence',{})
        raw=machine.get('features',{});colours={}
        for camera,prefix in (('CamL','caml'),('CamR','camr')):
            rgb=[raw.get(prefix+'_mean_'+channel) for channel in 'rgb']
            colours[camera]=[round(v) for v in rgb] if all(type(v) in (float,int) and math.isfinite(v) and 0<=v<=255 for v in rgb) else None
        items.append({'bean_sequence':machine.get('bean_sequence',row['sequence']),'bean_id':row['key'],'count':1,'recorded_passages':1,
            'feature_value':value(row,feature),'photos':{'CamL':None,'CamR':None},'colours':colours,
            'sample_count':machine.get('sample_count'),'missed_target_passages':0,
            'origin_operation_id':row['operation_id'],'origin_run_id':row['run_id'],
            'origin_url':f'/jobs/{row["operation_id"]}/measurements?scope=run',
            'origin_source':jobs[row['operation_id']].request.get('source'),
            'origin_image_mode':jobs[row['operation_id']].request.get('image_mode')})
        if ref:=photo(row):items[-1].update(photos=ref['photos'],photo_frame_index=ref['frame_index'])
    live=ctx['machine_job'].request.get('source')=='live'
    return {'measurement_evidence':True,'population':population,'basis':basis,'title':spec['title'],'unit':spec['unit'],
        'matching_count':len(matches),'reference_bean_count':len(matches),'count_basis':'tracked records','count_unit':'tracked record',
        'photographed_count':photographed,'missing_photo_reference_count':len(matches)-photographed,
        'photo_ordering':'saved-photo records first' if ctx['combined_evidence'] else 'source sequence',
        'page':page,'pages':pages,'items':items,'run_id':ctx['run'].id,'physical_confirmation':False,
        'evidence_label':('Image-derived observations across contributions; each record and photograph retain their source run. ' if ctx['combined_evidence'] else
            'Image-derived aggregates from live camera observations. ' if live else 'Image-derived aggregates from recorded replay; not a present physical passage. ')+
            'Pass-through membership is intended, not chute-verified. Colour swatches use recorded per-camera RGB statistics. '+
            'Available photos show the earliest saved crop pair for each track; chart values may aggregate several frames. Photos do not verify container arrival.'}


def register_routes(app, db, user_for, render):
    from fastapi import Request
    from fastapi.responses import HTMLResponse,JSONResponse

    @app.get('/jobs/{key}/measurements',response_class=HTMLResponse)
    def charts(request:Request,key:str,population:str='pass_through',profile_id:str='',scope:str='population'):
        user=user_for(request)
        with db.read() as session:
            ctx=context(session,key,profile_id,scope)
            if population not in {item['key'] for item in ctx['report']['series']}:raise DomainError('Choose a recorded contribution population')
            return render(request,'measurement_charts.html',db_session=session,user=user,population=population,
                inspection_url=f'/jobs/{key}/measurements/inspect?scope={scope}'+(f'&profile_id={profile_id}' if profile_id else ''),**ctx)

    @app.get('/jobs/{key}/measurements/inspect')
    def inspection(request:Request,key:str,feature:str,bucket:int,population:str='pass_through',basis:str='observed',page:int=1,profile_id:str='',scope:str='population'):
        user_for(request)
        with db.read() as session:
            ctx=context(session,key,profile_id,scope)
            return JSONResponse(inspect(session,ctx,feature=feature,bucket=bucket,population=population,basis=basis,page=page),
                                headers={'Cache-Control':'no-store'})
