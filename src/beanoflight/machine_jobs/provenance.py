"""Small manifests for immutable calibration and code used by a job."""
import hashlib
from pathlib import Path
from .store import fingerprint


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def tree_hashes(root):
    root=Path(root)
    return {str(p.relative_to(root)):file_hash(p) for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts}


def configuration(config):
    payload={'calibration':tree_hashes(config['calibration_pack']), 'nozzle_map_sha256':file_hash(config['nozzle_map'])}
    return {'id':fingerprint(payload),**payload}
