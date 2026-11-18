# Phase status — 2026-11-18

## Live-machine population evidence charts — 2026-11-18

Completed All Pass-through machine jobs now expose **Population charts &
measurement evidence** on job, output portion and container pages. The former
interactive chart view was limited to archived-reference simulations. The new
read-only view uses already retained machine particle features, with five numeric
distributions, population-line toggles, count/percentage scales, known/missing
coverage, mean/median/population SD and interval inspection with camera RGB swatches.
Multiple measurements of one track are counted once. All-pass output membership
remains intended, not chute-verified. No capture-probability simulation is applied.

Existing immutable profiles are supported without a backfill: links are derived,
charts use the historical evidence-receipt cutoff, and missing collection
association/count mismatches fail explicitly. An occupied/shared-source container
gets a run-contribution caveat rather than an invented complete-bin census.
Recorded replay remains distinct from present physical observations; the inventory
environment is unchanged. No image-download interface was added. **No image saves**
means no photographs; numeric features and camera colour swatches are still useful.

Validation: **57 focused domain/HTML tests passed** across machine jobs, charts,
reference inspection, segmented jobs and retained populations (one existing
Starlette TestClient deprecation warning). The reference-simulation Chromium chain
passed unchanged using its synthetic reference fixture. A loopback preview using the operational SQLite database opened
with `mode=ro` and machine dispatch disabled passed all five charts, line toggles,
keyboard interval inspection, RGB swatches and 1440/768/390 px layouts with no
JavaScript errors/overflow. Its real completed one-minute run has **311 unique
tracked records, 290 with measurements and 21 unknown**; no images were saved.
No physical run, valve command, measurement reprocessing or PostgreSQL test was
performed for this display change.

The LAN WebGUI was restarted after verifying a consistent SQLite backup at
`var/backups/training-before-measurement-charts-20261118T052708Z.db`.
Material/configuration/profile table hashes, the current run's original particles
and the machine journal matched before/after restart. Machine-job and actuator
processes were left running; outputs were reported off and there were no pending
commands. Authenticated LAN GET checks verified links, charts and colour inspection
without altering material/profile data. Existing credentials remain unchanged.
Browser/results/deployment records are under ignored `var/qa/machine-measurements/`.

Changed components: `measurement_charts.py`, the new chart template, WebGUI route
registration/profile-link helper, profile/job templates, shared chart JavaScript,
focused tests and operator/architecture/integration/startup-test documentation.
No schema migration, quantity accounting or machine-control implementation changed.
Image retrieval and broader live recipe support remain separate later work.
An additional existing integration gap surfaced during test setup: migration 0012
only permits a null Selected segment for `hopper_emptying`, whereas the newer
machine recipe uses `all_pass_through`. Align that validation before claiming a
working live-machine container-swap path. No physical swap was attempted; the
new chart view rejects unknown collection association rather than inventing it.

## Visible waiting indicators for longer workflow actions — 2026-11-14

Execution-boundary transitions, weighing predictions/review, final reconciliation,
release and other server requests now show a shared animated spinner with an action
description. The panel prevents repeat clicks and stays visible through native
submission/redirects until the resulting page arrives. Native validation, error
pages and history restoration remain usable. Field values, CSRF/review tokens and
idempotency keys are preserved; inputs are not disabled, rewritten or resent.
The indicator makes no percentage, time-remaining or server-heartbeat claim.

Existing simulation progress remains based on saved particle counts, with Stop
generation still usable. Small local spinners accompany background simulation and
chart-bean loading; the final population refresh uses the shared panel. CSS animation
continues while native navigation suspends script evaluation. Reduced-motion and
print styles are supported. No backend, schema, quantity accounting or hardware
interface changed.

Validation: **37 focused domain/HTML tests passed**, and the existing full Chromium
operator chain passed without JavaScript errors or responsive overflow. The new
`browser_busy_smoke.py` also passed: six deliberately held real workflow requests
cover pause-error recovery, finishing, awaiting reconciliation, loading weighings,
review and final acceptance. Compositor screenshots changed during the wait,
confirming the animation actually continued. Tests cover invalid forms, preserved
payloads, duplicate-submit suppression, Back navigation, reduced motion, a single
reconciliation and mass conservation. Phone waiting-panel screenshots were inspected.
The known TestClient deprecation warning remains.

The running LAN WebGUI serves the updated shared templates/assets immediately;
no server restart or migration was needed. Authenticated GET-only live checks passed
for spinner animation, completion cleanup and desktop/tablet/phone layouts, with no
page errors. Every database table matched by count/hash before and after those
checks; no live material operation was posted. Credentials remain unchanged.
PostgreSQL and hardware checks were not repeated for this browser-only change.

Changed components: `workflow_busy.js`, shared base/dialog and CSS, simulation and
weighing feedback, the browser acceptance harness, and operator/architecture/test
documentation. Waiting feedback does not add an asynchronous server job service or
a physical machine status/verification capability.

## Reset and refresh between container moves — 2026-11-14

Successful moves from either entry point now return to a blank **Move container**
selector with a one-time confirmation. Selecting another vessel automatically loads
its current location, revision and destination choices. The old move form is disabled
during navigation; the ordinary Choose button remains a no-JavaScript fallback.
Move pages use `Cache-Control: no-store`, and browser-history restoration reloads
current data. Only the currently selected vessel's actual location is excluded.
The prior behaviour of retaining the moved vessel is superseded by this reset.

Updated routes/template, added `container_move.js`, and extended the existing move
and browser checks. **20 focused tests passed**, including duplicate POST recovery,
consecutive moves, moving a second vessel to the first one's former location,
returning the first vessel with a fresh revision/key, and unchanged contents/mass.
Chromium passed the expanded operator chain plus 768/390 px checks with no page
errors or overflow. The known TestClient deprecation warning remains. No accounting,
schema or hardware behaviour changed; PostgreSQL/hardware were not rerun.

The LAN WebGUI was restarted after the verified backup
`var/backups/training-before-move-form-reset-20261114T060400Z.db`.
Every database table matched by count/hash before/after restart. Credentials are
unchanged and health reports simulation with hardware_control=false.
Authenticated read-only LAN checks also passed: blank start/clear, automatic loading
of two actual containers, exact destination choices/current revisions, unchecked
confirmations and desktop/tablet/phone layout. No live move was recorded and all
database table hashes remained unchanged during these checks.

## Location container lists match Current populations — 2026-11-13

Opening a location now uses the same responsive table as **Current populations**:
linked container/location, linked contents identity with pinned recipe/stream caption,
presence/release, net mass and available mass. Empty and archived vessels remain
visible. Pending collections show their WIP identity, saved net reading (or not yet
weighed), and zero availability. Existing contents in an unfinished addition remain
labelled as starting population rather than pretending the new fill is reconciled.

`population_table.html` is shared by stock and location detail; `web.py` reuses the
existing ledger-based portion view and pending-collection resolver in its read session.
Existing location, provenance, handling and segment assertions were extended.
**33 focused tests passed**, including mass/identity parity, pinned recipe captions,
pending collection status and unchanged inventory. The full Chromium operator chain
passed with no JavaScript errors or overflow at tablet/phone widths; the location
desktop screenshot was inspected. The existing TestClient deprecation warning remains.
No schema, accounting, sorter integration or dependency changed; PostgreSQL and
hardware were not rerun for this presentation change.

The LAN WebGUI was restarted after a verified backup at
`var/backups/training-before-location-populations-20261113T175149Z.db`.
All database table hashes matched before/after restart. Credentials are unchanged.
Authenticated read-only LAN checks passed for all **5 locations / 21 containers**;
the 8 assigned rows matched stock cell-for-cell. Desktop/tablet/phone views had
no overflow or page errors, and all database tables remained unchanged.

## Return to Move container after recording a move — 2026-11-13

Both location-move actions now redirect to **Move container**, keeping the moved
vessel selected and showing its updated location. Operators can select the next
vessel without returning through container details. Inventory/move accounting is
unchanged. Updated the existing redirect assertions and browser continuation plus
the operator guide; no migration or machine integration was required.

The **11 focused handling/location tests passed**, including retry identity and
unchanged contents/mass. The full Chromium receiving/sorting/manual-handling flow
passed with no page errors and no horizontal overflow at 768/390 px. The running
LAN WebGUI was restarted after a verified backup at
`var/backups/training-before-move-return-20261113T174440Z.db`;
all database table hashes matched before/after restart. Credentials are unchanged.
No hardware was invoked; PostgreSQL was not rerun for this navigation-only change.

## Pause, change containers and reconcile multiple fills — 2026-11-13

Running jobs now offer **Pause & change containers**; paused jobs can replace
Selected, Pass-through or both, then explicitly resume the same job. Feeding-stop,
in-flight-clear and readiness confirmations are required. These are workflow
declarations, with no physical stop/start command. Source, recipe and capture
assumptions stay pinned. An unchanged bin spans segments and is weighed once.

