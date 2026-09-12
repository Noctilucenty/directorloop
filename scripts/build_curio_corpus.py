#!/usr/bin/env python
"""Build the Curio-owned reference corpus for DirectorLoop.

Reads Leon's own finished Curio shorts (Curio-Automation) plus their published performance
data (Instagram insights, Facebook retention curves) and curio-publisher's publish ledger, and
writes a `ReferenceCorpus` (see `directorloop/domain/creative.py`) of `ReferenceCreative` rows:
one per finished production, with a `RetentionSeries` attached wherever we can confidently join
a production to a real published post.

READ-ONLY against Curio-Automation and curio-publisher. This script never writes, moves, copies
or deletes anything in those trees, never shells out into them, and never opens
`viral-intelligence/graph-pulls/2026-07-30/{page-direct,reels}.json` (only the four files listed
in SOURCE_FILES below are read there). Media files are referenced by absolute path, never copied.

Matching, in priority order, per candidate production:
  1. ledger:   a real (ok=true, dry_run=false) curio-publisher ledger row whose `file` points
               inside this production's folder AND whose `file_sha256` exactly matches the
               sha256 of this production's resolved final render. This is the only path that
               can also confirm a `platform_media_id`. Rows with a blank `file_sha256`
               ("uploaded-file identity unknown" backfills) are never treated as exact -- they
               carry no verifiable join and are surfaced in the join report instead.
  2. fb_slug:  a Facebook retention-curve post whose `slug`, normalized (lowercased, separators
               stripped), is contained in (or contains) a candidate production directory name,
               normalized the same way, with no other candidate also matching that slug.
  3. ig_caption_fuzzy: an Instagram reel whose `caption_head` token-set-overlaps the production's
               title or first spoken sentence at >=0.6, with a >=0.15 margin over the runner-up
               among ALL candidate productions. When several near-identical re-shoots of the same
               script tie for a reel (e.g. a topic's -V2/-V3/-V4 attempts), the tie is broken by
               picking whichever candidate's final render has the mtime closest to, but not
               after, the reel's publish timestamp; if none qualifies, the reel is left unmatched.
A reel or FB post is consumed by at most one production. A production matched on Instagram keeps
that in `performance`; a simultaneous Facebook curve is nested under
`provenance["facebook_retention_curve"]` instead of being dropped.

Usage:
  .venv/bin/python scripts/build_curio_corpus.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from directorloop.domain.creative import (  # noqa: E402
    ReferenceCorpus,
    ReferenceCreative,
    RetentionPoint,
    RetentionSeries,
    RetentionSourceType,
)
from directorloop.domain.ids import sha256_file, utc_now_iso  # noqa: E402
from directorloop.media.probe import inspect_media  # noqa: E402

# ----------------------------------------------------------------------------- fixed inputs (read-only)

CURIO_AUTOMATION = Path("/Users/leon/Desktop/dev/Curio-Automation")
CURIO_PUBLISHER = Path("/Users/leon/Desktop/dev/curio-publisher")
PRODUCTIONS_ROOT = CURIO_AUTOMATION / "data" / "productions"
VI_ROOT = CURIO_AUTOMATION / "data" / "viral-intelligence"

LEDGER_PATH = CURIO_PUBLISHER / "ledger" / "published.jsonl"
IG_INSIGHT_FILES = [
    VI_ROOT / "ig-insights-2026-07-31.json",
    VI_ROOT / "ig-insights-2026-08-01.json",
    VI_ROOT / "ig-insights-2026-08-02.json",
]
FB_CURVES_FILE = VI_ROOT / "fb-retention-curves-2026-07-30.json"
# The only files this script ever opens under viral-intelligence/. In particular it never lists
# or reads anything under viral-intelligence/graph-pulls/ (2026-07-30/page-direct.json and
# reels.json there hold access tokens and must never be opened).
SOURCE_FILES = [*IG_INSIGHT_FILES, FB_CURVES_FILE]

OUT_DIR = REPO_ROOT / "data" / "corpus"
OUT_JSON = OUT_DIR / "curio_references.json"
OUT_REPORT = OUT_DIR / "curio_join_report.md"

PERMISSION = "Leon-owned Curio short; analyzed with the owner's authorization for this project"
CATEGORY = "educational_short"

# ----------------------------------------------------------------------------- final-render selection

# Iteration/loop/proof artifacts to skip even when their name otherwise matches a final-render tier.
_EXCLUDE_RE = re.compile(
    r"loopx|-review|raw\.mp4$|-v[1-9](?:[-._]|$)|(?:^|[-_])s[1-6](?:[-._]|$)|mech1|mech2",
    re.IGNORECASE,
)
# (tier label, filename pattern), in preference order.
_FINAL_TIERS: list[tuple[str, re.Pattern]] = [
    ("custom-captioned", re.compile(r"-custom-captioned\.mp4$", re.IGNORECASE)),
    ("master-upload", re.compile(r"-master-upload\.mp4$", re.IGNORECASE)),
    ("outro-POST", re.compile(r"-outro-post\.mp4$", re.IGNORECASE)),
    ("POSTING", re.compile(r"posting.*\.mp4$", re.IGNORECASE)),
]


def find_final_render(production_dir: Path) -> tuple[str, Path] | tuple[None, None]:
    """Resolve the one final render for a production folder, searched recursively.

    Within a tier, several copies commonly exist (a top-level file plus duplicates under
    scratch/proof/failed-verify subfolders from earlier pipeline runs); the shallowest path wins.
    Falls through tiers in preference order; returns (None, None) if nothing matches.
    """
    all_mp4s: list[tuple[int, str, Path]] = []
    for dirpath, _dirnames, filenames in os.walk(production_dir):
        for name in filenames:
            if name.lower().endswith(".mp4"):
                full = Path(dirpath) / name
                rel = full.relative_to(production_dir)
                all_mp4s.append((len(rel.parts) - 1, str(rel), full))
    for tier_label, pattern in _FINAL_TIERS:
        candidates = [c for c in all_mp4s if pattern.search(c[2].name) and not _EXCLUDE_RE.search(c[2].name)]
        if not candidates:
            continue
        candidates.sort(key=lambda c: (c[0], c[1]))  # shallowest first, then lexicographic
        return tier_label, candidates[0][2]
    return None, None


# ----------------------------------------------------------------------------- text matching

_STOPWORDS = set(
    """
    a an the this that these those is are was were be been being
    i you he she it we they me him her us them my your his its our their
    of to in on at for with from by as and or but not no nor so if then than
    what which who whom will would can could should may might must
    do does did doing have has had having about into over under again further
    once here there when where why how all any both each few more most other
    some such only own same so
    isn don doesn aren wasn weren wouldn couldn shouldn won didn hadn hasn
    ll ve re d m t s
    """.split()
)


def tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, stopwords and single characters dropped."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff"
    "\U0000fe0f"
    "]+"
)


def strip_emoji(text: str) -> str:
    """Source captions occasionally carry emoji; deliverables here never do, quoted or not."""
    return _EMOJI_RE.sub("", text or "").strip()


def overlap_coefficient(a: set[str], b: set[str]) -> float:
    """Szymkiewicz-Simpson overlap: |A n B| / min(|A|, |B|). Robust to the size mismatch between
    a short title/first-sentence and a longer caption -- plain Jaccard punishes that mismatch
    even for a correct match."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def first_sentence(spoken: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", spoken.strip())
    return parts[0] if parts else spoken.strip()


