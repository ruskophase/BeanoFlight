"""Rebuildable, transactionally maintained summaries of immutable run evidence."""
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from .capture import expected_profile
from .models import Operation, Particle, Run, RunSummary, SortingCollection, SortingSegment, Version

SCHEMA = 'beanosort-run-summary/v1'
RUN = 'run'


def get_summary(session, run_id, collection=None):
    row = session.get(RunSummary, (run_id, collection.id if collection else RUN))
    if row is None or row.data.get('schema') != SCHEMA:
        return None
    if row.data.get('profile', {}).get('counts', {}).get('tracked') != row.particle_count:
        return None
    return row


def _context(session, run, environment):
    operation = session.get(Operation, run.operation_id)
    model = session.get(Version, operation.capture_model_id).data
    reference = None
    if run.source_versions.get('reference_population'):
        from .references import validate_recipe
        recipe = session.get(Version, operation.recipe_id).data
        reference = (validate_recipe(session, recipe), recipe)
    return model, reference, environment == 'simulation' and not run.source_versions.get('machine_job')


def _batch(items, context):
    model, reference, synthetic = context
    result = {'profile': expected_profile(items, model, synthetic=synthetic),
              'reference': None, 'mass_groups': {}, 'membership_groups': {}, 'destinations': {}}
    if reference:
        from .references import population_report
        dataset, recipe = reference
        result['reference'] = population_report(items, dataset, recipe, model)
    for item in items:
        payload = item['payload']
        sequence = (payload.get('reference_bean') or {}).get('bean_sequence')
        key = f"{sequence if sequence is not None else '-'}:{payload['decision']}:{payload['action']}"
        result['mass_groups'][key] = result['mass_groups'].get(key, 0) + 1
        destination = item.get('simulated_destination') or 'unknown'
        result['destinations'][destination] = result['destinations'].get(destination, 0) + 1
        membership = f"{sequence if sequence is not None else '-'}:{destination}"
        result['membership_groups'][membership] = result['membership_groups'].get(membership, 0) + 1
    return result


def _sum(target, incoming):
    """Add counts only; pinned schemas, definitions and evidence metadata are retained."""
    for key, value in incoming.items():
        if isinstance(value, dict):
            _sum(target.setdefault(key, {}), value)
        elif isinstance(value, list):
            previous = target.setdefault(key, ['0']*len(value))
            if len(previous) != len(value):
                raise ValueError('Summary histogram definitions differ')
            target[key] = [str(Decimal(a)+Decimal(b)) for a, b in zip(previous, value)]
        elif isinstance(value, int):
            target[key] = target.get(key, 0) + value
        else:
            target[key] = str(Decimal(target.get(key, '0'))+Decimal(value))


def _empty(session, run, scope, batch, *, reset=False):
    row = session.get(RunSummary, (run.id, scope))
    if row is None:
        row = RunSummary(run_id=run.id, scope_id=scope, revision=1)
        session.add(row)
    elif not reset:
        return row
    else:
        row.revision += 1
    row.particle_count, row.last_sequence = 0, None
    row.data = {'schema': SCHEMA, **deepcopy(batch)}
    return row


def _apply(row, batch, particles):
    data = deepcopy(row.data)
    profile, incoming = data['profile'], batch['profile']
    for field in ('counts', 'expected', 'histograms'):
        _sum(profile[field], incoming[field])
    for field in ('counts', 'histograms'):
        _sum(profile['synthetic_monitor'][field], incoming['synthetic_monitor'][field])
    for aspect, evidence in incoming['aspect_evidence'].items():
        profile['aspect_evidence'][aspect] = sorted(set(profile['aspect_evidence'][aspect]) | set(evidence))
    if data['reference'] is not None:
        _sum(data['reference']['populations'], batch['reference']['populations'])
    _sum(data['mass_groups'], batch['mass_groups'])
    _sum(data['destinations'], batch['destinations'])
    if 'membership_groups' in data:
        _sum(data['membership_groups'], batch['membership_groups'])
    row.particle_count += len(particles)
    if particles:
        last = max(item.source_sequence for item in particles)
        row.last_sequence = max(row.last_sequence, last) if row.last_sequence is not None else last
    row.revision += 1
    row.data = data


