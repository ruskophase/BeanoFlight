# BeanoSort — material workflow

A local material-workflow application inside the existing BeanoFlight repository.
It has its own dependencies and database. **Sorter Controls** provides explicit
manual operation through the existing machine WebGUI's HTTP service. BeanoSort
does not import hardware drivers or open serial ports. Sorting jobs and inventory
transactions do not automatically operate the machine.

The working slice is containers → receipt → reserve → simulated sorting → entered
weights → reconciliation → explicit release → stock, labels and traceability.
The simulated upstream is a compact fixture, not a replacement tracker/classifier.
An offline Registry adapter imports archived real runs. **Material handling** offers
four functions: move a container, move contents, **Package as product**, and
reconcile a container's current weight. Contents movements handle
whole or partial transfers into empty or occupied vessels in one confirmation.
Whole moves use the existing recorded balance without a weighing; partial moves
require a measured net mass. Whole moves into an empty vessel retain their portion
ID; splits and combinations create new portions with recorded ancestry. Combined
population evidence is retained only when all contributing portions have known
evidence; combinations involving unknown contents remain unknown. Container reweighing retains its prior
measurements and records signed adjustments; zero net contents closes the portion.
Known source populations retain their joint measurements, estimated count weights
and archived stereo photographs through combinations, splits and representative
weight changes. Portion and container profiles link to interactive contents charts
with interval-level photo and colour inspection. Prior profiles remain historical.
Existing quality holds and active-job locks are preserved. **Package as product**
records work at the **Packaging desk**: weigh either the contents removed or what
remains after packaging. The packaged mass leaves inventory as Final product.
Directly weighing removed contents calculates the remaining balance without
reweighing the source container. No individual
packets, product units or product stock are created. Partial packaging retains the
source portion and its representative population evidence; zero remaining contents
closes it and frees the container. Quality-held material must be released first.
Migration **0014** registers the Packaging desk; existing stock and history are
unchanged. Historical manual jobs and their original completion pages remain available.

**Finish / stop accepting new processing** opens final container weighing directly.
Confirm final weights only after feeding stops and in-flight beans are accounted
for. **Reconcile** records that boundary confirmation with the weight review;
there is no separate "Await reconciliation" action in the browser. Viewing the
weighing form alone does not change inventory or confirm that processing is clear.

Container dropdowns throughout the application show the container's location,
recorded net contents and nominal capacity. Contents include reserved material;
available source mass is shown separately. Dropdown masses use three decimal
places. Sorting containers awaiting weighing
are identified explicitly, as are capacities that have not been recorded.

## Start a hardware-free development system

From this directory, using Python 3.10 or later:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
mkdir -p var
cp .env.example .env
```

Replace `BEANOSORT_SECRET` in `.env` with a locally generated random secret:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
set -a
. ./.env
set +a
beanosort init --username admin
beanosort demo --username admin
uvicorn beanosort.web:create_app --factory --host 127.0.0.1 --port 8090
```

`init` prompts for a password of at least 12 characters. There is no default
password. Open <http://127.0.0.1:8090> and sign in. Keep the command running while
using the application. Repeat `init`/`migrate` safely; existing passwords are not
reset. `demo` is repeatable after a successful demo and refuses real databases.
`requirements.lock` records the exact packages used for this implementation's
verification; use `pip install -r requirements.lock` before `pip install -e .`
when reproducing that environment.

The demo contains a completed 20 kg synthetic job with two released outputs and
a second 20 kg source with two empty destinations for a new guided run. Every
training mass, feature, decision and monitor result is labelled synthetic.
For a repeatable complete genealogy, also run:

```bash
beanosort demo-chain --username admin
```

This withdraws 5 kg from the 16 kg sorted portion (11 kg remains), combines it with
3 kg from a second synthetic receipt, records six packs totalling 6 kg, and retains
2 kg bulk. It prints the product-lot ID. Re-running it creates no duplicate stock.
The separate 20 kg ready source remains available for interactive sorting.

New containers require **tare weight** (empty vessel mass) and **material capacity**
(nominal net material mass), both positive decimal values in kilograms. These are
stored at milligram precision and shown on the container detail page. Capacity is
checked against material net mass; registration tare is not automatically deducted
from an already entered net weight. The creation audit records both values, operator
and time, with entered tare labelled operator declaration or simulation. Existing
containers retain unknown tare/capacity where absent; migration 0005 invents no values.

