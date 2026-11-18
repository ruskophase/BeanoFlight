# All Pass-through machine integration

BeanoSort controls jobs through the Jetson job service on port 8091. The existing
manual ESP controller stays on port 8000. BeanoSort may run on a Raspberry Pi 5;
only JSON configuration, commands, status and numerical evidence cross the LAN.
The API intentionally has no image-download endpoint.

## Operator workflow

1. In **Sorter Controls**, connect to the Jetson's LAN address (manual port 8000),
   prepare the machine, stop feeding and release manual control.
2. In **Prepare a sorting job**, select **All Pass-through · live machine**,
   source material, a Pass-through container and a machine settings bundle.
   Selected is unused. Choose active feeding seconds and an image-saving mode.
3. Validate/reserve, then **START**. The Jetson acquires exclusive control,
   verifies settings and camera readiness, acquires an empty background and primes
   the first synchronized stereo pair, then starts the conveyor and vibrator.
   Failure to prime stops startup before feeding can be enabled. Its lease heartbeat is independent of the
   browser and measurement/export threads.
4. **PAUSE** stops only the conveyor/vibrator. Triggering, illumination, tracking,
   measurements and RAW recording continue. **RESUME** restarts feeding without
   reconfiguring PWM. Paused time does not count toward active feeding duration.
5. **END**, or duration expiry, stops feeding, allows two seconds for inflight
   material, finishes measurements and recordings, and releases outputs. After
   the final journal cursor arrives, the browser opens container weighing.
   **STOP ALL** immediately stops outputs and marks the run interrupted.

The same page supports the two explicitly labelled archived RAW replay sources
in simulation inventory. Replay never constructs a controller or sends hardware
commands. Its provenance is retained separately from physical live passages.
Pausing replay freezes its recorded timeline; it cannot recreate the physical
effect of stopping a feeder on an already-recorded scene.
The inventory environment banner still applies: actual live measurements do not
turn training stock into real inventory.

## Images and measurements

- **Save RAW frames** uses the native BeanoFastCap LZ4 recorder, metadata, paired
  manifest and copied calibration. Every native capture frame is written;
  actual losses and pairing statistics remain in its integrity report. The live
  analysis reads latest-frame shared memory independently. Recorded frames may
  therefore exceed frames processed live. Use the resulting recording with the
  existing `beanoflight.statistics_bundle` offline builder.
- **Save stereo bean crops** writes calibrated CamL/CamR PNGs for successfully
  measured pairs, with a bounded asynchronous writer. Every saved pair has a
  numerical event identifying its bean and frame. Image backlog or write failure
  is reported separately; numerical measurements are retained.
- **No image saves** releases RAW views after measurements. Transient live frames
  reside in `/dev/shm`; no camera frames are persisted by the job runner.

A bounded single measurement worker, with parallel left/right colour kernels,
prioritizes beans with fewer samples. There is no per-bean two-sample cap.
Numerical measurements use the same calibrated colour, mask and apparent-volume
kernels as offline bundles. Failed and skipped opportunities remain visible.
Apparent ellipsoid volume is not weighed mass. All Pass-through uses **zero**
measurements for selection and constructs no valve-plan publisher. Availability
before a nominal firing deadline is reported separately (bundle lead plus 4 ms
notice); replay availability is not a live timing guarantee.

Coverage denominators are confirmed tracker identities, not an independent
physical bean census. Live mass-based fill estimates need a calibrated mass
model; existing container weights are shown, but observed volume is not silently
converted into kilograms.

BeanoSort now exposes **Population charts & measurement evidence** from the job
and its Pass-through portion/container. It plots retained lightness, chroma,
major axis, projected area and volume proxy, including missing-feature counts.
Clicking a bin shows the matching tracked records and recorded camera mean-RGB
swatches. One track is counted once regardless of measurement sample count.
Input and intended Pass-through coincide for this recipe; bin arrival is not
independently verified. Historical profiles retain their evidence-receipt cutoff.
Charts cover the run contribution, not prior destination contents or unprocessed
source material. No image-download endpoint or hardware action was added.

## Reliability and boundaries

The Jetson owns a WAL SQLite numerical journal. Browser closure or loss of the
Pi does not stop its duration timer. BeanoSort's background worker pages at most
100 events/request, persists commands before sending, and advances its cursor
only after duplicate-safe particle ingestion and event storage. Gaps and failures
remain visible. Final weights cannot open until the final cursor is received.

END is a normal drain; STOP ALL or an interruption may leave incomplete evidence.
Interrupted jobs require an explicit review before weighing. Service restart
never resumes physical motion; committed samples for unfinished beans are
recovered into final bean records. The ESP's existing lease/failsafe remains in
force if the process fails. Numerical journal failure stops the run.

A drained, synchronized pause enables container replacement. New collections use
machine bean ordinals, not network arrival times. Source/recipe/image mode/settings
remain pinned for the job. Changing them requires a new job.

Each live run retains calibration and nozzle-map snapshots, software hashes,
settings, source provenance, raw timestamps, repeated measurements and a final
summary. Tracking positions, velocities, covariance and crossing predictions are
journalled with their source frame timestamps, including unmeasured tracks.
The journal is separate from the older trial-script BeanRegistry spool;
this new journal is the authoritative numerical export for these jobs.

## Deployment

Jetson: use the existing BeanoFlight Python environment. Run
`python -m beanoflight.machine_jobs.server --config /absolute/path/config.json`.
See `deployment/machine-jobs-config.example.json` and the systemd service example.
The token file must contain at least 32 random characters and be readable only by
its service account. The service must have access to the cameras, `/dev/shm`,
BeanoFastCap native binary, calibration, nozzle map and output storage.

BeanoSort server/Pi: migrate its database, set
`BEANOSORT_MACHINE_JOBS_TOKEN_FILE` to a private file containing the matching token,
and optionally `BEANOSORT_MACHINE_JOBS_PORT` (default 8091). Set the Jetson address
through Sorter Controls. Start BeanoSort normally. The All Pass-through recipe and
Live sorter station are installed when the integration is enabled.

The example uses HTTP with a bearer token for a trusted LAN. Do not expose either
machine service publicly. RAW preflight reserves space for active duration plus
maximum pause allowance and startup/drain margin; low storage stops the job.
Current limits: 1–1800 active seconds, 600 seconds pause allowance, plus 60 seconds
startup/drain margin. Replays and image saves remain local to the Jetson.
To offer several settings bundles, supply `settings_files` as an array of JSON
paths in the machine configuration. Each offered bundle has a content hash;
settings and calibration changes after preparation require a new job.

## Validation scope

Tests cover authenticated numerical-only API access, pinned identities, duplicate
commands/batches, LAN failure, journal gaps/final watermarks, crash recovery,
synthetic-evidence isolation, camera-independent lease handling, pause/resume and
automatic duration stop with fake devices, and browser-to-HTTP-to-database flows.
Both existing RAW recordings are replayed through calibrated tracking and full
colour/volume measurements. Replay includes decompression and disk access and is
not a benchmark of live headless performance. No physical feeding or valve firing
is performed by the implementation validation.