def _windows(session, run):
    periods = list(session.scalars(select(SortingSegment).where(SortingSegment.operation_id == run.operation_id)
                                  .order_by(SortingSegment.ordinal)))
    if not periods or any(period.first_sequence is None for period in periods):
        return [], []
    return [period.first_sequence for period in periods], periods


def _partition(particles, starts, periods):
    result = {RUN: particles}
    if not periods:
        return result
    scopes = defaultdict(list)
    for particle in particles:
        index = bisect_right(starts, particle.source_sequence)-1
        if index < 0:
            continue
        period = periods[index]
        for key in {period.selected_id, period.pass_through_id} - {None}:
            scopes[key].append(particle)
    result.update(scopes)
    return result


def rebuild(session, run, environment, *, chunk_size=2000):
    """One bounded-memory pass, used for upgrades or repair; never on a GET."""
    context = _context(session, run, environment)
    empty = _batch([], context)
    starts, periods = _windows(session, run)
    scopes = {RUN} | {key for period in periods for key in (period.selected_id, period.pass_through_id) if key}
    rows = {key: _empty(session, run, key, empty, reset=True) for key in scopes}
    query = select(Particle.source_sequence, Particle.payload, Particle.simulated_destination).where(
        Particle.run_id == run.id).order_by(Particle.source_sequence)
    chunk = []
    def consume():
        partitions = _partition(chunk, starts, periods)
        memo = {}
        for scope, particles in partitions.items():
            identity = tuple(item.source_sequence for item in particles)
            if identity not in memo:
                memo[identity] = _batch([{'payload':p.payload, 'simulated_destination':p.simulated_destination} for p in particles], context)
            _apply(rows[scope], memo[identity], particles)
    with session.no_autoflush:
        for sequence, payload, destination in session.execute(query).yield_per(chunk_size):
            chunk.append(SimpleNamespace(source_sequence=sequence, payload=payload, simulated_destination=destination))
            if len(chunk) >= chunk_size:
                consume();chunk.clear()
        if chunk:
            consume()
    session.flush()
    return rows[RUN]


def ensure(session, run, environment):
    row = get_summary(session, run.id)
    return row if row is not None else rebuild(session, run, environment)


def add_particles(session, run, particles, environment):
    """Called once for newly inserted particles, in the same accounting transaction."""
    context = _context(session, run, environment)
    starts, periods = _windows(session, run)
    memo = {}
    for scope, members in _partition(particles, starts, periods).items():
        row = session.get(RunSummary, (run.id, scope))
        if row is None:
            raise RuntimeError('Missing run summary; rebuild before adding observations')
        key = tuple(item.source_sequence for item in members)
        if key not in memo:
            memo[key] = _batch([{'payload':p.payload,'simulated_destination':p.simulated_destination} for p in members], context)
        _apply(row, memo[key], members)
    session.flush()


def initialize_collections(session, run, environment):
    """The first collections inherit all prior passages; new collections start empty."""
    base = ensure(session, run, environment)
    empty = None
    for item in session.scalars(select(SortingCollection).filter_by(operation_id=run.operation_id)):
        if session.get(RunSummary, (run.id, item.id)) is not None:
            continue
        if item.ordinal == 1:
            row = RunSummary(run_id=run.id, scope_id=item.id, revision=base.revision,
                particle_count=base.particle_count, last_sequence=base.last_sequence, data=deepcopy(base.data))
            session.add(row)
        else:
            if empty is None: empty = _batch([], _context(session, run, environment))
            _empty(session, run, item.id, empty)
    session.flush()


def reference_groups(summary):
    groups = []
    for key, count in summary.data['mass_groups'].items():
        sequence, decision, action = key.split(':')
        groups.append((int(sequence) if sequence != '-' else None, decision, action, count))
    return sorted(groups, key=lambda item: (-1 if item[0] is None else item[0], item[1], item[2]))


def membership_weights(summary, stream):
    groups = summary.data.get('membership_groups') if summary else None
    if groups is None or sum(groups.values()) != summary.particle_count:
        return None
    weights = {}
    for key, count in groups.items():
        sequence, destination = key.split(':')
        if destination == stream and sequence != '-':
            weights[int(sequence)] = weights.get(int(sequence), 0) + count
    return [[sequence, weights[sequence]] for sequence in sorted(weights)]
