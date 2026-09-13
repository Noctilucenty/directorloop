import asyncio
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

spec = importlib.util.spec_from_file_location('upload_gateway', Path(__file__).parents[1] / 'gateway.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
AUTH = {'X-Demo-Session': 'a' * 32, 'Origin': 'https://directorloop-demo.onrender.com'}


def upload_scope():
    return {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'path': '/analyze', 'raw_path': b'/analyze', 'root_path': '',
            'query_string': b'', 'method': 'POST', 'scheme': 'http',
            'headers': [(name.lower().encode(), value.encode()) for name, value in AUTH.items()]}


@pytest.mark.asyncio
async def test_stalled_body_times_out_and_releases_upload_slot(monkeypatch):
    monkeypatch.setattr(gateway, 'UPLOAD_IDLE_SECONDS', 0.01)
    app = FastAPI()
    dispatched = []

    @app.post('/analyze')
    async def endpoint(request: Request):
        await request.body()
        dispatched.append(True)
        return {'ok': True}

    guard = gateway.Guard(app, origins={AUTH['Origin']})

    async def stalled():
        await asyncio.Event().wait()

    messages = []
    async def send(message):
        messages.append(message)

    await asyncio.wait_for(guard(upload_scope(), stalled, send), 1)
    assert messages[0]['status'] == 408
    assert dict(messages[0]['headers'])[b'cache-control'] == b'no-store'
    assert not guard.upload_busy and not dispatched

    async def complete():
        return {'type': 'http.request', 'body': b'video', 'more_body': False}

    messages.clear()
    await guard(upload_scope(), complete, send)
    assert messages[0]['status'] == 200 and dispatched == [True]


@pytest.mark.asyncio
async def test_trickling_body_cannot_extend_total_deadline(monkeypatch):
    monkeypatch.setattr(gateway, 'UPLOAD_IDLE_SECONDS', 1)
    monkeypatch.setattr(gateway, 'UPLOAD_TOTAL_SECONDS', 0.04)
    app = FastAPI()

    @app.post('/analyze')
    async def endpoint(request: Request):
        await request.body()
        pytest.fail('An unfinished upload must not be dispatched')

    guard = gateway.Guard(app, origins={AUTH['Origin']})
    chunks = []

    async def trickle():
        await asyncio.sleep(0.01)
        chunks.append(True)
        return {'type': 'http.request', 'body': b'x', 'more_body': True}

    messages = []
    async def send(message):
        messages.append(message)

    await asyncio.wait_for(guard(upload_scope(), trickle, send), 1)
    assert messages[0]['status'] == 408 and chunks
    assert not guard.upload_busy


@pytest.mark.asyncio
async def test_body_deadline_does_not_cancel_dispatched_work(monkeypatch):
    monkeypatch.setattr(gateway, 'UPLOAD_TOTAL_SECONDS', 0.01)
    app = FastAPI()

    @app.post('/analyze')
    async def endpoint(request: Request):
        await request.body()
        await asyncio.sleep(0.03)
        return {'ok': True}

    guard = gateway.Guard(app, origins={AUTH['Origin']})
    async def receive():
        return {'type': 'http.request', 'body': b'video', 'more_body': False}
    messages = []
    async def send(message):
        messages.append(message)
    await guard(upload_scope(), receive, send)
    assert messages[0]['status'] == 200
    assert not guard.upload_busy


@pytest.mark.asyncio
async def test_multipart_timeout_closes_partial_file_before_dispatch(tmp_path, monkeypatch):
    import starlette.formparsers
    monkeypatch.setattr(gateway, 'UPLOAD_IDLE_SECONDS', 0.02)
    temporary_files = []
    original = starlette.formparsers.SpooledTemporaryFile
    def temporary(*args, **kwargs):
        file = original(*args, **kwargs)
        temporary_files.append(file)
        return file
    monkeypatch.setattr(starlette.formparsers, 'SpooledTemporaryFile', temporary)
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError('Incomplete upload must never reach engine')
    database = tmp_path / 'registry.sqlite3'
    app = gateway.create_app(engine_token='offline', database=database,
                             transport=httpx.MockTransport(handler))
    scope = upload_scope()
    scope['headers'].append((b'content-type', b'multipart/form-data; boundary=example'))
    first = True
    async def receive():
        nonlocal first
        if first:
            first = False
            return {'type': 'http.request', 'more_body': True, 'body':
                    b'--example\r\nContent-Disposition: form-data; name="request_id"\r\n\r\nrequest-1234567890\r\n'
                    b'--example\r\nContent-Disposition: form-data; name="file"; filename="clip.mp4"\r\n'
                    b'Content-Type: video/mp4\r\n\r\n' + b'x' * (1024 * 1024 + 10)}
        await asyncio.Event().wait()
    messages = []
    async def send(message):
        messages.append(message)
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert messages[0]['status'] == 408 and not calls
    assert temporary_files and all(file.closed for file in temporary_files)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0


def gateway_client(tmp_path):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'screening': {'available': True, 'model': 'fixture'},
                                             'weave': {'connected': True}})
        if request.url.path == '/api/uploads':
            return httpx.Response(200, json={'video_id': 'upload-fixture', 'duration_ms': 1000})
        if request.url.path == '/api/screenings':
            return httpx.Response(200, json={'job_id': 'job_abc_def', 'screen_id': 'screen_abc_def'})
        raise AssertionError('Unexpected engine request')
    database = tmp_path / 'registry.sqlite3'
    app = gateway.create_app(engine_token='offline', database=database, transport=httpx.MockTransport(handler))
    return TestClient(app), calls, database


