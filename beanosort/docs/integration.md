# Actual sorter integration map

## Enlarged population charts

The same `/jobs/{id}/measurements`, `/jobs/{id}/populations` and
`/portions/{id}/populations` views accept a validated `chart` presentation parameter.
Title/back links preserve profile, run/collection, combined-contents and population
scope. Existing inspection endpoints and source-specific photo GETs are reused;
no machine interface or data-ingestion change is required. A focused chart is the
same report and evidence, not a new measurement/profile or simulated population.

## Manual FastCamUSB tab during a live job

`beanosort/manual_fastcam.py` exposes `/jobs/{operation_id}/fastcam` plus authenticated
status, command and preview routes. They resolve `MachineJob.endpoint`/`remote_id`,
then use `/v1/jobs/{run_id}/fastcam/{status,commands,frame}` on the existing machine
API. Machine API bearer tokens and ESP32 lease IDs remain server-side.
`machine_jobs/manual_fastcam.py` serializes explicit actions with the existing
live runner's `CameraHTTP` client ID. The current ESP Web Controller lease checks
therefore remain intact; its existing `/api/fastcam/*` and
`/api/high-speed/enabled` interfaces require no changes. GPIO33 configuration is
read-only during the job. Neither the ESP controller service nor Pi listener needs
a code change/restart for this feature.

The runner creates the adapter after source priming, even without an automatic
USB-camera plan, and closes it before releasing its lease. Manual takeover finishes
the scheduled worker first. Requested/result/closed events are appended to the
existing compact journal and ingested as normal `MachineEvent` evidence; they add
no particle observations or inventory movements. All hardware tests use fakes,
including an HTTP ESP32 lease gate. Physical activation is left to the operator.

## Optional FastCamUSB live-job recording

The existing Waveform Generator/controller now exposes `/api/fastcam/status`,
`/api/fastcam/settings` and `/api/fastcam/command` through
`ESP_Web_controller/app/fastcam.py`. It bridges to the Pi listener configured by
`BEANO_FASTCAM_URL` (currently `http://10.203.77.197:8765`). Read-only defaults use
its current settings plus the controller's current `high_speed` setup; external
triggering alone is preselected. BeanoSort reaches these through the authenticated
machine job API, so it also works when the workflow server is on another device.

The optional manifest and `machine_jobs/fastcam.py` schedule only explicitly opted-in
live jobs, using the existing job lease. GPIO33 config is applied before capture
startup and its enable state brackets the recording window. All camera network IO
runs outside tracking/measurement and heartbeat threads. Job journal/status retains
requested/reported settings, acknowledgements, timing offsets, failures and Pi file
paths. There is no USB-frame-to-particle association or video download interface.
Real recording/timing has not been exercised during implementation; tests use fakes
and only GET status/defaults reads were made against the operational system.

November 19 recorder hardening: the standalone Pi app's `capture_core.py` re-exports
the maintained `tools/fastcamusb/recorder.py`; guarded `controller.py` integration
adds `recorder`, `recorder_policy` and scoped previous-session diagnostics to the
existing listener status. The Waveform Generator bridge passes these fields
through unchanged. A separate `ScheduledCapture` GET monitor persists compact
diagnostics without delaying its timer or the tracking/heartbeat paths. The job
page displays queue peaks/stalls, first-failure and observation times and stale
telemetry. The recorder writes AVI/CSV plus a diagnostics JSON sidecar on the Pi.
No new physical detector, image-to-particle association, database migration or
hardware-startup path is introduced. A saved-frame replay on the actual Lexar
filesystem recovered a >2-second write stall with all 5,400 frames intact; live
simultaneous camera/sorter load was not exercised for this update. See
[`../../tools/fastcamusb/README.md`](../../tools/fastcamusb/README.md).

## November 18: numerical machine evidence charts

The All Pass-through job integration is documented in
[`../../docs/machine-jobs.md`](../../docs/machine-jobs.md). The earlier offline-only
and manual-only limits below describe those earlier stages, not the subsequently
added job service. The new `measurement_charts.py` reads its retained compact
`Particle` payloads: `lab_l`, `lab_chroma`, `size_mm`, `area_mm2`,
`volume_proxy_mm3`, and per-camera mean RGB in `machine_evidence.features`.
It uses `Run`, `MachineJob`, immutable `Profile` and any existing collection
sequence boundaries, without adding requests or work to the machine pipeline.