Each replacement creates a collection identity linked to its own destination claim,
source-sequence windows, profile and eventual portion. Removed fills remain reserved
and unavailable until final accounting/release. Operators can save versioned gross
weights, print stable collection labels, and record a storage move. Stock/container
views show these as pending WIP, without posting duplicate available inventory.
Final weighing covers all fills and source remainder, reuses confirmed saved
readings, calculates signed discrepancy and retains explicit loss/retained entries.
Occupied replacements combine ancestry with pinned previous contents. Shared
Source/Pass-through hopper collections include source remainder and tare once.

The existing sampler, archived feature/photo records and capture model are reused.
Per-collection profiles, charts/inspectors and later sorting use scoped whole-bean
records; an unchanged bin accumulates its windows. Replay retains identities and
outcomes. A stale synthetic generation batch cannot cross a container boundary.
Manual real records without a known local sequence retain unknown collection
membership/composition rather than fabricating it. Simulated monitoring remains
simulation; measured masses are independent from predictions.

Validation actually run:

- Full existing/new SQLite suite: **197 passed**. A subsequent **9-test segmented
  suite passed**, adding occupied replacements/stale saved readings, interrupted
  accounting, and a source first introduced as hopper destination mid-job:
  **200 distinct tests passed across those runs**. The known TestClient deprecation
  warning remains.
- **41 focused PostgreSQL 14 tests passed** for segmented jobs, occupied destinations,
  hopper emptying and container reconciliation. The temporary database server was
  stopped afterwards.
- Chromium passed two changes, saved removed-bin weighing, storage move, five-vessel
  reconciliation/release, replay and scoped charts using the supplied actual bean
  bundle. Existing hopper and full receiving → sorting → split → blend → packaging
  browser flows also passed. Desktop/768/390 px checks had no horizontal overflow
  or JavaScript errors. Phone screenshots were inspected.
- A disposable snapshot upgraded without changing any existing row. All **230,849**
  original particles/digests/outcomes survived; five original runs replayed with zero
  insertions. A five-vessel job completed on this copy with an explicitly accepted
  0.003 kg model/rounding discrepancy, repeated completion and conserved ledger mass
  after that discrepancy. No live material was created for this test.
- Python compilation passed. No original sorter source was changed or imported;
  no controller, detector or hardware actuation test was performed.

The live training server was gracefully stopped, freshly backed up at
`var/backups/training-before-container-segments-20261113T114411Z.db`, and upgraded
through a verified staged migration **0012**. Every original table matched its
backup by count/content hash: 21 containers, 5 locations, 1 receipt, 6 operations,
12 portions, 16 ledger entries, 230,849 particles and 17 profiles. The three new
tables started empty; no backfill, seeding or reset occurred. Credentials and
session signing are unchanged. Health returned simulation/hardware_control=false
after restart at `http://10.203.77.198:8090`. Authenticated read-only checks of **30
live pages** at all three widths passed and left every database table unchanged.
WireGuard routing was not changed or separately revalidated.

Changes are under `beanosort/`: migration 0012/models, `segments.py`,
`segmented_weighing.py`, `segmented_reconciliation.py`, collection projections,
service/ingestion/profile/source-resolution helpers, labels, operator routes/forms,
stock/container/chart views, tests, and these documents. The staged training-reset
tool recognises the new material tables; it was tested only on disposable databases.

Limits: bins cannot be reused within the same active job; additional source
populations and recipe changes need a separately recorded job/input. There is no
automatic fill-level sensing or hardware pause/clear assurance. The legacy accepted
weight correction form is blocked for segmented jobs; review weights before final
acceptance. General multi-collection corrections and a physical/local boundary
adapter are later work. The next milestone is operator acceptance of repeated
full-bin changes and storage placement, before any physical controller integration.

## Hopper-emptying recipe and shared collection vessel — 2026-11-13

Created **Empty hopper · all beans to Pass-through** as an immutable photographed
reference recipe. All generated beans intentionally pass, including unknown-feature
records; no selection decisions/actions or capture trials occur. Selected is absent
from preparation and weighing. Distinct Source/Pass-through vessels need two gross
weighings; a shared vessel needs one. Its final net total includes any source beans
still present and returned hopper material. The system does not separately infer
hopper throughput or an unprocessed remainder from that one weight.

The shared source is pinned by the existing database guard, reserved/consumed once
and replaced by one child portion in the same vessel, with acyclic ancestry and no
new receipt. The normalized accounting baseline excludes this self-assignment from
extra input stock. Separate occupied outputs retain ordinary combination rules.
Partial/interrupted jobs, cancellation, repeat acceptance and replay are supported.
Shared-vessel composition remains inherited, charts describe generated passages,
and later sorting retains the whole source population rather than just its observed
hopper sample. Standard selective recipe/container rules are unchanged.

Components: explicit `sorting_modes.py`; reference recipe validation/evaluation;
preparation, reservations, reconciliation/proof validation, weighing, population
inheritance, captions, operator UI and tests. Added idempotent `beanosort hopper-recipe`
for development setup. No schema migration, tracker/classifier replacement or
hardware interface was added. This preset runs in isolated simulation/workflow;
it does not actuate the sorter or verify that a physical hopper is empty.

Validation: full **190-test SQLite suite passed**, plus the added occupied-all-pass
case. Final **27 focused hopper/weighing checks passed** after accounting/display
refinement. **12 PostgreSQL checks passed** across the hopper and occupied-destination
cases. Existing Chromium receiving/sort/split/blend/package workflows passed; the
new hopper browser workflow passed with both the small fixture and actual supplied
reference through one weighing, reconciliation, release and replay. Desktop/tablet/
phone checks reported no JavaScript errors or horizontal overflow. The existing
TestClient deprecation warning remains; the temporary PostgreSQL server was stopped.

A disposable copy replayed all four old runs without changing 201,310 original
particles, then returned a 20.200 kg source through one shared-vessel weighing,
preserving stock total and receipt count with idempotent completion. No live job
was created for verification. Live checks confirmed the all-pass preview, omitted
Selected, shared-source option and restoration of strict ordinary mode.

Before the LAN update, a verified backup was saved at
`var/backups/training-before-hopper-emptying-20261113T102305Z.db`.
Only the requested preset's version/audit/idempotency rows were appended. All other
table hashes matched, preserving 21 containers, 5 locations, 1 receipt, 5 operations,
12 portions, 16 ledger postings, 201,310 particles and 16 profiles. Credentials and
session signing were retained. Recipe ID:
`version-731b3f44-356b-43ab-b49a-9612c65010f6`. The WebGUI remains at
`http://10.203.77.198:8090`.

## Final container masses in completed job captions — 2026-11-13

Completed sorting jobs now show the reconciled net bean mass beside every Source,
Selected and Pass-through container name wherever the operation caption appears.
Source explicitly shows material remaining after processing, including zero.
Destination totals include starting contents, rather than showing only the new
addition. The caption identifies these as Reconciled net bean masses, excludes
tare and preserves the recorded job boundary after subsequent stock movements.
The effective reconciliation respects approved superseding measurement corrections.
Prepared/uncompleted jobs do not display final masses; missing historical masses
stay explicit rather than being replaced by zero or a prediction.

Changes cover `operation_labels.py`, the shared caption macro, operator guidance
and existing history/handling regressions. **30 focused tests passed**, including
weighing and material handling. The final three caption/correction checks also
passed after the display refinement. Tests cover partial source remainders,
post-job transfers, occupied destinations, release timing and corrected quantities.
No schema/migration, inventory command or machine interface changed. The existing
TestClient deprecation warning remains; PostgreSQL/hardware checks were not repeated.

A verified backup was saved before restarting at
`var/backups/training-before-operation-masses-20261113T100119Z.db`.
Every table matched by count/content hash: 21 containers, 5 locations, 1 receipt,
5 operations, 12 portions, 16 ledger postings, 201,310 particles and 16 profiles.
Credentials/session signing were unchanged. Live read-only browser checks matched
78 mass labels to reconciliations across stock, job and lineage views at
1440/768/390 px, with readable full-width mobile captions, no changed records and
no JavaScript errors or horizontal overflow. The LAN WebGUI remains at
`http://10.203.77.198:8090`.

## Recipe and vessel captions beneath operation IDs — 2026-11-13

Sorting operation IDs now show the pinned recipe/version and named Source,
Selected and Pass-through vessels in stock/job lists, job details, prepared-job
editing and portion lineage/measurement history. Manual-operation IDs show No
sorting recipe with applicable source/output roles; receiving uses its recorded
receiving containers. These captions describe job configuration/history and do not
establish physical execution. Raw audit/configuration JSON is unchanged.

The new read-only `operation_labels.py` resolves a sorting source from its immutable
`job_prepared` audit and destinations from that operation's retained assignments,
rather than a portion's present container or all inputs of a combined boundary.
Container names resolve the same permanent vessel IDs, including deleted-vessel
history. Missing preparation evidence stays Not recorded. One shared macro, the
existing page read session and a per-page cache keep the display consistent without
new inventory transactions. Changes also cover templates/styles, operator guidance
and history regression tests; no schema, machine integration or stock posting changed.

