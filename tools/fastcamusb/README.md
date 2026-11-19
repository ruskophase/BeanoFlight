# FastCamUSB bounded recorder

This is the maintained recorder update for the standalone Pi application at
`/home/doceave/Beano/FastCamUSB`. That application is not a Git checkout. Keep
the recorder, checksum-guarded integration patches and tests here rather than
copying its camera/native SDK into BeanoFlight. `capture_core.VideoRecorder` and
`recording_jpeg` remain public imports; CameraStream and native capture are unchanged.

The writer stream-copies compressed JPEGs to AVI with the existing ffmpeg command;
it does not re-encode them. The camera callback only reserves bounded memory and
enqueues a frame, without waiting for storage or queue space. The limits include
the in-flight JPEG. The first limit reached stops acceptance with a recorded
failure; accepted frames drain in order if output remains writable. A larger
buffer absorbs temporary stalls, not indefinitely slow or unavailable storage.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `FASTCAM_RECORDER_BUFFER_SECONDS` | `5` | Frame limit = ceiling(effective playback/stream fps × seconds); 900 frames at 180 fps. This is not a guarantee against bursts. |
| `FASTCAM_RECORDER_MAX_BUFFER_BYTES` | `67108864` | 64 MiB of accepted compressed JPEGs, including in-flight frame. |
| `FASTCAM_RECORDER_STALL_MS` | `100` | Report write steps at or above this threshold as stalls. |

These are recorder/service configuration, separate from persisted camera settings.
The byte budget excludes Python/queue overhead, one temporary normalized JPEG and
ffmpeg/OS buffers; it is not a total process RSS limit. Buffer sizing uses compressed
bytes, not decoded RGB. No disk mount, dirty-page policy, USB speed, exposure, frame
rate or trigger defaults are changed.

## Telemetry and evidence

The listener's `/api/status` includes `recorder_policy`, compact `recorder`
diagnostics, and `previous_recording` when a new camera session has replaced an old
one. Historical recorder failures no longer contaminate a new camera session.
AVI and timing CSV names/columns retain their existing meanings. Each recording
also writes `<stem>.diagnostics.json` on completion, plus one structured journal
summary. The sidecar has the actual first failure UTC and Pi monotonic/elapsed time,
accepted/written/rejected/abandoned frame counts, frame/byte peaks, longest pipe
write and queue wait, stall count and bounded histories (300 one-second samples,
64 most recent stalls; truncation is explicit). A live blocked write is
visible as `current_write_ms`. Pipe timing includes JPEG marker normalization but
excludes the CSV row write. A timing-file or mux failure remains an error. A
sidecar-save failure is separately reported; diagnostics cannot guarantee power-loss
durability. Written frames do not mean independently verified sensor exposures or
durable media writes.

BeanoFlight reads compact diagnostics approximately once a second in a dedicated
read-only monitor; a slow GET cannot hold up the scheduled STOP worker. Network
loss means stale/unknown telemetry, not a confirmed recorder failure. Source Pi
failure time and Jetson first-observed elapsed time stay separate. Failure is
shown promptly while the original recording window/cleanup and main sorter policy
remain in effect. A recording error does not negate a confirmed STOP acknowledgement.
Older runs have no retroactively fabricated telemetry.

## Install and rollback

Copy this directory to the Pi. While the listener confirms camera/recording/arming/
finishing are all false, run:

```bash
python3 install.py --app /home/doceave/Beano/FastCamUSB --check
python3 install.py --app /home/doceave/Beano/FastCamUSB
systemctl --user restart fastcamusb.service
curl -fsS http://127.0.0.1:8765/api/status
```

The installer refuses unknown `capture_core.py` or `controller.py` revisions,
validates patch/result hashes and syntax before writing, and backs up the replaced
files with a SHA-256 manifest under `FastCamUSB/recorder-backups/<UTC>/`. It does
not start a camera or alter `settings.json`. Keep the station idle through restart;
the running service does not hot-reload these modules. For rollback, while idle,
restore the backed-up `capture_core.py`, `controller.py` and `recorder.py` if present,
then restart the listener. The original pre-update app does not import recorder.py,
so an extra unused module can remain. Do not roll back during physical processing.

## Tests without hardware

```bash
python3 -m unittest discover -s tools/fastcamusb/tests -v
# Additionally exercise a staged patched controller with fake streams:
FASTCAMUSB_TEST_APP=/path/to/staged-app python3 -m unittest discover -s tools/fastcamusb/tests -v
```

Recorder tests use a fake ffmpeg process and event-controlled blocked output:
frame/byte bounds include in-flight frames, backlog recovery preserves order,
failure times survive draining/retries, failed writes account for abandoned frames,
and image coefficients remain unchanged. Optional controller tests import definitions
only and replace CameraStream with fakes; they issue no camera commands.

For a real ffmpeg/filesystem test on the Pi, replay an existing AVI into a **new**
offline-only directory. The tool uses ffprobe packet offsets to reuse JPEGs,
restores the empty APP1 camera marker to exercise normalization, submits 5,400
frames at 180 fps, and inserts a 1.3-second writer delay. It only GETs idle status.

```bash
python3 replay_check.py --module-directory /path/to/staged-app \
  --source /path/to/existing.avi --output /path/to/new-offline-test-directory
```

On 2026-11-19 the Lexar test saved all **5,400 frames / 30.0 s**, with a peak
**371 frames / 8,177,221 bytes** and longest write **2,059.5 ms**, without opening
a camera or modifying the source AVI. The new cap was 900 frames / 64 MiB; the
old 180-frame cap could not cover this backlog. This validates saved-frame recorder
recovery, not simultaneous live-camera/sorter load or hardware timing. Ignored
BeanoSort QA artifacts retain the test logs, sidecar, results and service checks.
