# Machine integration validation · 17 November 2026

BeanoSort database migrated from 0015 to 0016 and both services deployed locally. The existing controller service on port 8000 was left running. No physical feeding, illumination, camera triggering or valve firing was performed during these checks.

## Software and browser checks

- BeanoSort full suite: 340 passed. One existing Starlette/httpx deprecation warning.
- Machine service, shared-memory source and clock checks: 15 passed.
- BeanoFastCap recording, finalization and integrity checks: 17 passed.
- Browser workflow: prepare, START, PAUSE, browser closure/reopen, RESUME, END and direct weighing passed against a fake HTTP machine.
- An already-full receiving container starts capture paused. Feed begins only after an explicit capacity override.
- Numerical journal gaps block reconciliation; commands, particle ingestion and cursor recovery are duplicate-safe.

## Full recorded-data replay

These runs processed the complete recordings without real-time pacing. They include RAW decompression and full calibrated colour/volume work; their coverage must not be interpreted as live throughput.

| Recording | Frames processed | Confirmed tracks | Tracks with colour and volume | Stereo measurements | At least two pairs | Elapsed replay |
|---|---:|---:|---:|---:|---:|---:|
| reference | 3,601 | 1,129 | 1,113 (98.6%) | 2,047 | 697 | 118.3 s |
| white | 17,851 | 3,331 | 3,291 (98.8%) | 6,904 | 2,472 | 587.0 s |

Confirmation requires at least two tracking hits; discarded tentative detections are not counted as confirmed beans. Missing measurements remain explicit. Measurements used for classification/selection: zero, as required by All Pass-through.

For 51 distributed reference samples at the same 320-pixel crop size, the new measurement worker matched the archived offline lightness, chroma and ellipsoid-volume outputs exactly. Left/right colour extraction was then run concurrently with unchanged numerical results.

## Deployed HTTP replay

The real Jetson service was exercised through the BeanoSort adapter using a disposable database and recorded frames. Existing user inventory was not used. These short paced runs overlapped regression testing and are workload checks, not live camera benchmarks. Their lower coverage is retained here explicitly:

| Image mode | Tracks | Measured | Events received | Saved stereo pairs | Result |
|---|---:|---:|---:|---:|---|
| stereo | 72 | 54 (75.0%) | 484 | 54 | completed |
| raw | 17 | 14 (82.4%) | 120 | 0 | completed |

The stereo test exercised pause/resume and explicit END. The RAW replay exercised duration expiry and referenced the existing immutable RAW recording; it did not make a new physical recording. All numerical events through each final watermark arrived, and image bytes were not transferred. The live RAW recording path was checked with mocked native capture/finalization tests; physical RAW capture remains to be verified.

## Deployment and remaining validation

- Jetson service: user systemd unit `beano-machine-jobs.service`, enabled, port 8091, authenticated with a private token.
- BeanoSort: port 8090; All Pass-through recipe and Live sorter station installed.
- Before migration backup: `beanosort/var/backups/before-machine-jobs-20261116T223943Z.db`.
- Row counts and SHA-256 digests matched before/after migration for containers, portions, operations, ledger, assignments, reservations, destinations, reconciliations and measurements. Foreign-key checks passed.
- Tokens and runtime recordings are ignored by Git. The Pi configuration is documented in [machine-jobs.md](machine-jobs.md). BeanoSort has not been moved to a Pi.
- Next validation is a short physical All Pass-through run. Measure actual live coverage, paired-frame delivery and RAW integrity before inferring sustained live performance from replay.

Machine outputs were checked off after the replay tests; the control lease was released.

## First-live-start output-directory fix

The first live attempt selected no image saves and failed before feeding (zero active seconds). FastCap still validates an existing output directory for its shared-memory mode, but only the RAW capture wrapper created that directory. The shared producer now creates it before launching either capture command, preserving existing contents and reporting filesystem failures without starting a child.

- The missing-directory regression failed before the fix and passed afterwards.
- Machine service, shared-memory source and clock checks: 17 passed, including fresh/existing directories for both capture commands and rejection of a file used as the output directory.
- Actual subprocess argument parsing and FastCap recording-plan validation passed for `none`, `stereo` and `raw`, with capture sessions replaced by a simulation. No hardware outputs were enabled during this validation.
- The failed job and its journal remain intact. These startup checks do not replace a physical live-capture trial.

## Actuator service recovery · 18 November 2026

The next live START failed at the initial all-output stop check: BeanoActuator was
not running/listening on port 5592. The machine journal recorded zero active feeding
seconds and no beans. The main controller remained connected to its separate board;
it correctly refused to confirm a full stop without the valve service.

After the operator reported a hard reset and explicitly approved an actuator restart,
the existing headless actuator was started as the transient user unit
`beano-actuator.service`, using the stable valve-board identity
`/dev/serial/by-id/usb-Espressif_ESP32-S2_84:f7:03:f2:9c:de-if00` rather than the old
USB topology path. This unit is not enabled for future boots. The serial port had
no other owner before launch.

Repeated read-only controller status checks confirmed the valve service available,
connected and clock-synchronized, with all three gate states off and no test/busy
activity. Conveyor, vibration, triggering, strobe and high-speed output were also
reported off. No sorting START or manual output test was sent, and there were no
pending machine commands. The existing failed job and inventory were left intact.
Recovery metadata is in ignored `var/machine-jobs/actuator-restart-20261118.json`;
service errors go to `var/beano-actuator.log`. A new physical run remains an operator
action; these status checks do not verify physical capture or throughput.

## First-frame priming fix · 18 November 2026

The next attempt (`7eb88492-62e2-58d5-b82b-09d2263d8758`) reached camera readiness,
background acquisition and calibration, then failed with
`SourceError: live source must be primed before frame zero`. The runner had omitted
the live source's required `prime()` call. It now reserves the first synchronized
pair after setup and before feeding or a queued resume can enable the feeder.
The shared source/tracker/classifier and archived replay paths were unchanged.

The stricter fake-device regression failed before the fix and passed afterwards.
It covers direct and initially paused starts, correct background/prime/frame-zero
ordering, and priming failure with no feed-on call plus complete cleanup. **20 machine,
live-source and clock tests passed**, and **7 BeanoSort machine-adapter tests passed**.
All devices/cameras were faked or backed by temporary shared-memory files. No physical
START or camera capture was invoked to satisfy these tests. Python compilation passed.

Before deployment there were no active machine jobs or pending machine commands,
and controller status reported all outputs off with the actuator synchronized.
The idle `beano-machine-jobs.service` was reloaded; the actuator PID was unchanged.
The numerical journal was backed up at
`beanosort/var/backups/machine-journal-before-live-prime-20261118T045404Z.db`.
All three journal tables and 16 material/job tables matched by row count and hash
before/after deployment. A read-only API check preserved the failed job's original
error and final cursor 17. Deployment metadata is under ignored
`beanosort/var/qa/live-prime-deployment-20261118.json`.

The failed attempt remains subject to the ordinary interruption review/reconciliation;
zero recorded feeding time and zero processed beans do not establish that no material
physically moved. No inventory history was edited, no failed job was reset, and no
physical retry was performed. Full live-start/throughput acceptance remains to be
performed by the operator.
