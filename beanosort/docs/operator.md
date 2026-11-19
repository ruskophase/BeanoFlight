# Operator walkthrough and evidence semantics

## Enlarging a population chart

On a portion's population evidence page, click a chart's **title** to open that
chart on its own. The chart fills the available page width, with matching
bean photos and colour samples in a responsive grid beneath it. Click an interval, or focus
the chart and use ← / → then Enter. **Clear selection** clears those matches while
keeping the inspector available. Photo availability and evidence meanings are
unchanged; the inspector's expandable evidence note retains the detailed caveats.

**← Back to population evidence** returns to the page and chart you opened it from,
including its historical profile, contribution/collection or combined-contents
scope. Selected lines, vertical scale, evidence basis and inspected interval/page
are carried through the link. The focused view also works after refreshing or
opening its URL directly. Photos use ordinary page scrolling beneath the chart
on all screen sizes. This works for machine observations, archived-reference sorting results
and retained portion populations. It does not recalculate or rewrite stock evidence.

## Manual high-speed camera during sorting

On a live sorting job, click **Manual high-speed camera ↗** to open a separate
tab. The live job page can keep updating while you edit camera settings. This
also works for live jobs that did not request an automatic camera recording.
The tab uses the sorting job's existing machine lease through a camera-only API;
do not release sorting control or take over the ESP32 in another GUI.

Click **Use manual camera controls**. This ends any automatic FastCamUSB window
and waits for its recording/camera cleanup before enabling manual actions. That
automatic window will not resume. An unrelated existing camera session is not
silently taken over. Opening the tab alone only reads status.

1. Adjust camera settings if needed and click **Apply camera settings**. Values
   start from the current Pi/Waveform Generator settings; polling preserves edits.
2. Click **Start camera**, wait until ready, then **Record** (or **Record (arm)**
   with external triggering). **Stop recording** keeps the camera available for
   preview and another recording. **Stop camera** closes the USB session.
3. Toggle **Show preview in this tab** to pause browser preview requests. The
   preview is capped at five frames/s and pauses while the tab is hidden. Camera
   preview settings can also disable it during recording; acquisition settings
   require the USB camera to be stopped first.

External mode enables GPIO33 on camera start using its **existing** trigger and
illumination configuration, then disables it when the camera stops. Timing cannot
be reconfigured through this tab during a sorting run; free-running mode needs no
GPIO33 trigger. These controls do not change the conveyor, vibration motor, stereo
camera timing, valves or sorting recipe. Requested and acknowledged camera actions,
operator identities, failures and reported files are retained in the job journal.
The preview/video is separate from tracked-bean features and chute verification.

Closing the tab leaves the recording running. Job END/STOP, failure or completion
cleans up its manual camera session before releasing control. Lost activation
acknowledgements are not retried automatically. A disconnected tab shows unknown
status and disables actions; unsaved edits survive reconnection. Prepared,
finished and replay jobs cannot issue manual camera commands. During a live job,
controls are available while feeding or paused. No direct browser access to the
Pi is needed, including when the browser connects over WireGuard.

## Optional FastCamUSB recording during a live job

In **Prepare a sorting job**, choose the live-machine recipe and **Live cameras**.
Expand **Optional FastCamUSB recording** to load the most recent Waveform Generator
camera/listener settings. The section starts collapsed, and recording starts
unchecked. External triggering starts checked; other settings use the current GUI
values. You can edit resolution, playback/stream fps, exposure, preview rate,
device, output folder, preview options and GPIO33 trigger timing. Loading defaults
or preparing a job sends no camera or GPIO command. Offline defaults show a retry
message; leaving recording unchecked keeps the ordinary sorting workflow.

Check **Record with FastCamUSB during this job**, then enter **Start + seconds**
and **End + seconds**. +10 to +40 requests a 30-second window. Times use elapsed
seconds from the first feed-start acknowledgement, including pauses; they are
independent of the active-feeding counter. The window must end within the planned
feeding duration. END/STOP or an interrupted job can shorten or cancel it. The
AVI playback fps and actual capture timing are distinct; use the saved timestamp
CSV for actual frame timing rather than assuming an exact video duration.

On START, the optional camera is prepared before feeding: requested settings and
GPIO33 configuration must be acknowledged, and the USB camera must become ready.
An existing unmanaged camera session is not taken over. External mode arms the
camera and enables GPIO33 for the window, then disables GPIO33 and disarms it;
free-running mode uses RECORD/STOP. The camera is stopped during cleanup. The job
page shows requested timing, reported recording state, early termination/errors,
acknowledgements and reported AVI/CSV paths on the Raspberry Pi.

New recordings also show buffered frames/bytes and their peaks, longest recorder
write, write-stall count and any rejected/abandoned frames. The recorder uses a
five-second frame budget (900 frames at 180 fps) and a 64 MiB compressed-byte cap,
whichever is reached first. This absorbs brief output stalls but still fails
explicitly if output cannot recover. A diagnostics JSON path accompanies the AVI
and CSV. The Pi's first recorder-failure time and when the job received that failure
are separate; failure does not masquerade as a successful full recording window.
An old run without diagnostics says they are unavailable. Lost status polling
shows stale/unknown telemetry rather than treating retained values as live.

Camera network work runs separately from tracking and the machine heartbeat.
An auxiliary recording error after feeding starts is reported separately; it does
not introduce a new automatic sorter-stop policy. Lost activation replies are
not retried. Shutdown uncertainty remains an error, not proof that recording
stopped. Existing controller lease/failsafe behaviour still applies.