def submit(client, content, request_id='request-1234567890'):
    return client.post('/analyze', headers=AUTH, data={'request_id': request_id},
                       files={'file': ('clip.mp4', content, 'video/mp4')})


def probe_output(duration='1.0'):
    return {'streams': [{'codec_type': 'video', 'width': 16, 'height': 16}],
            'format': {'duration': duration}}


@pytest.mark.parametrize('duration', ['180.001', '0', '-1', 'NaN', 'Infinity'])
def test_invalid_duration_never_reaches_engine_storage(tmp_path, monkeypatch, duration):
    paths = []
    monkeypatch.setattr(gateway.shutil, 'which', lambda name: '/fixture/ffprobe')
    def probe(command, **kwargs):
        paths.append(Path(command[-1]))
        assert paths[-1].read_bytes() == b'container'
        return subprocess.CompletedProcess(command, 0, json.dumps(probe_output(duration)).encode())
    monkeypatch.setattr(gateway.subprocess, 'run', probe)
    client, calls, database = gateway_client(tmp_path)
    with client:
        assert submit(client, b'container').status_code == 422
    assert not any(method == 'POST' for method, _ in calls)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0
    assert paths and all(not path.exists() for path in paths)


def test_probe_is_local_bounded_and_cleans_up_on_timeout(tmp_path, monkeypatch):
    paths = []
    monkeypatch.setattr(gateway.shutil, 'which', lambda name: '/fixture/ffprobe')
    def probe(command, **kwargs):
        paths.append(Path(command[-1]))
        assert command[command.index('-protocol_whitelist') + 1] == 'file,pipe'
        assert command[command.index('-format_whitelist') + 1] == 'mov,matroska,webm'
        assert command[command.index('-select_streams') + 1] == 'v:0'
        assert kwargs['timeout'] == 10 and kwargs['stderr'] == subprocess.DEVNULL
        assert not kwargs.get('shell')
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(gateway.subprocess, 'run', probe)
    client, calls, database = gateway_client(tmp_path)
    with client:
        response = submit(client, b'container')
        assert response.status_code == 422
        assert 'validated in time' in response.json()['detail']
    assert not any(method == 'POST' for method, _ in calls)
    assert paths and all(not path.exists() for path in paths)
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0


def test_missing_probe_fails_closed_before_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.shutil, 'which', lambda name: None)
    client, calls, _ = gateway_client(tmp_path)
    with client:
        assert submit(client, b'container').status_code == 503
    assert not any(method == 'POST' for method, _ in calls)


def test_pending_legacy_upload_is_revalidated_without_erasing_history(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway.shutil, 'which', lambda name: '/fixture/ffprobe')
    monkeypatch.setattr(gateway.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(probe_output('181')).encode()))
    client, calls, database = gateway_client(tmp_path)
    with sqlite3.connect(database) as db:
        db.execute('INSERT INTO requests(id,sha,owner) VALUES(?,?,?)',
                   ('request-1234567890', hashlib.sha256(b'container').hexdigest(),
                    hashlib.sha256(AUTH['X-Demo-Session'].encode()).hexdigest()))
    with client:
        assert submit(client, b'container').status_code == 422
    assert not calls
    with sqlite3.connect(database) as db:
        assert db.execute('SELECT COUNT(*) FROM requests WHERE job_id IS NULL').fetchone()[0] == 1


@pytest.mark.parametrize('data', [{}, {'streams': []}, {'streams': [{'codec_type': 'audio'}]},
                                {**probe_output(), 'streams': [{'codec_type': 'video', 'width': 0, 'height': 16}]}])
def test_nonvideo_container_is_rejected(tmp_path, monkeypatch, data):
    monkeypatch.setattr(gateway.shutil, 'which', lambda name: '/fixture/ffprobe')
    monkeypatch.setattr(gateway.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, json.dumps(data).encode()))
    client, calls, _ = gateway_client(tmp_path)
    with client:
        assert submit(client, b'container').status_code == 422
    assert not any(method == 'POST' for method, _ in calls)


def test_real_video_preflight_accepts_short_rejects_long_and_spoofed(tmp_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg or not shutil.which('ffprobe'):
        pytest.skip('Real media integration needs ffmpeg and ffprobe')
    short, long = tmp_path / 'short.mp4', tmp_path / 'long.mp4'
    for path, duration in [(short, 1), (long, 181)]:
        subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=16x16:r=1',
                        '-t', str(duration), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)],
                       check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client, calls, database = gateway_client(tmp_path)
    with client:
        assert submit(client, long.read_bytes()).status_code == 422
        assert submit(client, b'#EXTM3U\nhttp://example.invalid/video.mp4').status_code == 422
        assert not any(method == 'POST' for method, _ in calls)
        with sqlite3.connect(database) as db:
            assert db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] == 0
        assert submit(client, short.read_bytes()).status_code == 200
    assert calls.count(('POST', '/api/uploads')) == 1
    assert calls.count(('POST', '/api/screenings')) == 1


@pytest.mark.asyncio
async def test_preflight_rewinds_upload_for_forwarding(monkeypatch):
    from starlette.datastructures import UploadFile
    content = io.BytesIO(b'container')
    content.seek(3)
    def probe(source, suffix):
        assert source.read() == b'container' and suffix == '.mp4'
        return 1000
    monkeypatch.setattr(gateway, '_probe_video_file', probe)
    upload = UploadFile(file=content, filename='clip.mp4')
    assert await gateway.preflight_video(upload, '.mp4') == 1000
    assert await upload.read() == b'container'
