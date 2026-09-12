"""Reference corpus -> genome per reference -> descriptive pattern statistics.

Reads data/corpus/curio_references.json (built by scripts/build_curio_corpus.py), extracts a CreativeGenome for every
reference (cached per file hash), and writes data/corpus/curio_corpus_with_patterns.json. Videos used in experiments
can be excluded so no video contributes to its own prior. With fewer than six references carrying the relevant
performance metric, no top/bottom split is computed and patterns are reported as plain frequencies.

Usage: .venv/bin/python scripts/analyze_corpus.py [--exclude SLUG ...] [--workers 4] [--limit N]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import time
from pathlib import Path

from directorloop.config import get_settings
from directorloop.creative.genome import extract_genome
from directorloop.creative.patterns import PATTERNS, extract_patterns
from directorloop.domain.creative import CreativeGenome, ReferenceCorpus, ReferenceCreative
from directorloop.domain.ids import utc_now_iso
from directorloop.observability import flush, init_weave
from directorloop.providers import build_providers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="data/corpus/curio_references.json")
    ap.add_argument("--output", default="data/corpus/curio_corpus_with_patterns.json")
    ap.add_argument("--exclude", nargs="*", default=[], help="production folder names or substrings to leave out")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    s = get_settings()
    init_weave(s)
    providers = build_providers(s)
    corpus = ReferenceCorpus.model_validate_json(Path(args.input).read_text(encoding="utf-8"))
    refs = [r for r in corpus.references if r.media_path and not any(x.lower() in (r.provenance.get("production_dir", "") + r.id).lower() for x in args.exclude)]
    if args.limit:
        refs = refs[: args.limit]
    print(f"{len(refs)} references to analyze ({len(corpus.references) - len(refs)} excluded or without media)", flush=True)
    t0 = time.monotonic()

    def one(ref: ReferenceCreative) -> tuple[ReferenceCreative, CreativeGenome | None, str | None]:
        try:
            g = extract_genome(Path(ref.media_path or ""), providers.probe, s.data_dir / "creative" / "genomes", category=ref.category, artifact_hash=ref.artifact_hash)
            return ref, g, None
        except Exception as exc:  # noqa: BLE001
            return ref, None, str(exc)[:160]

    analyzed: list[tuple[ReferenceCreative, CreativeGenome]] = []
    failures: list[str] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, (ref, g, err) in enumerate(ex.map(one, refs), 1):
            if g is None:
                failures.append(f"{ref.id}: {err}")
                print(f"  [{i}/{len(refs)}] FAILED {ref.id}: {err}", flush=True)
                continue
            ref.genome_id = g.id
            ref.patterns = [pid for pid, (_, fn, _) in PATTERNS.items() if fn(g)]
            analyzed.append((ref, g))
            print(f"  [{i}/{len(refs)}] {ref.id}: {len(g.beats)} beats, hook {g.hook.hook_type.value}, patterns {ref.patterns}", flush=True)
    patterns = extract_patterns(analyzed, scope="educational_short")
    out = ReferenceCorpus(
        id=corpus.id, version=corpus.version + 1, description=corpus.description + " Genomes and descriptive pattern counts added.",
        references=[r for r, _ in analyzed], patterns=patterns, built_at=utc_now_iso(),
        notes=corpus.notes + [f"excluded: {args.exclude}", f"genome failures: {len(failures)}", "pattern counts are descriptive frequencies, not causal evidence",
                              f"references with performance metrics: {sum(1 for r, _ in analyzed if r.performance)}"],
    )
    Path(args.output).write_text(out.model_dump_json(indent=2), encoding="utf-8")
    flush()
    print(f"\nanalyzed {len(analyzed)} references in {int(time.monotonic() - t0)} s; failures {len(failures)}")
    for p in patterns:
        split = f"; top {p.top_group_count}/{p.top_group_total} vs bottom {p.bottom_group_count}/{p.bottom_group_total} ({p.performance_metric})" if p.top_group_total else ""
        print(f"  {p.pattern_id:32s} {p.count:3d}/{p.total_comparable:<3d} ({p.support_ratio:.0%}){split}  {p.note}")
    print(json.dumps({"output": args.output, "failures": failures}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
