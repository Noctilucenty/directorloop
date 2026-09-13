"""Presentation-only upload/screening bridge. The existing engine owns its budget.

Run behind HTTPS with a separate, private demo access code. This process never
receives provider keys, changes the ledger, exposes the corpus, or runs edits.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

MAX_BYTES = 50 * 1024 * 1024
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELED'}
SAFE_ID = re.compile(r'^[a-zA-Z0-9_-]{16,80}$')
JOB_ID = re.compile(r'^job_[a-f0-9]+_[a-f0-9]+$')
SCREEN_ID = re.compile(r'^screen_[a-f0-9]+_[a-f0-9]+$')


def clean(value):
    if isinstance(value, str):
        value = re.sub(r'(?:/Users/|/home/|/tmp/)[^\s\"\']+', '[private path]', value)
        value = re.sub(r'(?:wandb_v1_|sk-proj-|apikey_)[A-Za-z0-9_\-]+', '[redacted]', value)
    return value


def selected(value, keys):
    return {k: clean(value[k]) for k in keys if k in value}


class Guard:
    def __init__(self, app, code, origins):
        self.app, self.code, self.origins = app, code, origins

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = dict(scope.get('headers', []))
        origin = headers.get(b'origin', b'').decode()
        if origin and origin not in self.origins:
            return await JSONResponse({'detail': 'This origin is not allowed.'}, status_code=403)(scope, receive, send)
        if scope['path'] != '/health' and scope['method'] != 'OPTIONS':
            if not hmac.compare_digest(headers.get(b'authorization', b''), ('Bearer ' + self.code).encode()):
                return await JSONResponse({'detail': 'Enter the presenter access code to run an analysis.'}, status_code=401)(scope, receive, send)
        limit = MAX_BYTES + 65536 if scope['path'] == '/analyze' else 4096
        try:
            size = int(headers.get(b'content-length', b'0'))
        except ValueError:
            size = -1
        if size < 0 or size > limit:
            return await JSONResponse({'detail': 'Video upload limit is 50 MB.'}, status_code=413)(scope, receive, send)
        total = 0

        async def bounded():
            nonlocal total
            message = await receive()
            if message['type'] == 'http.request':
                total += len(message.get('body', b''))
                if total > limit:
                    raise HTTPException(413, 'Video upload limit is 50 MB.')
            return message

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []), (b'cache-control', b'no-store'), (b'x-content-type-options', b'nosniff')]}
            await send(message)
        return await self.app(scope, bounded, private_send)


def create_app(*, code: str, engine_token: str, database: Path, origin='https://directorloop-demo.onrender.com', transport=None):
    if len(code) < 32:
        raise ValueError('A private demo code of at least 32 characters is required')
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, sha TEXT NOT NULL, video_id TEXT, job_id TEXT UNIQUE, screen_id TEXT UNIQUE)')
    os.chmod(database, 0o600)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(Guard, code=code, origins={origin, 'http://127.0.0.1:5188'})
    app.add_middleware(CORSMiddleware, allow_origins=[origin, 'http://127.0.0.1:5188'], allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type'], max_age=600)
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

    def own(job_id):
        if not JOB_ID.fullmatch(job_id):
            raise HTTPException(404, 'Unknown demo analysis.')
        found = rows('SELECT * FROM requests WHERE job_id=?', (job_id,))
        if not found:
            raise HTTPException(404, 'Unknown demo analysis.')
        return found[0]

    @app.get('/health')
    async def health():
        try:
            state = (await api('GET', '/api/health')).json()
            screen = state['screening']
            return {'available': screen['available'], 'reason': clean(screen.get('reason')), 'max_upload_bytes': MAX_BYTES, 'max_duration_ms': 180000, 'max_model_calls': 3, 'weave_connected': state['weave']['connected'], 'model': screen['model']}
        except HTTPException:
            return JSONResponse({'available': False, 'reason': 'The presenter’s analysis engine is offline.'}, status_code=503)

    @app.post('/analyze')
    async def analyze(request_id: str = Form(...), file: UploadFile = File(...)):
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
                if record['sha'] != sha:
                    raise HTTPException(409, 'This request identifier belongs to a different file.')
                if record['job_id']:
                    return selected(record, ('job_id', 'screen_id'))
            else:
                for record in rows('SELECT * FROM requests'):
                    if record['job_id']:
                        job = (await api('GET', '/api/jobs/' + record['job_id'])).json()
                        if job['state'] not in TERMINAL:
                            raise HTTPException(409, 'An analysis is already running. Wait for it to finish.')
                    else:
                        raise HTTPException(409, 'A previous submission needs presenter review before another can start.')
                state = await health()
                if not isinstance(state, dict) or not state.get('available'):
                    raise HTTPException(503, state.get('reason', 'Screening is unavailable.') if isinstance(state, dict) else 'The analysis engine is unavailable.')
                write('INSERT INTO requests(id,sha) VALUES(?,?)', (request_id, sha))
                record = {'id': request_id, 'sha': sha, 'video_id': None}
            if not record['video_id']:
                try:
                    uploaded = (await api('POST', '/api/uploads', files={'file': ('presenter-upload' + suffix, file.file, file.content_type or 'application/octet-stream')})).json()
                    if not 0 < uploaded['duration_ms'] <= 180000:
                        write('DELETE FROM requests WHERE id=?', (request_id,))
                        raise HTTPException(422, 'Use a video between 1 millisecond and 3 minutes.')
                    record['video_id'] = uploaded['video_id']
                    write('UPDATE requests SET video_id=? WHERE id=?', (record['video_id'], request_id))
                except HTTPException as exc:
                    if exc.status_code < 500:
                        write('DELETE FROM requests WHERE id=?', (request_id,))
                    raise
            result = (await api('POST', '/api/screenings', json={'video_id': record['video_id'], 'idempotency_key': 'public-demo-' + request_id})).json()
            write('UPDATE requests SET job_id=?,screen_id=? WHERE id=?', (result['job_id'], result['screen_id'], request_id))
            return selected(result, ('job_id', 'screen_id'))

    @app.get('/jobs/{job_id}')
    async def job(job_id: str):
        own(job_id)
        data = (await api('GET', '/api/jobs/' + job_id)).json()
        return selected(data, ('id', 'state', 'stage', 'created_at', 'started_at', 'ended_at', 'elapsed_ms', 'error', 'screen_id', 'cancel_requested'))

    @app.get('/jobs/{job_id}/events')
    async def events(job_id: str):
        own(job_id)
        data = (await api('GET', '/api/jobs/' + job_id + '/events.json')).json()
        return [selected(event, ('seq', 'ts', 'stage', 'message')) for event in data]

    @app.post('/jobs/{job_id}/cancel')
    async def cancel(job_id: str):
        own(job_id)
        data = (await api('POST', '/api/jobs/' + job_id + '/cancel')).json()
        return selected(data, ('id', 'state', 'cancel_requested'))

    @app.get('/reports/{screen_id}')
    async def report(screen_id: str):
        if not SCREEN_ID.fullmatch(screen_id) or not rows('SELECT id FROM requests WHERE screen_id=?', (screen_id,)):
            raise HTTPException(404, 'Unknown demo report.')
        data = (await api('GET', '/api/screenings/' + screen_id)).json()
        result = selected(data, ('id', 'status', 'duration_ms', 'created_at', 'ended_at', 'error', 'review_required', 'semantic_grounding_verified', 'automatic_edit_allowed', 'model_calls', 'input_tokens', 'output_tokens', 'weave_url', 'protocol_fingerprint'))
        result['windows'] = []
        for window in data.get('windows', []):
            item = selected(window, ('start_ms', 'end_ms', 'status', 'attention_context', 'semantic_grounding_verified', 'weave_url'))
            item['validation_issues'] = [clean(x) for x in window.get('validation_issues', [])]
            judgment = window.get('judgment')
            if judgment:
                item['judgment'] = selected(judgment, ('understanding', 'attention_risk', 'suggestion', 'moment_kind'))
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
    return create_app(code=values['DEMO_ACCESS_CODE'], engine_token=values.get('ENGINE_AUTH_TOKEN') or '', database=Path(values['GATEWAY_DATABASE']))
