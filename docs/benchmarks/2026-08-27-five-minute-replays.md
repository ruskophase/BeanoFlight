# Five-minute playback validation — 2026-08-27

Three production-valid stereo recordings made with calibration bundle
`beano-56cdb225d170-r2` were replayed through the isolated full pipeline with
real TensorRT inference. Each accepted run used frames 2, 8 and 14 as its
provisional empty background, 60 fps pacing, a 60-frame prepared buffer, two
stereo crops per bean, adaptive edge resizing and virtual actuation. No ESP32
actuator or machine motor was used by these tests.

## Accepted results

| Recording | Frames | FPS | Mean analysis | Pacing misses | Skips | Beans | TensorRT jobs | CamR unavailable | Peak temp | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| 80 step/s, 25% vibration | 18,001 | 60.0000 | 5.02 ms | 77 | 0 | 5,159 | 10,312 | 357/10,669 (3.35%) | 54.6 C | PASS |
| 90 step/s, 25% vibration | 18,001 | 59.9999 | 5.19 ms | 130 | 0 | 5,388 | 10,770 | 312/11,082 (2.82%) | 55.1 C | PASS |
| 110 step/s, 27% vibration | 18,001 | 59.9999 | 6.34 ms | 467 | 0 | 7,261 | 14,514 | 505/15,019 (3.36%) | 55.3 C | PASS |

Across the accepted runs, all 35,596 inference jobs completed. There were no
crop drops, inference drops, inference failures, Registry transport retries,
late sort decisions, awaiting decisions or suspected adjacent track fragments.
All 1,760 scheduled virtual actuations succeeded. Four, four and six beans
respectively used the intentional deadline-fallback classification path.

`Pacing misses` counts frames whose work ended after the exact frame deadline;
it does not mean those frames were discarded. The processed and source-timeline
rates remained at 60 fps. The accepted 110 step/s run reached 28.25 ms maximum
frame age, still below the 30 ms stale-frame ceiling.

The Registry grew by approximately 60 MiB from its just-started sample in every
run despite the substantial difference in bean population. That repeatable
one-time working-set growth, the bounded 256-record hot cache, stable 93–96 MiB
peak Registry RSS, falling Inferencer RSS and 50–54 MiB Sorter peak are not
evidence of a five-minute leak. A multi-run endurance test is still required to
prove a long-term plateau.

Stereo pairing remained synchronized to at most 1 microsecond. CamR local
refinement distance was 8.05, 7.61 and 10.08 px mean, with 17.47, 16.95 and
20.27 px p95. The 110 step/s maximum was 63.59 px, close to the configured
64 px search limit. Almost every unavailable CamR crop was a component clipped
by the image boundary; this is not by itself evidence that the new homography
is wrong, but the higher-density recording should remain the geometry stress
case.

## Duration-dependent defects found and corrected

1. Replay rejected more than 1,000 frames. The shared limit is now 100,000 in
   the settings validator, CLI help and GUI control. The conservative default
   remains 1,000 frames.
2. The first complete 80 step/s replay finished its frames but its benchmark
   outcome query timed out. A whole-run Registry response exceeded the intended
   4 MiB message envelope and the fixed two-second timeout. Registry protocol
   version 3 now exposes indexed pages of at most 100 records; the benchmark
   reads these with a five-second request timeout and a 30-second settlement
   window. The successful runs required 52, 54 and 73 pages with zero retries.
3. The first 90 step/s report left one bean awaiting a decision. It was born on
   final frame 18,000, completed its only possible inference sample, and could
   never obtain the requested second sample. Natural recorded-run boundaries
   now right-censor active tracks just like bounded live runs. The unchanged A/B
   rerun recorded one right-censored track and settled all 5,387 beans with jobs.
4. The first 110 step/s run discarded one frame after an isolated excursion
   beyond the 30 ms stale limit. It still completed every submitted job and
   decision. Replay reports now retain bounded `stale_skip_events` containing
   the frame index, count and age. An instrumented unchanged five-minute repeat
   skipped zero frames, so the original event was not sustained overload.

All 201 unit and integration tests pass after these changes.

## Background candidates

Frames 2, 8 and 14 lie inside the first 300 ms, before the recorded motor start.
All nine CamL/CamR pairs were visually reviewed as bean-free. Untouched
scientific PNGs and explicitly labelled display-stretched contact sheets are in
`/home/doceave/Beano/diagnostics/playback-long-duration-20260827/background-candidates`.
The operator confirmed on 2026-08-28 that all nine selected frames contain no
beans. Frames 2, 8 and 14 are therefore accepted backgrounds for all three
recordings.

The calibrated images are intrinsically dark (median 5–7/255). Between
successive candidates, mean absolute pixel difference is 2.39–2.77/255 and p99
is 10–12/255, consistent with stable sensor noise rather than moving beans.

## Object-size check

To test the concern caused by moving the cameras approximately 40 mm backward,
300 evenly spaced frames from each recording were compared with the current
2,000 px / 50 px minimums and a conservative 1,600 px / 45 px profile. The
smaller profile added only 4, 5 and 6 detections respectively and gave zero
detections on all nine empty candidates. Visual review showed almost all added
components were beans clipped at the top or bottom, generally failing the
current height filter at 46–48 px. They were not fully visible small beans.

The current minimums should therefore remain unchanged until a track-level A/B
test demonstrates recovered complete beans without extra fragments or false
births. Review sheets are in
`/home/doceave/Beano/diagnostics/playback-long-duration-20260827/min-area-sample`.

## Directional stereo residuals

Every sixth frame from all three recordings was subsequently reprocessed with
the production detector and CamR localizer, yielding 4,446, 4,657 and 6,138
successful directional residual measurements. Median CamR correction vectors
were (+2.13, -12.04), (+2.00, -11.85) and (+4.07, -14.21) px; distance p95 was
44.54, 43.87 and 48.09 px respectively.

This is not a constant camera-to-camera offset. Horizontal correction changes
from positive on the left to negative on the right, while vertical correction
grows from roughly -2 to -5 px at the top to -31 to -38 px at the bottom. A
diagnostic affine fit explains 72-78% of horizontal and 80-82% of vertical
variance, reducing median residual distance to 4.6-5.0 px. The fit was not
installed in the pipeline.

The repeatable field pattern indicates a plane/scale/projective mismatch. It is
consistent with the falling-bean plane differing from the PinkPlane calibration
plane, a repeatable PinkPlane placement error, or an upstream coordinate-domain
mismatch. A constant pixel offset is not an appropriate repair. Confirm the
physical calibration plane, repeat the homography with an unambiguous
orientation witness, then rerun the same residual analysis. Full samples,
4-by-3 field maps and the detailed interpretation are under
`/home/doceave/Beano/diagnostics/playback-long-duration-20260827/stereo-residuals`.

## Preserved reports

The full JSON reports, including bounded per-bean timing ledgers, are under
`/home/doceave/Beano/diagnostics/playback-long-duration-20260827`:

- `80sps-vib25-full-18001.json`
- `90sps-vib25-full-18001.json` (pre-fix unsettled boundary evidence)
- `90sps-vib25-full-18001-boundary-fixed.json`
- `110sps-vib27-full-18001.json` (isolated one-frame skip)
- `110sps-vib27-full-18001-repeat2.json` (instrumented accepted repeat)

The next physical phase should use the accepted 110 step/s recording as the
highest-density simulation stress case, then perform a witnessed direct-camera
run before enabling physical actuation. Classification and sorting quality need
human-labelled bean outcomes; throughput timing alone cannot establish model
accuracy.