Open a container and choose **Edit container** to change its name, location, vessel
type, tare and capacity. Tare and capacity remain required. Edits are audited with
previous/new values and a revision check protects against simultaneous edits.
Active jobs lock their source/destination details; capacity cannot be reduced below
the contents' recorded mass. Existing measurements retain their recorded tare basis.

Supervisors and administrators can **delete any empty, unclaimed container** after
confirmation, including a vessel used before. Its material/job history and old ID
remain resolvable. Used registrations are retained for traceability and removed from
storage membership; unused registrations can be removed outright. **Archive** remains
available for temporary retirement and restoration. Occupied or active-job containers
cannot be deleted. Migration 0008 extends the earlier archive/revision model.
Open a container from the register, then use **Delete container** beside **Edit
container** on its detail page. This opens a confirmation page naming the container;
occupied/reserved containers show the reason deletion is blocked.

## Changing containers during a sorting job

On a running job choose **Pause & change containers**. Stop feeding using the
existing machine controls, clear beans already in flight into the current bins,
then replace Selected, Pass-through, or both. Confirm readiness and record the
change. The job stays paused until **Resume sorting workflow**. These buttons
record workflow state; they send no hardware stop/start commands.

Every replacement gets a separate collection identity and keeps its own features,
photos and weights. An unchanged bin spans segments and is weighed once. Removed
bins remain held for this job; save their gross weights and move them to storage
if needed. Final reconciliation includes all collections plus source remainder
(deduplicated for shared Source/Pass-through), with explicit discrepancy approval.

Migration **0012** adds immutable segment/reading records and collection bindings;
it does not rewrite existing jobs, particles or stock. Recipe/source changes and
reuse of an already used bin within the active job are prohibited. Real records
without a trustworthy local sequence boundary keep bin-level composition unknown.
See [operator steps](docs/operator.md) and [accounting design](docs/architecture.md).

## Receiving supplier beans

The receiving tab follows four steps: **Source details → Empty receiving containers
→ Net scale readings → Storage locations**. One receipt/source lot can create up to
20 independently labelled portions, each in a different container. A typical supplier
bag may be 60 kg; supplier bags are not inventory vessels, and the system records
only the actual sum of entered container net weights.

Choose physically empty, unclaimed containers. Older containers with missing tare
or capacity remain listed: enter only the missing weights beneath their selection.
These values are validated and saved atomically with the receipt, with operator
provenance; existing registered weights are changed through **Edit container**.
Already occupied/reserved containers are explained under **Containers unavailable
for receiving**. A chosen container is disabled/hidden in the other receiving rows
and becomes available again when changed or removed. Server-side duplicate checks
remain in force. Confirm the scale
was zeroed with each empty vessel, then enter the bean net mass in kg to three
decimal places. The stored tare is a reference snapshot and is never subtracted
from this net reading. Scale ID and resolution are omitted; entry to three decimal
places does not establish the scale's actual precision. Select a storage place for
each container and confirm placement before recording the receipt. The final commit
is atomic and idempotent; competing receipts cannot both fill the same vessel.

Receiving accepts up to **120% of nominal container capacity**: a 25 kg container
can receive up to **30.000 kg** of beans. The form shows each container's limit and
the server rejects higher entries. Actual net mass is posted in full; the nominal
capacity stays unchanged. Receipt measurement/audit records retain the capacity,
allowance policy and amount above nominal. This allowance applies to receiving.
Migration 0009 permits later moves and metadata edits of these loaded vessels;
capacity reductions below the current contents remain blocked. A partial sort or
withdrawal can leave an already overfilled remainder in the original container.
Other material-handling output loads retain their existing capacity checks. Sorting
receivers use the warning/pause policy and explicit overrides below.

New receiving records no particle-count estimate, synthetic features, classifier
properties or moisture. Source metadata remains a supplier declaration. The database
environment supplies mass-evidence kind automatically: real inventory uses the
operator's scale reading, while isolated training inventory retains simulation
provenance without a separate simulation choice in the receiving form. The receipt
page links every output container and portion to the shared source lot.

