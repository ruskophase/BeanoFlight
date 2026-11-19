#!/usr/bin/env python3
"""Apply the reviewed integration to an idle, matching standalone Pi app."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--app', type=Path, required=True)
    p.add_argument('--status-url', default='http://127.0.0.1:8765/api/status')
    p.add_argument('--check', action='store_true', help='Validate without writing app files')
    args = p.parse_args()
    source = Path(__file__).resolve().parent
    manifest = json.loads((source/'integration.json').read_text())
    app = args.app.resolve()
    old = {}
    replacements = {'recorder.py': (source/'recorder.py').read_bytes()}
    # Reject unknown revisions before changing anything, including the module.
    with tempfile.TemporaryDirectory(prefix='fastcam-recorder-install-') as temp:
        for name, hashes in manifest.items():
            data = (app/name).read_bytes()
            old[name] = data
            if digest(data) == hashes['after']:
                replacements[name] = data
                continue
            if digest(data) != hashes['before']:
                raise SystemExit('Refusing unknown source revision: '+str(app/name))
            (Path(temp)/name).write_bytes(data)
            subprocess.run(['patch', '--batch', '--forward', '-d', temp, '-i', str(source/(name+'.patch'))], check=True)
            replacement = (Path(temp)/name).read_bytes()
            if digest(replacement) != hashes['after']:
                raise SystemExit('Patched checksum mismatch: '+name)
            replacements[name] = replacement
    for name, data in replacements.items():
        compile(data, name, 'exec')
    with urlopen(args.status_url, timeout=3) as response:
        status = json.load(response)
    if any(status.get(k) is not False for k in ('camera_running','recording','armed','finishing')):
        raise SystemExit('Camera must be confirmed idle before installing/reloading')
    if args.check:
        print('Source revisions verified; camera idle; no files changed.')
        return
    if (app/'recorder.py').exists():
        old['recorder.py'] = (app/'recorder.py').read_bytes()
    backup = app/'recorder-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup.mkdir(parents=True)
    for name,data in old.items():
        (backup/name).write_bytes(data)
    (backup/'manifest.json').write_text(json.dumps({k:digest(v) for k,v in old.items()},indent=2)+'\n')
    # The service must remain idle until its subsequent restart; no settings touched.
    for name, data in old.items():
        if (app/name).read_bytes() != data:
            raise SystemExit('Source changed during installation: '+name)
    for name,data in replacements.items():
        temporary = app/(name+'.recorder-update')
        temporary.write_bytes(data)
        shutil.copymode(app/name if (app/name).exists() else app/'capture_core.py',temporary)
        temporary.replace(app/name)
    print('Installed recorder update. Backup: '+str(backup))
    print('Restart fastcamusb.service only while the camera remains idle.')


if __name__ == '__main__':
    main()
