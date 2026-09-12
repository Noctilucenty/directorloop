"""Blinded human review server. Only review routes exist in this app, so it is safe to tunnel publicly.

Flow: GET /  ->  GET /next (assigns the least-answered pair, randomizes sides, signs a token)
      ->  GET /t/{token} (two unlabeled clips, one question)  ->  POST /t/{token}/answer
Media is served as /m/{token}/left.mp4 and /m/{token}/right.mp4; arm ids, version numbers and which side is the
original never appear in URLs or pages. One answer per browser session per pair. No login, no personal data.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field

from ..config import get_settings
from ..domain.creative import CreativeExperiment, PairwiseReview
from ..domain.ids import new_id, utc_now_iso

TOKEN_MAX_AGE_S = 60 * 60 * 6
SESSION_COOKIE = "dl_review_session"
RATE_WINDOW_S = 60
RATE_MAX = 40


@dataclass(frozen=True)
class PairSpec:
    experiment_id: str
    test_type: str  # hook | full
    left_arm: str  # control arm id (before randomization)
    right_arm: str
    left_path: Path
    right_path: Path


class AnswerIn(BaseModel):
    choice: str = Field(pattern="^(left|right|none)$")
    about: str = Field(default="", max_length=240)


class ReviewStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "reviews.jsonl"
        self.assign_path = self.root / "assignments.jsonl"
        self.lock = threading.Lock()
        self._assignments: dict[str, dict] = {}
        if self.assign_path.exists():
            for line in self.assign_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._assignments[row["id"]] = row

    def create_assignment(self, experiment_id: str, test_type: str, left: str, right: str) -> str:
        """Server-side record of which arm sits on which side. URLs only ever carry the random id."""
        aid = secrets.token_urlsafe(18)
        row = {"id": aid, "e": experiment_id, "t": test_type, "l": left, "r": right, "created_at": utc_now_iso()}
        with self.lock:
            self._assignments[aid] = row
            with open(self.assign_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return aid

    def assignment(self, aid: str) -> dict | None:
        return self._assignments.get(aid)

    def all(self) -> list[PairwiseReview]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(PairwiseReview.model_validate_json(line))
        return out

    def add(self, review: PairwiseReview) -> bool:
        with self.lock:
            for r in self.all():
                if r.session_hash == review.session_hash and r.experiment_id == review.experiment_id and r.test_type == review.test_type and {r.arm_left, r.arm_right} == {review.arm_left, review.arm_right}:
                    return False
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(review.model_dump_json() + "\n")
            return True


def load_pairs(data_dir: Path, test_types: tuple[str, ...] = ("hook",)) -> list[PairSpec]:
    """Every completed experiment contributes control-vs-variant pairs whose media files exist."""
    pairs: list[PairSpec] = []
    exp_dir = data_dir / "creative" / "experiments"
    hooks = data_dir / "creative" / "cache" / "hooks"
    if not exp_dir.exists():
        return pairs
    for f in sorted(exp_dir.glob("cexp_*.json")):
        if f.name.endswith(".detail.json"):
            continue
        try:
            exp = CreativeExperiment.model_validate_json(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if exp.status != "completed" or not exp.arms:
            continue
        control = next((a for a in exp.arms if a.label == "control"), None)
        if control is None or not control.artifact_path:
            continue
        for arm in exp.arms:
            if arm.label == "control" or not arm.artifact_path or arm.fitness is None or not arm.fitness.hard_gates_passed:
                continue
            for test in test_types:
                if test == "hook":
                    lp = hooks / f"{Path(control.artifact_path).stem[:40]}_hook3000.mp4"
                    rp = hooks / f"{Path(arm.artifact_path).stem[:40]}_hook3000.mp4"
                    if control.artifact_hash and arm.artifact_hash and _same_opening(exp, arm.id):
                        continue  # identical first 3 s: nothing to compare
                else:
                    lp, rp = Path(control.artifact_path), Path(arm.artifact_path)
                if lp.exists() and rp.exists():
                    pairs.append(PairSpec(exp.id, test, control.id, arm.id, lp, rp))
    return pairs


def _same_opening(exp: CreativeExperiment, arm_id: str) -> bool:
    arm = next((a for a in exp.arms if a.id == arm_id), None)
    if arm is None or arm.fitness is None:
        return False
    comp = arm.fitness.get("model_hook_preference_vs_control")
    return comp is not None and comp.unit == "n/a"


def create_app(data_dir: Path | None = None, secret: str | None = None) -> FastAPI:
    settings = get_settings()
    data_dir = data_dir or settings.data_dir
    store = ReviewStore(data_dir / "reviews")
    secret_path = data_dir / "reviews" / ".token_secret"
    if secret is None:
        if not secret_path.exists():
            secret_path.write_text(secrets.token_hex(32), encoding="utf-8")
            secret_path.chmod(0o600)
        secret = secret_path.read_text(encoding="utf-8").strip()
    signer = URLSafeTimedSerializer(secret, salt="directorloop-review")
    hits: dict[str, deque[float]] = defaultdict(deque)
    app = FastAPI(title="DirectorLoop review", docs_url=None, redoc_url=None, openapi_url=None)

    def rate_limit(request: Request) -> None:
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
        q = hits[ip]
        now = time.monotonic()
        while q and now - q[0] > RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_MAX:
            raise HTTPException(status_code=429, detail="too many requests; try again in a minute")
        q.append(now)

    def session_of(request: Request) -> tuple[str, bool]:
        sid = request.cookies.get(SESSION_COOKIE)
        if sid and len(sid) == 32:
            return sid, False
        return secrets.token_hex(16), True

    def decode(token: str) -> dict:
        try:
            aid = signer.loads(token, max_age=TOKEN_MAX_AGE_S)
        except BadSignature as exc:
            raise HTTPException(status_code=404, detail="this link has expired") from exc
        row = store.assignment(str(aid))
        if row is None:
            raise HTTPException(status_code=404, detail="this link has expired")
        return row

    @app.get("/", response_class=HTMLResponse)
    def landing() -> str:
        return _page(
            "Quick video test",
            "<p class='lead'>Two short video openings. Pick the one you would keep watching.</p>"
            "<p class='fine'>About 10 seconds. No sign-in. We store only your choice and an anonymous browser id.</p>"
            "<a class='btn' href='next'>Start</a>",
        )

    @app.get("/next")
    def next_pair(request: Request) -> RedirectResponse:
        rate_limit(request)
        sid, fresh = session_of(request)
        pairs = load_pairs(data_dir)
        if not pairs:
            return RedirectResponse("done?empty=1", status_code=303)
        answered = store.all()
        mine = {(r.experiment_id, r.test_type, frozenset((r.arm_left, r.arm_right))) for r in answered if r.session_hash == _hash(sid)}
        counts: dict[tuple[str, str, frozenset[str]], int] = defaultdict(int)
        for r in answered:
            counts[(r.experiment_id, r.test_type, frozenset((r.arm_left, r.arm_right)))] += 1
        open_pairs = [p for p in pairs if (p.experiment_id, p.test_type, frozenset((p.left_arm, p.right_arm))) not in mine]
        if not open_pairs:
            resp = RedirectResponse("done", status_code=303)
        else:
            low = min(counts[(p.experiment_id, p.test_type, frozenset((p.left_arm, p.right_arm)))] for p in open_pairs)
            choice = random.choice([p for p in open_pairs if counts[(p.experiment_id, p.test_type, frozenset((p.left_arm, p.right_arm)))] == low])
            swap = random.random() < 0.5
            left, right = (choice.right_arm, choice.left_arm) if swap else (choice.left_arm, choice.right_arm)
            token = signer.dumps(store.create_assignment(choice.experiment_id, choice.test_type, left, right))
            resp = RedirectResponse(f"t/{token}", status_code=303)
        if fresh:
            resp.set_cookie(SESSION_COOKIE, sid, max_age=60 * 60 * 24 * 7, httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return resp

    @app.get("/t/{token}", response_class=HTMLResponse)
    def test_page(token: str, request: Request) -> str:
        rate_limit(request)
        decode(token)
        safe = html.escape(token, quote=True)
        return _page(
            "Which would you keep watching?",
            f"""