Existing records and the trusted legacy receiving command retain their historical
evidence. No database migration is needed for this UI/command addition.

## Manual Sorter Controls

Open **Sorter Controls** in the navigation bar. The default machine service is
`127.0.0.1:8000`, reached from the BeanoSort server. An administrator or supervisor
can save a different machine IP address/hostname and port. Saved connections are
versioned in the configuration audit history; no schema migration is needed.
`BEANOSORT_SORTER_HOST` and `BEANOSORT_SORTER_PORT` set the initial defaults.

The panel copies the existing ESP Web Controller's controls: paired camera
triggering, illumination, vibration frequency/duty/direction, conveyor
speed/acceleration/direction, high-speed triggering, valve-channel test and stop
buttons. Its copied **Candidate 3** preset loads drafts only; Apply and Start
remain separate operations. Status updates preserve edited fields.

Viewing the panel reads status without acquiring control or changing outputs.
**Take control** acquires the machine service's exclusive operator lease. Existing
WebGUI operators and trials keep their authority; BeanoSort cannot take their
lease. **Release safely**, leaving the page, or the existing heartbeat timeout
stops outputs according to the machine service's existing policy. Stop controls
require a signed-in operator and CSRF token but do not require the lease.

These are **physical controls even in the simulation inventory environment**.
They do not attach a physical run to a stock job, classify beans, reconcile
material, or turn workflow pause/resume into physical motor commands. Those
integration steps remain separate work.

Commands use authenticated same-origin HTTP routes with a fixed action allowlist.
Browser control identities are bound to the signed-in session and saved machine
connection. Changing the connection invalidates old panels; requests never
silently retarget another machine. Commands are not retried after an uncertain
response. Non-heartbeat command results are audited independently of the stock
ledger. Status polls and heartbeats do not take accounting write locks. The
machine service remains responsible for serial access, validation, firmware
keepalives and its three-second lease/five-second firmware failsafe.

The `/health` response reports `hardware_control: true` for available manual
controls and `automatic_job_control: false`. Health does not contact the machine.
Unit and browser checks use an in-memory/local HTTP fixture; they never operate
the real controller.

## Fast transactions and derived run summaries

Migration **0015** adds a rebuildable `run_summaries` table. Each accepted batch
updates the run and collection counters, distributions, reference-membership
weights and mass-prediction groups in the same transaction as the new observations.
Replaying a batch never counts it twice. Raw particles, photographs, historical
profiles, measurements and the stock ledger remain unchanged. Out-of-order evidence
updates the collection determined by its recorded sequence window.

SQLite reads use query-only deferred transactions; writes retain `BEGIN IMMEDIATE`
and the accounting lock. Provisioning and the reviewed upgrade enable WAL, allowing
readers and writers to work concurrently. Use SQLite's backup API for an online
backup; copying only an open database file can omit its WAL contents.

Stop the app for the upgrade and warm the derived summaries before serving it.
The same rebuild command can repair derived summaries later:

```bash
beanosort migrate
beanosort rebuild-summaries --username admin
```

The command supports `--run-id` and requires an administrator or supervisor.
Missing summaries have a read-only fallback and are rebuilt before the next ingest;
GET requests never silently modify evidence or inventory. Detailed job distributions
load when opened, independently of transaction confirmation. Capacity polling has
only one refresh request in flight at a time. Splits and combinations perform one
population-enrichment pass per output and reuse archive verification within the
transaction. Evidence checks and exact milligram accounting remain in force.

## Receiving-container capacity during sorting

The job page shows live fill bars for the current Selected and Pass-through
receivers, with mass, percentage and a nominal-capacity marker. Bars are
green below 90%, orange from 90–100%, and red above 100%. A shared scale expands
in 25% steps (125%, 150%, …) so overfilled quantities remain visible. Unknown
incoming mass is marked as a lower bound; an unrecorded capacity has no invented
fill percentage. Bars refresh using the existing capacity-status polling.

The job page shows an orange warning from **90% of nominal net mass**. Starting
above 90% is allowed. BeanoSort automatically pauses its workflow and simulation
at **100%**. An operator may select **Override capacity warning & resume**; the
next pause is **105%**, then **110%**, **115%**, and so on. Above 100% the warning is
red. If a passage or already-arrived batch crosses several bands, resuming
acknowledges its present fill and arms the next 5% band rather than looping at an
already acknowledged threshold. Overrides and pauses are audited.