**20 focused tests passed**, including pinned versions, original source/destination
roles after transfers, occupied destinations, prepared-job editing/deletion,
traceability and existing web/manual handling. Chromium passed the full operator
workflow with desktop/tablet/phone layouts, no JavaScript errors or horizontal
overflow. Live read-only checks matched 29 captions to preparation audits and
destination records at 1440/768/390 px, with no changed records or browser errors.
On tablet/phone layouts each job caption occupies the full row above its mass
and status; visual and width checks cover every listed job to prevent narrow
columns from breaking names into single letters.
PostgreSQL and hardware checks were not repeated for this
display change. The existing TestClient deprecation warning remains.

Before the LAN restart, a verified backup was saved at
`var/backups/training-before-operation-captions-20261113T095050Z.db`.
Every table matched by count/content hash, preserving 21 containers, 5 locations,
1 receipt, 5 operations, 12 portions, 16 ledger postings, 201,310 particles and
16 profiles. Credentials/session signing were unchanged. The live WebGUI remains
at `http://10.203.77.198:8090`.

## Recipe and output captions beneath portion identities — 2026-11-13

Stock, container/current-history, portion/traceability, job, handling, receipt and
contents-label views now show the recorded recipe name, pinned version and output
stream beneath portion identities/links. Preparing a sorting job shows the same
description for the selected source. Transfers preserve the creation caption;
split/correction children retain the parent's recipe/stream with a qualifier.
Received portions say No sorting recipe, sorting remainders say Unprocessed
remainder, and blends refer to contributing histories. An occupied-destination
addition is explicitly marked Combined with existing contents.

The new read-only `portion_labels.py` resolver joins each portion to its creation
operation and matching output, rather than the latest recipe or current container.
One shared template component presents this consistently. Descriptions are cached
for each page and reuse its existing read session; no nested SQLite accounting
transaction is opened. Changes also cover source-selection JavaScript, caption
and lineage layout styles, operator documentation and provenance regression tests.
No schema/migration, inventory posting or hardware interface changed.

All **27 focused provenance, weighing, combination and handling web tests passed**,
along with the existing web/container checks. Recipe-version immutability, original
receipt labels, retained split/transfer provenance and combined-content qualifiers
are covered. The existing TestClient deprecation warning remains.
The complete Chromium operator workflow passed through sorting, splitting,
blending, packaging and label printing with tablet/phone checks, no JavaScript
errors and no horizontal overflow. Read-only live checks matched 30 captions to
creation recipes/streams in the database, checked source selection, and covered
1440/768/390 px layouts. No operational records were created for verification.

Before restarting the LAN WebGUI, a verified backup was saved at
`var/backups/training-before-portion-captions-20261113T085524Z.db`.
Every table matched by count/content hash: 21 containers, 5 locations, 1 receipt,
5 operations, 12 portions, 16 ledger postings, 201,310 particles and 16 profiles.
This preserved the operator's newly accepted reconciliation and new containers.
Credentials/session signing were unchanged. PostgreSQL and hardware checks were
not repeated for this display-only addition. The server remains at
`http://10.203.77.198:8090`.

## Reconciliation and small-area yield investigation — 2026-11-13

Investigated the two newest sorting jobs from a consistent copy of operator data.
The logged HTTP 500 for `op-5d6e757e-6a12-43c1-b7e2-9685656727b2` occurred while
the new review template expected `input_total_mg` but the old running server had
not yet been restarted. The previous update's restart/fallback resolved that
deployment mismatch. The current live review was exercised successfully (HTTP
200), without accepting reconciliation or changing material/configuration records.
On a disposable copy, the same job's signed review, acceptance/release and repeated
acceptance succeeded, with one reconciliation and unchanged particle hashes.

Both jobs use Small area, below reference median minus 2 sigma (13.217705 mm²).
The preceding 16.100 kg job recorded 107,333 particles, 1,373 attempts and 1,359
simulated Selected outcomes. Its expected Selected mean mass is 47.366 mg,
yielding 0.064 kg predicted net mass. Its accepted record instead contains
0.550 kg gross minus 0.500 kg tare = 0.050 kg net; this independently entered value
was preserved. The newest 4.400 kg job recorded 29,610 particles, 73 attempts and
70 simulated Selected outcomes. Its expected Selected count is 72.271 with
49.678 mg mean mass, yielding 0.004 kg rounded predicted net mass. Its source was
already the pass-through of dark- and large-outlier passes. These are rare small
beans under the explicitly assumed linear area/mass model; no mass unit or
count-to-mass conversion defect was found.

The weighing page now displays expected count × mean bean mass beside each
predicted addition, separating this expectation from synthetic destination counts,
starting contents, tare and independently entered weights. Added a regression
covering legacy preparations without baseline fields, review/acceptance, area
weighting and preservation of entered weights/particles. **25 focused SQLite tests
passed**. Live Chromium checks passed at 1440/768/390 px with no JavaScript errors
or horizontal overflow. No backend/schema change, PostgreSQL rerun, server restart
or hardware operation was needed for this template/test/documentation change.
The existing TestClient deprecation warning remains. Investigation artifacts are
under ignored `var/qa/`; the live latest job remains awaiting operator acceptance.

## Sorting into existing populations — 2026-11-13

**Prepare sorting** now excludes the chosen Selected container from Pass-through
and vice versa; clearing/changing a choice restores it. Both exclude the source.
Released, unreserved bulk stock is eligible as a destination alongside empty
containers. Starting destination contents are pinned and reserved with the feed,
and become unavailable work-in-progress when execution starts. Editing/deleting
an unstarted preparation releases/replaces all claims transactionally.

Reconciliation weighs the final **total gross weight** of each vessel, subtracts
its tare and distinguishes the starting stock from the new addition. The material
boundary includes feed plus previous destination contents. A positive addition
consumes the prior destination portion and creates a new combined portion with
both ancestries; no addition preserves the original portion. Both destinations
can be occupied. Repeated completion cannot create duplicate stock. Holds and
normal sorting capacity checks still apply.

Compatible simulated combinations retain whole reference feature/photo records,
with count-weighted Before addition / Added this run / Combined contents charts
and inspection. Combined portions can be sorted again. Mass/count relationships
remain explicit assumptions; uniform mixing, physical count and membership are
unverified. Incompatible or absent composition evidence stays unknown. New
weighted snapshots use sampler v2, while existing v1 runs replay unchanged.

Migration 0011 changes only the database destination guard, requiring matching
pinned, fully reserved contents. Components changed: `collections.py`, service,
weighing, population-source/inspection adapters, preparation/job/population UI,
browser workflows, domain tests and operator/architecture/startup documentation.
No sorter/hardware integration or new dependency was introduced. A destination
weighed below its starting contents requires a reviewed removal/rework workflow;
this addition flow rejects it. The existing single-input measurement correction
form is intentionally unavailable for combined outputs; review before acceptance.

Validation: **179 SQLite tests passed**, **24 focused PostgreSQL tests passed**,
and the final combined-profile web test passed. Existing Chromium operator checks
passed desktop/tablet/phone. The expanded reference workflow passed using the
actual supplied archive through repeated sorting, occupied collection, total gross
weighing, combined charts/photos and inspection, without JavaScript errors or
horizontal overflow. A disposable migrated copy replayed all four existing runs
without changing their 201,310 particle records, then completed a two-occupied-bin
simulation and release, creating two combined profiles and no new receipts.
The existing TestClient deprecation warning remains; a standalone Node syntax check
was unavailable, while Chromium exercised both changed scripts successfully.

Before the LAN update, a verified backup was saved at
`var/backups/training-before-occupied-destinations-20261113T083019Z.db`.
Every operator table's count/content hash matched before/after migration: 19
containers, 5 locations, 1 receipt, 10 portions, 13 ledger postings, 5 operations,
201,310 particles and 13 profiles. Credentials/session signing were retained.
Read-only live checks verified six occupied destination choices, exclusion in both
directions, restoration after clearing, source exclusion and 1440/768/390 px
layouts, with no material/configuration changes or browser errors. The temporary
PostgreSQL server was stopped. The WebGUI remains at `http://10.203.77.198:8090`.
No live sorting job was created or hardware operated for verification.

## Current contents in the container register — 2026-11-13

The **Containers** register now lists net contents mass from the same movement
ledger as **Stock & jobs**, a linked population ID for the active contents
assignment, and a clickable registered storage location. Container labels and
Assigned/Unassigned/Archived status remain visible. Unassigned vessels show zero
recorded mass and **No contents assigned**, preserving the separate physical
empty/clean confirmation. Historical assignments are not used for current links.
The responsive register table stacks its labelled cells on phones.

Changes cover the containers route/template, scoped table styles and operator
documentation. No inventory commands, schema migration or hardware access were
added. Thirty-one existing web/container/location tests passed, and the existing
Chromium operator workflow passed desktop/tablet/phone checks. Read-only live LAN
checks compare every displayed quantity and assignment/location link with the
database, follow both link types and check 1440/768/390 px layouts.

