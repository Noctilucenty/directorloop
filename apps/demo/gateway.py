"""Presentation-only upload/screening bridge. The existing engine owns its budget.

Run behind HTTPS. Anonymous visitors get an automatic per-page capability. This process never
receives provider keys, changes the ledger, exposes the corpus, or runs edits.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

MAX_BYTES = 50 * 1024 * 1024
MAX_DURATION_MS = 180000
UPLOAD_IDLE_SECONDS = 30
UPLOAD_TOTAL_SECONDS = 120
PROBE_TIMEOUT_SECONDS = 10
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELED'}
SAFE_ID = re.compile(r'^[a-zA-Z0-9_-]{16,80}$')
JOB_ID = re.compile(r'^job_[a-f0-9]+_[a-f0-9]+$')
SCREEN_ID = re.compile(r'^screen_[a-f0-9]+_[a-f0-9]+$')


def _probe_video_file(source, suffix):
    executable = shutil.which('ffprobe')
    if not executable:
        raise HTTPException(503, 'Video validation is unavailable. Please try later.')
    # Only a temporary bounded upload exists until admission succeeds. Probe
    # self-contained containers, with no network protocols or shell commands.
    with tempfile.NamedTemporaryFile(prefix='directorloop-upload-', suffix=suffix) as temporary:
        shutil.copyfileobj(source, temporary, length=1024 * 1024)
        temporary.flush()
        try:
            result = subprocess.run([
                executable, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-format_whitelist', 'mov,matroska,webm', '-select_streams', 'v:0',
                '-show_entries', 'format=duration:stream=codec_type,width,height,duration',
                '-of', 'json', temporary.name,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(422, 'This video could not be validated in time. Try a standard MP4 export.') from exc
        except OSError as exc:
            raise HTTPException(503, 'Video validation is unavailable. Please try later.') from exc
    try:
        data = json.loads(result.stdout)
        video = data['streams'][0]
        if (result.returncode != 0 or video.get('codec_type') != 'video'
                or int(video.get('width', 0)) <= 0 or int(video.get('height', 0)) <= 0):
            raise ValueError('No video stream')
        duration = float(data.get('format', {}).get('duration') or video.get('duration')) * 1000
        if not math.isfinite(duration) or not 1 <= duration <= MAX_DURATION_MS:
            raise HTTPException(422, 'Use a video between 1 millisecond and 3 minutes.')
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise HTTPException(422, 'This file is not a readable MP4, MOV, M4V or WebM video.') from exc
    return round(duration)


async def preflight_video(file, suffix):
    await file.seek(0)
    try:
        return await asyncio.to_thread(_probe_video_file, file.file, suffix)
    finally:
        await file.seek(0)


def clean(value):
    if isinstance(value, str):
        value = re.sub(r'(?:/Users/|/home/|/tmp/)[^\s\"\']+', '[private path]', value)
        value = re.sub(r'(?:wandb_v1_|sk-proj-|apikey_)[A-Za-z0-9_\-]+', '[redacted]', value)
    return value


def selected(value, keys):
    return {k: clean(value[k]) for k in keys if k in value}


def public_scorecard(value):
    if not isinstance(value, dict):
        return None
    result = selected(value, ('version', 'status', 'evidence_level', 'predicts_audience_outcomes', 'method'))
    result['limitations'] = [clean(x) for x in value.get('limitations', []) if isinstance(x, str)]
    metrics = value.get('metrics') or {}
    result['metrics'] = {name: selected(metrics[name], ('score', 'coverage', 'rated_ms', 'total_ms', 'rated_sections', 'total_sections', 'provisional', 'reason', 'explanation')) for name in ('creative', 'retention', 'virality') if isinstance(metrics.get(name), dict)}
    for name, metric in result['metrics'].items():
        drivers = metrics[name].get('drivers') or []
        metric['drivers'] = [selected(item, ('window_index', 'start_ms', 'end_ms', 'reason', 'observation_indices'))
                             for item in drivers[:3] if isinstance(item, dict)] if isinstance(drivers, list) else []
    result['improvements'] = [selected(item, ('window_index', 'start_ms', 'end_ms', 'aspect', 'action', 'reason', 'observation_indices')) for item in value.get('improvements', [])[:3] if isinstance(item, dict)]
    strengths = value.get('strengths') or []
    result['strengths'] = [selected(item, ('window_index', 'start_ms', 'end_ms', 'aspect', 'reason', 'observation_indices'))
                           for item in strengths[:3] if isinstance(item, dict)] if isinstance(strengths, list) else []
    timeline = value.get('timeline')
    if isinstance(timeline, list):
        result['timeline'] = [selected(item, ('window_index', 'start_ms', 'end_ms', 'score', 'reason', 'observation_indices'))
                              for item in timeline[:64] if isinstance(item, dict)]
    return result


def public_review_repair(value):
    if not isinstance(value, dict):
        return None
    attempted, completed = value.get('attempted_sections'), value.get('completed_sections')
    status = value.get('status')
    if (type(attempted) is not int or type(completed) is not int
            or attempted < 0 or completed < 0 or completed > attempted
            or not isinstance(status, str) or not re.fullmatch(r'[a-z_]{1,40}', status)):
        return None
    result = {'attempted_sections': attempted, 'completed_sections': completed, 'status': status}
    version = value.get('version')
    if isinstance(version, str) and re.fullmatch(r'[a-zA-Z0-9._-]{1,80}', version):
        result['version'] = version
    return result


class Guard:
    def __init__(self, app, origins):
        self.app, self.origins = app, origins
        self.upload_busy = False

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        origin = headers.get(b'origin', b'').decode()
        if origin and origin not in self.origins:
            return await JSONResponse({'detail': 'This origin is not allowed.'}, status_code=403)(scope, receive, send)
        if scope['path'] != '/health' and scope['method'] != 'OPTIONS':
            session = headers.get(b'x-demo-session', b'').decode()
            if not re.fullmatch(r'[a-f0-9]{32}', session):
                return await JSONResponse({'detail': 'A browser session identifier is required.'}, status_code=400)(scope, receive, send)
            scope['demo_owner'] = hashlib.sha256(session.encode()).hexdigest()
        limit = MAX_BYTES + 65536 if scope['path'] == '/analyze' else 4096
        try:
            size = int(headers.get(b'content-length', b'0'))
        except ValueError:
            size = -1
        if size < 0 or size > limit:
            return await JSONResponse({'detail': 'Video upload limit is 50 MB.'}, status_code=413)(scope, receive, send)
        total = 0
        uploading = scope['method'] == 'POST' and scope['path'] == '/analyze'
        body_complete = False
        body_deadline = asyncio.get_running_loop().time() + UPLOAD_TOTAL_SECONDS

        async def bounded():
            nonlocal total, body_complete
            if uploading and not body_complete:
                remaining = body_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise HTTPException(408, 'Video upload timed out. Please try uploading again.')
                try:
                    message = await asyncio.wait_for(receive(), min(UPLOAD_IDLE_SECONDS, remaining))
                except asyncio.TimeoutError as exc:
                    raise HTTPException(408, 'Video upload timed out. Please try uploading again.') from exc
            else:
                message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > limit:
                    raise HTTPException(413, 'Video upload limit is 50 MB.')
                body_complete = not message.get('more_body', False)
            return message

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []), (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]}
            await send(message)
        if uploading and self.upload_busy:
            return await JSONResponse({'detail': 'Another upload is being received. Try again when it finishes.'}, status_code=409)(scope, receive, send)
        if uploading:
            self.upload_busy = True
        try:
            return await self.app(scope, bounded, private_send)
        finally:
            if uploading:
                self.upload_busy = False


def create_app(*, engine_token: str, database: Path, legacy_session: str | None = None, origin='https://directorloop-demo.onrender.com', transport=None):
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, sha TEXT NOT NULL, video_id TEXT, job_id TEXT UNIQUE, screen_id TEXT UNIQUE, owner TEXT)')
        if 'owner' not in {r[1] for r in db.execute('PRAGMA table_info(requests)')}:
            db.execute('ALTER TABLE requests ADD COLUMN owner TEXT')
        if legacy_session:
            db.execute('UPDATE requests SET owner=? WHERE owner IS NULL', (hashlib.sha256(legacy_session.encode()).hexdigest(),))
    os.chmod(database, 0o600)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, origins={origin, 'http://127.0.0.1:5188'})
    app.add_middleware(CORSMiddleware, allow_origins=[origin, 'http://127.0.0.1:5188'], allow_methods=['GET', 'POST'], allow_headers=['X-Demo-Session', 'Content-Type'], max_age=600)
    client = httpx.AsyncClient(base_url='http://127.0.0.1:8787', headers={'Authorization': 'Bearer ' + engine_token} if engine_token else {}, timeout=90, transport=transport)
    lock = asyncio.Lock()

    def rows(query, values=()):
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(query, values).fetchall()]

    def write(query, values=()):
        with sqlite3.connect(database) as db:
            db.execute(query, values)

    async def api(method, path, **kwargs):
        try:
            response = await client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(503, 'The demo engine is reconnecting. No automatic retry was sent.') from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get('detail', 'The engine could not complete this step.')
            except ValueError:
                detail = 'The engine could not complete this step.'
            raise HTTPException(response.status_code, clean(str(detail)))
        return response

    def own(job_id, request):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, 'Unknown demo analysis.')
        found = rows('SELECT * FROM requests WHERE job_id=? AND owner=?', (job_id, request.scope['demo_owner']))
        if not found:
            raise HTTPException(404, 'Unknown demo analysis.')
        return found[0]

    @app.get('/health')
    async def health():
        try:
            state = (await api('GET', '/api/health')).json()
            screen = state.get('full_screening', state['screening'])
            return {'available': screen['available'], 'reason': clean(screen.get('reason')), 'max_upload_bytes': MAX_BYTES, 'max_duration_ms': MAX_DURATION_MS, 'max_model_calls': 16, 'max_sections': 8, 'weave_connected': state['weave']['connected'], 'model': screen['model']}
        except HTTPException:
            return JSONResponse({'available': False, 'reason': 'The analysis engine is offline.'}, status_code=503)

    @app.post('/analyze')
    async def analyze(request: Request, request_id: str = Form(...), file: UploadFile = File(...)):
        if not SAFE_ID.fullmatch(request_id):
            raise HTTPException(422, 'Invalid request identifier.')
        suffix = Path(file.filename or '').suffix.lower()
        if suffix not in {'.mp4', '.mov', '.m4v', '.webm'}:
            raise HTTPException(415, 'Choose an MP4, MOV, M4V or WebM video.')
        digest, size = hashlib.sha256(), 0
        while chunk := await file.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise HTTPException(413, 'Video upload limit is 50 MB.')
        await file.seek(0)
        if not size:
            raise HTTPException(422, 'This video file is empty.')
        sha = digest.hexdigest()
        async with lock:
            existing = rows('SELECT * FROM requests WHERE id=?', (request_id,))
            if existing:
                record = existing[0]
                if record['owner'] != request.scope['demo_owner']:
                    raise HTTPException(409, 'This request identifier is unavailable.')
                if record['sha'] != sha:
                    raise HTTPException(409, 'This request identifier belongs to a different file.')
                if record['job_id']:
                    return selected(record, ('job_id', 'screen_id'))
                if not record['video_id']:
                    # Revalidate pending uploads created before this protection,
                    # while keeping their ambiguous submission history intact.
                    await preflight_video(file, suffix)
            else:
                for record in rows('SELECT * FROM requests'):
                    if record['job_id']:
                        job = (await api('GET', '/api/jobs/' + record['job_id'])).json()
                        if job['state'] not in TERMINAL:
                            raise HTTPException(409, 'An analysis is already running. Wait for it to finish.')
                    else:
                        raise HTTPException(409, 'A previous submission needs review before another can start.')
                state = await health()
                if not isinstance(state, dict) or not state.get('available'):
                    raise HTTPException(503, state.get('reason', 'Screening is unavailable.') if isinstance(state, dict) else 'The analysis engine is unavailable.')
                await preflight_video(file, suffix)
                write('INSERT INTO requests(id,sha,owner) VALUES(?,?,?)', (request_id, sha, request.scope['demo_owner']))
                record = {'id': request_id, 'sha': sha, 'video_id': None}
            if not record['video_id']:
                try:
                    uploaded = (await api('POST', '/api/uploads', files={'file': ('presenter-upload' + suffix, file.file, file.content_type or 'application/octet-stream')})).json()
                    if not 0 < uploaded['duration_ms'] <= MAX_DURATION_MS:
                        write('DELETE FROM requests WHERE id=?', (request_id,))
                        raise HTTPException(422, 'Use a video between 1 millisecond and 3 minutes.')
                    record['video_id'] = uploaded['video_id']
                    write('UPDATE requests SET video_id=? WHERE id=?', (record['video_id'], request_id))
                except HTTPException as exc:
                    if exc.status_code < 500:
                        write('DELETE FROM requests WHERE id=?', (request_id,))
                    raise
            try:
                result = (await api('POST', '/api/screenings', json={'video_id': record['video_id'], 'coverage': 'full', 'idempotency_key': 'public-demo-' + request_id})).json()
            except HTTPException as exc:
                # This explicit admission rejection happens before queue creation. Keep
                # ambiguous connection failures for safe recovery with the same key.
                if exc.status_code == 503 and str(exc.detail).startswith('Screening budget has insufficient headroom'):
                    write('DELETE FROM requests WHERE id=? AND job_id IS NULL', (request_id,))
                raise
            write('UPDATE requests SET job_id=?,screen_id=? WHERE id=?', (result['job_id'], result['screen_id'], request_id))
            return selected(result, ('job_id', 'screen_id'))

    @app.get('/jobs/{job_id}')
    async def job(job_id: str, request: Request):
        own(job_id, request)
        data = (await api('GET', '/api/jobs/' + job_id)).json()
        return selected(data, ('id', 'state', 'stage', 'created_at', 'started_at', 'ended_at', 'elapsed_ms', 'error', 'screen_id', 'cancel_requested'))

    @app.get('/requests/{request_id}')
    async def recover_request(request_id: str, request: Request):
        # Read-only recovery after a refresh loses the upload response. Never
        # create a job, reveal another session's request, or retry inference.
        if not SAFE_ID.fullmatch(request_id):
            raise HTTPException(422, 'Invalid request identifier.')
        found = rows('SELECT job_id,screen_id FROM requests WHERE id=? AND owner=?',
                     (request_id, request.scope['demo_owner']))
        if not found:
            return {'state': 'unknown'}
        record = found[0]
        if not record['job_id'] or not record['screen_id']:
            return {'state': 'pending'}
        return {'state': 'submitted', **selected(record, ('job_id', 'screen_id'))}

    @app.get('/jobs/{job_id}/events')
    async def events(job_id: str, request: Request):
        own(job_id, request)
        data = (await api('GET', '/api/jobs/' + job_id + '/events.json')).json()
        return [selected(event, ('seq', 'ts', 'stage', 'message')) for event in data]

    @app.post('/jobs/{job_id}/cancel')
    async def cancel(job_id: str, request: Request):
        own(job_id, request)
        data = (await api('POST', '/api/jobs/' + job_id + '/cancel')).json()
        return selected(data, ('id', 'state', 'cancel_requested'))

    @app.get('/reports/{screen_id}')
    async def report(screen_id: str, request: Request):
        if not SCREEN_ID.fullmatch(screen_id) or not rows('SELECT id FROM requests WHERE screen_id=? AND owner=?', (screen_id, request.scope['demo_owner'])):
            raise HTTPException(404, 'Unknown demo report.')
        data = (await api('GET', '/api/screenings/' + screen_id)).json()
        result = selected(data, ('id', 'status', 'recorded_status', 'view_validation', 'duration_ms', 'created_at', 'ended_at', 'error', 'review_required', 'semantic_grounding_verified', 'automatic_edit_allowed', 'model_calls', 'input_tokens', 'output_tokens', 'weave_url', 'protocol_fingerprint'))
        result['scorecard'] = public_scorecard(data.get('scorecard'))
        result['review_repair'] = public_review_repair(data.get('review_repair'))
        result['windows'] = []
        for window in data.get('windows', []):
            item = selected(window, ('start_ms', 'end_ms', 'status', 'recorded_status', 'display_reason', 'attention_context', 'semantic_grounding_verified', 'weave_url'))
            assessment = window.get('attention_assessment')
            if isinstance(assessment, dict):
                item['attention_assessment'] = selected(assessment, ('version', 'status', 'risk', 'reason', 'semantic_grounding_verified'))
                item['attention_assessment']['excluded_checks'] = [clean(x) for x in assessment.get('excluded_checks', []) if isinstance(x, str)]
            item['delivery_signals'] = window.get('delivery_signals')
            item['validation_issues'] = [clean(x) for x in window.get('validation_issues', [])]
            judgment = window.get('judgment')
            if judgment:
                item['judgment'] = selected(judgment, ('understanding', 'attention_risk', 'suggestion', 'moment_kind'))
                item['judgment']['review_checks'] = [selected(c, ('aspect', 'status', 'reason', 'observation_indices')) for c in judgment.get('review_checks', [])]
                item['judgment']['uncertainties'] = [clean(x) for x in judgment.get('uncertainties', [])]
                item['judgment']['observations'] = [selected(x, ('text', 'kind', 'frame_timestamps_ms', 'asr_quote')) for x in judgment.get('observations', [])]
            else:
                item['judgment'] = None
            result['windows'].append(item)
        return result

    return app


def from_environment():
    from dotenv import dotenv_values
    values = dotenv_values(os.environ['DIRECTORLOOP_GATEWAY_ENV'])
    return create_app(engine_token=values.get('ENGINE_AUTH_TOKEN') or '', database=Path(values['GATEWAY_DATABASE']), legacy_session=values.get('LEGACY_DEMO_SESSION'))