Warnings use pinned starting contents plus estimated incoming mass. Reference
simulations use the existing area/mean-mass/capture model without display rounding;
legacy fixtures use planned mass per simulated passage. These are estimates, not
live scale readings. Manual/archived runs without an incoming mass model can warn
and pause on known starting contents, but unknown incoming mass is never invented.
BeanoSort has **no physical sorter control**; workflow pauses send no conveyor,
feeder or valve command. In-flight evidence is still retained when paused.

Changing receivers resets the new container's fill; unchanged collections retain
their fills and next-pause thresholds. A full replacement can be chosen with an
explicit capacity override. Reconciliation retains actual overfilled weights after
an earlier receiving-capacity override or explicit acceptance of a weighing review
showing the excess. Other quantity, quality and active-job guards still apply.
No schema migration is required; thresholds persist through audited overrides.

## Correcting a prepared sorting job

Selected and Pass-through choices now exclude each other and the source vessel.
Sorting destinations may contain suitable released, unreserved bulk stock. Preparing
pins/reserves their starting contents. Reconciliation uses final **total gross**
container weights; existing material is included in the accounting boundary. Positive
additions create new combined populations with both ancestries, while zero addition
keeps the original contents identity. Compatible simulated mixtures retain count-weighted
joint features/photos and can be sorted again. See [operator details](docs/operator.md).

Before using this release on an existing server, back up its database, stop the web
process, run `beanosort migrate` (including migration 0012), then restart. Startup does
not automatically migrate. The guard update preserves existing material/history.

Open a **Prepared** sorting job and choose **Edit prepared job**. The form includes
the source already reserved by that job and preselects its current bins, station,
quantity and configuration versions. You can correct the source or either output,
change planned mass, or select another recipe/configuration before starting.
Reconfirm physical readiness and that the job has not run, then **Save job changes**.

Saving creates a linked corrected job with a new operation ID. The original
configuration remains in history, while its claims are released and the corrected
claims acquired in one short transaction. Invalid or conflicting choices restore
the original job/claims automatically. The original simulation settings version is
kept unless explicitly changed; the count estimate is recalculated for planned mass.

**Delete prepared job & release containers** cancels the unstarted job and releases
its input reservation and both destination claims immediately. It disappears from
the current job list; the old URL retains its audit and reconfiguration link. No
material mass is consumed or moved by editing/deleting a prepared job. These actions
require a prepared sorting job with no run or material postings. Once a run exists,
editing/deletion stays blocked even while paused or failed. Started jobs must use
normal finishing/reconciliation. The same
accounting lock and idempotency keys protect races with Start and request replay.
No hardware commands are issued and no database migration is needed.

## Storage locations

Use **Locations** to add named places such as `Shelf --- NewBeans` and inspect the
containers recorded at each one. Existing container location names are registered
during migration 0007; fresh databases start with `Floor` and `LoadingBay`.
Names are unique without regard to case or surrounding whitespace. Locations have
no material balances, equipment configuration or capacity model of their own.

Select a registered location when creating/editing a container. To record where a
filled or empty vessel has been placed, open **Material handling → Move
container**, choose the container, then its destination location. Confirm you have
physically moved it and click **Record container move**. The container detail page
also offers a location move. This preserves contents identity,
material measurements and balances. Moves are audited operator declarations (or
simulation in training), with revision/retry protection. Active jobs lock these
moves; use the controlled job boundary before physically relocating those vessels.
Storage moves also work for older containers whose tare has not yet been recorded.

Location detail lists assigned, unassigned and archived vessels, with links to their
contents/history pages. Stock and portion pages link to their container's current
storage place. Locations support add, edit and delete. Renaming preserves the location ID and
updates the displayed name on its containers; past measurement/audit snapshots retain
their recorded names. **Delete location** appears beside **Edit location** on a
location's detail page for supervisors/admins. Its confirmation page allows deletion
only when no containers remain there, including archived vessels. Deleted location IDs resolve to an audit
record and their names can be reused for a new location. Existing processing stations
retain their separate role.

Visit **Evidence guide** for the 10,000/2,000-target expected-count example and
the real-inventory evidence distinction; it creates no inventory.