def clean_title(data: dict) -> str:
    title = data.get("title") or ""
    if not title:
        title = " ".join(data.get("captions", {}).get("title_lines", []) or [])
    title = title.replace("/", " ")
    return re.sub(r"\s+", " ", title).strip()


def normalize_slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ----------------------------------------------------------------------------- candidate productions


@dataclass
class ProductionCandidate:
    dir_name: str
    dir_path: Path
    production_json_path: Path
    title: str
    spoken: str
    first_sentence: str
    render_naming: str
    final_path: Path
    sha256: str
    mtime: float
    # filled in only for productions that end up with a match
    ig_match: dict | None = None  # {"media_id","record","captured_at","source_file","evidence"}
    fb_match: dict | None = None  # {"post","evidence"}
    ledger_match: dict | None = None  # {"row","evidence"}


def load_production_candidates() -> list[ProductionCandidate]:
    candidates: list[ProductionCandidate] = []
    for name in sorted(os.listdir(PRODUCTIONS_ROOT)):
        prod_dir = PRODUCTIONS_ROOT / name
        if not prod_dir.is_dir():
            continue
        pj_path = prod_dir / "production.json"
        if not pj_path.is_file():
            continue
        data = json.loads(pj_path.read_text())
        render_naming, final_path = find_final_render(prod_dir)
        if final_path is None:
            continue
        spoken = data.get("script", {}).get("spoken") or ""
        if not spoken:
            continue
        candidates.append(
            ProductionCandidate(
                dir_name=name,
                dir_path=prod_dir,
                production_json_path=pj_path,
                title=clean_title(data),
                spoken=spoken,
                first_sentence=first_sentence(spoken),
                render_naming=render_naming,
                final_path=final_path,
                sha256=sha256_file(final_path),
                mtime=final_path.stat().st_mtime,
            )
        )
    return candidates


