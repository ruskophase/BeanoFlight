"""Bounded reads of committed stereo crops; never opens a camera or controller."""
from pathlib import Path
import uuid

MAX_CROP_BYTES=4*1024*1024
PNG_SIGNATURE=b'\x89PNG\r\n\x1a\n'


def read_crop(store, root, key, sequence, camera):
    if str(uuid.UUID(key))!=key or type(sequence) is not int or not 1<=sequence<=2**63-1 or camera not in {'CamL','CamR'}:
        raise ValueError('Invalid saved crop identity')
    job=store.job(key)
    if job['state'] not in {'completed','failed'}:
        raise ValueError('Saved crops are available after the machine run finishes')
    if job['request'].get('image_mode')!='stereo':raise KeyError('No stereo crops for this run')
    event=store.event(key,sequence);data=event['data']
    if event['kind']!='images':raise KeyError('This event has no saved crops')
    if not isinstance(data,dict) or not isinstance(data.get('paths'),list):raise KeyError('Invalid saved crop record')
    bean=data.get('bean_sequence');frame=data.get('frame_index')
    if type(bean) is not int or bean<0 or type(frame) is not int or frame<0:
        raise KeyError('Invalid saved crop record')
    relative=f'stereo/{bean}/{frame:08d}-{camera}.png'
    if relative not in data.get('paths',[]):raise KeyError('This camera crop was not saved')
    base=Path(root).resolve();folder=(base/key).resolve();path=(folder/relative).resolve()
    if not folder.is_relative_to(base) or not path.is_relative_to(folder):raise KeyError('Saved crop unavailable')
    try:
        with path.open('rb') as image:body=image.read(MAX_CROP_BYTES+1)
    except OSError:raise KeyError('Saved crop unavailable') from None
    if not body.startswith(PNG_SIGNATURE) or len(body)>MAX_CROP_BYTES:
        raise KeyError('Saved crop is not a supported PNG')
    return body