Follow [the operator walkthrough](docs/operator.md). New simulated jobs use the
intended processing mass and the editable mean bean mass in **Simulation Settings**.
Initially, 0.15 g per green coffee bean gives approximately 6,667 particles/kg or
133,333 particles/20 kg. The assumed capture probability is **99%** for new jobs.
The repeatable original demo and the Evidence guide retain their historical 97%
example; completed evidence is never rewritten.

Leave the optional test count blank to use the mass estimate, or enter 250 for a
quick single-batch demonstration. **Generate all remaining particles** processes
resumable batches of 250. Closing the page or stopping generation preserves committed
batches; reopen the job to continue. Execution still needs explicit finish,
reconciliation and release. The development guard is 1,000,000 particles per run;
larger estimates require a smaller planned quantity or an explicit test count and
are never silently truncated. No physical tracker coverage is inferred.

## Simulation Settings and count assumptions

Open `/simulation-settings`. Administrators can change the material description,
mean bean mass in grams, and sorting realization rate (capture percentage).
Enter a reason to save a new immutable version. Operators can read settings;
simultaneous conflicting edits require a reload. Historical receipts retain any previously accepted count assumptions, and prepared
jobs retain both their mean and capture model even if
settings change before starting or replaying them.

The current receiving WebGUI leaves particle count unknown. The mean is used later
when preparing synthetic sorting runs, as a **calculated prediction**. Simulation
Settings do not change real sorter controls or automatically select a new model for
real manual jobs.
Measured weights remain authoritative and separate. Mean mass is stored as integer
micrograms, stock as integer milligrams, and count expectations use Decimal arithmetic.