This USB recording is separate from CamL/CamR bean crops used by population
charts. Its frames are not automatically associated with tracked beans, and its
files are not downloaded into inventory records. Keep Pi recordings with your
external evidence backups. Replay jobs cannot activate this physical camera.

## Live-machine population charts

From a completed All Pass-through machine job, open its Pass-through container or
material portion and choose **Population charts & measurement evidence** in
**Population evidence**. The job itself also has this link once its run exists.
Previously completed jobs work without being rerun. The reference-simulation
**Population charts & bean photos** view remains available for those simulations.

The machine view plots recorded lightness, chroma, major axis, projected area and
ellipsoid volume proxy. Toggle the population lines, choose percentage/count, or
click an interval to inspect matching tracked records and available camera mean-RGB
swatches. A bean measured on several frames contributes once. Known/missing counts,
units, bin edges, denominators, mean/median and population SD are visible. Missing
features remain unknown. Input and intended Pass-through coincide in an all-pass
run; neither proves arrival in a bin or a complete physical census.

For an addition into an occupied container, the portion view includes earlier
observations from its pinned additive contents history. Compare **Latest addition
observations**, **Earlier contents observations**, and **Combined retained
observations**. **Recorded contributions and photograph settings** links each
source's frozen profile/run view and shows its own image-saving mode. A latest
**No image saves** choice does not remove earlier photographs.

When inspecting the combined series, records with saved crop references appear
first; records without photos remain in pagination and the matching totals.
Each card shows its original run and image mode. Photos are never borrowed across
runs, even if their local bean numbers match. Choose a contribution link to see
that run alone. Combined histograms/statistics describe count-weighted retained
observations, not mass-weighted composition or a physical census of the vessel.
Earlier material without independently scoped observations remains explicitly
unknown. Splits/re-sorting do not imply individual bean recognition or photo matches.

A saved profile uses its historical evidence cutoff rather than silently including
later received observations. Prior contributions use their pinned profile versions.
Replay is labelled separately from live observations; live cameras do not convert
training inventory into real inventory. Unprocessed source material remains unknown.

With **Save stereo bean crops**, click a chart interval after the run completes to
see available CamL/CamR photos alongside the colour swatches. Each track uses its
earliest saved crop pair; its chart measurements may aggregate several frames.
Click a photo to open it at full size. Photos load on demand through the server;
the source files stay on the machine. Missing files or a disconnected machine show
an unavailable-photo message without removing the colour sample.

**No image saves** means no bean photographs were saved. **Save RAW frames** does
not create individual bean crops for this inspector. Numerical measurements and
recorded colour swatches still work in either case. Chart/photo browsing changes
no stock mass, profile, recipe, machine settings or outputs. Images are camera
observations, not verified bin arrival.

## Waiting for longer actions

Execution-boundary changes, loading container weighings, reconciliation review,
final acceptance/release and other server requests show an animated spinner and an
action description until the result loads. No completion percentage or remaining
time is invented. The waiting panel prevents repeated clicks while a request is
pending; required-field validation stays on the form, and server errors show their
normal review page. Returning with Back clears any previous waiting panel.

Background simulation retains its recorded-particle progress bar and Stop button,
with a small spinner while batches are pending. Refreshing its final population
evidence shows the waiting panel. Loading beans for a chart interval also shows a
small local spinner. Reduced-motion preferences are respected.

## Pause, replace full output containers, resume

1. On a running job choose **Pause & change containers**. This pauses the workflow
   record and simulation generation. Stop the physical machine/feed using its
   existing controls; BeanoSort does not operate it.
2. Let beans already in flight clear into the existing bins before removing them.
   Keep either current destination or select replacements for one or both streams.
   Selected and Pass-through cannot share a vessel in a selective recipe.
3. Confirm feeding stopped, in-flight material cleared and destinations in place.
   Record the reason, then **Record replacements**. Review the new assignments
   before choosing **Resume sorting workflow** and resuming physical feeding.
4. **Container collection history** lists current and removed fills. A bin that was
   not changed continues the same collection across boundaries. A removed bin stays
   reserved/WIP; it is unavailable to receiving, other jobs, blending or packaging.
   Print its collection label, save/revise its gross weight, or use **Move removed
   container** to record storage placement. A saved reading is versioned, not edited
   away. Keep the contents unchanged until final accounting.
5. Finish and confirm in-flight material as usual. **Weigh containers & reconcile**
   lists every distinct fill, plus the source remainder. Confirm every gross weight,
   including saved readings; tare is deducted once per vessel. Review the signed
   discrepancy, then accept/release under the existing policy. Outputs get separate
   portion identities, recipe/stream captions, lineage and collection-scoped charts.

Bins already used in the job cannot be reused or assigned to its other stream.
Suitable released existing stock can be a replacement destination; its starting
contents are pinned and included in accounting. A known full bin cannot be a new
replacement. Capacity is a nominal mass limit, not a physical fill-level sensor;
the operator decides when to pause. Source, recipe and capture assumptions remain
pinned. Feeding repeatedly from the reserved source is supported; another source
population requires a separately recorded input/job. Changing the recipe requires
a new job. Cancel is not a way to clear material that has already moved.