A verified backup was saved before restarting at
`var/backups/training-before-container-register-20261113T073626Z.db`. Every table's
count and content hash matched before/after restart, preserving the operator's
newer re-sort results: 18 containers, 5 locations, 8 portions, 10 ledger postings,
3 operations, 64,367 particle records and 1 receipt. Credentials/session signing
were retained. The LAN WebGUI remains running at `http://10.203.77.198:8090`.

## Further sorting of retained simulated populations — 2026-11-12

Released simulated Selected/Pass-through populations can now be used as a source
for another sorting job. The preparation pins a compact immutable source-population
profile; generation and ingestion share a deterministic weighted sampler restricted
to that population's retained synthetic members. Previously missed targets remain
in Pass-through and can be selected on a later pass. New passages receive fresh run
identities and capture trials while keeping whole joint features and photo links.
Recipe thresholds remain versioned reference thresholds. Initial receipt sampling
and all historical run replay remain unchanged.

Count estimation uses the retained source mean mass rather than resetting to the
generic unsorted mean. Gross predictions preserve the original area/mass assumption
and use the source mean for test-sample expansion and source remainder estimates.
Actual entered quantities remain independent. Each completed pass consumes its
source and creates reconciled children through the existing ledger/lineage rules;
no new receipt or duplicate stock is created. The WebGUI shows a **Further sorting
pass** card, source-specific count preview, pinned assumptions, normal progress,
population charts/photos and gross-container reconciliation for each new pass.

Whole transfers preserve eligibility. Representative split children and unprocessed
remainders can be re-sorted with explicitly inherited relative-frequency assumptions.
Held/unreleased inputs, incompatible datasets, positive mass with no usable synthetic
members, unrepresented splits and blend/package/rework replay remain guarded.
Supporting those combined/rework populations is a future adapter milestone.

Changed components: `population_sources.py`, shared reference particle construction,
prepare/start/generate/ingest commands, count-assumption pinning, weighing model,
preparation/job/population UI and documentation/tests. No schema migration, new
dependency or sorter/hardware integration was added. Validation: the complete
**174-test SQLite suite passed**; **25 focused PostgreSQL tests passed** (six
re-sorting cases plus existing container weighing). Fourteen re-sorting/preparation
checks also passed after the final display refinement. Tests cover both outputs,
new recipes, repeated trials, second/third passes, replay, preserved old particles,
genealogy, reservations, source-specific mass/count estimates, changing settings,
partial remainders, representative splits, incompatible datasets and quality holds.
The existing TestClient deprecation warning remains.

The Chromium reference workflow passed with the actual supplied archive through
receive → sort → weigh/release → prepare a second pass → simulate/replay → population
comparison → second weighing/release, with no JavaScript errors. New input histograms
contain no feature bins absent from the prior simulated Selected population.
A disposable copy of the existing 33,333-particle run also completed a further
500-passage sort using its 78 retained reference identities, preserving every prior
particle and creating no new receipts. Test destinations with explicit tare were
created only in that disposable copy.

Before the LAN restart, a verified archive was saved at
`var/backups/training-before-population-resort-20261112T162508Z.db`. Every table's
count/content hash matched before/after restart: 1 receipt, 6 portions, 7 ledger
postings, 33,333 particles, 2 operations, 4 profiles, 18 containers and 5 locations.
Credentials/session signing were unchanged and the temporary PostgreSQL server was
stopped. Live source eligibility and count preview were checked read-only; no new
sorting job was created in operational training stock for verification.

## Population chart toggles and bean inspection — 2026-11-12

The chart legend now uses independent **Input**, **Selected** and **Pass-through**
checkboxes. Visible series determine the vertical scale. Clicking an interval near
a line opens a left-hand inspector with matching archived paired photos, per-view
mean RGB colour swatches, feature values and passage contributions. A population
selector compares the same interval in other visible populations; the clicked bin
is highlighted. The inspector stacks above charts on tablet/phone layouts and has
bounded pagination, keyboard access, explicit empty states and a loading/retry state.

Expected-output inspection lists weighted possible contributors using the pinned
capture assumption; simulated inspection uses persisted destinations and includes
missed targets in Pass-through. Repeated passages share one archived photo pair
with multiplicity shown separately. Missing features/colours/photos stay unknown.
No new physical evidence, stock counts or membership claims are created. SQL
grouping avoids loading every run particle into a browser or ORM inspection query.
Recipe previews pin their displayed capture version for inspection across concurrent
settings edits. Existing chart/photo navigation and weighing estimates are retained.

Changes are confined to the read-only inspection module/routes, population
templates/scripts/styles, operator/architecture documentation and focused tests.
No schema migration, dependency, sorter import or hardware action was added.
Validation: **30 focused SQLite tests passed**; **13 new inspection tests passed on
PostgreSQL**, and its 13 existing reference tests also passed. These cover exact
histogram bin totals, expected/simulated membership, 0/97/100% capture, missing
recorded features, pagination, missing colour/photo evidence, pinned models, request
validation, authentication and unchanged particle/stock records. Chromium passed
the full photographed-reference workflow using both the small fixture and the
actual supplied bundle; the expanded checks cover line visibility, left-side photos,
swatches, pagination, empty intervals, keyboard selection, retry and 1440/768/390 px
layouts without JavaScript errors or horizontal overflow.

Before the LAN restart, a verified backup was saved at
`var/backups/training-before-chart-inspection-20261112T153201Z.db`. Every database
table matched by count and content hash before/after restart, preserving the newly
received material and completed job: 1 receipt, 6 portions, 7 ledger postings,
33,333 particles, 1 reconciliation, 16 containers and 5 locations. Session signing
and credentials were unchanged. The temporary PostgreSQL test server was stopped.
Read-only live LAN browser checks followed an output container into its population
charts, toggled lines and opened the left photo/colour inspector. Its interval total
matched the frozen histogram exactly. Desktop/tablet/phone checks passed with no
JavaScript errors or overflow, and all material record counts remained unchanged.

## Fresh training inventory — 2026-11-12 14:24 SAST

At the operator's explicit request, all received beans and processing records were
removed from the active isolated simulation database, including the pending
`op-58a95ba9-6881-42e5-9380-5395fdcff351` job. All 23 material tables are empty,
including receipts, portions, assignments, ledger, operations, reservations,
destinations, runs, particles, measurements, profiles, genealogy and package records.
The 16 containers, 5 locations, 8 configuration versions, simulation settings,
reference features/photos, station definitions and user accounts were preserved.
Every preserved configuration row and the complete schema/guards match the original.

The existing offline maintenance utility staged and verified the reset before
atomic activation with the web server stopped. A complete recoverable archive is
`var/backups/training-before-fresh-start-20261112T122414Z.db`; its matching private
environment backup retains the previous signing configuration. The archive keeps
the old receipts, pending job, 66,667 particle records and all other prior history.
The current session secret was rotated to invalidate stale forms; the existing
password and all user records remain unchanged. Operators must sign in again.
Station telemetry was cleared and an audited reset records the archive reference;
no physical empty/clean confirmation was inferred and no hardware was operated.

Both maintenance tests passed. Integrity/foreign-key checks, empty material tables,
exact retained configuration, rejected old sessions, fresh login and authenticated
stock/container/location/recipe/settings/receiving/preparation pages passed after
restart. The LAN WebGUI remains running at `http://10.203.77.198:8090` and is ready
for fresh receiving. No application code or migration was changed for this reset.

## Gross container weighing and reviewed reconciliation — 2026-11-12

Reference simulations now offer **Weigh containers & reconcile**: gross weights
for Selected, Pass-through and the source vessel, with registered tare deducted.
All three require confirmation, including an empty source. Missing legacy tare is
entered explicitly and committed only with accepted reconciliation. Sampling is
omitted; optional retained/loss terms remain available. **Reconcile** previews the
signed difference without changing stock. Acceptance posts idempotent accounting,
and supervisors/admins release permitted outputs. Operator-only acceptance leaves
outputs held for supervisor review. A large run shows a saving message while its
profile is recorded.

Suggested simulation masses use the pinned mean bean mass × bean area / reference
arithmetic mean area, expected capture membership and an explicit sample expansion
factor where an operator chooses a reduced test count. The model is versioned and
saved in reconciliation evidence; it is not a calibrated mass measurement. Source
remaining mass includes unprocessed material after interrupted generation. Accepted
net weights update separate count estimates on output/remainder profiles. Original
particle/photo records, joint features, histograms and synthetic outcomes stay intact.
No beans or images are added/removed to force mass balance. Gross/tare/revision,
operator, timestamps, model references and signed discrepancy remain auditable.

Supervisors/admins can explicitly accept a signed discrepancy outside the pinned
tolerance in the **isolated simulation only**, with its own audit record. Real
inventory retains strict policy guards; composition approval and explicit quality
holds remain enforced. Non-reference/manual jobs retain detailed net accounting.
No schema migration, new dependency, machine import or hardware command was added.

