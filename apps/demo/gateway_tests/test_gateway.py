import importlib.util
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('demo_gateway', Path(__file__).parents[1] / 'gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
SESSION = 'a' * 32
AUTH = {'X-Demo-Session': SESSION, 'Origin': 'https://directorloop-demo.onrender.com'}

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
    app = gateway.create_app(engine_token='not-public', database=tmp_path/'registry.sqlite3', transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        yield client, calls

def submit(client, request_id='request-1234567890', content=b'video'):
    return client.post('/analyze', headers=AUTH, data={'request_id':request_id}, files={'file':('clip.mp4',io.BytesIO(content),'video/mp4')})

def test_auth_origin_oversize_prevent_engine_dispatch(bridge):
    client, calls=bridge
    assert client.post('/analyze').status_code == 400
    assert client.post('/analyze',headers={**AUTH,'Origin':'https://evil.test'}).status_code == 403
    assert client.post('/analyze',headers={**AUTH,'Content-Length':str(60*1024*1024)}).status_code == 413
    assert calls == []

def test_preflight_allowed_origin_only(bridge):
    client,_=bridge
    r=client.options('/analyze',headers={'Origin':AUTH['Origin'],'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'x-demo-session,content-type'})
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


def test_anonymous_sessions_own_only_their_jobs(bridge):
    client,calls=bridge
    assert submit(client).status_code==200
    other={**AUTH,'X-Demo-Session':'b'*32}
    before=len(calls)
    for route in ['/jobs/job_abc_def','/reports/screen_abc_def','/jobs/job_abc_def/events']:
        assert client.get(route,headers=other).status_code==404
    assert client.post('/jobs/job_abc_def/cancel',headers=other).status_code==404
    assert len(calls)==before
    assert client.post('/analyze',headers=other,data={'request_id':'request-1234567890'},files={'file':('clip.mp4',b'video','video/mp4')}).status_code==409
    assert len(calls)==before


@pytest.mark.asyncio
async def test_concurrent_upload_rejected_before_body_consumption():
    import asyncio
    entered, release = asyncio.Event(), asyncio.Event()
    reads=[]
    async def app(scope, receive, send):
        entered.set()
        await release.wait()
        await receive()
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'ok'})
    guard=gateway.Guard(app,origins={AUTH['Origin']})
    scope={'type':'http','path':'/analyze','method':'POST','headers':[(b'x-demo-session',SESSION.encode()),(b'origin',AUTH['Origin'].encode())]}
    async def receive():
        reads.append(1)
        return {'type':'http.request','body':b'x','more_body':False}
    first_messages=[]
    async def first_send(x):first_messages.append(x)
    pending=asyncio.create_task(guard(dict(scope),receive,first_send))
    await entered.wait()
    second_messages=[]
    async def second_send(x):second_messages.append(x)
    await guard(dict(scope),receive,second_send)
    assert second_messages[0]['status']==409
    assert reads==[]
    release.set()
    await pending
    assert first_messages[0]['status']==200 and len(reads)==1
    third_messages=[]
    async def third_send(x):third_messages.append(x)
    await guard(dict(scope),receive,third_send)
    assert third_messages[0]['status']==200


def test_full_health_blocks_upload_when_only_quick_budget_fits(tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.path == '/api/health'
        return httpx.Response(200, json={
            'screening': {'available': True, 'model': 'Qwen', 'reason': None},
            'full_screening': {'available': False, 'model': 'Qwen', 'reason': 'Screening budget has insufficient headroom for 8 requests.'},
            'weave': {'connected': True},
        })

    app = gateway.create_app(engine_token='offline', database=tmp_path/'registry.sqlite3', transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        health = client.get('/health').json()
        assert health['available'] is False and health['max_model_calls'] == 8
        assert submit(client).status_code == 503
        assert all(method == 'GET' for method, _ in calls)


def test_known_prequeue_budget_rejection_does_not_orphan_public_request(tmp_path):
    attempts = []

    def handler(request):
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'screening': {'available': True, 'model': 'Qwen'}, 'weave': {'connected': True}})
        if request.url.path == '/api/uploads':
            return httpx.Response(200, json={'video_id': 'upl-test', 'duration_ms': 31637})
        if request.url.path == '/api/screenings':
            attempts.append(request.content)
            if len(attempts) == 1:
                return httpx.Response(503, json={'detail': 'Screening budget has insufficient headroom for 8 requests.'})
            return httpx.Response(200, json={'job_id': 'job_abc_def', 'screen_id': 'screen_abc_def'})
        raise AssertionError('Unexpected engine request')

    app = gateway.create_app(engine_token='offline', database=tmp_path/'registry.sqlite3', transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        assert submit(client).status_code == 503
        recovered = submit(client, request_id='request-new-1234567')
        assert recovered.status_code == 200
        assert recovered.json()['job_id'] == 'job_abc_def'
        assert b'"coverage":"full"' in attempts[-1]


def test_ambiguous_screening_response_preserves_same_request_recovery(tmp_path):
    attempts = []
    uploads = []

    def handler(request):
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'screening': {'available': True, 'model': 'Qwen'}, 'weave': {'connected': True}})
        if request.url.path == '/api/uploads':
            uploads.append(1)
            return httpx.Response(200, json={'video_id': 'upl-test', 'duration_ms': 31637})
        if request.url.path == '/api/screenings':
            attempts.append(request.content)
            if len(attempts) == 1:
                raise httpx.ReadTimeout('Response lost after possible queue creation', request=request)
            return httpx.Response(200, json={'job_id': 'job_abc_def', 'screen_id': 'screen_abc_def'})
        raise AssertionError('Unexpected engine request')

    app = gateway.create_app(engine_token='offline', database=tmp_path/'registry.sqlite3', transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        assert submit(client).status_code == 503
        assert submit(client, request_id='request-new-1234567').status_code == 409
        recovered = submit(client)
        assert recovered.status_code == 200
        assert len(uploads) == 1 and attempts[0] == attempts[1]


def test_scorecard_whitelist_keeps_zero_and_unknown_separate_and_redacts_nested_text():
    scorecard = {'version':'creative-potential-v1','status':'partial','evidence_level':'model_rubric','predicts_audience_outcomes':False,
        'secret':'private','method':'0..4 scaled to 100','limitations':['Do not read /Users/private/secret.txt'],
        'metrics':{'creative':{'score':0,'coverage':1,'reason':'Observed','private_key':'hidden'},
                   'retention':{'score':None,'coverage':0,'reason':'Missing'},'unexpected':{'private_key':'hidden'}},
        'improvements':[{'start_ms':2000,'end_ms':4000,'aspect':'pacing','action':'Inspect /Users/private/clip.mp4','reason':'Repetition.','observation_indices':[0],'raw_output':'hidden'}]}
    clean = gateway.public_scorecard(scorecard)
    assert clean['metrics']['creative']['score'] == 0
    assert clean['metrics']['retention']['score'] is None
    assert clean['predicts_audience_outcomes'] is False
    assert set(clean['metrics']) == {'creative','retention'}
    assert '/Users/' not in str(clean) and 'private_key' not in str(clean) and 'raw_output' not in str(clean)
    assert gateway.public_scorecard(None) is None


def test_upload_response_recovery_is_owned_and_read_only(bridge):
    client, calls = bridge
    assert submit(client).status_code == 200
    before = list(calls)
    result = client.get('/requests/request-1234567890', headers=AUTH)
    assert result.json() == {'state': 'submitted', 'job_id': 'job_abc_def', 'screen_id': 'screen_abc_def'}
    other = {**AUTH, 'X-Demo-Session': 'b' * 32}
    assert client.get('/requests/request-1234567890', headers=other).json() == {'state': 'unknown'}
    assert client.get('/requests/request-unknown-12345', headers=AUTH).json() == {'state': 'unknown'}
    assert client.get('/requests/bad', headers=AUTH).status_code == 422
    assert result.headers['cache-control'] == 'no-store'
    assert calls == before


def test_pending_upload_recovery_does_not_retry_engine(tmp_path):
    import sqlite3
    import hashlib
    database = tmp_path / 'registry.sqlite3'
    calls = []
    def handler(request):
        calls.append(request.method)
        raise AssertionError('Recovery must not dispatch an engine operation')
    app = gateway.create_app(engine_token='offline', database=database, transport=httpx.MockTransport(handler))
    with sqlite3.connect(database) as db:
        db.execute('INSERT INTO requests(id,sha,owner) VALUES(?,?,?)',
                   ('request-pending-12345', 'fixture', hashlib.sha256(SESSION.encode()).hexdigest()))
    with TestClient(app) as client:
        assert client.get('/requests/request-pending-12345', headers=AUTH).json() == {'state': 'pending'}
    assert not calls
