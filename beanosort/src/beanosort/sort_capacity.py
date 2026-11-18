"""Estimated receiver fill, audited operator overrides and workflow-only pauses."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select

from .capture import probability, simulated_destination
from .models import Audit, Container, Destination, IngestBatch, Particle, Run, SortingCollection, Version
from .segments import current, is_open, particle_filter


def override_limits(session, operation):
    limits = {}
    for entry in session.scalars(select(Audit).where(Audit.operation_id == operation.id,
            Audit.action == 'capacity_override').order_by(Audit.created_at, Audit.id)):
        for key, value in entry.data.get('next_pause_percent', {}).items():
            limits[key] = max(limits.get(key, 100), value)
    return limits


def overridden_containers(session, operation):
    return {cid for entry in session.scalars(select(Audit).where(Audit.operation_id == operation.id,
        Audit.action == 'capacity_override')) for cid in entry.data.get('container_ids', [])}


def snapshot(session, operation, environment):
    destinations = list(session.scalars(select(Destination).filter_by(operation_id=operation.id, active=True)))
    if not destinations:
        return []
    run = session.scalar(select(Run).filter_by(operation_id=operation.id))
    segment = current(session, operation)
    limits = override_limits(session, operation)
    reference = bool(run and run.source_versions.get('reference_population'))
    from .weighing import predicted_mass
    model = predicted_mass(session, operation, environment) if reference else None
    from .run_summaries import get_summary
    summary = get_summary(session, run.id) if run else None
    total = summary.particle_count if summary else session.scalar(select(func.count(Particle.id)).filter_by(run_id=run.id)) if run else 0
    result = []
    for destination in destinations:
        collection = session.scalar(select(SortingCollection).filter_by(destination_id=destination.id))
        if segment and (not collection or not is_open(segment, collection)):
            continue
        container = session.get(Container, destination.container_id)
        shared = bool(destination.readiness.get('source_return'))
        stream = collection.stream if collection else destination.stream
        starting = 0 if shared else destination.readiness.get('existing_mass_mg', 0)
        amount = None
        basis = 'Incoming mass unavailable; recorded starting contents only'
        if not run or not total:
            amount = Decimal(operation.boundary_mg if shared else starting)
            basis = 'Recorded starting contents; no incoming passages recorded'
        elif reference:
            projection = predicted_mass(session, operation, environment, collection=collection) if collection else model
            population = projection['populations'][stream]
            incoming = population.get('unrounded_net_mg', population['predicted_net_mg'])
            if incoming is not None and projection['available']:
                amount = Decimal(starting) + Decimal(incoming)
                if shared and collection:
                    # The shared vessel also contains unprocessed source material.
                    remainder = model['populations']['remainder'].get('unrounded_net_mg',model['populations']['remainder']['predicted_net_mg'])
                    if remainder is None: amount = None
                    else: amount += Decimal(remainder)
                basis = 'Estimated from reference area, pinned mean mass and expected capture; not a scale reading'
        elif environment == 'simulation' and run.expected_particles:
            query = select(func.count(Particle.id)).where(Particle.run_id == run.id,
                Particle.simulated_destination == stream)
            condition = particle_filter(session, collection) if collection else None
            if collection and condition is None:
                amount = None
            else:
                if condition is not None: query = query.where(condition)
                scoped = get_summary(session, run.id, collection)
                received = scoped.data['destinations'].get(stream,0) if scoped else session.scalar(query)
                amount = Decimal(starting) + Decimal(received)*operation.planned_mg/run.expected_particles
                if shared:
                    amount += Decimal(operation.boundary_mg) - Decimal(total)*operation.planned_mg/run.expected_particles
                basis = 'Estimated from simulated destinations and planned mass per test passage; not a scale reading'
        known = amount if amount is not None else Decimal(operation.boundary_mg if shared else starting)
        capacity = container.capacity_mg
        percent = known*100/capacity if capacity else None
        result.append({'destination_id': destination.id, 'container_id': container.id, 'label': container.label,
            'stream': 'Source / Pass-through' if shared else stream.replace('_', ' ').title(),
            'logical_stream': stream, 'shared_source': shared,
            'capacity_mg': capacity, 'mass_mg': int(known.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            'mass_exact_mg': str(known),
            'mass_known': amount is not None, 'basis': basis,
            'fill_percent': format(percent, '.1f') if percent is not None else None,
            'percent_exact': str(percent) if percent is not None else None,
            'level': 'red' if percent is not None and percent > 100 else 'orange' if percent is not None and percent >= 90 else 'normal',
            'next_pause_percent': limits.get(destination.id, 100),
            'resume_pause_percent': max(limits.get(destination.id,100),105+int((percent-100)//5)*5)
                if percent is not None and percent>=100 else limits.get(destination.id,100)})
    return result


def enforce(session, user, operation, environment):
    if operation.kind != 'sorting' or operation.status != 'running': return False
    exceeded = [row for row in snapshot(session, operation, environment) if row['percent_exact'] is not None
                and Decimal(row['percent_exact']) >= row['next_pause_percent']]
    if not exceeded: return False
    from .service import audit
    operation.status = 'paused'
    audit(session, user, 'capacity_pause', {'receivers': exceeded, 'hardware_action': 'none',
        'reason': 'Receiver reached its next nominal-capacity threshold; operator may override by resuming'}, operation)
    return True


def acknowledge(session, user, operation, environment):
    rows = [row for row in snapshot(session, operation, environment) if row['percent_exact'] is not None
            and Decimal(row['percent_exact']) >= 100]
    if not rows: return
    from .service import audit
    limits = {row['destination_id']: max(row['next_pause_percent'],
        105 + int((Decimal(row['percent_exact'])-100)//5)*5) for row in rows}
    audit(session, user, 'capacity_override', {'container_ids': [row['container_id'] for row in rows],
        'next_pause_percent': limits, 'receivers': rows, 'reason': 'Operator explicitly resumed with full receiving containers',
        'hardware_action': 'none'}, operation)


def generation_prefix(session, operation, run, rows):
    """Stop simulated generation at the first passage crossing a threshold."""
    fills = snapshot(session, operation, 'simulation')
    watched = [row for row in fills if row['capacity_mg'] and row['mass_known']]
    if not watched: return rows
    scale = Decimal(operation.planned_mg)/run.expected_particles
    reference = run.source_versions.get('reference_population')
    factor = None
    capture_model = session.get(Version, operation.capture_model_id).data
    capture = probability(capture_model)
    if reference:
        from .population_sources import input_profile
        profile = input_profile(session, operation.id)
        model = profile.data['mass_model'] if profile else {}
        area = model.get('reference_mean_area_mm2')
        if not area:
            from .references import load_dataset, metric_statistics, validate_recipe
            dataset = validate_recipe(session, session.get(Version, operation.recipe_id).data)
            beans, _ = load_dataset(dataset)
            area = metric_statistics(beans, 'area_mm2')['mean']
        if not area: return rows
        reference_mean = Decimal(model.get('reference_mean_mass_ug') or 150000)/1000
        input_mean = Decimal(model.get('input_mean_bean_mass_mg') or reference_mean)
        factor = scale/input_mean*reference_mean/Decimal(str(area))
    totals = {row['destination_id']: Decimal(row['mass_exact_mg']) for row in watched}
    segment = current(session, operation)
    for index, particle in enumerate(rows):
        if reference:
            area = particle['features'].get('area_mm2')
            if area is None: return rows
            weight = Decimal(str(area))*factor
            if particle['decision'] == 'selected' and particle['action'] == 'attempted':
                weights = {'selected': weight*capture, 'pass_through': weight*(1-capture)}
            elif particle['decision'] == 'pass': weights = {'selected': Decimal(0), 'pass_through': weight}
            else: return rows
        else:
            targeted = particle['decision'] == 'selected' and particle['action'] == 'attempted'
            destination = simulated_destination(run.id, particle['key'], run.seed, targeted,
                                               capture_model)
            weights = {'selected': scale if destination == 'selected' else Decimal(0),
                       'pass_through': scale if destination == 'pass_through' else Decimal(0)}
        for row in watched:
            shared = row['shared_source']
            addition = weights[row['logical_stream']]
            if shared:
                addition = addition-scale if segment or not reference else Decimal(0)
            totals[row['destination_id']] += addition
            if totals[row['destination_id']]*100 >= row['capacity_mg']*row['next_pause_percent']:
                return rows[:index+1]
    return rows


def first_batch_range(session, run):
    batch = session.scalar(select(IngestBatch).filter_by(run_id=run.id).order_by(IngestBatch.created_at, IngestBatch.id))
    if batch and batch.batch_key.startswith(('reference-v1:', 'fixture-v1:')):
        _, start, end = batch.batch_key.split(':')
        return int(start), int(end)
    return 0, min(250, run.expected_particles)


def final_overfill_allowed(session, user, operation, container, amount, *, requested=False):
    """Keep actual overfilled weights recordable after an explicit operator override."""
    if not requested and container.id not in overridden_containers(session, operation):
        return False
    from .service import audit
    audit(session, user, 'capacity_final_override', {'container_id':container.id,'actual_net_mg':amount,
        'nominal_capacity_mg':container.capacity_mg,'reason':'Operator accepted an over-capacity sorting container',
        'approval':'final weighing review' if requested else 'previous receiving-capacity override'}, operation)
    return True