Validation: **155 SQLite tests passed**, including 19 gross-weighing cases; **44
focused PostgreSQL tests passed**, covering this module and existing sorting rules.
These include missing weights/confirmations, signed excess/shortfall, milligram
precision, stale tare/revision, missing-tare rollback, interrupted generation,
0%/100% capture, pinned assumptions, role/CSRF/review-token guards, retries and
real-inventory isolation. The existing Starlette TestClient deprecation warning
remains. Compilation and both Chromium operator workflows passed. The photographed
workflow also passed using the actual supplied bundle, with no JavaScript errors or
horizontal overflow at desktop/tablet/phone widths.

A disposable copy of the current 66,667-passage job passed reconciliation, release
and retry in about 14 seconds with all particle identities/digests/destinations
unchanged. Its suggested gross weights are 0.941 kg Selected, 10.065 kg Pass-through
and 14.000 kg source; the signed discrepancy is −0.006 kg and remains visible.
This test did not reconcile the operator's live job. Before restarting, a complete
verified backup was saved at
`var/backups/training-before-gross-weighing-20261112T121734Z.db`. Every table matched
by count and content hash before/after restart: 2 receipts, 4 portions, 4 ledger
postings, 66,667 particles and no reconciliations. Existing credentials/session
secret, 16 containers, 5 locations, recipes and settings were preserved. No physical
hardware verification was performed. The temporary PostgreSQL server was stopped.
The restarted LAN WebGUI passed read-only authenticated checks at 1440/768/390 px:
all three gross suggestions and net displays are correct, confirmations start
unchecked, no JavaScript errors/overflow occur, and material records are unchanged.
The operator's current job remains awaiting reconciliation.

## Default full processing quantity — 2026-11-12

Selecting a source in **Prepare sorting** now fills **Intended processing quantity**
with its full available mass. The field remains editable for partial sorting;
choosing another source fills that source's mass and updates the count preview.
An existing prepared job keeps its saved partial quantity when its editor opens.
The browser copies an exact server-provided kg string, retaining finer historical
precision when present. Server quantity/accounting rules remain unchanged.

Eight prepared-job tests and the complete Chromium operator workflow passed,
including automatic defaults, manual changes, source switching, count previews,
saved partial editing and tablet/phone layouts. Read-only LAN browser checks
confirmed the current stock defaults and editable quantity with no JavaScript
errors. This template/script change is live without a server restart, schema change
or inventory modification. The LAN WebGUI remains running.

## Receiving container availability and unique choices — 2026-11-12

Receiving no longer silently excludes empty legacy vessels without a recorded
tare. Selectable rows show missing tare/capacity inputs only where needed; the
operator enters the values and they are audited/saved atomically with the receipt.
Known weights remain unchanged and require **Edit container** for correction.
Errors roll back both missing-weight completion and material postings. The 120%
capacity allowance and independent net-scale mass remain enforced. Occupied or
reserved vessels remain unavailable and are listed with their blocking reason.

The browser hides/disables containers already selected in other rows, restores them
after changing/removing the selection, and keeps the row's own choice available.
Changing a vessel clears its stale entered weights, net reading, tare confirmation
and storage destination. Server duplicate/occupancy/revision checks remain
authoritative. Previously opened forms for containers with registered weights
retain their original idempotency payload when no new metadata is supplied.

Validation: **32 focused tests passed in each SQLite/PostgreSQL run set**, including
the new cross-backend missing-weight rollback/commit case. The full existing
Chromium operator workflow passed duplicate-choice removal/restoration, while a
dedicated disposable legacy-container browser workflow passed required inline tare/
capacity, 120% limit, correct net totals, occupied explanations and desktop/tablet/
phone layouts. No new dependency or schema migration was required.

Before restarting the LAN server, a verified backup preserved every database row,
including the operator's newly received beans (1 receipt, 2 portions and 2 ledger
postings at restart). Read-only live browser checks confirmed that unknown-tare
containers are selectable, tare input is required, duplicate options are hidden/
disabled, and occupied vessels have an explanation. Existing receipt/portion and
unknown-weight counts stayed unchanged during verification. No live bean receipt
or inferred tare was added. The temporary PostgreSQL server was stopped; the LAN
WebGUI remains running with the existing sessions and credentials.

## Operator-requested fresh inventory — 2026-11-12

At the operator's explicit request, the isolated LAN training inventory was reset
to contain **no received beans or processing records**. All material tables are
empty: receipts, portions, assignments, ledger, jobs, reservations, destinations,
runs/particles/monitor events, profiles, measurements, genealogy, blends, product
lots and packs. The **16 containers, 5 locations, 8 configuration versions,
simulation settings, reference dataset/photos, users and station definitions**
were preserved exactly. The 3 active recipes and legacy retirement remain intact.
Station last-received telemetry was cleared. A reset audit records the archive.

`maintenance.py` prepares a separate SQLite training copy and refuses real
inventory, unknown schemas, non-admin users and reused output paths. The complete
original was backed up at
`var/backups/training-before-inventory-reset-20261112T102105Z.db`; the private
environment backup has the matching timestamp. Append-only DELETE guards were
temporarily removed only in the unserved staging transaction, then restored
verbatim and checked against the original schema before activation. The original
archive retains all history. Current schema/foreign-key/integrity checks passed.

Two maintenance tests passed, including full receipt-to-pack cleanup, unchanged
configuration/reference/retirement history, restored ledger guards, legacy unknown
tare preservation, fresh receiving and refusal of real inventory. A disposable copy
of the actual training DB also passed authenticated pages and a new 1 kg receipt;
that test receipt was never added to live stock. The verified clean copy was then
activated atomically with the server stopped. Session signing was rotated so stale
forms cannot restore archived material; passwords and all other environment
settings remain unchanged. Old sessions were verified rejected, and existing login,
empty stock/jobs, unassigned containers, locations, recipes and receiving were checked
read-only over LAN. Three containers currently have the recorded tare needed for
receiving; the other historical vessel records remain unchanged. The server is running.

## Area recipes and legacy retirement — 2026-11-12

The active catalogue now includes **Large area: above median plus 2 sigma** and
**Small area: below median minus 2 sigma**, using
`projected_area_geomean_mm2_median` in mm². The reference median is 33.1772670456 and
population σ is 9.9797807760. Strict thresholds are **area > 53.1368285975 mm²**
(47 reference beans) and **area < 13.2177054937 mm²** (15). At the current 99%
capture assumption, expected Selected counts are 46.53 and 14.85. These remain
predictions, independently of stock mass and stochastic capture results.

Area is available in recipe design, population charts and photo captions. Existing
immutable datasets lacking area metadata derive new recipe statistics from their
verified original JSONL; the dataset row and old recipes/profiles are unchanged.
Older runs keep their original five-feature payloads and replay deterministically.

The legacy fixture version is retired through an append-only audit/idempotency
command. It is absent from the current catalogue and new-job choices, with a server
guard rejecting submitted retired IDs. Historical links/jobs remain available;
existing jobs retain their pinned version. Correcting an unstarted retired-recipe
job requires an explicit current recipe. No schema migration or hardware change
was required.

Validation: **64 focused SQLite tests passed** (39 reference/capture/preparation/
WebGUI plus 25 workflow) and **64 passed on PostgreSQL**. Chromium exercised both
area previews against the actual 1,129-bean bundle, checking the 47/15 target counts,
area charts, legacy retirement and the existing reference simulation/replay/
reconciliation/release/stock workflow, including tablet/phone layouts. Older-feature
compatibility, strict thresholds, immutable history and retirement guards are tested.
The unchanged TestClient deprecation warning remains.

A verified backup preceded adding the two recipe versions and retiring the legacy
version on the LAN training system. All original stock, jobs, runs, profiles,
measurements, ledger rows and reference metadata were preserved; integrity and
foreign-key checks passed. After restart, authenticated read-only checks verified
the active catalogue, both exact thresholds/expected counts, recipe preselection,
area definitions, legacy history and rejection of new-job legacy selection. No
live stock/jobs were created for verification. The temporary PostgreSQL test server
was stopped; the LAN WebGUI remains running.

## Photographed reference populations and sorting recipes — 2026-11-12

The LAN WebGUI now has **Sorting recipes**, a versioned catalogue with property/
comparison/σ recipe creation for administrators, exact threshold previews, five
count-weighted population charts and representative bean photographs. The first
recipe selects **darker outliers: L* < median − 2σ**, as clarified by the operator.
For the specified October 26 export, median is 24.0521249771 and population σ
(ddof=0) is 2.9590313683; the strict threshold is **L* < 18.1340622405**.

The linked HTTP bean file matched the local export byte-for-byte. Its **1,129
distinct tracked beans** aggregate **3,304 stereo samples**; the original has 6,608
photos. The importer verified the manifest and copied all bean records plus **2,258
representative photos** to an immutable, fingerprinted local snapshot. The original
bundle remains untouched. Its preview targets 78 beans; at 99% assumed capture it
predicts 77.22 Selected and 1,051.78 Pass-through, with 0.78 expected missed targets.

