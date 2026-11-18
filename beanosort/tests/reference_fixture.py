"""Small explicit test reference; no images or hardware libraries required."""
import base64
import hashlib
import json
from pathlib import Path


def bundle(directory):
    root=Path(directory); (root/'photos').mkdir(parents=True)
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3ioAAAAASUVORK5CYII=')
    entries=[]; rows=[]
    for sequence,lightness in enumerate([10,22,23,24,25,26,27,28,29,30],1):
        row={'schema':'beanoflight-statistics-bean/v1','bean_id':f'test/{sequence:06d}',
             'bean_sequence':sequence,'sample_count':3,'combined_lab_l_mean':lightness,
             'combined_lab_chroma_mean':lightness/2,'caml_ellipse_major_mm_median':lightness/4,
             'rotational_ellipsoid_volume_proxy_mm3_median':lightness*5,
             'projected_area_geomean_mm2_median':lightness*2,
             'caml_ellipse_aspect_ratio_median':lightness/20}
        for camera in ('CamL','CamR'):
            name=f'photos/{sequence}-{camera}.png'; (root/name).write_bytes(png)
            row['photo_'+camera]=name
            entries.append({'path':name,'sha256':hashlib.sha256(png).hexdigest(),'bytes':len(png)})
        rows.append(row)
    raw=''.join(json.dumps(row)+'\n' for row in rows).encode();(root/'beans.jsonl').write_bytes(raw)
    entries.append({'path':'beans.jsonl','sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)})
    (root/'manifest.json').write_text(json.dumps({'schema':'beanoflight-statistics-bundle/v1','files':entries,
        'provenance':{'purpose':'synthetic automated test fixture'},'summary':{'bean_photos':{'image_count':20}}}))
    return root