This enables numerical charts from completed jobs that never saved photos. Links
are derived on job/container/portion pages, preserving existing profile versions.
The subsequent crop-viewing addition uses retained `images` events to choose the
earliest saved frame per track. `machine_photos.py` proxies authenticated,
bounded PNG GETs from the machine API's terminal-job crop endpoint. Source files
remain on the machine, with no RAW download or image upload in processing.
Combined portion views follow pinned previous-contents profiles and resolve each
record's crop against its original job, so a later no-image addition cannot mask
earlier saved crops. This adds no machine API/control change. Pooled records are
labelled historical observations; missing portion coverage and calibration/source
incompatibilities remain explicit rather than a claimed complete bin census.
Registry/feature schemas and hardware control paths are unchanged. Intended
Pass-through membership and known tracked coverage remain distinct from physical
arrival verification and a full bean census.

## November 16: explicit manual controls

**Sorter Controls** now copies the existing ESP Web Controller panel and forwards
an allowlist of manual HTTP actions to a saved IP address/hostname and port.
It reads `/api/state` for status; explicit actions cover the controller's control
lease, triggers, illumination, high-speed timing, vibration, conveyor and valve
test/stop. The authenticated BeanoSort proxy binds browser identities to the user
session and saved connection, checks CSRF and records non-heartbeat results in
the audit history. Status reads and heartbeats do not take accounting write locks.

This does not import the controller, open USB/serial, change its startup path,
alter firmware, subscribe to the Registry or change the physical timing path.
The running machine service owns the exclusive lease, keepalive and failsafe.
Only status was read from the real service during validation; command tests use
a fake HTTP machine. Stock jobs, including automatic capacity pauses and container
changes, still issue no physical control commands. Live run attribution and job
execution coupling remain future integration work.

The November 13 container-change workflow adds **no machine-control integration**.
Its pause/resume state and feed/clear/readiness confirmations are workflow records.
Simulation uses a committed source-sequence boundary. Manual real records can pin
a known local sequence and timezone-qualified time; without a recorded boundary,
per-collection association stays unknown. The existing controller has no new
BeanoSort stop/start or automatic network-loss dependency. Archived source evidence
and source sequences remain immutable; ingestion after a change does not apply the
latest bin assignment retroactively. Physical boundary capture and a live adapter
remain later work.

The photographed simulation additionally consumes the exact bundle linked at
`../20261026T050213.812362Z-beans-90sps-vib28-gain60-3nozzles-test/postprocess/statistics-bundle/`.
Its HTTP `beans.jsonl` was verified byte-for-byte against the local file. The
existing `beanoflight-statistics-bundle/v1` manifest and
`beanoflight-statistics-bean/v1` rows supply 1,129 unique bean aggregates from 3,304
stereo samples. Representative `photo_CamL`/`photo_CamR` paths select 2,258 of the
6,608 photos. `beanosort/references.py` verifies/copies the records/photos without
importing any sorter startup code. Full source metadata and hashes remain available.

Feature mapping uses actual fields: `combined_lab_l_mean`,
`combined_lab_chroma_mean`, `caml_ellipse_major_mm_median`,
`rotational_ellipsoid_volume_proxy_mm3_median` and
`caml_ellipse_aspect_ratio_median`, and area recipes use
`projected_area_geomean_mm2_median` (mm²). Shape/defect categories and moisture remain
unknown; a dark outlier is not automatically a defect. Volume remains a proxy,
never bean mass. Reference statistics do not establish cross-machine comparability,
physical population coverage or observed sorting outcomes. Archived routing is not
reused: the proposed recipe and capture outcomes are explicitly simulated.

Inspected against the working tree on 2026-11-10 and 2026-11-11. Paths below are relative to
`BeanoFlight/` unless prefixed `../`. Existing modified files were read, not changed.