New reference simulations sample whole archived records with replacement, keeping
joint properties and photo identity together. Source sampling and one capture
outcome per new passage are deterministic for stored run ID/seed/sequence; bounded
batch replay and process restart preserve them. Ingestion rejects altered sample
rows. Each job pins its exact recipe/dataset and separate capture version; old
fixture jobs and repeatable demo seeds retain their previous behaviour. Reports
compare Input, Selected and Pass-through as expectations or simulated outcomes,
with count/percentage scales and photo examples including missed targets. Output
container profiles link back to these reports. Completed reports remain frozen.

Archived feature origin is image-derived, while sampled membership, new decisions,
assumed issued actions and arrivals remain simulated. No physical capture is
confirmed, moisture remains unknown, volume is a proxy, and mass is always an
independent operator entry. Unknown recipe features stay unresolved. Reference
recipes currently execute only in isolated simulation and start from received
source portions. Replay of already sorted/split/blended populations is explicitly
blocked pending an appropriate population adapter. No physical recipe application,
chute hardware or machine timing integration was added. Large million-passage
throughput and live machine performance were not benchmarked.

Validation: **128 tests passed on SQLite** and **60 focused reference/capture/
workflow/prepared-job/WebGUI tests passed on PostgreSQL**. They cover p=0/1/0.97,
joint-feature preservation, expected conservation, restart/replay, tamper rejection,
missing values, source isolation, immutable recipes/datasets, photo access and
independent stock mass. Chromium passed the complete new workflow using the actual
bundle (preview, photos, seeded simulation, charts, replay, scale weights, release
and output stock analysis) at desktop/tablet/phone sizes, plus the existing complete
operator workflow. The sole existing TestClient deprecation warning remains.

Migration 0010 passed on a disposable copy of the training database without changing
existing rows. Before applying it live, a verified SQLite backup was taken. Registering
the reference/first recipe added only configuration/audit/idempotency records; hashes
of all original stock, jobs, runs, measurements, profiles, assignments and ledger
records stayed unchanged. Integrity/foreign-key checks passed, and the server was
restarted with the existing credentials/session configuration. Authenticated LAN
checks verified the catalogue, threshold/expected counts, served photos and recipe
preselection without creating live stock or jobs. Temporary test servers/databases
were stopped/removed; the LAN training WebGUI remains running.

Changed components: `references.py`, models/migration 0010, service/CLI integration,
recipe/population routes/templates, local SVG chart script/styles, profile links,
regression/browser tests and project documentation, all under `beanosort/`.
Next: operator acceptance of these reference simulations, then an adapter for
replaying the actual membership or evidence-aware inherited composition of processed
input portions before enabling reference re-sorting or planning physical recipes.

## Receiving capacity allowance — 2026-11-12

Receiving now accepts net bean mass up to **120% of nominal container capacity**,
inclusive: a 25 kg vessel can receive 30.000 kg. Both receiving commands enforce the
same integer-milligram limit. The browser shows each selected container's limit and
uses it for weight validation. Entered mass is recorded in full; nominal capacity
and tare remain unchanged. Measurement/audit snapshots preserve the nominal
capacity, permitted limit, amount above nominal and policy `nominal-capacity-120-percent/v1`.

Migration 0009 updates the capacity guard so existing overfilled containers can
still move, be renamed or receive other metadata edits. Capacity reductions below
the current material remain blocked. Partial sorting/withdrawals can leave an
already overfilled remainder in the original vessel, bounded by the original input
mass; this does not compound the allowance or authorize overfilled new outputs in
other operations. Empty-container, reservation, environment and accounting rules
remain in force. No sorter or hardware code changed.

Validation: the **117-test SQLite suite passed**, followed by **46 focused
receiving/sorting/manual-handling tests on each of SQLite and PostgreSQL** after
adding the two remainder cases. An earlier **63-test PostgreSQL receiving/container/
location suite passed**. Chromium passed the receiving limit, rejection at 120.1%,
successful receipt at 120%, and the existing full operator workflow, including
phone/tablet layout checks. Tests verify measured mass, nominal capacity, evidence,
retry safety, atomic rejection, subsequent moves/edits and conservation with
overfilled remainders. The one existing TestClient deprecation warning remains.

The forward migration passed on a disposable copy of the training database, with
all material-table contents unchanged. A verified backup was taken before applying
0009 to the LAN training system; hashes/counts of every existing data table were
preserved and integrity/foreign-key checks passed. After restarting, login and the
updated receiving page/script were verified read-only. There are currently no
eligible empty containers with known tare, so an actual overfill receipt was tested
only in disposable databases. No live inventory records were created or changed
for verification. The temporary PostgreSQL test server was stopped; the LAN WebGUI
remains running.

## Visible location deletion — 2026-11-12

Location detail now shows **Delete location** beside **Edit location** for
supervisors/admins. It opens a confirmation page naming the location. Locations
containing vessels show the blocking containers and links to move them; only a
cleared location offers the confirmed deletion form. Archived containers also block
deletion. The existing transactional command, revision checks, audit history and
retry protection remain unchanged; there is no database migration.

Validation: **14 focused location tests passed**, including read-only confirmation
GETs, permissions, CSRF, confirmation requirements, a container arriving after the
page was opened, clearing the location and duplicate deletion submissions. Chromium
passed empty-location deletion, the occupied-location explanation and the complete
existing operator workflow; existing tablet/phone layout checks passed. No full
database regression rerun was needed for this UI update. A verified backup preserved
all training database table counts. After restarting the LAN server, all five location
pages and confirmation pages were checked: one empty location allowed confirmation,
and four occupied locations blocked deletion. No user locations were deleted during
live verification, and no hardware was invoked.

## Central container move workflow — 2026-11-12

**Move, blend & package** now has a **Move container** card. Operators choose a
filled or empty vessel, inspect its recorded location and contents, select another
registered location, and confirm they have physically moved it. Recent moves show
the container, previous/new location and recorded time. The existing audited move
command preserves contents identity, measurements, balances and quality holds;
active-job vessels remain locked. This is distinct from pouring material into
another vessel through a transfer operation. No schema or hardware integration changed.

Validation: **11 focused handling-WebGUI/location tests passed**. These cover the
new flow's confirmation and CSRF checks, retry safety, unchanged mass/measurements,
active-job protection and preserved quality holds, alongside existing location rules.
Chromium passed the complete receiving/sorting/manual-handling workflow through the
new central move page, with no JavaScript errors or overflow at 768/390 pixels.
The LAN training database was backed up and passed integrity/foreign-key checks;
all table counts were preserved. The server was restarted and login, the new card,
container selection and destination form verified over the LAN address. No user
container locations were changed during live verification.

## Container action layout — 2026-11-12

**Delete container** now shares the detail-page action row with **Edit container**.
Delete actions were removed from the container register and the print-label row.
The existing confirmation page and deletion rules remain in use. Eight focused
container tests and the Chromium workflow passed, including tablet/phone layouts.
Live rendered HTML confirmed the shared action row and absence of list deletion
links. This template-only change required no restart or database changes.

## Visible container deletion — 2026-11-12

The container register now shows a **Delete** action beside each container, and
detail pages have a prominent **Delete container** link at the top. They open a
dedicated confirmation page naming the container. Occupied or active-job vessels
display the blocking reason; eligible empty vessels require explicit confirmation.
The existing role checks, revisions, audit and history-preserving deletion command
are unchanged. The action is no longer hidden inside a collapsed history-area form.

Validation: 12 focused container/web tests passed, including confirmation-page role
checks and read-only GET behaviour. Chromium passed list/detail deletion of empty
unused/used containers and the complete existing workflow, including tablet/phone
layouts. No backend quantity rules changed, so the full database suites were not
repeated for this visibility update. The training database was backed up, the LAN
server restarted, and live list/detail/confirmation plus occupied protection checked.
No user containers were deleted during verification; no hardware was invoked.

## Prepared sorting job edit/delete update

Unstarted prepared sorting jobs now have prominent **Edit prepared job** and
**Delete prepared job & release containers** actions. The editor preselects the
current source (including its own reserved stock), station, planned mass, bins and
configuration versions. A correction creates a linked replacement job, cancels the
old request and atomically transfers claims. Invalid/conflicting corrections roll
back to the original prepared job and reservations. Existing immutable inputs,
configuration references and assumptions are retained; no ledger movement occurs.

Deletion cancels the unstarted request, releases the source reservation and both
destinations, and removes the job from the current list. Old links keep an audit
page and link to the correction/reconfiguration path. Server checks reject edit or
deletion after any run, even if paused/failed, and require confirmation that no
material was processed. Idempotency and the accounting lock protect request replay
and races with Start. Original settings versions are prefilled, including legacy
capture models, with new planned-mass estimates pinned on corrected jobs.

