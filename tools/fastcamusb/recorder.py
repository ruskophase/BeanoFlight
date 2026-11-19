"""Bounded compressed-JPEG recorder for the existing Pi FastCamUSB application.

Only the writer thread performs file/pipe IO. This module never opens a camera.
"""
from collections import deque
import csv
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

SCHEMA = 'fastcam-recorder-diagnostics/v1'


def buffer_policy(fps, seconds=None, byte_limit=None, stall_ms=None):
    try:
        seconds = float(seconds if seconds is not None else os.getenv('FASTCAM_RECORDER_BUFFER_SECONDS', '5'))
        byte_limit = int(byte_limit if byte_limit is not None else os.getenv('FASTCAM_RECORDER_MAX_BUFFER_BYTES', str(64 * 1024**2)))
        stall_ms = float(stall_ms if stall_ms is not None else os.getenv('FASTCAM_RECORDER_STALL_MS', '100'))
        if not all(math.isfinite(x) and x > 0 for x in (fps, seconds, stall_ms)) or byte_limit < 1:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError('Recorder fps, buffer seconds, byte cap and stall threshold must be positive finite values') from None
    return dict(schema=SCHEMA, buffer_seconds=seconds, frame_limit=max(1, math.ceil(fps * seconds)),
                byte_limit=byte_limit, stall_threshold_ms=stall_ms, fps_basis=fps,
                byte_basis='accepted compressed JPEG bytes, including the in-flight frame')


def recording_jpeg(jpeg):
    """Strip only the camera's empty APP1 marker; do not re-encode image data."""
    offset = 2
    while offset + 4 <= len(jpeg) and jpeg[offset] == 0xff:
        marker = jpeg[offset + 1]
        if marker in (0xda, 0xd9):
            break
        size = int.from_bytes(jpeg[offset + 2:offset + 4], 'big')
        if size < 2 or offset + size + 2 > len(jpeg):
            break
        end = offset + size + 2
        if marker == 0xe1 and jpeg[offset + 4:end] == b'\x00\x00':
            return jpeg[:offset] + jpeg[end:]
        offset = end
    return jpeg