For **Empty hopper**, only Pass-through is needed. Source can also be a collection,
including after a swap: weigh that vessel once at the end because it includes
unprocessed source beans. Its early weight cannot be saved while feeding can still
change it. Other removed bins can be weighed immediately. No separate source
remainder is counted when its final contents are included in a collection.

Simulation boundaries use the committed particle sequence and retain original
seeded destinations. For manual/archived real records, enter the first local
particle sequence after the cleared boundary only if actually recorded. A source
time needs an explicit timezone. Leaving the sequence blank preserves material
accounting but makes collection membership/composition unknown; server receive
time cannot supply this evidence. Late/replayed records use their source sequence,
not whichever bin is current on receipt. Neither simulated monitoring nor operator
boundary declarations establish physical capture verification.

Review final weights before accepting. The old single-stream post-reconciliation
correction form is unavailable for jobs with swaps; a general multi-collection
correction workflow remains future work. Independent loss/retained entries and
unexplained discrepancies stay explicit. Output release is still required.

## Hopper emptying and ordinary job evidence

For hopper cleanup choose **Empty hopper · all beans to Pass-through**. Every
simulated passage intentionally passes, even when a feature is unknown. There
are no selection decisions/actions and no Selected container or Selected weighing.
The capture-rate assumption does not reduce intentionally passed material.

Choose a separate Pass-through vessel for two final gross weighings (Pass-through
and Source), or choose the **same vessel as Source and Pass-through** for one
weighing. That final gross weight includes returned hopper beans and any beans
still in the vessel. Its tare is deducted once; the whole source is reserved once,
consumed once and replaced by one resulting portion with complete lineage. No
separate source remainder is posted. This weight does not separately establish
how much went through the hopper; that quantity remains unknown. Explicit losses,
retained material and discrepancy approval still apply. Confirm in-flight material
as usual before weighing.

Same-vessel simulation weights assume the whole source mass is retained. Particle
charts describe generated passages; unprocessed contents retain an inherited
population estimate. Partial/interrupted simulations and later re-sorting retain
that description. A different occupied Pass-through vessel uses normal combination
rules. Standard selective recipes still require three different container roles.
This reference-backed preset is available in the isolated simulator. BeanoSort
sends no machine commands or physical hopper-empty confirmation.

Operation IDs also carry a caption with the pinned recipe/version and the named
**Source**, **Selected** and **Pass-through** containers. These are the assignments
recorded for that job, including prepared/cancelled jobs; later transfers and
container reuse do not change which vessels the job used. Names resolve the same
permanent vessel identities. The caption appears in job lists/details, prepared-job
editing, material operations and traceability/measurement history. Receiving and
manual handling say **No sorting recipe** and use their applicable receiving or
source/output container roles. Unrecorded history stays explicit. The caption is
workflow configuration/history, not independent proof of physical execution.
After a sorting job is completed, each vessel name includes its reconciled net
bean mass, for example **Pass-through: AmmoCrate---0009 (4.1 kg)**. Source shows
the mass **remaining** after processing, including explicit zero. These are final
job weights, including any starting destination contents, and remain unchanged by
later stock movements. Approved superseding measurement corrections are respected.

Portion IDs now have a caption with the recorded sorting recipe, its exact version
and the output stream, for example **Dark outliers: L* below median minus 2 sigma
(v1) · Selected**. The same caption appears in stock, container/current-history,
portion/traceability, job, material-handling and contents-label views. Selecting a
sorting source also shows its caption. Changing a recipe later does not relabel
existing material. Received material says **No sorting recipe**; unprocessed
remainders are explicit. Splits retain their parent's recipe/stream with a split
qualifier, while blends point to contributing histories. A sorting addition into
an occupied vessel is marked **Combined with existing contents**, rather than
claiming the previous beans passed through the new recipe.

The **Containers** register lists each vessel's current net contents mass, a
linked population ID for its currently assigned portion, and a clickable
storage location. Mass comes from the same movement ledger as **Stock & jobs** and
excludes container tare. Unassigned vessels show **No contents assigned**; their
zero recorded mass does not establish physical emptiness or cleanliness.

During **Prepare sorting**, Selected and Pass-through exclude each other's chosen
container, and neither can use the source vessel. Changing/clearing a choice makes
it available again. Destinations may be empty or contain released, unreserved bulk
beans. Choices show their existing mass and nominal capacity; confirm empty vessels
are physically empty/clean and existing contents are suitable for combination.

Preparing reserves both the feed and any starting destination populations. Starting
keeps them in work-in-progress; they cannot be used by another station. Deleting or
editing an unstarted job releases/replaces these reservations together. At weighing,
enter the **final total gross container weight**, including its earlier beans.
Simulation suggestions add the new predicted beans to the pinned starting mass.
The review separates starting contents, new additions and final totals; its input
boundary includes feed plus starting destination stock. Prior beans are not counted
as new sorter observations or treated as beans that passed through this recipe.

A positive net addition creates a new combined portion linked to the feed and the
previous destination portion. The previous contents label becomes historical.
Zero net addition preserves the prior portion's identity. Release still requires
reconciliation and the normal policy/quality approvals. Sorting capacity
warnings and pauses can be overridden explicitly as described below; the
receiving-only 20% allowance is unchanged. A final
destination mass below its starting contents is a removal/rework case and is blocked
by this addition flow; the existing single-input correction form does not apply to
combined-destination measurements. Review/change weights before accepting them.