# ----------------------------------------------------------------------------- ledger


def load_real_ledger_rows() -> list[dict]:
    if not LEDGER_PATH.is_file():
        return []
    rows = []
    for line in LEDGER_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("ok") is True and row.get("dry_run") is False:
            rows.append(row)
    return rows


def match_ledger(candidates: list[ProductionCandidate], ledger_rows: list[dict]) -> list[dict]:
    """Attach a validated ledger row to each candidate it exactly matches. Returns the list of
    ledger rows that pointed at one of our productions but could NOT be verified (blank/mismatched
    sha256), for the join report."""
    by_dir = {c.dir_name: c for c in candidates}
    unverified: list[dict] = []
    seen_dirs: set[str] = set()
    for row in ledger_rows:
        file_field = row.get("file") or ""
        marker = "data/productions/"
        idx = file_field.find(marker)
        if idx == -1:
            continue
        rest = file_field[idx + len(marker) :]
        prod_dir_name = rest.split("/", 1)[0]
        cand = by_dir.get(prod_dir_name)
        if cand is None:
            continue
        sha = (row.get("file_sha256") or "").lower()
        if len(sha) == 64 and sha == cand.sha256.lower():
            if cand.ledger_match is None:  # first verified row wins; stays deterministic (file order)
                cand.ledger_match = {
                    "row": row,
                    "evidence": (
                        f"ledger sha256 exact match on {cand.render_naming} render; "
                        f"platform={row.get('platform')}; utc={row.get('utc')}"
                    ),
                }
        else:
            if prod_dir_name not in seen_dirs:
                unverified.append(row)
                seen_dirs.add(prod_dir_name)
    return unverified


# ----------------------------------------------------------------------------- instagram insights


def load_ig_reels() -> tuple[dict[str, dict], dict[str, str]]:
    """Merge the three daily IG snapshots, keeping the latest `_captured` record per media_id.
    Returns (media_id -> {"record","captured_at","source_file"}, permalink -> media_id)."""
    by_id: dict[str, dict] = {}
    for path in IG_INSIGHT_FILES:
        data = json.loads(path.read_text())
        captured_at = data["_captured"]
        for reel in data["reels"]:
            mid = reel["media_id"]
            prev = by_id.get(mid)
            if prev is None or captured_at > prev["captured_at"]:
                if reel.get("caption_head"):
                    reel = {**reel, "caption_head": strip_emoji(reel["caption_head"])}
                by_id[mid] = {"record": reel, "captured_at": captured_at, "source_file": str(path)}
    by_permalink = {v["record"]["permalink"]: mid for mid, v in by_id.items() if v["record"].get("permalink")}
    return by_id, by_permalink


