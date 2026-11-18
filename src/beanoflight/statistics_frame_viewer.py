"""Read-only local statistics viewer with on-demand recording frames."""

from __future__ import annotations

import argparse
import hashlib
import json
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import cv2

from .calibration import find_pinkplane_homography
from .source import MMapRawVideoSource, SourceError
from .statistics_photo_dashboard import _source_recording


def frame_jpeg(source: MMapRawVideoSource, index: int, camera: str) -> bytes:
    if camera not in {"CamL", "CamR"} or not 0 <= index < source.metadata.frame_count:
        raise ValueError("invalid frame request")
    frame = source.frame(index)
    try:
        if camera == "CamL":
            mosaic, lut = frame.mosaic, source._stored_detection_lut
        else:
            mosaic, lut = frame.right_mosaic, source._right_stored_detection_lut
        return _encode_frame(mosaic, lut)
    finally:
        source.release_frame(frame)


def paired_frame_jpegs(source: MMapRawVideoSource, index: int) -> tuple[bytes, bytes, int]:
    """Decode one synchronized RAW pair once for a portable frame pack."""

    if not 0 <= index < source.metadata.frame_count:
        raise ValueError("invalid frame request")
    frame = source.frame(index)
    try:
        if frame.right_frame_index is None:
            raise SourceError("synchronized CamR frame is unavailable")
        return (
            _encode_frame(frame.mosaic, source._stored_detection_lut),
            _encode_frame(frame.right_mosaic, source._right_stored_detection_lut),
            frame.right_frame_index,
        )
    finally:
        source.release_frame(frame)


def _encode_frame(mosaic, lut) -> bytes:
    if lut is None:
        raise SourceError("camera response is unavailable")
    # Review-only full-frame colour preview; measured crops remain the
    # calibrated colour source for statistics and focus analysis.
    colour = cv2.cvtColor(lut[mosaic], cv2.COLOR_BayerRG2BGR)
    ok, encoded = cv2.imencode(".jpg", colour, [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise SourceError("could not encode full frame")
    return encoded.tobytes()


class ViewerHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str, source: MMapRawVideoSource | None, **kwargs):
        self.source = source
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/index.html")
            self.end_headers()
            return
        if url.path == "/api/frame":
            if self.source is None:
                self.send_error(404, "Full frame not available")
                return
            query = parse_qs(url.query)
            try:
                frame = int(query["frame"][0])
                camera = query["camera"][0]
                content = frame_jpeg(self.source, frame, camera)
            except (ValueError, KeyError, IndexError, SourceError, OSError):
                self.send_error(404, "Full frame not available")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(content)
            return
        super().do_GET()


def matching_recording(bundle: Path) -> Path | None:
    """Only read full frames from the recording named by the bundle hash."""

    recording = _source_recording(bundle)
    if recording is None:
        return None
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        return recording
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = (manifest.get("provenance") or {}).get("recording_manifest", {}).get("sha256")
        if expected and hashlib.sha256((recording / "recording.json").read_bytes()).hexdigest() != expected:
            return None
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return recording


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="Statistics bundle inside a recording, or detached")
    parser.add_argument("--port", type=int, default=0, help="Local port (default: choose automatically)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    bundle = args.bundle.expanduser().resolve()
    if not (bundle / "dashboard/index.html").is_file():
        parser.error("bundle has no dashboard/index.html")
    recording = matching_recording(bundle)
    source = None
    if recording is not None:
        try:
            homography = find_pinkplane_homography(recording)
            if homography is None:
                raise SourceError("homography missing")
            source = MMapRawVideoSource(recording, crop_processing="calibrated")
            source.configure_stereo(homography, (2, 8, 14))
        except (SourceError, OSError, ValueError) as exc:
            if source is not None:
                source.close()
            source = None
            print(f"Full frame not available: {exc}")
    handler = partial(ViewerHandler, directory=str(bundle), source=source)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    address = f"http://127.0.0.1:{server.server_port}/dashboard/index.html"
    print(f"Statistics dashboard: {address}")
    print("Full-frame source:", recording if source is not None else "unavailable; black-frame fallback")
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if source is not None:
            source.close()


if __name__ == "__main__":
    main()