Compatible simulated combinations retain whole reference feature/photo records,
weighted by estimated bean counts rather than directly by mass. Their charts compare
**Before addition**, **Added this run** and **Combined contents**. They can be sorted
again using that combined population. Counts/composition remain estimates and mixing
uniformity is unverified. Missing/incompatible count or feature evidence leaves the
combined description unknown and prevents unsupported simulation replay.

For photographed-bean simulations, open **Sorting recipes** and choose **Dark
outliers: L* below median minus 2 sigma**. Its preview shows the reference statistics,
threshold, expected distributions and photos. The supplied reference has 1,129
distinct tracked beans; multiple images of one bean are not separate source beans.
The first rule selects **L* < 18.134062**, using median − twice population σ (ddof=0).

The catalogue also offers **Large area: above median plus 2 sigma** and
**Small area: below median minus 2 sigma**. Both use the recorded two-view projected
area in mm², with one value per tracked bean. The reference median is 33.177267 mm²
and σ is 9.979781 mm². Large selects **area > 53.136829 mm²** (47 reference beans);
Small selects **area < 13.217705 mm²** (15). Their previews include area distributions
and show the area beside each photographed example. The comparisons remain strict.

The legacy fixture recipe no longer appears in the current catalogue or new-job
choices. Its old links and jobs remain traceable and show that the recipe is retired.
An existing job keeps its pinned version; if editing an unstarted job that used a
retired recipe, explicitly select a current recipe before saving.

Choose **Prepare sorting with this recipe**, then select available source stock,
destinations and intended mass. The seeded simulator samples whole reference beans
with replacement; larger jobs can repeat a photo. Every passage retains the source
bean's joint properties and one capture outcome. No physical sorter commands are
sent. Existing jobs keep their original recipe; edit an unstarted preparation or
create another job to change it.

To sort material again, choose its released Selected or Pass-through portion as
the source on **Prepare sorting**. Select a recipe from the same reference dataset,
the quantity to process and two suitable destinations, then prepare/start as usual.
The **Further sorting pass** card records its pinned source population. Sampling
uses only that source's retained simulated members, preserving joint properties
and photo references, including earlier missed targets in Pass-through. The new
pass gets fresh passage identities and seeded capture trials. Its recipe keeps its
versioned thresholds; thresholds are not recalculated from the smaller source.

Count estimates and gross-weight predictions retain the source population's mean
bean mass and original area/mass assumption. Changing Simulation Settings can select
a new capture assumption for the next pass without rewriting prior evidence. Entering
a reduced test count remains a representative sample. Finish, weigh all three
containers, review the discrepancy and accept/release the new outputs as usual.
Their history traces through every pass to the original receipt; no new receipt
or stock is invented by re-sorting.

Full container transfers keep the portion eligible. Partial processing creates an
explicit source remainder with a representative-composition assumption. Manual split
children can be re-sorted when the split explicitly assumed a representative
withdrawal. These are inherited estimates, not copied observed counts. Held material,
incompatible reference datasets, manually blended/packaged/rework populations and a positive
mass with no usable simulated members remain blocked instead of reverting to the
original unsorted reference. A blend-aware replay adapter is a later extension.

Use **Population charts & bean photos** from the job or output container.
**Expected composition** gives decimal predictions; **Simulated destination
outcomes** gives stochastic destinations. Change percentage/count scale and browse
Input/Targeted/Simulated Selected/Simulated Pass-through photos. Dark missed targets
can appear in Pass-through. Photos are archived source images, not chute observations.
Reference features are image-derived; membership, decisions, actions and arrivals
in these jobs are simulated. Enter output scale weights independently. Moisture,
physical source coverage and physical capture confirmation remain unknown/unverified.

The **Input**, **Selected** and **Pass-through** legend checkboxes independently
show/hide their lines across the charts. Axes rescale to the visible lines. Click
a histogram interval near a visible line to open **Beans in chart interval** on
the left. The selected interval is highlighted. Choose **Inspect population** to
compare another visible population in the same interval; switch expected/simulated
basis with the usual control. On tablets/phones the inspector appears above the
charts and scrolls into view. Close it to restore the full chart width.

The inspector shows paired archived photos, each camera's recorded mean RGB colour
sample and the chart property value. Missing colour/photos remain explicitly
unknown/unavailable. Repeated simulated passages are grouped under one reference
photo pair, with their passage count; **Previous/Next** pages cover all matching
reference beans. Expected output contributions may be fractional and describe
possible membership. Simulated membership uses persisted destinations, including
missed targets in Pass-through. Neither is physical chute verification or a new
estimate of weighed stock. Empty intervals show no unrelated photos. Keyboard users
can focus a chart, use left/right arrows to choose an interval, and press Enter.
Inspection is read-only and does not change beans, profiles or stock quantities.

Administrators can **Design a recipe version** in the catalogue: choose a reference,
property, below/above/within/outside comparison and σ multiplier. Saving the same
name appends an immutable version. Statistics use known values and population σ,
centred on the median. Missing values never silently pass a recipe/specification.
Recipes remain separate from the capture assumption pinned for each job.

