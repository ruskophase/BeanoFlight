# Architecture and reversible decisions

## Single-chart population evidence views

`chart_view.options` provides presentation-only `?chart=<feature>` links for
machine evidence, reference sorting results and retained portion pages. Existing
handlers still resolve the report, historical profile and inspection URL using the
same scope. Only known report features are accepted. The return URL is built from
the current local path/query, excluding `chart`; no arbitrary return target or
Referer is used. No schema, ledger, evidence aggregation or crop API change.

The templates render one SVG and collapse background provenance. They retain the
full report JSON and show an explicit evidence summary. `populations.js` reuses the
same interval/photo inspector, carries optional `view_*` presentation parameters
through title/back links and restores valid choices without changing observations.
SVG coordinates resize with the focused plot; ordinary chart coordinates remain
unchanged. Focused layout uses the full page width for the chart, followed by a
responsive photo/colour grid in normal page flow. The DOM and keyboard order match
this chart-first layout on all screens; there is no sticky sidebar or inner photo
scroll area. Detailed evidence stays expandable. Images stay on-demand and
retain their original source identity, availability and evidence labels.

## Job-scoped manual high-speed camera

`manual_fastcam.html`/`manual_fastcam.js` provide a separate operator tab linked
from live jobs. Authenticated role/CSRF-protected BeanoSort routes proxy status,
bounded JPEG preview and explicit commands to the job's pinned machine endpoint.
They never expose the machine bearer token or controller client ID to the browser.
Camera commands have unique request IDs and no activation outbox/retry worker.

`machine_jobs.manual_fastcam.ManualCamera` is attached only to live runners. Its
single worker serializes camera-only requests with the runner's existing lease
identity; it never acquires a second ESP32 lease or touches feed/valve/stereo timing
controls. A `take_manual` boundary joins the scheduled capture worker's cleanup
before enabling manual actions, so the old timer cannot stop a later manual
recording. The immutable original plan remains; manual changes are explicit journal
events. GPIO33 enable/disable uses existing validated controller timing, with no
mid-run timing configuration endpoint. The runner owns final cleanup.

The machine API persists command identity/intent before queueing work, deduplicates
identical requests and rejects reused IDs with changed payloads. Validation of busy
state happens before reserving a new ID. Network calls run outside capture and
heartbeat threads and database transactions. A failed/lost reply is not retried
automatically. Camera state, requested/result events and shutdown uncertainty remain
separate; none establishes physical bin membership or changes stock quantities.
Status GETs are read-only; late replies cannot overwrite a newer command result.
The UI preserves dirty fields, avoids replacing its form, caps preview at 5 fps,
pauses preview in hidden tabs and shows unavailable state on connection loss.

## Optional FastCamUSB scheduling

The collapsed `fastcam_prepare.html` section loads current camera defaults lazily
through authenticated `/machine-fastcam/defaults` → machine API
`/v1/fastcam-defaults` → existing Waveform Generator `/api/fastcam/status` and
`/api/state`. These are GETs, not settings application. External triggering is the
only forced default. `beanosort.fastcam.plan` validates operator fields and pins a
`beano-fastcam-plan/v1` object inside the existing immutable job manifest. The
machine validates it again before accepting a new run. No new migration/settings
service is required. Unchecked plans are omitted; replay rejects them.

`beanoflight.machine_jobs.fastcam.ScheduledCapture` runs bounded camera HTTP IO on
its own worker. The existing controller lease authorizes it. Optional GPIO33 timing
is configured while all capture outputs are off, before paired-camera startup.
After live source priming, camera/settings readiness is verified with feeding off.
The first acknowledged feed start fixes a monotonic epoch; resume never resets it.
The recording window uses elapsed time through pauses, not accumulated feed time.
External recording arms before enabling GPIO33 and disables it before disarming.
Early end/stop signals the worker without network IO in the frame path; cleanup
finishes outside frame processing and before releasing the controller.

Requested settings/times, reported settings/trigger state, command acknowledgements,
actual request/ack offsets, failures and file references remain separate in journal
events/status. These are command/recorder reports, not independently verified
physical captures. The auxiliary worker never issues valve plans. Startup camera
failure prevents feeding; a later auxiliary error does not invent a new machine
network-loss policy. Activation is not retried after uncertainty. Repeated job-start
requests use the existing manifest identity and cannot create a second worker;
restart recovery never silently resumes the recording timer. Pi files stay external
to relational rows. USB and tracking clocks/identities are not automatically joined.

Recorder hardening is maintained under `tools/fastcamusb/`: a compressed-frame
writer, checksum-guarded patches for the standalone Pi app, installer and tests.
The bounded queue includes in-flight frames and bytes, defaults to 5 seconds/64 MiB,
and writes a versioned diagnostics sidecar with first-failure time, counts, queue
peaks and bounded write-stall/sample histories. `CameraStream` stays unchanged.
Controller status scopes a recorder to its owning stream, preventing historical
errors from poisoning the next session. No schema or inventory mutation is involved.

`ScheduledCapture` additionally has a read-only telemetry monitor. Its GET can
time out independently while the schedule worker issues STOP on its own deadline;
late replies after cleanup starts are ignored. Source failure timestamps and
server observation time remain distinct, and monitoring loss means unknown state.
Recording failure and planned-window interruption are separate status fields.
Shutdown can be acknowledged despite a persisted recorder error. No later auxiliary
error changes the main sorter's continue/stop policy.