Validation: **109 tests passed on SQLite** (40 workflow/web/settings, 8 prepared-job,
61 remaining) and the full **109-test suite passed with PostgreSQL 14**. Tests cover
claim release/reuse, source/destination corrections, swapped streams, failed-edit
rollback, concurrent Start, after-start guards, replay, CSRF and pinned versions.
Chromium edited/deleted/reprepared a job and ran the full existing material workflow;
desktop/tablet/phone checks passed without page errors or overflow.

No schema migration was needed. A verified backup was saved before restarting the
authorized LAN WebGUI, preserving 16 containers, 5 locations, 14 portions, 19 movements,
10 operations, 1 run, 1,000 particles, 8 profiles, 5 receipts and 55 audits. The user's
example `op-d10951ae-0ec2-4910-b72c-8174dddc00eb` was confirmed prepared with no run;
its existing input reservation and two destinations were left for the user to correct
through the new controls. No hardware commands were issued.

## Location and container management update

Both sections now support add/edit/delete when empty. Locations have audited name
changes and revision checks; renaming updates linked container display names while
keeping their IDs, placements and quantities. Location deletion requires zero
containers, including archived ones, and preserves an audit page for old links.
Names of deleted locations can be reused for a new location identity. Configuration
initialization does not recreate deliberately deleted default locations.

Deletion is available for all empty, unclaimed containers, including used vessels.
Used registrations are retained for genealogy and detached from current storage;
unused registrations can be removed outright. Deleted vessels are excluded from all
current lists/choices and cannot receive new assignments. Old scans show their
deletion and material history. Archive/restore remains available. Occupied or
active-job vessels and non-empty locations remain protected at service/database level.

Validation: **101 tests passed on SQLite** (21 management/container/location plus
80 remaining) and the full **101-test suite passed with PostgreSQL 14**. Chromium
passed add/edit/delete locations, renaming an occupied location, deleting an empty
used vessel, retained traceability, and the existing receiving-to-packaging workflows
at desktop/tablet/phone widths without page errors or overflow. Historical membership
assertions were scoped to current tables after the new location audit became visible.

Migration 0008 was applied after a verified backup. Existing values were preserved
for 16 containers, 5 locations, 14 portions, 19 movements, 10 operations, 1 run, 1,000
particles, 8 profiles, 5 receipts and 54 audits. No actual user containers or locations
were deleted during deployment. Integrity and FK checks passed. The authorized LAN
WebGUI was restarted; no machine controls were invoked.

## Guided receiving update

Receiving now follows the requested four steps: source details, empty labelled
containers with known tare, net scale readings in kg to three decimal places, and
storage locations. The operator can add containers under the same source details;
one receipt/source lot links their separate material portions. Only actual entered
net weights enter stock. A supplier's nominal 60 kg bag is descriptive context and
is not an inventory vessel or a substituted measured quantity.

The new flow records no particle-count/profile assumption. Scale ID, resolution,
gross/tare entry and simulation selectors are removed from receiving. Registered
tare and scale-zeroing confirmation are retained in measurement provenance without
subtracting tare from an already net reading. Other bean properties remain unknown.
Storage placement is confirmed and posted with the receipt, with atomic validation
and replay protection. Eligible choices exclude occupied/claimed/archived vessels
and missing tare. Existing records and legacy CLI receipt evidence remain intact.

Validation: **95 tests passed on SQLite** (10 receiving/web plus 85 remaining) and
the full **95-test suite passed with PostgreSQL 14**. Tests cover a 60 kg receipt split
across 30.125 / 29.875 kg containers, net/tare independence, shared ancestry, missing
tare, confirmations, invalid/competing loads, unknown profiles and duplicate retries.
Chromium passed the new four-step two-container workflow and all existing handling
flows, including tablet/phone layouts. A pre-existing QR-image assertion was made
to wait for image load; the browser rerun passed without page errors or overflow.

No schema migration was needed. Before restarting the authorized LAN WebGUI, a
verified SQLite backup preserved 15 containers, 11 portions, 16 movements, 9 operations,
1 run, 1,000 particles, 8 profiles, 4 receipts, 49 audits and 5 storage locations.
There was no hardware actuation. Draft entries are held in the browser until final
submission; receiving container claims are validated at final commit.

## Storage locations update

The new **Locations** section creates named places and lists containers stored at
each one. Container creation/editing uses registered location choices; the container
page records moves of empty or filled vessels. Stock and portion views link to their
current place. Locations group vessels only and do not introduce material balances
or station capabilities. Archived vessels remain visible in their location listing.

Migration 0007 imports existing names, links containers through a foreign key and
enforces matching display names in the database. Case/outer-space variants resolve
to one name. Location names are stable in this initial UI. Moves retain contents,
assignments, measured masses and material genealogy; operator/simulation audits,
idempotency and revision checks protect retries/concurrent edits. Active-job and
archived-container move restrictions remain enforced. Older containers can move
without inventing a missing tare value.

Validation: **89 tests passed on SQLite** (17 container/web, 7 locations, 65 remaining)
and the full **89-test suite passed with PostgreSQL 14**. Tests cover legacy migration,
filled-container moves, no duplicate places/moves, competing operators, active-job
locks, foreign keys and unchanged stock measurements. Chromium passed location
creation, dropdown assignment, a filled-container move, and the full existing material
workflow at desktop, tablet and phone sizes without page errors or overflow.

The training database was backed up and upgraded without changing existing record
values: 15 containers, 11 portions, 16 movements, 9 operations, 1 run, 1,000 particles,
8 profiles, 4 receipts and 48 audits were preserved. Its registered locations are
Floor (4 vessels), LoadingBay (1), Training material bench (4) and Training shelf (6).
Integrity/foreign-key checks passed. The authorized WebGUI remains running on the
LAN; no sorter controls were invoked. The temporary PostgreSQL test server was stopped.

## Container editing and removal update

Container detail now offers **Edit container** for name, location, vessel type,
tare and capacity. Changes retain the permanent ID and append previous/new values
to the audit. Revision checks reject stale edits; retries are idempotent. Tare and
capacity remain required, active job sources/destinations are locked, and capacity
cannot fall below the current material balance. The latest entered tare evidence is
displayed while historical material measurements remain intact.

Supervisors/admins can delete unused empty containers after confirmation. Used empty
containers can be archived and restored. Archives are excluded from stock workflow
choices, but scans still resolve their history. Deleted unused IDs resolve to their
deletion audit, including after reuse of the former human-readable name. Database
guards enforce archive/assignment and current-capacity constraints.

Validation: **82 tests passed on SQLite** (17 focused editing/weights/web and 65
remaining tests) and the full **82-test suite passed with PostgreSQL 14**. Chromium
passed edit, archive, restore and delete plus the receiving-to-packaging workflow,
with no page errors or horizontal overflow at desktop, tablet and phone sizes. One
browser assertion was corrected for CSS uppercasing of the deleted-record heading;
the rerun passed. The existing TestClient/httpx deprecation warning remains.

The training database was backed up and upgraded to migration 0006. All 14 containers
and their existing values, 11 portions, 16 movements, 9 operations, 1 run, 1,000
particles, 8 profiles, 4 receipts and 47 audits were preserved. Existing containers
remain active at revision 1. The authorized LAN WebGUI was restarted; no machine
software was invoked. Deletion/archive of occupied material is intentionally
unavailable; material must first be moved/accounted through an operation.

## Required container weights update

New containers require positive tare weight and net material capacity in kilograms,
stored as integer milligrams. The form, service and database enforce both fields;
registered values cannot be cleared. Container detail displays the values and tare
evidence. Creation audits record quantity, units, operator and time. Real entered
tare is an operator declaration; training tare is simulation. It does not alter
entered material net mass. Demo fixtures now supply explicit synthetic tare/capacity.

Migration 0005 adds a nullable tare column for historical records, with guards for
new registrations. All 13 existing training containers, their original capacities
and all material/audit records survived the forward migration unchanged. Missing
historical weights display “Not recorded”; no backfill was invented. A verified
SQLite backup was saved before upgrading and restarting the authorized LAN WebGUI.

Validation: **74 distinct tests passed on SQLite** (9 focused container/web and 65
remaining regression tests), and the **74-test suite passed with PostgreSQL 14**.
Tests include missing/invalid values, precision, direct database enforcement,
forward migration with legacy data, and independence of tare from material balances.
Chromium passed the full workflow and container creation/display checks at desktop,
tablet and phone widths, without page errors or overflow. Only the existing
Starlette TestClient/httpx deprecation warning remains. No hardware was invoked.

## Simulation Settings update

The operator WebGUI now has an editable **Simulation Settings** tab. Its initial
mean is **0.15 g per green coffee bean** and capture probability **99%**. Research
sources and the distinction between a practical assumption and a universal measured
mean are included on the page and in the README. New receipts can accept a
mass-derived count estimate, visible in stock/portion/container views. New simulated
jobs use planned processing mass by default and pin the settings at preparation.
Manual test-count overrides remain explicit. Original 97% runs and demo history
are preserved. Count assumptions do not modify entered mass, establish physical
capture, or make an unknown product specification pass.