def ig_retention_series(entry: dict) -> RetentionSeries:
    r = entry["record"]
    captured_dt = datetime.fromisoformat(entry["captured_at"].replace("Z", "+00:00"))
    caveats = [
        "summary metrics only; no per-second curve",
        f"watch_through={r.get('watch_through')} (Meta-derived average watch / duration, not completion)",
    ]
    ts = r.get("timestamp")
    if ts:
        posted_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if (captured_dt - posted_dt).total_seconds() < 72 * 3600:
            caveats.append("early-age snapshot")
    survive_3s = r.get("survive_3s_pct")
    return RetentionSeries(
        source_type=RetentionSourceType.HISTORICAL_OWNED,
        platform="instagram",
        plays=r.get("views_ig"),
        reach=r.get("reach_ig"),
        avg_watch_time_ms=r.get("avg_watch_ms"),
        hold_3s=(survive_3s / 100.0) if survive_3s is not None else None,
        completion_rate=None,
        shares=r.get("shares"),
        saves=r.get("saved"),
        likes=r.get("likes"),
        comments=r.get("comments"),
        provenance=entry["source_file"],
        collected_at=entry["captured_at"],
        caveats=caveats,
    )


# ----------------------------------------------------------------------------- facebook curves


def load_fb_posts() -> tuple[list[dict], str]:
    data = json.loads(FB_CURVES_FILE.read_text())
    return data["posts"], data["_captured"]


def match_fb_slugs(candidates: list[ProductionCandidate], fb_posts: list[dict]) -> list[tuple[dict, list[str]]]:
    """Attach the FB post to the one candidate whose normalized directory name contains (or is
    contained in) the normalized slug, when that candidate is unique. Returns the (post, all
    on-disk folder name matches incl. non-candidates) pairs that were NOT accepted, for the report."""
    norm_by_dir = {c.dir_name: normalize_slug(c.dir_name) for c in candidates}
    by_dir = {c.dir_name: c for c in candidates}
    # every folder on disk (not just candidates) purely for join-report transparency
    all_dirs = sorted(p.name for p in PRODUCTIONS_ROOT.iterdir() if p.is_dir())
    norm_all_dirs = {d: normalize_slug(d) for d in all_dirs}

    rejected: list[tuple[dict, list[str]]] = []
    for post in fb_posts:
        slug_norm = normalize_slug(post["slug"])
        if len(slug_norm) < 8:
            rejected.append((post, []))
            continue
        cand_hits = [d for d, dn in norm_by_dir.items() if slug_norm in dn or dn in slug_norm]
        if len(cand_hits) == 1:
            by_dir[cand_hits[0]].fb_match = {
                "post": post,
                "evidence": f"FB slug={post['slug']!r} normalized-contained in production dir {cand_hits[0]!r}",
            }
        else:
            all_hits = [d for d, dn in norm_all_dirs.items() if slug_norm in dn or dn in slug_norm]
            rejected.append((post, all_hits))
    return rejected


def fb_retention_series(post: dict, duration_ms: int, source_file: str, captured_at: str) -> RetentionSeries:
    true_series = post["true_series"]
    n = len(true_series)
    points = []
    for i, frac in enumerate(true_series):
        t_ms = round(i * duration_ms / (n - 1)) if n > 1 else 0
        points.append(RetentionPoint(t_ms=t_ms, remaining_fraction=min(1.0, max(0.0, frac))))
    avg_watch_s = post.get("avg_watch_s")
    return RetentionSeries(
        source_type=RetentionSourceType.HISTORICAL_OWNED,
        platform="facebook",
        points=points,
        plays=post.get("views_total"),
        avg_watch_time_ms=round(avg_watch_s * 1000) if avg_watch_s is not None else None,
        provenance=source_file,
        collected_at=captured_at,
        caveats=[
            "first four buckets are always 1.0 on Meta's chart and carry no information about "
            "the first ~3 seconds; that is the cohort definition, not a measurement",
            f"duration_ms={duration_ms} from ffprobe of the matched local render, not from Meta's "
            "(unreliable / sometimes absent) reported duration_s",
        ],
    )