## Read-only machine population charts

`measurement_charts.py` reads the already ingested `Particle` feature aggregates
for a `MachineJob`/`Run`; it does not query a controller or import a
capture/hardware startup path. Authenticated GET routes under
`/jobs/{id}/measurements` provide five histograms and a paginated numeric/colour
inspector. `populations.js` shares the reference view's interactive charts and
supports explicit series names without relabelling live evidence as simulation.
Links on job/container/portion pages are derived, including for existing immutable
profiles. No schema migration, backfill or historical-profile mutation is needed.

Each record contributes once, regardless of its image sample count. Histograms use
count weighting, aligned half-open bins and a visible unknown category in the
denominator. Definitions retain units, edges and known/missing statistics. Area and
volume are paired-view proxies, not measured mass; other plotted features use
available camera aggregates as recorded by the existing adapter. Camera RGB swatches
come from the original numeric payload; absent photographs are never substituted.
Configuration/calibration references remain pinned and cross-run comparability
is not assumed. All-pass membership is intended, not physically confirmed.

Historical views filter by the profile's server evidence-receipt time, independently
of source event time. Frozen tracked counts must match retained records; a mismatch
is an explicit error. Where a collection exists its existing sequence boundary
scopes the chart; unknown association is rejected. Shared-source profiles retain
an explicit limitation on unprocessed material. Remainders do not receive an invented per-bean chart. This is derived
display data and performs no writes, re-simulation or mass/count reconciliation.

For combined machine output profiles, `context` follows only the immutable
`destination_combination.previous_profile_id` chain. It checks the pinned portion,
profile chronology and cycles, and reads each run at its own profile cutoff and
collection boundary. Stable particle IDs prevent double-counting retained records.
It does not sweep arbitrary feed ancestry, assign old photos to re-sorted tracks,
or claim a known subset after an unobserved split. Unsupported/unprofiled earlier
contents remain an explicit limitation linked to their portion.

The derived v2 combined report aligns bins for latest/previous/combined observation
series and retains contributing run/profile/configuration references. It weights
records, not stock masses or assumed physical source fractions. Differing source
or machine/settings/calibration references are flagged; compatibility is not
inferred. `scope=run` preserves the single-run view. Neither report changes stored
profile versions or their composition/release assertions.

`machine_photos.py` separately links the retained `images` events to each record's
originating run/bean identity. It chooses the earliest saved frame, returns authenticated
same-origin crop URLs, and fetches bytes only when a visible image is requested.
`Transport.crop` performs a bounded, timed PNG GET against the job's pinned machine
endpoint, outside the database read transaction. The machine API resolves only
committed crop events of terminal stereo jobs under that run's directory; arbitrary
paths, RAW files and symlink escapes are rejected. It limits each image to 4 MiB.
No capture code, per-bean upload or inventory write is added. Photos supplement
the frozen numerical profile; an image sample need not equal its multi-frame
aggregate. Combined interval inspection resolves references per original job,
orders saved-crop records first and retains missing-photo records in pagination.
Reference lookups are chunked to respect SQLite parameter limits; image bytes
still load only on demand. Failed photo loads retain the RGB swatch and an
unavailable message.

## Browser waiting feedback

`workflow_busy.js` presents a shared native dialog for pending form requests and
same-origin page links. Specific messages cover execution boundaries, weighing,
reconciliation, release and population loading. Native form submission/redirects,
validation, CSRF/idempotency fields and server transaction boundaries are preserved;
inputs are not disabled or resent. Duplicate submissions are suppressed while the
dialog is active. Error/new pages and history restoration clear it. Local wizard
events, anchors, downloads and new-tab/external links keep their normal behaviour.
HTML attributes are read explicitly because controls named `method`/`target` mask
form DOM properties.

The spinner uses CSS animation: JavaScript evaluation/timers in an outgoing page
may be suspended during native navigation. It makes no server-progress, heartbeat,
percentage, remaining-time or hardware-state claim. Background simulation keeps its
actual stored-particle progress and stop control; chart-inspection loading remains
local. There is no new per-particle request, server job service or machine dependency.

## Container changes within one job (migration 0012)

`SortingCollection` binds a distinct fill to one pinned `Destination`, container,
logical stream and code (`selected`, `selected_2`, etc.). It is an intermediate
contents identity while inventory remains WIP, not an additional posted stock
balance. Each physical bin appears once per job. A completed positive fill points
to its resulting portion; an occupied bin with zero addition keeps its old portion.
The code lets existing output/measurement uniqueness remain intact.

`SortingSegment` is append-only: fixed recipe, operator/evidence, optional local
source time, inclusive first source sequence and selected/pass collection bindings.
An unchanged collection spans several segments. Sequence windows, not receipt time,
scope profiles, mass predictions, galleries, count estimates and retained whole-bean
sampling for a later sorting pass. Unknown real boundaries produce unknown membership
for the affected job's collection analysis, never an invented timing association.
Intervals can be empty. Late events fall into their recorded source interval.