Migration 0004 adds append-only settings and operation-assumption records. The
application initializes defaults without replacing existing settings. Short batch
ingestion now bulk-checks duplicates and inserts parent particles before monitor
events; the browser can generate remaining batches automatically and resume after
disconnection. The practical fixture guard is one million particles per run;
larger estimates produce an explicit error instead of truncation.

Verification for this update:

- **69 distinct automated tests passed on SQLite** (43 existing capture/workflow/web,
  11 settings/conversion/ingestion tests, 15 handling/adapter tests); the complete
  **69-test suite passed on PostgreSQL 14**. Both reported only the existing
  Starlette TestClient/httpx deprecation warning.
- Chromium exercised settings edits/history, receipt count preview and opt-out,
  automatic batches, sorting/release, split, blend and packaging. Desktop, 768 px
  tablet and 390 px phone checks passed without page errors or horizontal overflow.
  Artifacts are in ignored `var/qa/`.
- A disposable **20 kg / 133,333-particle** run with 99% capture completed, including
  reopening its database at 50,000 particles and replaying the first batch. Expected
  Selected/Pass-through were 26,400.33 / 106,932.67; synthetic outcomes were 26,405 /
  106,928, with 262 synthetic timeouts and no physical confirmations. Independent
  entered 4 kg / 16 kg output masses reconciled and released. Generation took 92 s
  and the complete service-level exercise 122 s on this host; these are local test
  timings, not a production capacity guarantee.
- The existing training database was backed up with SQLite's backup API before
  migration. Integrity checks passed; all 11 portions, 16 movements, 9 operations,
  1 run, 1,000 particles, 8 profiles and 4 receipts were preserved. The authorized
  training WebGUI was restarted on `0.0.0.0:8090`; sign-in and pages passed from
  localhost and the host's LAN address. WireGuard routing was not changed or proven
  by these local HTTP checks. The temporary PostgreSQL test server was stopped.

Changed components: migration 0004, `simulation.py`, model/service/CLI/web code,
operator templates, `simulation.js`, settings/capture/count tests and browser
acceptance, plus these project documents. No machine-control modules were modified
or invoked. Hardware testing, a new live adapter, and production load testing were
not performed. The next step is operator testing with a representative counted and
weighed green-coffee sample to choose a lot-specific mean if needed.

## Working capabilities

| Phase | Delivered | Remaining boundary |
|---|---|---|
| 0: discovery/design | Repository map, modular application placement, domain/evidence/quantity decisions | Review assumptions against operational practice |
| 1: receiving to stock | Complete hardware-free guided workflow, migrations, authentication, labels, reservations, simulator, expected profiles, weighing, reconciliation, explicit release, audit and demo | Production deployment and operational acceptance |
| 2: sorter integration | Read-only archived Registry v3 exporter, optional exact-run statistics mapping, durable spool, batch import/replay, source provenance | Asynchronous live ingestion, installation identity, reconnect/cursor policy, performance qualification |
| 3: material handling | Whole transfers, partial splits/remainders, losses, returned count sampling, hold disposition, superseding unreleased sorting-weight corrections; re-sort released bulk through a new sorting job | General rework/adjustments, retained-material recovery, separately managed removed samples |
| 4: blending | Whole-portion manual plans/execution, pinned profiles/specifications, actual contributions, count-aware predictions, explicit unknown/compliance status | Partial multi-input recovery, real calibration comparability, richer constraints, optimisation |
| 5: packaging | Versioned SKU/format, actual per-pack net masses, product lots/labels, remaining bulk/losses and receipt genealogy | Individual pack placement/movements, returns and opened-pack rework |
| 6: physical verification | Replaceable simulated monitor interface | Physical chute detectors, association assurance and automatic machinery |

All implementations share one material ledger and lineage. There are no placeholder
Blending or Packaging screens: their bounded manual workflows execute through stock
release. None of this starts a conveyor, valve, GPIO, camera, serial link or sorter
service. Real and synthetic stock remain in separate databases.

`beanosort demo` seeds the original receive/sort example. `beanosort demo-chain`
adds a repeatable 20 kg receipt → 16/4 kg sort → 5/11 kg split → 5+3 kg blend →
six packs totalling 6 kg plus 2 kg bulk genealogy. Both reject operational real-mode
seeding; repeated commands do not duplicate stock.

## Verification actually run

| Check | Result |
|---|---|
| Full domain/authenticated HTML suite, SQLite | **57 passed** |
| Same full suite, isolated PostgreSQL 14.24 schemas | **57 passed** |
| Final focused adapter suite after colour/mock-model provenance refinements | **5 passed on each backend**, including one additional test (58 distinct tests verified across runs) |
| Chromium 140 / Playwright 1.55 | Sorting → split → blend → packaging → released product lot passed; zero page errors |
| Browser responsive checks | Stock, blend, packaging and product pages at 768/390 px: no horizontal overflow; desktop/product and label views inspected |
| Actual November archived Registry export/import/replay | **2,856 records imported**, replay inserted **0**; no stock ledger changes from ingestion; no simulated or physical capture declarations |
| Actual September archived Registry export/profile | **18,225 records**; mock classifier and virtual actuator provenance retained; no physical capture confirmation |
| Source/migration/test Python compilation and Git whitespace check | Passed |
| Existing BeanoFlight regressions from the preceding Phase 1 work | **47 passed**: registry 21, classification 5, timing ledger 1, live statistics fakes 8, synthetic statistics bundle 12 |

The actual November import used an automatically removed, disposable real-mode
**test** database with clearly identified fixture mass records. It did not create
operational inventory. At p=0.97, its 63 valid reported action attempts yield 61.11
expected Selected and 1.89 expected Pass-through; 2,793 destinations remain unknown.
Reported gate cycles never become physical capture verification. The September
archive's virtual actions do not count as real attempts, and its mock categories do
not become real material characteristics.

Tests exercise precision, conservation, conflicting reservations, WIP after failures,
explicit remainder/loss/discrepancy, immutable evidence/configuration, stale labels,
container reuse, corrections, reviewed holds, count weighting, unknown specification
properties, actual versus nominal pack mass, full receipt genealogy, and retry safety.
Spool recovery tests interrupt after the first committed 500-record batch and resume
without changing particle identities. Synthetic capture tests cover p=0/1/0.97,
1,940/8,060 expectations, one destination per bean and no fake physical confirmation.

A single upstream Starlette TestClient deprecation warning remains. Current browser
screenshots and logs are under ignored `var/qa/`; exported archives are under ignored
`var/`. No new dependency or hardware test was needed for the offline adapter.

## Important limits

- Archived imports are intentionally read-only. There is no live controller API,
  Registry subscription, physical detector, automatic blending/packing or optimiser.
  Machine network-loss/failsafe behaviour is unchanged.
- Requested recipe metadata is distinct from archived execution settings. The
  adapter pins the latter but does not claim that a requested recipe was applied.
- Only inspected schema 3 live-source archives and matching statistics bundles are
  accepted. Duplicate per-bean decisions/actions, wrong/shortened run identities,
  active runs, hash changes and malformed data are rejected explicitly. An import
  that covers all archived tracked records still has unknown physical coverage.
- Real feature comparability remains unknown; a returned count sample cannot
  manufacture calibration compatibility. Mock classifications remain simulation.
  Pixel volume is never converted into calibrated mm³ or measured mass.
- Blend planning reserves complete independently handled portions; split first for
  partial contributions. Actual input contributions must match those portions.
  Unknown evidence is never a compliance pass. Batch prediction does not prove
  physical uniformity or every package's composition.
- Manual output discrepancies are recorded and held under the pinned policy.
  General manual-operation correction, partial multi-input blend recovery and
  retained-material recovery require later reviewed workflows. The implemented
  correction applies only to existing, unreleased sorting output streams.
- Product lots currently share one collection vessel. Individual pack movements,
  opened packs/returns, attachments, moisture measurement entry and richer product
  constraints are deferred. No legal labelling or regulatory certification is claimed.
- Writes use a short global accounting lock; production multi-station load, live
  latency, external scanner/printer devices, HTTPS deployment, compose startup and
  backup/restore operational drills were not tested. Backup instructions cover both
  relational data and external evidence artifacts.

## Changed components and next milestone

All additions/changes remain under `BeanoFlight/beanosort/`: `adapter.py`,
`handling.py`, database models/migration 0003, evidence/service logic, CLI/demo,
operator routes/templates, tests and documentation. The original sorter and existing
uncommitted repository work were preserved. No commits were pushed or hardware
services deployed. Disposable test servers were stopped; the separately authorized
LAN training WebGUI remains running.

The next concrete milestone is supervised operational acceptance of receiving,
weighing, reconciliation and manual handling on the target server, followed by a
separate durable **read-only** live Registry adapter with explicit station identity,
gap recovery and performance measurements. Extend rework/correction coverage before
adding optimisation or any physical automation.