| Actual module / interface | Available evidence and planned consumption |
|---|---|
| `src/beanoflight/models.py`: `BeanRef(run_id, sequence)` | Full run UUID + sequence is the authoritative run-scoped identity; BeanoSort adds station identity. Never use a shortened displayed ID or a frame ID. New passes have new observations, not new receipt material. |
| `src/beanoflight/registry_models.py`: `RunSession` | `run_id`, `state`, `source_path`, `source_kind`, `settings`, source/monotonic clock anchors, frame count/FPS and timestamps. States created/running/paused/completed/failed map to reported execution evidence, not material reconciliation. |
| Same: `SortingDecision` | `decision_id`, `source`, `timestamp_ns`, `gate_indices`, `policy_version`, `reason`, `acknowledged_timestamp_ns`, actuation/close/crossing timestamps, `gate_windows`, `nozzle_map_sha256`, revision and timing marks. A selection decision is not proof of issuance or capture. |
| Same: `ActuationResult` | `decision_id`, `source`, actual open/close timestamps, `success`, `detail`. Represents a reported virtual/physical **gate cycle**, not a verified bean destination. Must inspect source/mode and outcome, not treat `success` as capture. |
| `src/beanoflight/registry_sqlite.py`: `SQLiteBeanRepository`, schema 3 | SQLite WAL tables `sessions`, `beans`, `observations`, `track_states`, `predictions`, `inference_jobs`, `enrichments`, `sorting_decisions`, `actuation_results`, `registry_events`. Single machine-owned writer. A read-only snapshot/query is a safe offline source. BeanoSort must never instantiate its writer against the machine DB. |
| `src/beanoflight/registry_zmq.py`, `registry_service.py`; `docs/bean-registry.md` | `ZeroMQRegistryClient`: `events_since`, `events_since_compact`, `event_cursor` and snapshot queries. Persistent stream sequence is recovery authority; PUB/SUB can drop notifications. Future adapter runs outside the processing path with bounded paged pulls/spooling. No network request per bean. |
| `src/beanoflight/classification.py` and `registry_models.py`: `Enrichment` | `classification_evidence`, `classification_pooled`, `classification_decision_basis`; source, kind, value, version, result ID, time, confidence. The decision-basis vector is what actually drove the decision even if canonical pooling raced. Scores are not calibrated capture probabilities. |
| `src/beanoflight/sorter.py`: `SorterSettings` | Reject categories, minimum confidence, gate probability threshold, lead/lag and minimum notice, adjacent-gate pair option, `policy_version`. Current default policy label is `simulation-v3-ensemble`. Hash/snapshot exact settings rather than assuming that label uniquely versions all parameters. |
| `src/beanoflight/live_statistics.py` | `observations.jsonl`, `beans.jsonl`, `capture.json`; capture schema `beanoflight-live-statistics-capture/v2`. Full `run_id`, `bean_sequence`, sample counts, visibility/fallback failures, source timestamps and file hashes. Join observations to confirmed bean ledger: tentative tracks may have orphan observations. |
| `src/beanoflight/statistics_bundle.py`, `live_statistics_bundle.py`, `statistics_features.py` | Per-bean medians in `beans.jsonl`/CSV, sampled observations, manifests with schemas/calibration provenance/definitions/hashes. Fields include `bean_sequence`, `sample_count`, `terminal_status` and `*_median` features. `equivalent_sphere_volume_proxy_mm3` and `rotational_ellipsoid_volume_proxy_mm3` are derived proxies, not physical mass. Safe post-run source without new live computations. |
| `../BeanoFastCap/docs/recording-bundle.md`, `src/beanoflight/source.py`, `calibration.py`, `nozzle_map.py` | Recording bundles, exact `pairs.csv` source timestamps, calibration packs, PinkPlane `homography.json`, nozzle maps/hashes. Retain source time domains; server receipt time is not arrival time. |
| `../ESP_Web_controller/app/main.py`, `serial_manager.py`, `valve_controller.py` | Existing FastAPI machine-control service with serial/valve actions and lease/failsafe policy. **Not imported.** Explicit manual controls use the already-running service's HTTP API. Do not reuse startup paths to read status: serial connection can send STOPALL. |
| `src/beanoflight/esp32_actuator.py`, `actuator_app.py`, `performance_benchmark.py` | Actuator service can open USB and drive outputs, including test paths. Not started. Existing hardware watchdog/continuation policy remains machine-owned. |

## Authority and missing guarantees

The registry owns reported execution evidence. BeanoSort owns material identity,
quantities, authorization and release. Neither a decision acknowledgment nor an
OPEN/CLOSE event verifies capture. The repository has no current chute detector,
container membership, measured stock mass, receipt/lot ledger or released material
population model. No permanent cross-pass bean recognition is assumed.