<p class='lead'>Play both. Which one would you be more likely to keep watching?</p>
<div class='pair'>
  <figure><video src='../m/{safe}/left.mp4' playsinline controls preload='metadata'></video><figcaption>Left</figcaption></figure>
  <figure><video src='../m/{safe}/right.mp4' playsinline controls preload='metadata'></video><figcaption>Right</figcaption></figure>
</div>
<div class='choices'>
  <button data-c='left'>Left</button><button data-c='none' class='quiet'>No preference</button><button data-c='right'>Right</button>
</div>
<label class='fine' for='about'>Optional: what do you think the video is about?</label>
<input id='about' maxlength='240' autocomplete='off'>
<p id='msg' class='fine'></p>
<script>
document.querySelectorAll('.choices button').forEach(b => b.addEventListener('click', async () => {{
  document.querySelectorAll('.choices button').forEach(x => x.disabled = true);
  const r = await fetch('{safe}/answer', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{choice: b.dataset.c, about: document.getElementById('about').value}})}});
  if (r.ok) {{ location.href = '../thanks'; }} else {{ document.getElementById('msg').textContent = 'Could not save that answer. ' + (await r.text()); }}
}}));
</script>""",
        )

    @app.get("/m/{token}/{side}.mp4")
    def media(token: str, side: str, request: Request) -> FileResponse:
        data = decode(token)
        if side not in ("left", "right"):
            raise HTTPException(status_code=404)
        arm = data["l"] if side == "left" else data["r"]
        pair = next((p for p in load_pairs(data_dir) if p.experiment_id == data["e"] and p.test_type == data["t"] and {p.left_arm, p.right_arm} == {data["l"], data["r"]}), None)
        if pair is None:
            raise HTTPException(status_code=404)
        path = pair.left_path if arm == pair.left_arm else pair.right_path
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/t/{token}/answer")
    def answer(token: str, body: AnswerIn, request: Request) -> JSONResponse:
        rate_limit(request)
        data = decode(token)
        sid, _ = session_of(request)
        chosen = {"left": data["l"], "right": data["r"]}.get(body.choice)
        review = PairwiseReview(
            review_id=new_id("rev"), experiment_id=data["e"], test_type=data["t"], arm_left=data["l"], arm_right=data["r"],
            shown_order=[data["l"], data["r"]], choice=body.choice, chosen_arm_id=chosen, response_text=body.about.strip()[:240],
            session_hash=_hash(sid), created_at=utc_now_iso(),
        )
        if not store.add(review):
            return JSONResponse({"recorded": False, "reason": "already answered"}, status_code=409)
        return JSONResponse({"recorded": True})

    @app.get("/thanks", response_class=HTMLResponse)
    def thanks() -> str:
        return _page("Thank you", "<p class='lead'>Saved. Another pair?</p><a class='btn' href='next'>Next pair</a>")

    @app.get("/done", response_class=HTMLResponse)
    def done() -> str:
        return _page("All done", "<p class='lead'>You have seen every pair we have right now. Thank you.</p>")

    return app


def _hash(sid: str) -> str:
    return hashlib.sha256(("directorloop-review:" + sid).encode()).hexdigest()[:24]


def summarize(data_dir: Path) -> list[dict]:
    """Per pair: n answers, preference counts for each arm, ties. For the operator view and policy updates."""
    store = ReviewStore(data_dir / "reviews")
    groups: dict[tuple[str, str, frozenset[str]], list[PairwiseReview]] = defaultdict(list)
    for r in store.all():
        groups[(r.experiment_id, r.test_type, frozenset((r.arm_left, r.arm_right)))].append(r)
    out = []
    for (exp, test, arms), rows in groups.items():
        counts: dict[str, int] = defaultdict(int)
        ties = 0
        for r in rows:
            if r.chosen_arm_id:
                counts[r.chosen_arm_id] += 1
            else:
                ties += 1
        out.append({"experiment_id": exp, "test_type": test, "arms": sorted(arms), "n": len(rows), "preferred": dict(counts), "no_preference": ties,
                    "label": "BLINDED CONTINUE-WATCHING PREFERENCE (human test; a small convenience sample, not retention)"})
    return out


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{html.escape(title)}</title>
<style>
:root {{ --bg:#15120f; --ink:#f3ead9; --muted:#b9ad97; --accent:#d9a45b; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.45 -apple-system,system-ui,sans-serif; padding:20px 16px 40px; }}
main {{ max-width:720px; margin:0 auto; }} h1 {{ font-size:1.4rem; margin:0 0 .5rem; }} .lead {{ font-size:1.05rem; }} .fine {{ color:var(--muted); font-size:.9rem; }}
.pair {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:14px 0; }} figure {{ margin:0; }} video {{ width:100%; aspect-ratio:9/16; background:#000; border-radius:10px; }}
figcaption {{ text-align:center; color:var(--muted); margin-top:4px; }}
.choices {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin:12px 0; }}
button, .btn {{ display:inline-block; text-align:center; padding:14px 10px; border-radius:10px; border:1px solid var(--accent); background:var(--accent); color:#1a140c; font-weight:600; font-size:1rem; text-decoration:none; }}
button.quiet {{ background:transparent; color:var(--ink); border-color:#5b5145; }} button:disabled {{ opacity:.5; }}
input {{ width:100%; padding:10px; border-radius:8px; border:1px solid #5b5145; background:#211c17; color:var(--ink); margin-top:6px; font-size:1rem; }}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""


def main() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8790, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()


def dump_summary(data_dir: Path) -> str:
    return json.dumps(summarize(data_dir), indent=2)