Use **Locations** to add storage places such as `Shelf --- NewBeans`. Floor,
LoadingBay and the places already recorded on existing containers remain available.
Open a location to see the containers stored there, including marked archived vessels.
Opening a location lists its containers in the same layout as **Current populations**
on Stock & sorting jobs: container/location links, linked contents identity and recipe,
presence/release status, net mass and available mass. Empty and archived vessels remain
listed. Pending sorting collections show their collection identity, recorded net weight
when available, and zero availability until reconciliation/release.
Use **Edit location** to rename it. Beside it, supervisors/admins can choose
**Delete location** and confirm deletion after moving or removing every container,
including archived containers. The confirmation page lists any containers that
still block deletion. Renaming updates displayed names without
recording a physical move; old location links retain their history after deletion.
Use **Material handling → Move container** to select a filled or empty vessel,
choose its new location, confirm you have physically moved it, and click **Record
container move**. After saving, the Move container page confirms the move and resets
to a blank container choice. Selecting a vessel automatically loads its current
location, fresh confirmation form and valid destinations; **Choose container** also
works without JavaScript. Only that selected vessel's current location is excluded,
so another vessel can immediately move to a previous vessel's former location.
The container page also offers a location move and returns to this same page. Recent moves
appear in the handling section and the container history. Its contents, mass and
quality status travel with it. To pour beans into another vessel, use **Transfer
a whole portion** instead.
Active job bins must first finish their job or cancel its unstarted preparation
before moving. Choose
these same registered locations when creating or editing a container.

1. Confirm the environment banner. Simulation uses isolated training stock;
   real inventory is a separate database and session. Sign in as an operator.
2. Create three containers with permanent labels, locations, **empty-container
   tare weight (kg)** and **material capacity (kg)**. Both weights are required
   and must be positive; capacity means nominal net material mass, excluding the
   container. The detail page shows both values. Existing containers retain
   “Not recorded” for missing weights. Registration does not change material net
   weights or replace receipt weighing evidence. Print the vessel
   labels with the browser's Print command. A USB keyboard-wedge scanner can
   enter a printed ID/link in the global scan field; manual entry also works.
   Open a container and choose **Edit container** to update its name, location,
   type, tare or capacity. Save your changes; a stale form requires reloading.
   Details are locked during an active job. Capacity cannot be reduced below the
   current net contents; previously overfilled vessels can still be renamed or moved.
   Edits retain previous values in the audit history. Reprint the permanent
   vessel label after a name change if you want the printed name to match.
   Supervisors/admins can delete empty, unclaimed containers, including those with
   previous history, or archive them for later restoration. Archived containers remain scannable and can be restored. Deletion
   retains a record for the old ID and preserves material history. Deleted vessels
   are removed from their storage location membership.
3. On **Receive material**, complete source details first. Choose one or more empty
   labelled containers, then confirm they were empty/clean. Containers missing
   historical tare/capacity remain available; enter the missing values below their
   selection. They are saved with the receipt. Existing weights remain fixed here;
   use **Edit container** to change them. Selecting a container removes it from
   the other rows until you change/remove its selection. Open **Containers
   unavailable for receiving** to see occupied/reserved vessels and the reason.
   Zero
   each empty vessel on the scale before adding beans and enter its **net bean mass
   in kg to three decimal places**. Receiving allows up to **20% above nominal
   container capacity**, so a 25 kg vessel accepts up to **30.000 kg** of beans.
   Each weight entry shows its receiving limit. The entered mass is recorded in
   full and the nominal capacity is unchanged. Select each container's storage location and
   confirm placement. The receipt shares one source-lot ID across the independently
   handled portions. Supplier bags are source metadata only; a nominal 60 kg bag is
   never used to fabricate a measured weight. No simulation, scale ID, resolution
   or particle-count fields are needed. Other bean properties remain unknown.
4. Prepare sorting. Choose the available source. **Intended processing quantity**
   defaults to its full available mass; change it to process a smaller quantity.
   Choosing another source fills that source's full mass. An existing prepared
   job keeps its saved quantity when you open the editor. Choose the station,
   recipe, capture model and discrepancy policy. Choose different Selected and
   Pass-through vessels, empty or holding suitable released stock, and confirm
   readiness. The source and any existing destination contents are reserved;
   another operator cannot use them or the destinations concurrently.
5. In simulation, start with a seed and particle count, then process batches of
   250 fixture particles. Three frames or two valves still produce one logical
   capture trial per target. Progress and coverage are stored. Reloading or
   restarting the server does not change an outcome. **Replay first batch safely**
   demonstrates idempotency, including after completion.
6. Inspect expected counts and distributions separately from simulated monitoring.
   The former are decimal predictions under the pinned capture assumption; the
   latter are synthetic destinations, arrivals, associations and timeouts. Neither
   is physical verification. Count expectations never overwrite a weight.
7. A pause/failure preserves all WIP and partial evidence.
   Never cancel a started job to pretend that no material moved.
8. Select **Finish / stop accepting new processing** and record the transition.
   The job opens **Weigh containers & reconcile** directly; there is no separate
   **Await reconciliation** action or intervening weighing-navigation button.
   Wait for feeding to stop and all in-flight beans to be accounted for before
   confirming final weights. Merely opening weighing does not mark the boundary
   complete. Valid, confirmed weights advance it automatically when **Reconcile**
   opens the review. An unfinished weighing can be reopened from the job's
   **Continue to container weighing** link.
   Enter **gross container-plus-beans weight in kg** for Selected, Pass-through
   and the source container. The recorded tare is deducted separately. The source
   must be weighed even if empty: its gross weight should then equal its tare.
   All three weight confirmations are required. Missing historical tare must be
   entered explicitly; it is saved only when reconciliation is accepted.
   Sampling is not used in this simplified flow. Optional retained/loss quantities
   are under **Other material, if any**; they default to explicit zero.