`segments.py` implements paused-only replacement in one short accounting transaction.
It checks feeding/clear/readiness declarations, pin consistency, unique destinations,
released compatible stock and available nominal capacity, then reserves new bins and
their initial stock. All prior claims remain active until reconciliation. No bin
reuse, source replacement or recipe change is allowed. Simulation derives its cutoff
from a contiguous committed prefix. Ingestion checks a generation segment marker so
work generated before a change cannot be committed into the wrong collection;
acknowledged old batches still replay idempotently without redrawing outcomes.

`CollectionWeighing` stores immutable versioned gross/tare/operator readings for
removed bins. Closed fills can move to storage with an audited physical confirmation;
they remain claimed and unavailable, including if their ledger assignment is still
the pinned starting population. Shared source-return bins cannot be early-weighed
or moved because they remain the feed. Collection labels resolve job history even
after the physical vessel is reused later.

`segmented_weighing.py` builds one final row per collection and a distinct source
remainder unless Source is already a collection. It binds review proofs to the exact
collection set, final segment, tare revisions and latest saved-reading versions.
`segmented_reconciliation.py` posts all measured outputs once, with starting contents,
source remainder, explicit loss/retention and signed unexplained difference. Source
mass and any shared-source tare occur once. Existing append-only ledger, ancestry,
quality release and transaction locks apply. Profiles freeze the scoped evidence;
whole-run summaries remain separate. Weight estimates never rewrite particles or
measured quantities. General accepted-weight corrections for these jobs are deferred.

SQLite and PostgreSQL constraints validate bindings/recipe/lifecycle and protect
segments/readings and collection identity from mutation. Migration 0012 only creates
new tables/guards: legacy jobs bootstrap collections lazily at their first swap, in
the same transaction. No historical particle, inventory or recipe backfill occurs.
BeanoSort's pause/resume is a workflow record. There is no controller stop/start,
new automatic network-loss policy, fill-level detection or physical verification.

## Hopper-emptying workflow

The immutable reference-recipe mode `operation_mode=hopper_emptying`,
`relation=pass_all` bypasses property-based selection, including unknown features.
It retains existing feature snapshots/deterministic samplers; every generated
passage has `decision=pass`, `action=not_issued`. No capture trial or hardware
command is introduced. The current preset is simulation-scoped.

Selected is absent. Different Source/Pass-through vessels require two weighings;
a shared vessel requires one. The existing occupied-destination DB guard pins the
source's full assignment/reservation in raw destination readiness, with
`source_return=true`. No schema migration is needed. Normalized material baselines
exclude this self-assignment from additional stock, reusing its one reservation.
Start/resume/reconciliation validate the pin. The original portion is consumed
once and one child takes the shared vessel; remaining source beans are included
in its final total. Hopper-return mass and separate unprocessed remainder stay
unknown, rather than being invented zero readings.

Weighing proofs require exactly the distinct participating roles. Source return
preserves population inheritance and limits run charts to generated passages;
subsequent simulation resolves the inherited whole source. Ordinary recipes keep
their existing guards. Captions combine Source / Pass-through and show the final
net mass once. Replayed ingestion/completion remain idempotent.

## Occupied sorting destinations (migration 0011)

`collections.py` pins each destination's active contents identity, ledger mass,
profile and assignment. These are reserved under the sorting job before a destination
claim is inserted. Migration 0011 replaces the empty-only database guard with a
guard requiring matching pinned contents and an active full-mass reservation for an
occupied sorting destination. Active reservation/destination/assignment uniqueness,
quality checks, accounting lock and ledger non-negativity remain enforced.

There is one sorter feed. Existing destination beans are additional material inputs
only when a positive net addition causes a combination at reconciliation. Starting
marks all reserved material WIP; cancellation/editing before start only releases
claims. Final gross/tare/net measurements include prior contents. Reconciliation
stores feed mass, starting destination masses, final totals, derived additions and
signed discrepancy. Its complete boundary is feed plus starting contents. Unchanged
destination stock appears on both sides of that weighing boundary but is not posted
as consumed or duplicated as new output. `feed_input` distinguishes actual feed from
additional consumed inputs using the pinned destination identities.

A positive addition consumes that destination's previous portion, ends its assignment
and creates a held combined portion with lineage from both the feed and prior contents.
Ledger postings and baseline input links commit atomically with reconciliation;
retry keys prevent duplicate completion. Release approves estimated/unknown evidence
under the pinned policy. Zero addition retains the old identity. Below-starting
destination masses require later reviewed removal/rework accounting; the old
single-input correction form is explicitly blocked for occupied-destination jobs.