class VideoRecorder:
    def __init__(self, folder, fps, external_trigger=False, *, buffer_seconds=None,
                 max_buffer_bytes=None, stall_ms=None):
        self.policy = buffer_policy(fps, buffer_seconds, max_buffer_bytes, stall_ms)
        if not shutil.which('ffmpeg'):
            raise RuntimeError('Recording requires ffmpeg.')
        folder = Path(folder).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (datetime.now().strftime('FastCam_%Y%m%d_%H%M%S_%f') + '.avi')
        self.path.touch(exist_ok=False)
        self.fps, self.external_trigger = fps, external_trigger
        self.timing_path = self.path.with_suffix('.csv')
        self.diagnostics_path = self.path.with_suffix('.diagnostics.json')
        self.frames = 0
        self.message = self.error = self.diagnostics_error = None
        self.timing_file = None
        self.first_received_ns = self.last_received_ns = None
        self.lock = threading.Lock()
        self.pending = queue.Queue(maxsize=self.policy['frame_limit'])
        self.stopping = threading.Event()
        self.buffered_frames = self.buffered_bytes = self.inflight_bytes = 0
        self.high_frames = self.high_bytes = self.accepted = self.rejected = self.ignored_after_failure = 0
        self.written_bytes = self.abandoned = self.stall_count = 0
        self.last_write_ms = self.max_write_ms = self.max_queue_wait_ms = 0.0
        self.write_started_ns = None
        self.started_at = self._utc()
        self.started_ns = time.monotonic_ns()
        self.stopped_accepting_at = self.failure_at = self.failure_ns = self.failure_reason = None
        self.finalized = False
        self.samples = deque(maxlen=300)
        self.stalls = deque(maxlen=64)
        self.sample_count = 0
        self.last_sample_ns = 0
        self.log = tempfile.TemporaryFile()
        rate = Fraction(str(fps)).limit_denominator(100000)
        rate_text = f'{rate.numerator}/{rate.denominator}'
        try:
            self.timing_file = self.timing_path.open('x', newline='')
            self.timing_writer = csv.writer(self.timing_file)
            self.timing_writer.writerow(['frame', 'sequence', 'camera_timestamp_us', 'host_monotonic_ns', 'elapsed_seconds'])
            self.process = subprocess.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                '-f', 'mjpeg', '-framerate', rate_text, '-i', 'pipe:0', '-an', '-c:v', 'copy', '-r', rate_text,
                '-bsf:v', f'setts=ts=N:duration=1:time_base={rate.denominator}/{rate.numerator}',
                '-f', 'avi', str(self.path)
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log)
        except Exception:
            if self.timing_file:
                self.timing_file.close()
                self.timing_path.unlink(missing_ok=True)
            self.log.close()
            self.path.unlink(missing_ok=True)
            raise
        self.thread = threading.Thread(target=self._write, name='video-writer', daemon=True)
        self.thread.start()

    @staticmethod
    def _utc():
        return datetime.now(timezone.utc).isoformat()

    @property
    def running(self):
        return self.thread.is_alive()

    def _fail_locked(self, reason, message):
        if self.failure_at is None:
            self.failure_at = self._utc()
            self.failure_ns = time.monotonic_ns()
            self.failure_reason = reason
            self.error = message
        self.stopped_accepting_at = self.stopped_accepting_at or self.failure_at
        self.stopping.set()

    def enqueue(self, jpeg, timestamp_us=0, sequence=0):
        """Never wait for storage or queue space; all rejections are explicit."""
        now = time.monotonic_ns()
        with self.lock:
            if self.stopping.is_set():
                if self.failure_at is not None:
                    self.ignored_after_failure += 1
                return False
            frame_full = self.buffered_frames >= self.policy['frame_limit']
            byte_full = self.buffered_bytes + len(jpeg) > self.policy['byte_limit']
            if frame_full or byte_full:
                self.rejected += 1
                reason = 'frame_limit' if frame_full else 'byte_limit'
                self._fail_locked(reason, 'Recording stopped: recorder buffer overflow (' + reason +
                                  '); the writer may be delayed by output IO or scheduling.')
                return False
            self.pending.put_nowait((jpeg, timestamp_us, sequence, now))
            self.accepted += 1
            self.buffered_frames += 1
            self.buffered_bytes += len(jpeg)
            self.high_frames = max(self.high_frames, self.buffered_frames)
            self.high_bytes = max(self.high_bytes, self.buffered_bytes)
            self.first_received_ns = self.first_received_ns or now
            self.last_received_ns = now
            return True

    def stop(self):
        with self.lock:
            self.stopped_accepting_at = self.stopped_accepting_at or self._utc()
            self.stopping.set()

    def diagnostics(self, *, history=False):
        now = time.monotonic_ns()
        with self.lock:
            data = dict(schema=SCHEMA, recording_id=self.path.stem, policy=dict(self.policy),
                started_at=self.started_at, stopped_accepting_at=self.stopped_accepting_at,
                failure_at=self.failure_at, failure_reason=self.failure_reason,
                failure_monotonic_ns=self.failure_ns,
                failure_elapsed_seconds=(self.failure_ns - self.started_ns) / 1e9 if self.failure_ns is not None else None,
                clock='Pi monotonic clock; not interchangeable with Jetson or camera timestamps',
                accepted_frames=self.accepted, written_frames=self.frames, rejected_frames=self.rejected,
                ignored_after_failure=self.ignored_after_failure, abandoned_frames=self.abandoned,
                buffered_frames=self.buffered_frames, queued_frames=self.pending.qsize(),
                buffered_bytes=self.buffered_bytes, inflight_bytes=self.inflight_bytes,
                peak_buffered_frames=self.high_frames, peak_buffered_bytes=self.high_bytes,
                written_bytes=self.written_bytes, last_write_ms=self.last_write_ms, max_write_ms=self.max_write_ms,
                current_write_ms=(now - self.write_started_ns) / 1e6 if self.write_started_ns is not None else 0,
                max_queue_wait_ms=self.max_queue_wait_ms, stall_count=self.stall_count,
                first_received_monotonic_ns=self.first_received_ns, last_received_monotonic_ns=self.last_received_ns,
                received_span_seconds=(self.last_received_ns - self.first_received_ns) / 1e9 if self.first_received_ns is not None else None,
                finalized=self.finalized, diagnostics_path=str(self.diagnostics_path), diagnostics_error=self.diagnostics_error,
                error=self.error, image_data_reencoded=False)
            if history:
                data.update(queue_samples=list(self.samples), stalls=list(self.stalls),
                    samples_total=self.sample_count, samples_truncated=self.sample_count > len(self.samples),
                    stalls_truncated=self.stall_count > len(self.stalls))
            return data

    def _sample_locked(self, now):
        if now - self.last_sample_ns >= 1_000_000_000:
            self.samples.append(dict(elapsed_seconds=(now-self.started_ns)/1e9,
                buffered_frames=self.buffered_frames, buffered_bytes=self.buffered_bytes, written_frames=self.frames))
            self.sample_count += 1
            self.last_sample_ns = now

    def _write(self):
        code = None
        try:
            while True:
                try:
                    jpeg, timestamp_us, sequence, received_ns = self.pending.get(timeout=.1)
                except queue.Empty:
                    if self.stopping.is_set():
                        break
                    continue
                tick = time.monotonic_ns()
                with self.lock:
                    self.inflight_bytes = len(jpeg)
                    self.write_started_ns = tick
                    self.max_queue_wait_ms = max(self.max_queue_wait_ms, (tick-received_ns)/1e6)
                try:
                    payload = recording_jpeg(jpeg)
                    self.process.stdin.write(payload)
                    completed = time.monotonic_ns()
                    duration = (completed-tick)/1e6
                    number = self.frames + 1
                    self.timing_writer.writerow([number, sequence, timestamp_us, received_ns,
                        f'{(received_ns - self.first_received_ns) / 1e9:.9f}'])
                    with self.lock:
                        self.frames = number
                        self.written_bytes += len(payload)
                        self.last_write_ms = duration
                        self.max_write_ms = max(self.max_write_ms, duration)
                        if duration >= self.policy['stall_threshold_ms']:
                            self.stall_count += 1
                            self.stalls.append(dict(at=self._utc(), frame=number, write_ms=duration,
                                buffered_frames=self.buffered_frames, buffered_bytes=self.buffered_bytes))
                finally:
                    with self.lock:
                        self.buffered_frames -= 1
                        self.buffered_bytes -= len(jpeg)
                        self.inflight_bytes = 0
                        self.write_started_ns = None
                        self._sample_locked(time.monotonic_ns())
            self.process.stdin.close()
            code = self.process.wait(timeout=15)
            if code:
                raise RuntimeError(f'Video writer exited with code {code}.')
            self.timing_file.close()
            self.finalized = True
        except Exception as exc:
            with self.lock:
                self._fail_locked('write_failed', f'Recording failed: {exc}')
        finally:
            self.stopping.set()
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            if not self.process.stdin.closed:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass
            self.log.seek(0)
            details = self.log.read().decode(errors='replace').strip()
            self.log.close()
            if details and self.error:
                self.error += ' ' + details[-1000:]
            if self.timing_file and not self.timing_file.closed:
                try:
                    self.timing_file.close()
                except OSError as exc:
                    with self.lock:
                        self._fail_locked('timing_failed', f'Could not finish recording timestamps: {exc}')
            with self.lock:
                self.abandoned = self.accepted - self.frames
                while not self.pending.empty():
                    self.pending.get_nowait()
                self.buffered_frames = self.buffered_bytes = self.inflight_bytes = 0
                self.write_started_ns = None
                self.stopped_accepting_at = self.stopped_accepting_at or self._utc()
            if self.frames == 0:
                self.path.unlink(missing_ok=True)
                self.timing_path.unlink(missing_ok=True)
                self.message = 'No frames received; no recording was created.'
                if code is not None and self.failure_reason == 'write_failed' and 'does not contain any stream' in details:
                    self.error = self.failure_at = self.failure_ns = self.failure_reason = None
            else:
                self.message = f'Saved {self.path} ({self.frames} frames at {self.fps:g} fps) + timing CSV: {self.timing_path.name}'
            try:
                metadata = self.diagnostics(history=True)
                metadata['finished_at'] = self._utc()
                temporary = self.diagnostics_path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(metadata, indent=2, allow_nan=False) + '\n')
                temporary.replace(self.diagnostics_path)
            except Exception as exc:
                self.diagnostics_error = str(exc)
            print(json.dumps(dict(event='recorder_finished', recording_id=self.path.stem, frames=self.frames,
                failure_at=self.failure_at, failure_reason=self.failure_reason, error=self.error,
                peak_buffered_frames=self.high_frames, peak_buffered_bytes=self.high_bytes,
                max_write_ms=self.max_write_ms, stall_count=self.stall_count,
                diagnostics_path=str(self.diagnostics_path), diagnostics_error=self.diagnostics_error)), flush=True)