9. Simulated weights are prefilled using the pinned mean bean mass and reference
   area: **bean mass = mean bean mass × bean area ÷ reference mean area**.
   Each output shows its expected bean count, area-derived mean mass per bean and
   predicted new net mass. Many small outliers can still have a low total mass:
   about 1,359 beans at 47 mg each weigh only 64 g. The displayed expected count
   can differ from the run's stochastic simulated destination count. Starting
   destination stock and tare are excluded from this addition estimate.
   Expected Selected/Pass-through weights use the pinned capture probability;
   intentional passes and missed targets enter Pass-through. These are predictions,
   not scale readings. A deliberately reduced test count has an explicitly recorded
   expansion factor for mass prediction. It does not add particle/photo records.
   Check or change the weights, then click **Reconcile**. This only previews the
   accounting; stock is unchanged until accepted.
10. Review **signed discrepancy = original input − accounted net mass**. A positive
    number is a shortfall; a negative number is excess. Neither is automatically
    called waste. Accept the reviewed weights, estimated composition/counts and
    signed difference. A supervisor/admin can **Accept reconciliation & release
    outputs**; ordinary operators record held outputs for supervisor review.
    Isolated simulation permits an explicitly approved, audited discrepancy outside
    the pinned tolerance. Real inventory still enforces its pinned tolerance and
    composition policy. Retained material and explicit quality holds stay unavailable.
    Non-reference/manual jobs also open gross container weighing from Finish/Stop;
    their existing detailed net-mass accounting form remains available on the job
    once it awaits reconciliation.
11. Open either output container for mass, availability, profile and placement.
    Open its portion for measurement provenance, receipt ancestry, later descendants,
    ledger and audit. Print contents labels. Labels carry identity, not a snapshot
    of mutable properties. Old labels show historical disposition after bin reuse.

The accepted net mass recalculates a **bean-count estimate** using that stream's
expected mean bean mass; the source remainder uses the reference-wide mean.
Unknown model information leaves the count unknown. This assumes the weighed
material represents the predicted composition. It does not establish a census,
reassign beans, or add/remove photos to force a balance. Original run histograms,
particle counts and simulated destinations stay intact, while container details
and output analysis show the separate weight-adjusted count estimate. The three
estimates share the same model assumptions and are not independent measurements.

**Real-mode workflow:** manual start records an external run reference and WIP;
it sends no physical request. Use existing sorter controls and their safety process.
The archived-run exporter and spool importer accept real upstream evidence; use the
README commands or the supervisor import form. Profiles remain expectations without
capture confirmation. Real mode refuses the fixture generator.
The Evidence guide demonstrates the same expected-count arithmetic without creating
fake real stock. No real station connection is provided in this phase.

**Evidence vocabulary:** direct measurement is a scale reading; an operator/supplier
declaration is a claim. Image-derived features and model-derived categories retain
their sources/versions. A controller record reports an issued/scheduled action, not
capture. Simulation is synthetic. Calculated prediction is an expectation, never a
physical census. Unknown stays unknown; moisture is never inferred from images.

**Before starting:** open the prepared job and choose **Edit prepared job** to fix
an incorrect source, destination container, quantity or configuration. Its existing
reservations remain until you save. Saving redirects to a linked corrected job;
the original configuration remains in history. If validation fails, the old
reservations stay intact. **Delete prepared job & release containers** frees the
reserved source and both destinations so you can prepare another job. Confirm that
no material has been processed. Both controls are available only before any run.

**Exceptions:** preparation can be cancelled safely before processing. Interrupted
jobs finish through the same reconciliation boundary. Out-of-tolerance jobs remain
held. A supervisor can supersede incorrect unreleased sorting output measurements
with a reason and new measurements. This creates corrected child identities; print
new contents labels. The old measurements/labels remain historical. This cannot
introduce a previously absent output or change a released boundary. Explicit quality
holds can be cleared only with a reviewed disposition, and this does not release
unreconciled outputs. There is no destructive measurement edit or policy override. Administrative
configuration changes create new versions for future jobs. Existing jobs keep the
versions selected during preparation.

Data freshness is shown with last receipt time. “Recent data received” is not proof
of a live machine connection. After the configured interval, the station is stale.


## Manual Sorter Controls

Choose **Sorter Controls** to open the familiar machine control panel. The machine
IP address or hostname and port appear above it. Administrators and supervisors
can save a different connection. `127.0.0.1:8000` refers to the existing controller
running on the same computer as the BeanoSort server.

Opening the page reads status. Choose **Take control** before applying timing or
motor settings, starting an output, pulsing a camera/illumination cycle, or testing
the valves. If the existing WebGUI or a trial holds control, its operator is shown
and Take control remains unavailable. **STOP ALL OUTPUTS** and **Stop valve outputs**
can be used without owning the lease.

**Load Candidate 3** fills the controls without sending settings or starting
outputs. Edits remain until you apply them or refresh the page. Triggering and
illumination can be switched independently. Vibration controls include frequency,
duty and direction; conveyor controls include steps/s, acceleration and direction.
The valve test uses the existing −V1 → V0 → +V1 sequence.