The initial 0.15 g is a practical rounded assumption, not a global measured mean.
[Cassamo et al. (2022), Table 3](https://www.mdpi.com/2073-4395/12/10/2540)
report green Arabica group means of 13.2–18.5 g per 100 beans at approximately
11–12% moisture. A [Kenyan Arabica study, Table 1](https://www.sciencedirect.com/science/article/pii/S0963996922013114)
reports 7,420 green beans/kg (approximately 0.135 g/bean). Variety, grade, moisture
and defects change the mean; use a counted, weighed representative sample for a
lot-specific assumption. These studies do not establish capture performance.

99% means one independent Bernoulli trial per targeted logical particle. It predicts
1% missed targets entering Pass-through, without forcing an exact quota. Expected
profiles use fractional counts; synthetic destinations provide no physical confirmation.

For an existing database, back it up, stop this web application, then run
`beanosort init --username EXISTING_ADMIN` using its existing environment. This applies
all pending migrations (including 0004/0005) and initializes settings only if absent; existing accounts, passwords,
stock and job configurations are preserved. Restart Uvicorn afterward. Migration
alone creates the tables; `init` supplies the initial settings and records their author.

## PostgreSQL and multiple operators

PostgreSQL is the shared-server target. SQLite is an explicit development option;
it serializes readers and writers in this first implementation. Both backends use
the same migrations and domain commands. No server database existed in the sorter
project; its Registry SQLite database is never reused.

Use an existing local PostgreSQL instance, or the optional database-only compose
file (`docker compose up -d database`) after setting `BEANOSORT_DB_PASSWORD` in
your ignored `.env`. Set the SQLAlchemy URL locally, then run `beanosort init`:

```text
BEANOSORT_DATABASE_URL=postgresql+psycopg://beanosort:URL_ENCODED_PASSWORD@127.0.0.1:5432/beanosort_training
```

`beanosort user alice --role operator` creates an ordinary operator;
`--role supervisor` permits release and holds. Administrators append recipe,
capture-model, reconciliation-policy, product and packaging specification versions. The shell CLI assumes trusted
server administration; ordinary operators use the authenticated WebGUI.

For real material, provision a **separate database**, set
`BEANOSORT_ENVIRONMENT=real`, use a different session secret/port and initialize
without demo seeding. Real mode supports manual workflow records and reviewed
compact-particle imports, including the offline adapter below. It cannot launch a
synthetic upstream fixture or claim a live machine connection.
Missing particle evidence yields unknown coverage and composition. A source run
reference is an operator association, not proof the machine executed a request.

For tablet/LAN use, serve behind a local HTTPS reverse proxy, set
`BEANOSORT_PUBLIC_URL` to its actual reachable address and
`BEANOSORT_SECURE_COOKIES=1`. Use normal firewall controls and proxy login rate
limiting. Labels encode that configured address; they never embed mutable mass or
profile data. Do not expose the development Uvicorn server directly to the internet.

## Read-only archived sorter integration

Only completed/failed **live-source BeanRegistry schema 3** runs are currently
supported. Export never connects to machine IPC or imports a controller. Supply an
archived database path, full source run ID and a new spool directory:

```bash
beanosort export-registry /path/to/registry.sqlite FULL_SOURCE_RUN_ID ./var/run-spool
# Optional: --statistics /path/to/matching/statistics-bundle
```

An optional feature bundle must declare the **exact full source run identity** and
match its `beans.jsonl` hash. Re-tracked/offline bundles with a different run ID are
rejected; shortened bean labels are insufficient. Unavailable features stay unknown.

In a real-mode workflow, prepare the source/destinations, record manual start with
that same full source run ID, then use **Import or replay an archived Registry spool**
on the job page as a supervisor, or:

```bash
beanosort import-spool BEANOSORT_RUN_ID ./var/run-spool --username admin
```

Export writes and syncs batches before its manifest. Import verifies all hashes,
identities and ordinals, pins the snapshot, then commits bounded batches. Retry the
same directory after interruption or a lost response. Committed batches are not
counted twice. Do not change a pinned spool; later/different evidence requires a new
reviewed boundary. An incomplete import displays missing tracked records. Even a
complete import does **not** establish a complete physical census.

`esp32-s2-gptimer` successful gate-cycle evidence can support an assumed capture
trial. Virtual actuator results do not establish real issuance. Absent/late decisions
remain unknown; no decision is never treated as intentional pass. All real output
profiles remain expected values, with zero physically confirmed captures. The
requested recipe and the archived source settings are separate immutable records;
the adapter does not assert they matched or send either to the machine.

## Sorting recipes and photographed reference simulation

For an imported reference, create the versioned hopper-emptying preset with
`beanosort hopper-recipe --username admin` (optionally `--dataset-id <id>`).
This idempotent command uses the latest registered reference by default. In
**Prepare sorting**, choose **Empty hopper · all beans to Pass-through**: Selected
is disabled. Source may also be Pass-through, requiring one final gross weighing;
different vessels require two. The preset exercises isolated simulation/workflow
recording and sends no hardware command. Existing selective recipes are unchanged.

Open **Sorting recipes** for the catalogue, exact versioned thresholds, reference
photos and population previews. Administrators can design a rule for lightness,
chroma, CamL major axis, ellipsoid volume proxy, CamL aspect ratio or projected
area (two views, mm²). Saving an
existing name appends a version. Comparisons are strict; unknown values remain
unresolved and cause no selection attempt.

The first recipe is **Dark outliers: L* below median minus 2 sigma**. It uses
`combined_lab_l_mean` from one aggregate per distinct tracked bean. The specified
October 26 bundle contains **1,129 distinct beans**, 3,304 stereo samples and 6,608
original photos. Its median is 24.0521249771 L*, population standard deviation
(ddof=0) is 2.9590313683, and the recipe selects **L* < 18.1340622405**. It targets
78 reference beans. A 99% capture assumption predicts 77.22 Selected and 1,051.78
Pass-through, including 0.78 expected missed targets. These are count expectations,
not measured masses or physically confirmed destinations.

Two area recipes use `projected_area_geomean_mm2_median` from that same reference.
The median is 33.1772670456 mm² and population σ is 9.9797807760 mm²:

| Recipe | Strict selection rule | Targeted reference beans |
|---|---|---:|
| Large area | Area > 53.1368285975 mm² (median + 2σ) | 47 |
| Small area | Area < 13.2177054937 mm² (median − 2σ) | 15 |

The legacy fixture recipe is retired in the running training catalogue and removed
from new-job choices. Its immutable version remains attached to historical/existing
jobs. Retired versions cannot be used to prepare new jobs; correcting an unstarted
job with a retired recipe requires choosing a current recipe. Existing jobs retain
their pinned execution configuration. Retirement is an audited command, not deletion
of the referenced version. Development fixtures still support the historical demo.

Register a reference without editing the source bundle:

```bash
beanosort migrate
beanosort reference-import /path/to/statistics-bundle \
  --storage ./var/reference-datasets --name 'Green coffee reference' \
  --source-url 'http://SERVER:8084/path/to/statistics-bundle/dashboard/index.html#overview'
```

This verifies manifest hashes, copies all aggregate records and two representative
photos per bean into a local content-addressed snapshot, and registers its default
recipe. Repeating it is safe. It creates **no stock, receipts or jobs**. The supplied
dataset contributes 2,258 representative images; its full sampled-frame photo set
stays in the original bundle. Runtime simulation/photos work without the original
dashboard server.

Choose the reference recipe for a **new simulation job**, or use **Prepare sorting
with this recipe** from its preview. The source may be received stock, a released
simulated Selected/Pass-through output, an unprocessed remainder or a representative
split. Further passes pin and resample the source population's own joint features
and photos with retained relative multiplicity. They never refill it from the full
unsorted reference. Use a recipe from the same dataset and finish/reconcile/release
each new pass normally. Populations without compatible retained joint archives, packaged material and
non-representative removals remain blocked from simulated replay.
Existing jobs retain their original recipe. Reference recipes currently execute
only in isolated simulation.

Selecting a source on **Prepare sorting** fills **Intended processing quantity**
with its full available mass. The field remains editable for partial sorting and
the particle-count preview follows the entered quantity. Changing the source fills
the new source's mass; reopening an existing preparation keeps its saved quantity.

The mass-derived count still uses pinned Simulation Settings, independently of the
reference sample size. Simulation samples whole bean records uniformly **with
replacement**, retaining joint properties, source IDs and photo links. Repeated
photos represent separate synthetic passages. Run ID, seed and passage sequence
deterministically fix each sample and capture outcome across retries/restarts.

Use **Population charts & bean photos** on a job or output container. Switch
expected/simulated series and percentages/counts, inspect bins, and browse
input/targeted/destination examples. The preview uses every original bean once;
a run chart uses ingested passages. Archived image measurements, synthetic
membership/decisions and predicted output counts are distinguished. Output weights
remain independent operator entries. See [operator steps](docs/operator.md).

The Input/Selected/Pass-through legend checkboxes show or hide each population's
lines. Click a chart interval to inspect its corresponding beans in the left panel,
with paired photos, recorded mean colour swatches and paginated passage counts.
Choose another visible population in that panel to compare the same interval.
Expected contributors and stored simulated destinations remain clearly distinct;
inspection does not modify stock. On narrow screens the panel appears above charts.

### Simplified simulation weighing

After finishing a reference simulation, open **Weigh containers & reconcile**.
Enter the **gross container-plus-beans weight** for Selected, Pass-through and
the source container; the registered tare is deducted. Confirm all three weights,
including the source if empty. Simulation suggestions use the pinned mean bean
mass scaled by each reference bean's area / mean area, with expected capture
membership. They are clearly labelled predictions and remain editable.

**Reconcile** previews the signed input-minus-accounted discrepancy without
changing inventory. Explicit acceptance records weights, recalculates estimated
counts and, for supervisors/admins, releases permitted outputs. Simulation-only
discrepancy acceptance is audited; real-inventory tolerance guards remain strict.
Sampling is omitted; optional retained/lost masses remain available. Original beans,
photos and destinations are preserved instead of being fabricated to balance mass.
The full gross/tare/model evidence is retained with reconciliation and profiles.

## Configuration and migrations

All configuration variables are in `.env.example`. UTC timestamps are stored;
display defaults to Africa/Johannesburg. `beanosort migrate` applies forward
Alembic migrations. Downgrades intentionally refuse destructive schema reversal.
Startup never migrates or seeds automatically.

To change assumptions, write a JSON file and append a version:

```bash
beanosort version capture_model 'Independent capture' ./capture-model.json
beanosort version reconciliation_policy 'Workshop policy' ./policy.json
```

Capture model fields are documented in `src/beanosort/capture.py` (`CAPTURE_DEFAULT`).
A reconciliation policy requires integer `tolerance_mg` and boolean
`allow_estimated_composition`. The provided zero-discrepancy policy is a conservative
development choice, **not a universal acceptable process discrepancy**. Set an
approved policy before preparing a job. Each job pins exact version IDs.

## Tests

```bash
python -m pytest -q
python -m compileall -q src
```

The suite migrates temporary databases and tests domain rules independently of
the UI, plus an authenticated end-to-end HTML form workflow. To run the same suite
on PostgreSQL, create a dedicated test database whose name ends in `_test`:

```bash
BEANOSORT_TEST_POSTGRES_URL='postgresql+psycopg://user:password@localhost/beanosort_test' python -m pytest -q
```

Tests create and drop only unique `test_<uuid>` schemas in that test database.
They never connect to Registry, ESP services, camera capture or machine IPC.
See [phase status](docs/status.md) for actual verification and known limitations.

The retained-machine chart tests use fake job events in both real/simulation test
databases, including reconciliation into empty/occupied containers, missing
measurements, historical cutoffs, replay labels and unchanged profiles/stock:

```bash
python -m pytest -q tests/test_measurement_charts.py tests/test_machine_jobs.py
```

Optional browser acceptance uses a disposable simulation database, starts/stops
its own loopback server and writes screenshots under ignored `var/qa/`:

```bash
python -m pip install playwright==1.55.0
playwright install chromium --only-shell
python tests/browser_smoke.py
python tests/browser_reference_smoke.py
python tests/browser_receiving_smoke.py
python tests/browser_material_smoke.py
python tests/browser_busy_smoke.py
```

Set `BEANOSORT_REFERENCE_BUNDLE=/path/to/statistics-bundle` for the second browser
test to use an actual bundle; otherwise it creates a tiny synthetic fixture. Both
use disposable databases/snapshots. The busy-indicator test holds real workflow
requests at a synthetic network gate and checks compositor animation, validation,
error/history recovery, form payloads and single reconciliation posting. It never
changes live stock. The normal domain/HTML tests require neither
Playwright nor a browser. To validate
an actual archive against export, import and replay without touching operational
inventory, use the optional disposable test harness:

```bash
python tests/archived_run_smoke.py /path/to/registry.sqlite FULL_SOURCE_RUN_ID
```

It creates and removes its own test database, records no physical output weights,
and checks that ingestion does not post stock movements.

## Backup and restore

An explicitly requested fresh **simulation** inventory can be staged offline with
`beanosort.maintenance.stage_training_inventory_reset(source, staged, backup)`.
This is an administrator maintenance function, not an operator WebGUI action. Stop
the training server first. It refuses real inventory and existing output paths,
archives the entire source, and prepares a separate verified copy with empty
material/workflow tables while retaining configuration, vessels, locations,
reference samples, recipe retirement, users and configuration audits. It does not
activate the copy. Review its result and integrity/foreign-key/schema checks before
atomic replacement; invalidate old sessions and restart without running `demo`.
The archive keeps the old material history. A software reset does not establish
that any vessel is physically empty or clean. For the most recent requested reset,
see [phase status](docs/status.md).

PostgreSQL: take a `pg_dump --format=custom` backup using locally supplied standard
libpq connection settings (`PGHOST`, `PGDATABASE`, a protected `.pgpass`), and keep
it outside the working directory. Use a database owner for backup; restore into a
new, empty database using `pg_restore --no-owner --dbname=NEW_DATABASE backup.dump`.
Check `/health`, ledger totals, sample lineage and labels before switching the
server URL. Never restore over the only live copy. PostgreSQL triggers and pinned
records are included. Keep the original session secret private or rotate it to
invalidate existing sessions after recovery.

For SQLite development, stop BeanoSort and use the SQLite backup API (or `.backup`
with its CLI) to a new file. Do not copy an active database file without its WAL
state. Restore to a new path and check the immutable environment marker. The
application stores no raw camera frames. Keep a paired backup of
`var/reference-datasets/` (features, manifests and representative photos), imported
spool directories and referenced Registry/statistics artifacts with their hashes.
Database backups alone do not contain those files. Restore snapshots at their
recorded paths before starting the app; unavailable/changed snapshots block
reference simulation instead of substituting fixtures.

Architecture and invariants: [architecture](docs/architecture.md).
Actual sorter evidence and gaps: [integration map](docs/integration.md).
