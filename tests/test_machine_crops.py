"""Saved crop delivery uses completed journal evidence, never hardware startup."""
import base64
from http.server import ThreadingHTTPServer
import threading
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from uuid import uuid4

import pytest

from beanoflight.machine_jobs.crops import MAX_CROP_BYTES,read_crop
from beanoflight.machine_jobs.server import Manager,make_handler
from test_machine_jobs import config

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a6T8AAAAASUVORK5CYII=')


def saved(manager,*,mode='stereo',state='completed'):
    key=str(uuid4());manager.store.create(key,{'image_mode':mode})
    path=manager.root/key/'stereo/42/00000009-CamL.png';path.parent.mkdir(parents=True);path.write_bytes(PNG)
    seq=manager.store.append(key,'images',{'bean_sequence':42,'frame_index':9,
        'paths':['stereo/42/00000009-CamL.png']})
    manager.store.status(key,{'state':state,'final_sequence':seq,'metrics':{}})
    return key,seq,path


def test_crop_api_auth_and_read_only_delivery(tmp_path):
    manager=Manager(config(tmp_path));key,seq,path=saved(manager)
    server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(manager,'secret'*10))
    worker=threading.Thread(target=server.serve_forever);worker.start()
    url=f'http://127.0.0.1:{server.server_port}/v1/jobs/{key}/crops/{seq}/CamL'
    before=manager.store.events(key)
    try:
        with pytest.raises(HTTPError) as exc:urlopen(url)
        assert exc.value.code==401
        request=Request(url,headers={'Authorization':'Bearer '+'secret'*10})
        for _ in range(2):
            with urlopen(request) as response:
                assert response.read()==PNG and response.headers['Content-Type']=='image/png'
                assert response.headers['Cache-Control'].startswith('private')
                assert response.headers['X-Content-Type-Options']=='nosniff'
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(url,data=b'{}',headers={'Authorization':'Bearer '+'secret'*10}))
        assert exc.value.code==404
        assert before==manager.store.events(key) and manager.runners=={}
    finally:server.shutdown();worker.join();server.server_close();manager.lockfile.close()


def test_only_committed_matching_run_and_camera_are_readable(tmp_path):
    manager=Manager(config(tmp_path));key,seq,path=saved(manager)
    try:
        assert read_crop(manager.store,manager.root,key,seq,'CamL')==PNG
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq,'CamR')
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq+1,'CamL')
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,str(uuid4()),seq,'CamL')
        with pytest.raises(ValueError):read_crop(manager.store,manager.root,'../../outside',seq,'CamL')
        with pytest.raises(ValueError):read_crop(manager.store,manager.root,key,seq,'../../outside')
        measurement=manager.store.append(key,'measurement',{'bean_sequence':42})
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,measurement,'CamL')
        evil=manager.store.append(key,'images',{'bean_sequence':42,'frame_index':9,'paths':['../../outside.png']})
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,evil,'CamL')
        outside=tmp_path/'outside.png';outside.write_bytes(PNG);path.unlink();path.symlink_to(outside)
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq,'CamL')
        path.unlink();path.write_bytes(b'not a PNG')
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq,'CamL')
        with path.open('wb') as file:file.write(PNG);file.truncate(MAX_CROP_BYTES+1)
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq,'CamL')
        path.unlink()
        with pytest.raises(KeyError):read_crop(manager.store,manager.root,key,seq,'CamL')
    finally:manager.lockfile.close()


@pytest.mark.parametrize('mode,state',[('none','completed'),('raw','completed'),('stereo','running'),('stereo','paused')])
def test_no_raw_or_active_run_downloads(tmp_path,mode,state):
    manager=Manager(config(tmp_path));key,seq,_=saved(manager,mode=mode,state=state)
    try:
        with pytest.raises((KeyError,ValueError)):read_crop(manager.store,manager.root,key,seq,'CamL')
    finally:manager.lockfile.close()