# ----------------------------------------------------------------------------- instagram caption fuzzy match


@dataclass
class FuzzyAttempt:
    media_id: str
    caption_head: str
    timestamp: str | None
    best_score: float
    best_dir: str | None
    runner_score: float
    runner_dir: str | None
    decision_dir: str | None
    evidence: str


def match_ig_captions(
    candidates: list[ProductionCandidate], ig_by_id: dict[str, dict]
) -> list[FuzzyAttempt]:
    pool = {c.dir_name: c for c in candidates if c.ledger_match is None}
    token_cache = {d: (tokenize(c.title), tokenize(c.first_sentence)) for d, c in pool.items()}
    attempts: list[FuzzyAttempt] = []
    for media_id in sorted(ig_by_id):
        entry = ig_by_id[media_id]
        record = entry["record"]
        caption_tokens = tokenize(record.get("caption_head", ""))
        scores: list[tuple[float, str]] = []
        for d, _cand in pool.items():
            title_tokens, sentence_tokens = token_cache[d]
            score = max(overlap_coefficient(caption_tokens, title_tokens), overlap_coefficient(caption_tokens, sentence_tokens))
            scores.append((score, d))
        scores.sort(key=lambda x: (-x[0], x[1]))
        if not scores:
            continue
        best_score, best_dir = scores[0]
        runner_score, runner_dir = scores[1] if len(scores) > 1 else (0.0, None)
        decision_dir: str | None = None
        evidence: str

        if best_score >= 0.6 and (best_score - runner_score) >= 0.15:
            decision_dir = best_dir
            evidence = (
                f"clear win: score={best_score:.3f} vs runner_up={runner_score:.3f} ({runner_dir})"
            )
        elif best_score >= 0.6:
            competing = [d for s, d in scores if s >= 0.6]
            if len(competing) >= 2:
                reel_ts = None
                if record.get("timestamp"):
                    reel_ts = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
                before = sorted(
                    ((pool[d].mtime, d) for d in competing if reel_ts is not None and pool[d].mtime <= reel_ts),
                    key=lambda x: -x[0],
                )
                if before:
                    decision_dir = before[0][1]
                    evidence = (
                        f"tied at score={best_score:.3f} among {competing}; resolved to render "
                        f"closest before reel timestamp ({record.get('timestamp')})"
                    )
                else:
                    evidence = (
                        f"tied at score={best_score:.3f} among {competing}; no candidate render "
                        "precedes the reel timestamp -> left unmatched"
                    )
            else:
                evidence = (
                    f"best={best_score:.3f} ({best_dir}) vs runner_up={runner_score:.3f} ({runner_dir}); "
                    "margin < 0.15 -> left unmatched"
                )
        else:
            evidence = f"best={best_score:.3f} below the 0.6 floor -> left unmatched"

        attempts.append(
            FuzzyAttempt(
                media_id=media_id,
                caption_head=record.get("caption_head", ""),
                timestamp=record.get("timestamp"),
                best_score=best_score,
                best_dir=best_dir,
                runner_score=runner_score,
                runner_dir=runner_dir,
                decision_dir=decision_dir,
                evidence=evidence,
            )
        )
        if decision_dir is not None:
            winner = pool.pop(decision_dir)
            winner.ig_match = {
                "media_id": media_id,
                "entry": entry,
                "evidence": (
                    f"caption_head={record.get('caption_head', '')!r} matched title={winner.title!r} "
                    f"/ first_sentence={winner.first_sentence!r} ({evidence})"
                ),
            }
    return attempts


# ----------------------------------------------------------------------------- assembling references