Compatible simulation profiles combine whole reference records using estimated count
contributions (mass divided by each input's mean bean mass). Original observations
and images stay immutable. Stored rational weights preserve joint properties;
version-2 weighted sampling scales a SHA256 draw across the full cumulative weight
range. Existing v1 modulo sampling/replay remains pinned and unchanged. Mixed-profile
charts and inspectors show before/added/combined estimates, not purported chute
verification. Compatible collected populations can be replayed; unknown/incompatible
evidence yields unknown composition rather than a fabricated distribution.

`simulation.js` excludes duplicate destination/source assignments and restores choices
after changes. Readiness distinguishes physical empty/clean confirmation from approval
of existing contents. No controller, actuator or per-bean network dependency is added.

## Population-aware subsequent simulation passes

`population_sources.py` resolves released single-source bulk populations. A prepared
re-sort appends a `Profile(stream="simulation_input", version=1)` snapshot without
a portion assignment or stock posting. Its immutable JSON pins dataset/fingerprint,
source portion/profile, source run/operation evidence, compact reference-sequence
weights, mass/count assumptions and a content fingerprint. Existing completed runs
need no migration/backfill: their persisted synthetic destinations supply the weights.
Received stock preserves the original v1 deterministic sampler and old run replay.

New runs pin that input profile in `Run.source_versions.reference_population`.
The weighted SHA256 sampler draws whole records with replacement from that population
only. A cumulative integer-weight lookup avoids expanding per-particle records in
memory. Generation and ingestion validate against the same pinned sampler; stable
run/seed/sequence keys make restart, out-of-order replay and retries deterministic.
Each passage uses the existing recipe/feature logic and one new capture trial.
Archived identity/photo reuse is not permanent physical recognition. The new recipe's
reference thresholds stay fixed; no statistic is retroactively re-centred on its input.

Mean mass for count estimation comes from the retained source count density where
available, otherwise its area-weighted population mean. Gross prediction keeps the
original reference area/mass calibration and expands a reduced test count using
the source-specific mean. The remainder shares that source mean and representativeness
assumption. New settings may select a new capture probability while earlier mass
evidence and results remain immutable. Original particles, profiles and measurements
are never rewritten; ordinary ledger postings and new child portions preserve lineage.

Transferred whole portions retain eligibility. Representative split children and
unprocessed remainders inherit relative frequencies with explicit shared assumptions;
sampling weights are not stock balances or duplicated observed counts. No new material
tables/migration are needed. Held/unreleased stock, incompatible datasets, empty
synthetic memberships, unrepresented splits and blend/package/rework inputs remain
guarded. Multi-input blend replay remains a later extension.

## Interactive population inspection

`population_inspection.py` provides authenticated read-only JSON inspection for
recipe previews (`/recipes/{id}/inspect`) and reference runs
(`/jobs/{id}/populations/inspect`). Each request validates the feature, exact
histogram interval, population and evidence basis. It returns at most 24 distinct
archived beans per page with weighted passage totals, paired photo references and
recorded per-view mean RGB samples. Missing measurements remain unknown. Runs use
SQL grouping/filtering of their recorded features/decisions/actions/destinations;
raw reference features never fill gaps in a run's observed feature schema.

Expected contributors use the pinned capture probability once per valid attempted
target; intentional passes contribute only to Pass-through. Simulated queries use
the persisted destination. The inspector groups repeated passages into their
source photo pair and retains multiplicity separately. The bin total matches the
corresponding plotted count. Recipe-page inspection URLs pin the displayed capture
model so concurrent settings edits cannot change an already-open preview's weights.
No database rows, stock balances or historical profiles are modified.

`populations.js` hides/shows series with native legend checkboxes, rescales axes,
highlights the clicked interval and fills a left-hand inspector. The nearest
visible line determines its initial population; an explicit population selector
can compare the same interval. The panel stacks above charts on narrow screens.
Pagination bounds image loading, aborted/outdated fetches cannot replace a newer
selection, and fetch failure has a retry state distinct from an empty population.
All labels/bean identifiers use text nodes rather than inserting untrusted HTML.
No new dependency or schema migration is needed.

## Gross container weighing (no schema change)

`weighing.py` supplies the three-container review model; `weigh.html`,
`weigh_review.html` and `weighing.js` provide gross entry, exact milligram tare
subtraction, confirmation and signed discrepancy review. The POST review performs
no inventory writes. A signed, operator/job-bound review expires after one hour.
Acceptance validates current container revisions/tares and posts one idempotent
reconciliation. Any missing legacy tare is recorded atomically with that posting.
Release is a separate short transaction; a failed release leaves held outputs for
supervisor review. Retrying acceptance does not duplicate either posting/release.

Reference simulations pin `mean-area-proportional-bean-mass/v1`: a bean at the
reference arithmetic mean projected area has the pinned mean mass; other masses
scale linearly with area. This is an assumption, not a fitted mass calibration.
Expected stream masses weight each valid action attempt by p / (1−p); intentional
passes enter Pass-through. Grouping immutable reference-sequence/decision/action
records avoids loading all images or ORM particles for weighing predictions.
An explicitly reduced test fixture represents planned_mass / mean_mass beans;
the recorded expansion factor is prediction-only. Source remaining mass is the
original boundary minus planned mass × recorded/declared passages. Interrupted
run coverage remains incomplete. Rounding individual suggestions to 0.001 kg
does not force a balanced result.

Accepted measurements retain gross, tare, net, confirmation, container revision,
operator and timestamps. Reconciliation accounting stores the exact reviewed
model/settings/dataset/capture references and signed difference. Child profiles
add `mass_count_estimate` / `estimated_count` / `count_per_kg`, with a common
assumption-dependence group. Counts divide entered net mass by the expected
stream's mean mass; remainder uses the reference-wide mean. Original particle
features, photos, histograms and stochastic destinations are never rewritten.

A supervisor/admin may explicitly approve a nonzero discrepancy in the isolated
simulation only. The original policy ID/tolerance, signed difference and acceptance
reason remain in `simulation_discrepancy_approved` audit evidence. Real inventory,
quality holds and composition permissions retain their existing release guards.
Sampling is absent from this simplified flow; retained material and declared losses
remain explicit optional accounting terms, and unexplained differences stay distinct.

## Photographed reference simulation (migration 0010)

Area recipes also support `projected_area_geomean_mm2_median` in mm². Older
immutable dataset registrations may lack area statistics in their metadata even
though the original JSONL contains the measurement. New recipes derive/pin those
statistics from the verified original records without changing the registration.
Sampling uses the dataset's pinned feature definitions plus the recipe's explicitly
added metric; older five-feature runs reproduce their original payloads exactly.

Retirement appends a `recipe_retired` audit and idempotency command. Current
catalogues/new preparations exclude those version IDs, including server-side
validation of manually submitted IDs. Existing job/configuration/profile history
is retained. Prepared-job correction requires a current replacement recipe;
existing jobs retain their original pinned execution version. No version row or
foreign-key reference is deleted or edited, and no schema migration is needed.

`reference_datasets` registers immutable, fingerprinted snapshots. Full aggregate
bean JSONL, source manifest, photo hash index and representative images live under
`var/reference-datasets/<fingerprint>/`; relational rows retain metadata, feature
definitions/statistics and a snapshot location. Original exports are read-only.
`references.py` imports existing measurements; it performs no detection, tracking,
classification model inference or image processing.

Recipes use ordinary append-only `Version(kind='recipe')` records with schema
`beanosort-reference-recipe/v1`. They pin dataset identity/fingerprint, exact source
field, known-value statistics, population σ, strict comparison, multiplier and
thresholds. The first rule is `combined_lab_l_mean < median − 2σ`. Missing values
are unresolved. A recipe is separate from its job's capture-model version.

Run source metadata pins the dataset/sampler/rule; SHA-256 over sampler version,
dataset fingerprint, run ID, seed and passage sequence chooses one whole source row
uniformly with replacement. Logical passage IDs stay run-scoped; source bean IDs
are references, not permanent recognition of new physical material. All properties
and photos share one source row and one persisted capture destination. Ingestion
rejects rows differing from the deterministic sample, independently of batch replay.

Feature evidence is simulation with explicit image-derived origin, while decisions,
assumed actions and monitor events remain simulated. No synthetic arrivals count as
physical confirmation. Raw source features remain available for future joint queries.
Population reports use aligned, versioned bins with count-weighted expectations,
separate simulated destinations and explicit unknowns; completed profiles freeze
the report. Measured mass remains entirely independent of predicted bean counts.

The catalogue preview evaluates each original bean once with the current capture
assumption; run reports describe the ingested simulated passages using their pinned
model. Local SVG histograms offer percentage/count and expected/simulated views.
Photo routes require sign-in and verify registered file hashes. No raw arbitrary
filesystem paths or remote photo URLs are exposed as fetch endpoints.

Reference recipes execute only in isolated simulation and accept received stock,
retained simulated outputs, unprocessed remainders and representative split children.
The population-source adapter above preserves their composition for further passes.
Blend/package/rework replay remains guarded pending its own adapter. Legacy fixture jobs retain their
original generator and configuration; registering a reference does not rewrite them.

## Placement and sequence

The workspace root is not a Git repository. BeanoFlight, BeanoFastCap and
ESP_Web_controller are separate component repositories with existing uncommitted
changes. No applicable AGENTS.md files were found in the workspace or its parents.
BeanoSort is an independent Python subproject in `BeanoFlight/beanosort/`; it does
not alter the existing distribution, entry points, dependencies or control path.

Implementation sequence: relational foundations → evidence/capture functions →
transactional workflow → operator pages → demo and recovery/concurrency tests.
FastAPI follows the existing ESP Web Controller convention; Jinja/HTML/CSS avoids
adding a JavaScript build chain. SQLAlchemy and Alembic support PostgreSQL as the
shared-server database, plus an explicit SQLite development option. One modular
server, no broker, cloud dependency or paid service.

## Domain and accounting

| Entity | Meaning / invariant |
|---|---|
| Receipt / source lot | Material entering the environment, with optional supplier/origin/type data; distinct from contents identity |
| Portion | Independently handled population; stock, WIP or consumed; release/hold separate |
| Container / assignment | Reusable vessel, one active portion; historical assignments remain; readiness is a separate per-job declaration |
| Operation / inputs / outputs | General many-to-many transformation boundary, receiving, sorting, transfer, split, blend, packaging, profiling and reviewed corrections |
| Ledger | Immutable signed integer milligram postings; balance is a sum, never an editable weight |
| Reservation / destination claim | Prevent concurrent source/destination allocation; reservations do not consume material |
| Measurement | Entered value with evidence kind, method, device, time, tare basis and optional resolution; independent of count predictions |
| Reconciliation | Input, outputs, remainder, retained mass, loss, sample and signed unexplained difference; never fabricated waste |
| Lineage | Parent→new child edges with operation; acyclic; known ancestry does not assert exact proportions |
| Station | Named capability boundary; supports simulation or manual records; no hardware methods |
| Version | Separate kinds for recipe, capture model, reconciliation policy and product/packaging definitions; immutable handling plans pin ingredients and profile IDs; append-only |
| Run / batch / particle | Namespaced source run and logical particle keys, immutable compact features and decisions/actions |
| Profile | Versioned evidence snapshot, pinned at reconciliation/release; later evidence cannot rewrite it |
| Audit | Operator, station where applicable, UTC time, operation and reason |

All public material commands take a short database transaction. Phase 1 serializes
mutations on the database's singleton world row (`SELECT FOR UPDATE` on PostgreSQL).
This deliberately favours correctness over concurrent write throughput. SQLite uses
`BEGIN IMMEDIATE`, including reads, to avoid its legacy deferred transaction races.
The lock is released before a run processes more data or awaits operator action.
These choices follow the [SQLAlchemy SQLite transaction guidance](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html)
and [SQLite isolation contract](https://www.sqlite.org/isolation.html).

Database constraints enforce unique active assignments, placements, reservations,
destination claims, source sequences, commands and operation outputs. Triggers
protect immutable records, pinned operation configuration, negative ledger balances,
reservation eligibility, destination claims and cyclic lineage. PostgreSQL trigger
checks also lock the relevant portion/container. Normal application users have no
SQL access; database ownership remains a trusted administrative capability.

A job reserves its **whole source portion** even when intended processing mass is
smaller. Starting makes the portion WIP and unavailable. Reconciliation consumes
that identity and creates positive-mass Selected, Pass-through, remainder and retained
children as applicable. A remainder returns to the source vessel under a new ID;
retained material remains held at the station. Zero-mass streams have no empty
material portion. Samples/losses are explicitly recorded removals, not available stock.
Manual splits use the same superseding-parent strategy; transfers preserve identity.

Entered output mass may disagree with predicted counts without changing either.
Release requires supervisor/admin approval, explicit estimated/unknown-composition
acceptance, no explicit quality holds, and the pinned discrepancy policy. There is
no requirement for nonexistent chute verification. A quality hold blocks subsequent
use; supervisors may record reviewed hold disposition without bypassing reconciliation.
Corrections of unreleased sorting weights consume the old output identities into
new corrected children and append a Correction; original measurements remain intact.

## Execution and evidence

Creation validates and immediately prepares a job; a saved Draft is deferred.
Prepared → Running → Finishing → Awaiting reconciliation → Completed, with
Running ↔ Paused and Running/Paused → Failed → Finishing. Only Prepared may be
cancelled as a reservation-only action. Failures after starting preserve WIP,
reservations and material facts. Finishing requires explicit in-flight accounting
confirmation before reconciliation. Execution state and release state are separate.

Real and simulation are **different databases**, guarded by an immutable environment
marker. Synthetic receipt/measurement evidence is refused in real mode and vice
versa. Execution method is independently stored. Real workflow state is a manual
record, never a machine command. The current synthetic fixture makes no use of
hardware or competing tracker/classifier logic.

The capture model is an immutable version: p defaults to decimal 0.97. An independent
SHA-256 seeded draw uses model algorithm, full run ID and logical particle key.
Frames and valve multiplicity are irrelevant. All attributes use the one destination;
misses enter Pass-through. No exact percentage is imposed. Expected profiles use
Decimal p×attempted and intentional-pass+(1−p)×attempted. Selected decisions without
action evidence enter an unresolved category. A real attempt requires controller
evidence. Classifier scores are never reinterpreted as calibrated probabilities.

Simulated expected arrivals, chute detections, associations and timeouts are distinct
events. Their clocks are synthetic relative milliseconds, not calibrated chute
timings. The adapter protocol reserves room for ambiguous associations and detector
fault/unavailable events; no physical monitor implementation exists. Physical
confirmed count remains zero. Real-mode profiles never draw per-bean destinations.

Profiles preserve denominators, count weighting, histogram boundaries, unit/schema
definitions, unknown fractions, overlapping defect categories, joint shape/brightness
bins, recipe/model references and evidence kinds. Compact features remain queryable.
The fixture intensity/shape bins are explicitly synthetic definitions; a real adapter
must declare a compatible mapping, never assume cross-machine comparability. Volume
proxies do not become mass. Moisture and physical census coverage remain unknown.
Remainder/retained children do not duplicate the parent's observed particle count.

## Reliability and scope

Receipt, preparation and reconciliation commands require stable content-checked
idempotency keys. Run starts and releases are idempotent; ingest batches and logical
particles have separate identity/content checks. Exact replays after completion do
nothing. New late evidence is rejected explicitly after the accounting boundary;
returned count samples create new profiles without editing the historical sorting snapshot. Out-of-order batches are accepted
before that boundary. Source ordinal gaps are exposed; real completeness remains
unknown. Malformed batches roll back entirely and report an error.

The offline adapter snapshots completed Registry schema 3 runs read-only, writes
synced compact batches and a final hash manifest, and pins that source manifest on
the workflow run. Interrupted imports resume by content-checked batch keys. It
retains the Registry cursor as provenance, not as a live subscription checkpoint.
Synthetic batches regenerate from the immutable run specification. Background live
consumption and machine network-loss behaviour are unchanged and unimplemented here.

## Manual material handling

HandlingPlan is immutable: source quantities/profile IDs, destination readiness,
policy and optional specification version are pinned. Whole portions are reserved;
split ingredients first for a partial blend contribution. Start moves them to WIP.
Only a prepared plan can cancel without accounting. Transfers append placement
history with the same material identity and no ancestry edge. Other operations
consume inputs and create held child portions, measurements and reconciliation.
Input ledger quantities are not relabelled as newly measured input weights. Actual
blend contributions are recorded separately and must match the reserved whole portions.

A mass withdrawal does not copy the parent's observed count. With an explicit
representativeness assumption it inherits distributions and, if justified, a
count-per-mass estimate; children share a dependence group and source profile.
A returned counted/weighed sample can establish an estimated count density. The
sample-return confirmation prevents an unrecorded stock removal. This does not
establish cross-machine calibration comparability.

Blend predictions weight count properties by estimated ingredient counts, not mass.
All ingredients need count-density evidence and a compatible profile schema/key;
otherwise composition stays unknown. Real archive comparability conservatively
remains unknown until a validated calibration mapping exists. Synthetic fixtures
are comparable only within their version. Product constraints currently support
count-based category fractions, units and evidence requirements. Unsupported
constraints are rejected. Unknown is never a compliance pass; prediction passes
are never verified compliance. A known failure or missing required evidence blocks
release. General stock with unknown composition may release under explicit policy
approval without any claim of specification compliance. Unknown loss composition
or unexplained blend discrepancy invalidates the output composition prediction.

Packaging pins SKU/format/nominal mass, creates a product-lot identity and immutable
per-pack actual net quantities, accounts for remaining bulk and losses, and retains
ancestry. Pack count, bean count and bulk mass remain separate. A collection vessel
contains one product-lot portion. Opened-pack rework, separate placement of individual
packs and returns are not implemented. Batch estimates do not verify each package.

Large images/statistics stay external; the source bundle keeps references and hashes.
No optimizer, automatic feeding/packaging, physical monitor, or independent image
processing pipeline is implemented. Arbitrary manual-operation corrections and
partially executed multi-input blends requiring separate remainders remain held
for a later reviewed rework workflow; no software cancellation erases their WIP.
## Simulation assumptions added in migration 0004

`simulation_settings` holds immutable versions of a material description, integer
mean mass in micrograms, capture-model version, author, reason and research context.
`simulation_assumptions` pins a settings version and a calculated count basis to a
receipt or prepared simulation job. Both tables reject updates/deletes in SQLite
and PostgreSQL. Changes use the same short accounting transaction and audit ledger
as other commands, with optimistic comparison of the previous settings ID plus an
idempotency key. A conflicting editor cannot silently overwrite a newer version.

The initial green coffee mean is 0.15 g; the initial new-simulation capture rate is
99%. Original 97% capture models, demos and historical profiles remain intact.
Receipts opt into a named mean assumption; unknown is retained otherwise. A prepared
simulation uses planned processing mass, not the whole reserved boundary, to compute
`mass_mg × 1000 / mean_mass_ug`. The non-integer expectation is preserved, with
half-up rounding solely for the synthetic fixture size and approximate display.
Explicit test-count overrides are recorded separately in immutable run metadata.
No estimate creates a stock movement, physical observation, or measured moisture.

Bulk ingestion validates and deduplicates the complete bounded batch before inserting
particles and then their monitor events. Automatic UI generation makes sequential
250-particle requests with saved progress; it is not a machine control loop. Stable
run/particle/seed identities and fixed batch boundaries preserve outcomes after
reconnect or replay. The full profile is refreshed after generation stops rather
than recalculated on every batch. A million-particle development limit rejects larger
automatic fixture sizes explicitly; it is not a claim of tested production capacity.

## Container registration weights (migration 0005)

New registrations require positive capacity and tare as integer milligrams. Capacity
is nominal **net material** mass; no relationship such as tare < capacity is assumed.
The creation audit supplies operator, timestamp, units and simulation/operator-declared
tare provenance. Receipt measurements retain their independent mass and tare basis.
The database keeps nullable fields for historical unknowns, rejects new registrations
with either value absent, and rejects clearing a previously recorded value. This
preserves existing containers and their lineage without fabricating tare or capacity.

## Container edits and retirement (migration 0006)

Container IDs remain permanent when names, locations, types or weights change.
`archived` excludes a vessel from new assignments; `revision` supports optimistic
concurrency checks under the existing short database accounting lock. Edits and
retirement commands use idempotency keys and append audits of previous/new values.
Current tare evidence is taken from the latest audit containing a tare declaration;
past material measurements and ledger postings remain immutable.

Sources reserved for jobs, work-in-progress containers and active destinations are
locked against metadata edits or removal. Occupied stock may be edited; capacity
reductions below its current material balance are prohibited. Database guards prohibit
occupied/claimed archives, new active assignments to archives and those reductions.
Foreign keys and service validation prohibit physical deletion of used containers.
Supervisors/admins can delete only unused empty registrations, retaining a deletion
audit that resolves the old scanned ID. Used empty vessels are archived/restored.

## Storage locations (migration 0007)

`locations` contains stable IDs, names and normalized unique name keys. A container
has one `location_id` FK. The existing display string remains for compatible exports;
database guards require it to match the referenced location name. Existing names are
registered and linked without rebuilding containers or touching material records.
Case/outer-space variants map to one canonical name. New databases initialize Floor
and LoadingBay; trusted legacy CLI calls that provide a name resolve/register it
transactionally. New browser forms use registered IDs.

Locations group containers only; their membership/counts are relational queries.
They do not own stock, stations, quantity ledgers or workflow steps. A storage move
changes only container placement/revision and appends an operator-declared/synthetic
audit with prior/new location. It leaves assignments, material identities and measured
masses intact. Existing global short-transaction locking, revision checks and command
keys prevent conflicting/repeated moves; active jobs and archived vessels reject moves.
Location editing and empty deletion are described in the management update below.

## Guided supplier receiving

The browser now uses a four-step source → containers → net readings → storage flow.
`Workflow.receive_loads` posts a single receiving operation/receipt/source lot with
one independently handled output portion per empty vessel (up to 20 per commit).
Supplier bags never become inventory. The boundary equals the sum of entered net
masses, not an assumed 60 kg. Receiving makes no particle-count/profile assumptions.

Both receiving commands accept net mass up to 120% of nominal capacity. The limit
uses integer milligrams (`capacity_mg * 6 // 5`); no authoritative float arithmetic
or measurement clipping occurs. The browser receives the same integer-derived limit.
Measurement/audit snapshots retain policy `nominal-capacity-120-percent/v1`, nominal
capacity, permitted receiving mass and excess over nominal. Existing receipts remain
unchanged. New output loads in other material operations retain their existing
capacity checks. Remainders in the original vessel may retain its existing overfill,
bounded by the operation's original input mass rather than adding another allowance.
Migration 0009 allows location/name/tare/revision updates of already overfilled
vessels, while retaining database protection against capacity reductions below
the current mass. No ledger or measurement rows are rewritten by that migration.

Known registered tare is snapshotted, together with container revision and the
operator's confirmation that the empty vessel was zeroed on the scale. Net bean
readings are not reduced by tare again. The UI accepts kg at a 0.001 kg entry
increment; that reporting format is not asserted as device resolution or accuracy.
Mass provenance is derived from the isolated database environment; no operator
simulation/device/resolution selector is required. Real input uses operator-entered
scale measurement, and training input remains explicitly synthetic in the records.

Receiving also lists empty legacy vessels without tare/capacity. The operator may
complete only missing weights inline; no value is inferred. The transaction checks
the submitted container revision, validates positive exact weights and the 120%
receiving limit, audits the completed configuration, and posts the receipt together.
Any error rolls back both configuration completion and inventory. Existing weights
cannot be overwritten through receiving. The UI disables/hides choices selected
in other rows and restores them on deselection/removal; server duplicate and
occupancy/reservation checks remain authoritative. Unavailable containers are
listed with their blocking reason instead of being unexplained omissions.

All containers/quantities/locations are validated before postings. One short locked
transaction posts measurements, ledger, assignments, confirmed placement changes and
audit, with command-key replay. Invalid second loads roll back the entire receipt;
competing users cannot both assign the same container. Final placement is confirmed
before stock is committed. The browser keeps entries in memory between steps; drafts
are not stored/reserved before final submission. A receipt page links all output
portions and containers to their shared source. Legacy receiving commands/evidence
remain intact; historical quantities with finer precision are displayed exactly.


## Location and empty-container management (migration 0008)

Locations have revisions and audited rename/delete commands. A database trigger
synchronizes renamed locations to linked container display strings and increments
container revisions. The location ID and material identities do not change. A
location can be deleted only when it has no registered containers, including
archived ones; the database and command enforce this. Its deletion audit keeps old
links resolvable and permits reuse of the name for a new location ID. Initializing
configuration does not recreate deliberately deleted default locations.

Any empty unclaimed container is deletable after confirmation. An unused registration
can be physically deleted; a used registration is marked deleted and detached from
current storage, with its last recorded location retained as historical text/audit.
Assignment/job foreign keys remain intact, and scans resolve the deletion plus
material history. Database guards prohibit deleting occupied/claimed vessels or
assigning new active contents/destinations to deleted vessels. Listings and all
workflow choices exclude deleted containers. Archive/restore remains available for
temporary retirement. Permanent container IDs and historical names stay reserved
when a used registration is retained.

## Prepared sorting job corrections

Pre-run corrections use `edit_prepared_sorting` and `delete_prepared_sorting` under
the existing short accounting lock. Both require kind sorting, state prepared,
no Run, no outputs/ledger postings, and operator confirmation of no processing.
Edit reuses the same preparation validation/claim creation as a new job. It releases
old claims, validates/reserves the replacement and cancels the old operation in a
single transaction; failure rolls everything back. Input/configuration/assumption
records stay append-only, with before/after job snapshots and cross-linked audits.
Old IDs cannot start after replacement; new IDs isolate execution identities.

Delete cancels and releases claims with a `job_deleted` audit. The current job list
excludes deleted/replaced requests while their URLs remain available. No material
ledger movement or physical actuation occurs. The original pinned settings are
prefilled in the editor, including legacy capture models; planned-mass estimates
are recreated on the new operation. Immutable executed-run rules are unchanged.