Machine identity needs an explicit registered station ID and adapter installation
identity; source run UUID alone is not a station inventory. Immutable recipe snapshots,
classifier artifact hashes, calibration/illumination compatibility and recording
coverage must be extracted and validated per source. Some statistics exports use
formatted bean IDs: join with full run identity and numeric sequence, never infer
uniqueness from shortened display text. Feature availability differs by capture path.
Rejected edge components may never acquire a BeanRef. Statistics completeness for
confirmed tracks is not a physical-particle census.

Chute travel timing is not recorded/calibrated. Existing nozzle gate timing is
insufficient to invent detector arrival delays. Phase 1's 100 ms arrival and 200 ms
timeout defaults are **synthetic test timing** and independently versioned assumptions.

## Implemented offline adapter

`beanosort/src/beanosort/adapter.py` reads archived schema 3 SQLite databases with
`mode=ro`, `query_only=ON` and a consistent read transaction. Only completed/failed
live-source sessions are supported. It exports one compact record per full BeanRef,
preserving track status, decision, classification decision basis, gate-cycle result,
source clock values and session settings. It does not import BeanoFlight modules.

A known `esp32-s2-gptimer` successful gate cycle with positive duration supports a
reported action attempt. Virtual actuator records in a live-camera run remain
simulated action evidence and do not trigger a real 97% trial. Standard accepted /
low-confidence pass reasons are recognized conservatively; no-gate selections are
selection/action discrepancies. Other missing/late decisions remain unknown.
Multiple decisions/actions for one particle require a reviewed segment adapter and
are rejected rather than collapsed. Source settings are hashed/pinned separately
from the workflow's requested recipe; their equivalence is not asserted.

Optional feature ingestion accepts inspected standard/live statistics-bundle schemas,
requires the exact source run ID, validates the bean file hash and maps available
per-bean L*, chroma, major axis in mm and rotational-ellipsoid mm³ proxy medians.
Unavailable mm calibration, shape and defect ontology remain unknown. Classification
categories retain model provenance and are not automatically renamed as defects.
Cross-machine/calibration comparability stays unknown. Full images are never loaded.

The spool contains synced batches of at most 500 logical particles and a manifest
written last. Import validates every file/count/identity before pinning the immutable
SourceBundle. Each batch commits independently with a stable fingerprint/path key;
a process restart can replay the same spool. Duplicate complete imports create no
particles or material movements. New evidence cannot rewrite reconciled profiles.

Actual archived export checked: `diagnostics/white-beans-20261031/sorting-selection/`
`runs/white-sort-candidate2-newmap-conv100-vib34-f4000-lead5-lag0-2min-first-20261106/trial.sqlite`
(relative to workspace root), full run `0e43fe45a70f420582109caef61de68e`:
**2,856 tracked records, 63 reported physical gate-cycle actions, 2,793 unknown
decisions**. At p=0.97, the attributable expectations are 61.11 Selected, 1.89
Pass-through and 2,793 unresolved; this is not the entire physical output population.
Physical capture confirmation remains zero. No real inventory was created by export.

A second September archive was exported and profiled: 18,225 records from live
camera data with virtual actuator results and a mock classifier. This is why source/action provenance is checked
separately. Classification decision-basis `model_version` may identify the pooling
method; `ensemble.member_result_ids` are resolved to the actual classifier versions.
Known mock categories stay in source evidence and are excluded from real feature
claims. An unrelated white-bean postprocessing bundle used a different run ID;
it cannot safely be joined to the November trial just because the beans look similar.

Next integration work is a durable asynchronous Registry journal reader with explicit
installation identity, reconnect checkpoints, gap quarantine and performance testing.
The offline adapter changes no live IPC subscription or hardware timing path.
The separate manual controller API integration is described above.

Compact import records use `schema=beanosort-compact-particle/v1`, `key`, zero-based
adapter `sequence`, optional `source_at`, `decision` (`selected/pass/unknown`),
`action` (`attempted/not_issued/unknown`), `features` and per-aspect `evidence` (kind,
source, version). See `fixture_particle`/`validate_particle` for the exact contract.
These sequence ordinals are **not** Registry journal sequence numbers. Real
attempts require `controller_record` action evidence. Unknown or unissued actions
are unresolved. Optional absent image fields stay unknown; synthetic fields cannot
be relabelled/imported by the application into a real database.