def build_reference(cand: ProductionCandidate, fb_duration_cache: dict[str, int]) -> ReferenceCreative:
    provenance: dict[str, Any] = {
        "production_dir": str(cand.dir_path),
        "production_json": str(cand.production_json_path),
        "final_render_path": str(cand.final_path),
        "render_naming": cand.render_naming,
    }

    performance: RetentionSeries | None = None
    ig_series: RetentionSeries | None = None
    fb_series: RetentionSeries | None = None
    # Identity confirmation (permalink/platform_id/evidence) is independent of whether that
    # identity happened to carry a usable metrics snapshot; keep the two separate so a ledger
    # match that found no IG metrics can never be mislabeled by a later, metrics-bearing FB match.
    identity_method: str | None = None
    identity_confidence: str | None = None
    identity_evidence: str | None = None

    if cand.ledger_match is not None:
        row = cand.ledger_match["row"]
        provenance["permalink"] = row.get("permalink")
        if row.get("platform_id"):
            provenance["platform_media_id"] = row.get("platform_id")
        identity_method = "ledger"
        identity_confidence = "exact_ledger"
        # An exact ledger join only carries metrics if that permalink also shows up in an IG
        # snapshot; a post published outside the three captured days has no metrics available.
        ig_hit = cand.ledger_match.get("ig_entry")
        if ig_hit is not None:
            ig_series = ig_retention_series(ig_hit)
            identity_evidence = cand.ledger_match["evidence"] + f"; caption={ig_hit['record'].get('caption_head', '')!r}"
        else:
            identity_evidence = cand.ledger_match["evidence"] + "; no IG insights snapshot covers this permalink's window"
    elif cand.ig_match is not None:
        entry = cand.ig_match["entry"]
        provenance["permalink"] = entry["record"].get("permalink")
        identity_method = "ig_caption_fuzzy"
        identity_confidence = "caption_fuzzy_high"
        identity_evidence = cand.ig_match["evidence"]
        ig_series = ig_retention_series(entry)

    if cand.fb_match is not None:
        post = cand.fb_match["post"]
        duration_ms = fb_duration_cache[cand.dir_name]
        fb_series = fb_retention_series(post, duration_ms, str(FB_CURVES_FILE), post.get("_captured_at_file", ""))

    # `performance` always prefers Instagram metrics when we have them (spec); otherwise it falls
    # back to whatever Facebook curve we found, independently of which method confirmed identity.
    if ig_series is not None:
        performance = ig_series
        if fb_series is not None:
            provenance["facebook_retention_curve"] = fb_series.model_dump(mode="json")
    elif fb_series is not None:
        performance = fb_series

    if identity_method is not None:
        provenance["match_method"] = identity_method
        provenance["match_confidence"] = identity_confidence
        provenance["match_evidence"] = identity_evidence
        if fb_series is not None and performance is not fb_series:
            provenance["facebook_slug_match"] = cand.fb_match["evidence"]
    elif fb_series is not None:
        provenance["match_method"] = "fb_slug"
        provenance["match_confidence"] = "exact_slug"
        provenance["match_evidence"] = cand.fb_match["evidence"]
    else:
        provenance["match_method"] = "none"
        provenance["match_confidence"] = "unmatched"
        provenance["match_evidence"] = "no ledger, FB slug, or IG caption match cleared the acceptance bar"

    slug = cand.dir_name.lower()
    return ReferenceCreative(
        id=f"ref_{slug}",
        title=cand.title,
        source="owned_curio",
        permission=PERMISSION,
        provenance=provenance,
        category=CATEGORY,
        artifact_hash=cand.sha256,
        media_path=str(cand.final_path),
        performance=performance,
        label="REFERENCE CREATIVE",
    )


# ----------------------------------------------------------------------------- join report


