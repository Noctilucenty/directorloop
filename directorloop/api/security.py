"""Single-operator API authentication and bounded request bodies.

The browser exchanges an existing bearer token for a short-lived HttpOnly cookie,
so same-origin video playback can authenticate without secrets in media URLs.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings

COOKIE_NAME = "directorloop_session"
SESSION_SECONDS = 12 * 60 * 60
JSON_BODY_BYTES = 1024 * 1024
MULTIPART_OVERHEAD_BYTES = 64 * 1024


def loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_exposure(settings: Settings) -> None:
    if settings.dl_mode == "production" or not loopback_host(settings.dl_bind_host):
        if len(settings.dl_local_auth_token) < 32:
            raise ValueError("Production or non-loopback binding requires DL_LOCAL_AUTH_TOKEN with at least 32 characters")
        if settings.dl_ingest_allow_private_hosts:
            raise ValueError("Private-host ingestion is a local test option and cannot be enabled for public binding")
    if settings.dl_max_upload_mb <= 0:
        raise ValueError("DL_MAX_UPLOAD_MB must be positive")


class SessionAuth:
    def __init__(self, settings: Settings):
        self.token = settings.dl_local_auth_token
        self.secure = settings.dl_mode == "production"
        self.signer = URLSafeTimedSerializer(self.token, salt="directorloop-session-v1",
                                             signer_kwargs={"digest_method": hashlib.sha256}) if self.token else None

    def bearer_valid(self, request: Request) -> bool:
        header = request.headers.get("authorization", "")
        return bool(self.token) and hmac.compare_digest(header.encode(), f"Bearer {self.token}".encode())

    def cookie_valid(self, request: Request) -> bool:
        if self.signer is None:
            return False
        try:
            return self.signer.loads(request.cookies.get(COOKIE_NAME, ""), max_age=SESSION_SECONDS) == {"scope": "directorloop"}
        except (BadSignature, SignatureExpired):
            return False

    def require(self, request: Request) -> None:
        if not self.token or self.bearer_valid(request):
            return
        if not self.cookie_valid(request):
            raise HTTPException(401, "authentication required")
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = urlsplit(request.headers.get("origin", ""))
            if (origin.scheme, origin.netloc) != (request.url.scheme, request.url.netloc):
                raise HTTPException(403, "same-origin request required")


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, auth: SessionAuth):
        self.app, self.settings, self.auth = app, settings, auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        protected = path == "/api" or path == "/media" or path.startswith(("/api/", "/media/"))
        if scope["type"] != "http" or not protected:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            if not (path == "/api/session" and scope["method"] == "POST"):
                self.auth.require(request)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
            return
        limit = (self.settings.dl_max_upload_mb * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES
                 if path == "/api/uploads" else JSON_BODY_BYTES)
        detail = (f"upload request exceeds {self.settings.dl_max_upload_mb} MB plus multipart overhead"
                  if path == "/api/uploads" else "request body is too large")
        try:
            declared = int(request.headers.get("content-length", "0"))
        except ValueError:
            await JSONResponse({"detail": "invalid content length"}, status_code=400)(scope, receive, send)
            return
        if declared < 0 or declared > limit:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, detail)
            return message

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start" and self.auth.token:
                headers = list(message.get("headers", []))
                headers.extend([(b"cache-control", b"private, no-store"), (b"vary", b"Cookie, Authorization")])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, bounded_receive, private_send)


def install_security(app: FastAPI, settings: Settings) -> SessionAuth:
    auth = SessionAuth(settings)
    app.add_middleware(SecurityMiddleware, settings=settings, auth=auth)

    @app.post("/api/session", include_in_schema=False)
    def login(request: Request) -> JSONResponse:
        if not auth.bearer_valid(request) or auth.signer is None:
            raise HTTPException(401, "missing or invalid bearer token")
        response = JSONResponse({"authenticated": True, "expires_in_seconds": SESSION_SECONDS})
        response.set_cookie(COOKIE_NAME, auth.signer.dumps({"scope": "directorloop"}), max_age=SESSION_SECONDS,
                            httponly=True, secure=auth.secure, samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.delete("/api/session", include_in_schema=False)
    def logout() -> JSONResponse:
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE_NAME, httponly=True, secure=auth.secure, samesite="strict", path="/")
        return response

    return auth