Choose **Release safely** to stop and relinquish control. Leaving the page also
releases control; lost heartbeats use the machine's existing timeout policy.
Release before changing the machine address. An old panel cannot send commands
to a newly configured address; reload it to continue.

This page operates physical equipment even while inventory is labelled simulation.
Manual controls do not start/stop a stock sorting job or move/reconcile material.
Job workflow pause/resume continues to record workflow state separately. A failed
or unconfirmed command is shown and is not automatically retried.

## Transaction responsiveness

Routine job pages and fill bars use committed run summaries, maintained together
with each incoming batch. Original bean observations and photo references remain
the evidence; the summaries are replaceable derived data. Pausing or finishing
records the state and its audit entry without rebuilding the whole run first.
Detailed **Expected distributions and joint properties** load when you open that
section, leaving the job controls usable while the tables arrive. The tables state
how many committed particle records they represent.

Migration **0015** adds these summaries. The upgrade warms existing runs before
serving the app. Administrators or supervisors can repair/rebuild a run with
`beanosort rebuild-summaries --run-id RUN_ID --username USERNAME`; this changes
only derived summaries. It does not resimulate beans, reclassify observations,
change photographs, or post stock movements. Concurrent reads use SQLite WAL;
accounting writes remain serialized and atomic. Online backups must use SQLite's
backup mechanism to include the WAL, as the existing maintenance tools do.

## Receiver fill warnings and automatic workflow pauses

The **Receiving container fill** bars show the current Selected and Pass-through
containers' estimated net mass, nominal capacity and percentage. Bars are green below
90%, orange from 90–100%, and red above 100%. The scale expands in 25%
steps as needed, with 100% marked as the nominal limit; an overfilled bar is not
clamped at 100%. Both receivers share that percentage scale. The bars update
without reloading the page and follow replacement containers automatically.
Missing incoming-mass evidence is shown as a lower bound, and missing nominal
capacities are identified without a percentage.

During sorting, the job page warns in orange when either current receiving
container reaches 90% of its nominal net mass capacity. You can start above 90%.
At 100%, BeanoSort pauses the workflow and stops generating simulation passages.
Select **Override capacity warning & resume** to continue with those containers,
or change the containers while paused. Every override and automatic pause is
recorded with the receiving-container identities, fill estimate and threshold.

After resuming at 100%, the next pause is at 105%, then 110%, 115%, and each further
5% of nominal capacity. Warnings above 100% are red. Resuming acknowledges the
current fill; a large passage or already-arrived batch can cross more than one
threshold, so the next pause is the first 5% band above the accepted fill. One
receiver's override does not disable the other receiver's capacity pauses. Restarting
the app does not lose the acknowledged thresholds. Existing quality holds still
prevent starting or resuming.

The fill combines pinned starting contents with incoming mass estimates. Reference
simulations use the pinned area/mean-mass model and expected capture; legacy
fixtures use simulated destinations and planned mass per test passage. Starting
contents and source-return vessels are counted once. Replaced containers stop
contributing warnings; unchanged collection fills continue across container swaps.
A replacement already at or above capacity requires the capacity-override checkbox
on the change-containers form. It remains paused until you explicitly resume.

These figures are estimates, not current scale measurements. Runs without an
incoming mass model show that limitation and can only protect thresholds established
by known starting contents. No unobserved incoming mass is guessed. BeanoSort
records workflow state and has no physical sorter connection: it sends no conveyor,
feeder or valve stop command. Use the existing machine controls when needed. Late
or in-flight evidence is preserved even while the workflow is paused.

At reconciliation, keep the actual measured weights, including overfilled vessels.
An earlier receiving-capacity override permits those weights; accepting a review
that explicitly shows a nominal-capacity excess also approves recording it. The
excess and approval remain in history; no stock, measurements or particle evidence
are rewritten to fit a capacity limit.

## Material handling

The page has four functions: **Move container**, **Move contents**, **Package as
product**, and **Reconciliation**. Routine contents movements and weighings are
recorded in one confirmation; no separate preparation, start or release is needed.

**Move container** changes only the vessel's location. Its material identity and
mass are retained. Existing job restrictions still apply.

**Move contents** takes a source container and a destination, which may be empty
or occupied. Select **Empty the source container** to transfer its entire recorded
balance without weighing. The mass-entry label is crossed out and the input is
disabled; any saved draft value is ignored. Unticking the checkbox restores the
draft and requires a measured **net mass moved** for a partial transfer.

Complete movements carry recorded balances forward without creating a new scale
measurement or weight adjustment. Use **Reconciliation** separately to correct a
recorded weight. A partial move must leave a positive calculated remainder; that
remainder is not a separate source weighing. Destination totals add the complete
recorded source balance, or the measured partial transfer, to existing contents.

| Movement | Portion identities |
| --- | --- |
| Whole contents into an empty container | Retain the existing portion ID |
| Partial contents into an empty container | New moved portion and new source remainder |
| Whole contents into an occupied container | New combined portion with both parents |
| Partial contents into an occupied container | New combined portion and new source remainder |

All four cases preserve ancestry and earlier measurements. Movement is blocked
when the resulting destination would exceed its nominal capacity. Existing quality
holds pass to resulting material; movement does not clear them. Reserved, packaged
or work-in-progress contents cannot be changed through these tools.