def write_join_report(
    candidates: list[ProductionCandidate],
    total_production_dirs: int,
    fuzzy_attempts: list[FuzzyAttempt],
    fb_rejected: list[tuple[dict, list[str]]],
    ledger_unverified: list[dict],
    ig_total: int,
    fb_total: int,
) -> None:
    lines: list[str] = []
    lines.append("# Curio owned-corpus join report")
    lines.append("")
    lines.append(f"Generated {utc_now_iso()}.")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append(f"- production folders scanned: {total_production_dirs}")
    lines.append(f"- finished productions (production.json + a resolved final render): {len(candidates)}")
    by_tier: dict[str, int] = {}
    for c in candidates:
        by_tier[c.render_naming] = by_tier.get(c.render_naming, 0) + 1
    for tier, n in by_tier.items():
        lines.append(f"  - final render tier `{tier}`: {n}")
    lines.append(f"- Instagram reels in the three snapshots (latest per media_id): {ig_total}")
    lines.append(f"- Facebook posts with a retention curve: {fb_total}")
    n_ledger = sum(1 for c in candidates if c.ledger_match is not None)
    n_ig_fuzzy = sum(1 for c in candidates if c.ledger_match is None and c.ig_match is not None)
    n_fb = sum(1 for c in candidates if c.fb_match is not None)
    n_unmatched = sum(1 for c in candidates if c.ledger_match is None and c.ig_match is None and c.fb_match is None)
    lines.append(f"- matched by ledger (sha256-verified): {n_ledger}")
    lines.append(f"- matched by IG caption fuzzy match: {n_ig_fuzzy}")
    lines.append(f"- matched by FB slug containment: {n_fb}")
    lines.append(f"- unmatched (reference kept, performance null): {n_unmatched}")
    lines.append("")

    lines.append("## Accepted matches")
    lines.append("")
    lines.append("| production | method | confidence | evidence |")
    lines.append("|---|---|---|---|")
    for c in candidates:
        if c.ledger_match:
            row = c.ledger_match["row"]
            method, conf = "ledger", "exact_ledger"
            evidence = c.ledger_match["evidence"] + (
                "; IG snapshot hit" if c.ledger_match.get("ig_entry") else "; no IG snapshot for this window"
            )
        elif c.ig_match:
            method, conf = "ig_caption_fuzzy", "caption_fuzzy_high"
            evidence = c.ig_match["evidence"]
        elif c.fb_match:
            method, conf = "fb_slug", "exact_slug"
            evidence = c.fb_match["evidence"]
        else:
            continue
        evidence = evidence.replace("|", "\\|")
        lines.append(f"| {c.dir_name} | {method} | {conf} | {evidence} |")
    lines.append("")

    lines.append("## IG caption fuzzy-match attempts (all reels, including unmatched)")
    lines.append("")
    lines.append("| media_id | timestamp | best (score) | runner-up (score) | decision | evidence |")
    lines.append("|---|---|---|---|---|---|")
    for a in fuzzy_attempts:
        cap = a.caption_head.replace("|", "\\|")
        best = f"{a.best_dir} ({a.best_score:.3f})"
        runner = f"{a.runner_dir} ({a.runner_score:.3f})" if a.runner_dir else "-"
        decision = a.decision_dir or "unmatched"
        lines.append(f"| {a.media_id} | {a.timestamp} | {best} | {runner} | {decision} | {a.evidence} |")
        lines.append(f"| | | caption_head | {cap} | | |")
    lines.append("")

    unmatched_reels = [a for a in fuzzy_attempts if a.decision_dir is None]
    lines.append(f"## Unmatched IG reels ({len(unmatched_reels)})")
    lines.append("")
    for a in unmatched_reels:
        lines.append(f"- `{a.media_id}` ({a.timestamp}): {a.caption_head!r}")
    lines.append("")

    lines.append("## FB slugs not accepted (ambiguous, too short, or no candidate)")
    lines.append("")
    for post, hits in fb_rejected:
        lines.append(f"- slug `{post['slug']}` (post {post['id']}): on-disk folder matches = {hits or 'none'}")
    lines.append("")

    lines.append("## Ledger rows pointing at a candidate production but not sha256-verified")
    lines.append("")
    for row in ledger_unverified:
        lines.append(
            f"- slug `{row.get('slug')}` platform={row.get('platform')} sha256={row.get('file_sha256') or '(blank)'} "
            f"note={row.get('note')!r}"
        )
    lines.append("")

    OUT_REPORT.write_text("\n".join(lines) + "\n")


