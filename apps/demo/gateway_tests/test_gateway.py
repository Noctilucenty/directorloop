import importlib.util
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('demo_gateway', Path(__file__).parents[1] / 'gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
CODE = 'demo-test-only-code-' + 'a' * 32
AUTH = {'Authorization': 'Bearer ' + CODE, 'Origin': 'https://directorloop-demo.onrender.com'}

@pytest.fixture
def bridge(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        p = request.url.path
        if p == '/api/health':
            return httpx.Response(200, json={'screening': {'available': True, 'model': 'Qwen', 'reason': None}, 'weave': {'connected': True}})
        if p == '/api/uploads':
            return httpx.Response(200, json={'video_id': 'upl-test', 'duration_ms': 6000})
        if p == '/api/screenings':
            return httpx.Response(200, json={'job_id': 'job_abc_def', 'screen_id': 'screen_abc_def'})
        if p == '/api/jobs/job_abc_def':
            return httpx.Response(200, json={'id': 'job_abc_def', 'state': 'RUNNING', 'params': {'path': '/Users/secret/file.mp4'}})
        if p == '/api/screenings/screen_abc_def':
            return httpx.Response(200, json={'id':'screen_abc_def','status':'complete','semantic_grounding_verified':False,'automatic_edit_allowed':False,'artifact_path':'/Users/secret/file.mp4','raw_output':{'secret':'hidden'},'windows':[{'status':'complete','judgment':{'understanding':'x','observations':[{'text':'x','kind':'visible_fact','frame_timestamps_ms':[166],'asr_quote':None}]}}]})
        return httpx.Response(404, json={'detail':'missing'})
    app = gateway.create_app(code=CODE, engine_token='not-public', database=tmp_path/'registry.sqlite3', transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        yield client, calls

def submit(client, request_id='request-1234567890', content=b'video'):
    return client.post('/analyze', headers=AUTH, data={'request_id':request_id}, files={'file':('clip.mp4',io.BytesIO(content),'video/mp4')})

def test_auth_origin_oversize_prevent_engine_dispatch(bridge):
    client, calls=bridge
    assert client.post('/analyze').status_code == 401
    assert client.post('/analyze',headers={**AUTH,'Origin':'https://evil.test'}).status_code == 403
    assert client.post('/analyze',headers={**AUTH,'Content-Length':str(60*1024*1024)}).status_code == 413
    assert calls == []

def test_preflight_allowed_origin_only(bridge):
    client,_=bridge
    r=client.options('/analyze',headers={'Origin':AUTH['Origin'],'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'})
    assert r.status_code==200
    assert r.headers['access-control-allow-origin']==AUTH['Origin']
    r=client.options('/analyze',headers={'Origin':'https://evil.test','Access-Control-Request-Method':'POST'})
    assert r.status_code==400

def test_only_owned_resource_routes(bridge):
    client,calls=bridge
    for route in ['/jobs/job_other_a','/reports/screen_other_a','/api/videos','/api/runs','/api/ingest/url','/media/../../.env']:
        assert client.get(route,headers=AUTH).status_code==404
    assert calls==[]

def test_submit_is_idempotent_and_single_active(bridge):
    client,calls=bridge
    first=submit(client)
    assert first.status_code==200
    assert submit(client).json()==first.json()
    assert submit(client,content=b'other').status_code==409
    assert submit(client,request_id='request-9876543210').status_code==409
    assert calls.count(('POST','/api/screenings'))==1
    assert calls.count(('POST','/api/uploads'))==1

def test_results_whitelist_and_no_promotion(bridge):
    client,_=bridge
    assert submit(client).status_code==200
    job=client.get('/jobs/job_abc_def',headers=AUTH)
    assert 'params' not in job.json()
    r=client.get('/reports/screen_abc_def',headers=AUTH)
    assert r.status_code==200
    assert '/Users/' not in r.text and 'raw_output' not in r.text and 'artifact_path' not in r.text
    assert r.json()['semantic_grounding_verified'] is False
    assert r.json()['automatic_edit_allowed'] is False
    assert r.headers['cache-control']=='no-store'

def test_invalid_file_or_request_does_not_dispatch(bridge):
    client,calls=bridge
    assert submit(client,request_id='bad').status_code==422
    assert submit(client,content=b'').status_code==422
    assert client.post('/analyze',headers=AUTH,data={'request_id':'request-1234567890'},files={'file':('bad.txt',b'foo')}).status_code==415
    assert calls==[]
