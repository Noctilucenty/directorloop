"""Acquire a video from a link so it can be judged.

- Direct media links (http/https pointing at a video file): streamed with a size cap, every redirect re-validated.
- Platform links (Instagram, TikTok, YouTube, X): retrieved with yt-dlp when it is installed; otherwise refused plainly.
- Every host is resolved first and private, loopback, link-local, multicast and reserved addresses are refused, so a link
  cannot make the server fetch from inside the network.
A video that arrives by link is marked analysis-only for editing until its owner confirms the right to create a derivative
(the API enforces this); uploading the original file is the other way to edit.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 300 * 1024 * 1024
TIMEOUT_S = 180
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm"}
PLATFORMS = {
    "instagram.com": "instagram", "www.instagram.com": "instagram",
    "tiktok.com": "tiktok", "www.tiktok.com": "tiktok", "vm.tiktok.com": "tiktok", "vt.tiktok.com": "tiktok",
    "youtube.com": "youtube", "www.youtube.com": "youtube", "m.youtube.com": "youtube", "youtu.be": "youtube",
    "x.com": "x", "twitter.com": "x", "www.x.com": "x", "www.twitter.com": "x",
}


class IngestError(ValueError):
    pass


@dataclass
class Acquired:
    path: Path
    sha256: str
    source_url: str
    kind: str  # direct | platform
    platform: str | None
    title: str
    uploader: str | None
    bytes: int
    elapsed_ms: int


def classify_url(url: str) -> dict[str, Any]:
    try:
        u = urlparse(url.strip())
    except ValueError as exc:
        raise IngestError("not a valid link") from exc
    if u.scheme not in ("http", "https") or not u.hostname:
        raise IngestError("only http and https links are accepted")
    if u.username or u.password:
        raise IngestError("links with embedded credentials are refused")
    host = u.hostname.lower()
    if host in PLATFORMS:
        return {"kind": "platform", "platform": PLATFORMS[host], "host": host}
    if Path(u.path).suffix.lower() in VIDEO_SUFFIXES:
        return {"kind": "direct", "platform": None, "host": host}
    return {"kind": "unknown", "platform": None, "host": host}


def check_host(host: str, port: int | None, allow_private: bool = False) -> None:
    if port not in (None, 80, 443) and not allow_private:
        raise IngestError("only the standard web ports are allowed")
    try:
        infos = socket.getaddrinfo(host, port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise IngestError(f"the host {host} could not be resolved") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise IngestError("the link points at a private or local network address, which is refused")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_direct(url: str, work_dir: Path, allow_private: bool = False, max_bytes: int = MAX_BYTES) -> Acquired:
    started = time.monotonic()
    current = url
    work_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(30.0, read=60.0)) as client:
        for _ in range(4):
            u = urlparse(current)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise IngestError("a redirect left http/https")
            check_host(u.hostname, u.port, allow_private)
            with client.stream("GET", current, headers={"User-Agent": "DirectorLoop/0.1 (video judging)"}) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location")
                    if not loc:
                        raise IngestError("a redirect had no destination")
                    current = urljoin(current, loc)
                    continue
                if resp.status_code != 200:
                    raise IngestError(f"the link returned HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                suffix = Path(urlparse(current).path).suffix.lower()
                if not (ctype.startswith("video/") or (ctype in ("application/octet-stream", "binary/octet-stream", "") and suffix in VIDEO_SUFFIXES)):
                    raise IngestError(f"the link does not serve a video (content type {ctype or 'missing'})")
                declared = int(resp.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise IngestError("the video is larger than 300 MB")
                ext = suffix if suffix in VIDEO_SUFFIXES else {"video/quicktime": ".mov", "video/webm": ".webm"}.get(ctype, ".mp4")
                fd, tmp_name = tempfile.mkstemp(prefix="incoming_", suffix=ext, dir=work_dir)
                tmp = Path(tmp_name)
                size = 0
                try:
                    with open(fd, "wb") as out:
                        for chunk in resp.iter_bytes(1 << 20):
                            size += len(chunk)
                            if size > max_bytes:
                                raise IngestError("the video is larger than 300 MB")
                            if time.monotonic() - started > TIMEOUT_S:
                                raise IngestError("the download took longer than 180 seconds")
                            out.write(chunk)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                title = re.sub(r"[^A-Za-z0-9 ._\-]", "", Path(urlparse(current).path).stem)[:80] or "linked video"
                return Acquired(path=tmp, sha256=_sha256(tmp), source_url=url, kind="direct", platform=None, title=title, uploader=None, bytes=size,
                                elapsed_ms=int((time.monotonic() - started) * 1000))
    raise IngestError("too many redirects")


def fetch_platform(url: str, platform: str, work_dir: Path, max_bytes: int = MAX_BYTES, runner: Any = subprocess.run) -> Acquired:
    exe = shutil.which("yt-dlp") or str(Path.home() / ".local/bin/yt-dlp")
    if not Path(exe).exists():
        raise IngestError(f"{platform} links need yt-dlp, which is not installed on this server; upload the file instead")
    started = time.monotonic()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_dir = Path(tempfile.mkdtemp(prefix="platform_", dir=work_dir))
    argv = [exe, "--no-simulate", "-J", "--no-playlist", "--no-progress", "--restrict-filenames", "--max-filesize", str(max_bytes),
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4", "-o", str(out_dir / "%(id)s.%(ext)s"), url]
    try:
        proc = runner(argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise IngestError("the platform download took longer than 180 seconds") from exc
    if proc.returncode != 0:
        shutil.rmtree(out_dir, ignore_errors=True)
        last = (proc.stderr or "").strip().splitlines()[-1:] or ["unknown error"]
        raise IngestError(f"{platform} did not provide the video: {last[0][:200]}")
    try:
        meta = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        meta = {}
    files = [Path(d["filepath"]) for d in meta.get("requested_downloads", []) if isinstance(d, dict) and d.get("filepath")] or \
            sorted(p for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_SUFFIXES)
    if not files or not files[0].exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        raise IngestError(f"{platform} returned no video file")
    path = files[0]
    size = path.stat().st_size
    if size > max_bytes:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise IngestError("the video is larger than 300 MB")
    title = re.sub(r"[^A-Za-z0-9 ._\-]", "", str(meta.get("title") or meta.get("id") or f"{platform} video"))[:80] or f"{platform} video"
    return Acquired(path=path, sha256=_sha256(path), source_url=str(meta.get("webpage_url") or url), kind="platform", platform=platform, title=title,
                    uploader=str(meta["uploader"])[:80] if meta.get("uploader") else None, bytes=size, elapsed_ms=int((time.monotonic() - started) * 1000))


def acquire(url: str, work_dir: Path, allow_private: bool = False) -> Acquired:
    info = classify_url(url)
    if info["kind"] == "platform":
        return fetch_platform(url, info["platform"], work_dir)
    return fetch_direct(url, work_dir, allow_private=allow_private)