# ----------------------------------------------------------------------------- main


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    total_production_dirs = sum(1 for p in PRODUCTIONS_ROOT.iterdir() if p.is_dir())
    candidates = load_production_candidates()

    ledger_rows = load_real_ledger_rows()
    ledger_unverified = match_ledger(candidates, ledger_rows)

    ig_by_id, ig_by_permalink = load_ig_reels()

    # Resolve ledger matches against the IG snapshots by permalink (ledger rows rarely carry a
    # usable platform_id; the permalink is the reliable join key both sides share).
    for c in candidates:
        if c.ledger_match is None:
            continue
        row = c.ledger_match["row"]
        if row.get("platform") == "instagram":
            permalink = row.get("permalink")
            mid = ig_by_permalink.get(permalink) if permalink else None
            if mid is not None:
                c.ledger_match["ig_entry"] = ig_by_id[mid]

    fuzzy_attempts = match_ig_captions(candidates, ig_by_id)

    fb_posts, fb_captured_at = load_fb_posts()
    for post in fb_posts:
        post["_captured_at_file"] = fb_captured_at
    fb_rejected = match_fb_slugs(candidates, fb_posts)

    fb_duration_cache: dict[str, int] = {}
    for c in candidates:
        if c.fb_match is not None:
            fb_duration_cache[c.dir_name] = inspect_media(c.final_path).duration_ms

    references = [build_reference(c, fb_duration_cache) for c in candidates]
    references.sort(key=lambda r: r.id)

    corpus = ReferenceCorpus(
        id="curio_owned_v1",
        version=1,
        description=(
            "Leon's own finished Curio educational shorts (Curio-Automation productions) joined "
            "against their real Instagram/Facebook performance where a confident join exists. "
            "Built for DirectorLoop's reference-pattern research; not a claim that every listed "
            "creative performed well."
        ),
        references=references,
        patterns=[],
        built_at=utc_now_iso(),
        notes=[
            "Matching priority: curio-publisher ledger (sha256-verified) > Facebook slug "
            "containment > Instagram caption fuzzy match; see data/corpus/curio_join_report.md "
            "for every accepted and rejected candidate.",
            "Facebook retention curves: the first four buckets are always 1.0 on Meta's chart and "
            "carry no information about the first ~3 seconds of playback; that is the cohort "
            "definition Meta uses, not a measurement.",
            "Instagram `watch_through` is Meta's average-watch-time-over-duration ratio, not a "
            "completion rate; it is preserved only in each series' caveats, never as completion_rate.",
            "References without a confident performance join are still included with "
            "performance=null; they remain valid reference creatives for structural analysis.",
        ],
    )

    OUT_JSON.write_text(corpus.model_dump_json(indent=2) + "\n")

    write_join_report(
        candidates=candidates,
        total_production_dirs=total_production_dirs,
        fuzzy_attempts=fuzzy_attempts,
        fb_rejected=fb_rejected,
        ledger_unverified=ledger_unverified,
        ig_total=len(ig_by_id),
        fb_total=len(fb_posts),
    )

    n_ledger = sum(1 for c in candidates if c.ledger_match is not None)
    n_ig_fuzzy = sum(1 for c in candidates if c.ledger_match is None and c.ig_match is not None)
    n_fb = sum(1 for c in candidates if c.fb_match is not None)
    n_perf = sum(1 for r in references if r.performance is not None)
    print(f"productions scanned: {total_production_dirs}")
    print(f"references built: {len(references)}")
    print(f"matched via ledger: {n_ledger}")
    print(f"matched via IG caption fuzzy: {n_ig_fuzzy}")
    print(f"matched via FB slug: {n_fb}")
    print(f"references with performance attached: {n_perf}")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