**Reconciliation** records a current gross or net scale reading and a reason.
Gross readings deduct the registered tare; use net mode if the scale was already
tared. It retains the portion ID for positive remaining contents, appends the signed
adjustment and keeps previous weighings. Zero net contents closes the portion and
frees the container. Previously unrecorded contents receive a new identity with
unknown origin and composition. A truthful weighing above nominal capacity is
recorded and flagged; this is not permission to add material above capacity.

Combining two portions with known population evidence retains their joint
properties and photographs. Mixing a known population with contents without such
evidence, in either direction, leaves the combined population unknown. Combining
two unknown populations also leaves it unknown. Their original source records and
ancestry remain available. Whole movements do not require a representative-sample
assumption, because all the source contents move.

The forms show the resulting balances before recording. A form is rejected if
another operator changes the chosen material, its placement, profile, quality or
job availability. Reload it to review the current state. Retried submissions do
not repeat a recorded movement.

Population estimates assume representative material by default. The **Population
estimates** section allows this assumption to be cleared for selective movements
or removals; the resulting composition then stays unknown. Compatible joint bean
features and photographs remain available for later simulated sorting passes.
They describe inherited estimates, not duplicated observed beans or verified mixing. Combined and split portions keep frozen, dataset-scoped archive identities,
all joint feature measurements and stereo photo references. Open **Population charts
& bean photos** on the current portion or container profile to inspect its weighted
composition, chart intervals, corresponding images and recorded colour samples.
Earlier profiles remain available under **Earlier population evidence**. Combining
portions never adds observations or changes the source archives. Multiple archives
can be inspected together; simulated replay still requires a compatible single
reference dataset.

**Package as product** records packaging for sale at the **Packaging desk**.
Select the source container and package the desired amount. Under **Scale reading
reflects**, choose **Mass of contents removed** to weigh just the removed material,
with any packaging tared. Enter that net reading in kilograms in **Scale reading**;
the remainder is calculated from the recorded source balance. The source container
does not need reweighing. The removed mass must be positive and no greater than its
recorded contents; entering the entire recorded balance empties the container.

Alternatively, enter gross container-plus-remaining-contents mass (the registered
tare is deducted) or remaining net contents from an already tared scale. Review the
preview and select **Record final product**. For an empty container, enter its tare
in gross mode or zero in remaining-net mode.

Final-product mass is either the **measured mass of contents removed**, or
**recorded contents before packaging minus measured contents remaining**.
For example, removing a measured 1.25 kg from 2 kg recorded leaves a calculated
0.75 kg; weighing a remaining 0.75 kg instead records the same 1.25 kg as Final
product. History distinguishes calculated remainders from measured remaining
contents and never substitutes a calculated balance for a scale measurement.
The removed material is kept in the operation history
with its source, operator, Packaging desk, scale reading and calculation. It is not
available stock. No packets, unit counts, product lots or product stock are created.

The remaining contents retain their portion ID and, under the default
representative-material assumption, their population charts and photographs with
updated estimated counts. Clear that assumption for selective packaging. Emptying
the source closes the portion and frees its container while retaining earlier
history and evidence. Reserved, archived, work-in-progress, already packaged and
quality-held material cannot be packaged through this tool. Release any quality
hold first; packaging cannot clear it.

When weighing what remains, that net weight must be less than the recorded contents;
the difference is attributed to Final product. When weighing removed contents,
that measured net mass is attributed to Final product. Use **Reconciliation** separately for spills,
unrecorded additions or weight corrections; do not also reconcile a removal already
recorded by packaging. Duplicate submissions do not debit stock twice, and forms
are rejected if their source contents changed after opening. Migration **0014**
adds the Packaging desk without changing existing material balances.

Old transfer, split, blend and packaging jobs remain
available in the operation history and can be completed using their original pages.
Old preparation bookmarks lead to the new tools; new legacy jobs cannot be created.

On an available bulk portion, **Record a returned count sample** still records a
counted, weighed representative sample. Confirm all sample material was returned.
Its count-per-mass estimate is retained with the previous profile history; it is
not a full census or a sampled removal workflow.

## Simulation Settings and mass-derived input counts

Use the **Simulation Settings** navigation tab to inspect current assumptions and
their history. An administrator can enter a mean bean mass (grams), material name,
and sorting realization rate (percent), then save a new version with a reason.
The initial values are **Green coffee, 0.15 g, 99% capture probability**. The page
includes research links. The rate concerns valid selection attempts, not classifier
accuracy or physical verification. Missed targets join Pass-through.

The current **Receive material** flow records actual entered net mass and leaves
particle count unknown. Historical receipt estimates retain their original settings
version. The mean mass is applied later when preparing a synthetic run; 1 kg at
0.15 g/bean gives approximately 6,667 simulated particles. Entered weights remain
independent of those later predictions.

On **Prepare sorting**, the job pins the displayed simulation settings. Its intended
processing mass determines the proposed fixture count. Leave **Optional test particle
count** blank to use that estimate, or enter 250 for a brief test. At **Run evidence**,
use **Generate all remaining particles** to process saved batches automatically.
**Stop generation after this batch** stops submitting new synthetic data; it is not
a physical machine stop. You can reload/reconnect and continue. Pausing the workflow
also prevents further generation until resumed. Finish, reconcile the actual entered
masses, and explicitly release stock as usual. A small test count or an incomplete
fixture never proves a full census of the represented physical material.

Changes to settings do not alter earlier receipts, prepared/running jobs, simulated
outcomes or frozen profiles. Real manual jobs keep their separately selected capture
model; this tab operates no hardware.
