"""Freeze and run a bounded sponsor-model screening comparison, without platform outcomes.

Preparation is offline. Execution is opt-in and uses a guarded provider. The recorded
Terra readings are descriptive references, never human labels or a promotion gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from directorloop.audit.review import (  # noqa: E402
    WINDOW_INSTRUCTION,
    WINDOW_SCHEMA,
    FrameCache,
    build_viewer_payload,
    words_from_transcript,
)
from directorloop.creative.signals import VideoSignals, audio_rms  # noqa: E402
from directorloop.domain.ids import new_id, sha256_file, utc_now_iso  # noqa: E402
from directorloop.jobs.worker import safe_failure  # noqa: E402
from directorloop.media.frames import SampledFrame  # noqa: E402
from directorloop.media.probe import inspect_media  # noqa: E402
from directorloop.media.transcribe import Transcript, TranscriptSegment, TranscriptToken  # noqa: E402
from directorloop.observability.weave_ops import current_call_ref, traced  # noqa: E402
from directorloop.providers.base import ProbeMedia  # noqa: E402

DEFAULT_BATCH = REPO / 'data' / 'sponsor-pilot' / 'analysis'
DEFAULT_STAGING = REPO / 'data' / 'sponsor-pilot' / 'staging'
DEFAULT_OUTPUT = REPO / 'data' / 'sponsor-pilot' / 'output'
EVIDENCE_LABEL = 'screen_model_judgment; not validated retention or predicted views'
PAYLOAD_NODES = (
    'WINDOW_SCHEMA', 'WINDOW_INSTRUCTION', 'CONTEXT_FPS', 'CURRENT_FPS', 'CONTEXT_WIDTH',
    'CURRENT_WIDTH', 'VIEWER_WORD_LIMIT', '_words_block', '_audio_lines', 'words_heard_by',
    'words_from_transcript', 'build_viewer_payload', 'viewer_frame_times', 'frame_guard_ms', '_times',
)


class ScreeningReply(BaseModel):
    """Validate every required field, rather than quietly replacing malformed output."""

    model_config = ConfigDict(strict=True, extra='allow')
    understanding: str
    expectation: str
    open_question: str
    reaction: Literal['engaged', 'neutral', 'losing_interest', 'confused']
    attention_risk: Literal['low', 'medium', 'high']
    cause: str
    payoff: Literal['none', 'waiting', 'delivered']
    waiting_since_s: float | int | None


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def freeze_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(json_bytes(value))
        handle.flush()
        os.fsync(handle.fileno())
    return sha256_file(path)


def payload_node_hashes(path: Path) -> dict[str, str]:
    selected = {}
    for node in ast.parse(path.read_text()).body:
        key = getattr(node, 'name', None)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            key = node.target.id
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            key = node.targets[0].id
        if key in PAYLOAD_NODES:
            selected[key] = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
    if set(selected) != set(PAYLOAD_NODES):
        raise ValueError('Cannot verify the complete frozen prefix payload builder')
    return selected


def select_sources(batch: Path, staging: Path) -> list[dict[str, Any]]:
    """First three distinct completed media hashes by staged filename; never open info.json."""
    completed = {}
    for path in sorted((batch / 'results').glob('*.json')):
        row = json.loads(path.read_text())
        if row.get('status') == 'complete':
            completed[row['artifact_sha256']] = {**row, 'receipt_path': str(path)}
    selected, seen = [], set()
    for path in sorted(staging.glob('*.mp4'), key=lambda p: p.name):
        digest = sha256_file(path)
        if digest not in completed or digest in seen:
            continue
        row = completed[digest]
        selected.append({
            'staged_identity': path.name, 'staged_path': str(path), 'source_sha256': digest,
            'audit_path': row['audit_path'], 'baseline_receipt_path': row['receipt_path'],
            'baseline_receipt_sha256': sha256_file(Path(row['receipt_path'])),
        })
        seen.add(digest)
        if len(selected) == 3:
            return selected
    raise ValueError('Need three distinct complete baseline clips present in staging')


def load_transcript(path: Path) -> Transcript:
    value = json.loads(path.read_text())
    value['segments'] = [TranscriptSegment(**{**segment, 'tokens': tuple(TranscriptToken(**t) for t in segment.get('tokens', []))})
                         for segment in value['segments']]
    return Transcript(**value)


def freeze_case(run_dir: Path, case_id: str, payload: Any, baseline: dict[str, Any] | None,
                source_sha256: str, *, control: bool = False) -> dict[str, Any]:
    frames = []
    if not control:
        for index, frame in enumerate(payload.media.frames):
            relative = Path('frames') / f'{case_id}-{index:03d}.jpg'
            target = run_dir / relative
            target.parent.mkdir(exist_ok=True)
            with target.open('xb') as handle:
                handle.write(frame.jpeg)
            frames.append({'path': str(relative), 'sha256': sha256_file(target), 'timestamp_ms': frame.timestamp_ms,
                           'width': frame.width, 'height': frame.height})
    instruction = payload.instruction
    if control:
        instruction = WINDOW_INSTRUCTION.format(start=0, end=payload.window.end_ms / 1000,
                                                words='(withheld: no transcript supplied)',
                                                audio='(withheld: no audio measurements supplied)')
        instruction += '\nCONTROL: No images, video, transcript or audio were supplied. State what cannot be determined; do not invent video content.'
    row = {
        'case_id': case_id, 'source_sha256': source_sha256, 'control': control,
        'media_kind': 'none' if control else 'frames', 'duration_ms': payload.window.end_ms,
        'frames': frames, 'instruction': instruction,
        'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
        'window': payload.window.model_dump(mode='json') if not control else None,
        'recorded_baseline': baseline, 'baseline_mode': 'recorded' if baseline else None,
    }
    relative = Path('cases') / f'{case_id}.json'
    return {'path': str(relative), 'sha256': freeze_json(run_dir / relative, row), 'case_id': case_id, 'control': control}


def prepare(run_dir: Path, batch: Path, staging: Path, *, model: str, max_calls: int = 10,
            max_output_tokens: int = 2048, disable_thinking: bool = False) -> dict[str, Any]:
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError('Preparation requires a new empty directory; frozen pilots are never overwritten')
    if not model or max_calls != 10 or not 128 <= max_output_tokens <= 4096:
        raise ValueError('Pilot needs a named model, exactly ten allowed calls, and 128..4096 output tokens')
    run_dir.mkdir(parents=True, exist_ok=True)
    current_payload = payload_node_hashes(REPO / 'directorloop/audit/review.py')
    if current_payload != payload_node_hashes(batch / 'snapshot/directorloop/audit/review.py'):
        raise ValueError('Current prefix payload differs from the frozen recorded baseline')
    selected = select_sources(batch, staging)
    case_files = []
    for index, source in enumerate(selected):
        source_path = Path(source['staged_path'])
        report_path = Path(source['audit_path'])
        report = json.loads(report_path.read_text())
        chronological = report_path.parent / 'chronological_reactions.json'
        reactions = json.loads(chronological.read_text())
        if report['status'] != 'complete' or report['artifact_hash'] != source['source_sha256']:
            raise ValueError('Baseline is incomplete or does not match source media')
        transcript_paths = sorted((batch / 'preprocessing' / source['source_sha256']).glob('*-transcript.json'))
        if len(transcript_paths) != 1:
            raise ValueError('Ambiguous or missing frozen ASR receipt')
        transcript_path = transcript_paths[0]
        transcript = load_transcript(transcript_path)
        info = inspect_media(source_path)
        signals = VideoSignals(report['duration_ms'], info.width, info.height, [], [], audio_rms(source_path))
        frames = FrameCache(source_path, report['duration_ms'])
        coarse = reactions['coarse_reactions']
        chosen = [next(r for r in coarse if r['end_ms'] == end) for end in (2000, 4000)] + [coarse[-1]]
        if len({r['end_ms'] for r in chosen}) != 3:
            raise ValueError('Selected clip cannot provide three distinct chronological windows')
        for window_index, reaction in enumerate(chosen):
            if reaction.get('error'):
                raise ValueError('Recorded comparison window has an error')
            payload = build_viewer_payload(frames, signals, words_from_transcript(transcript),
                                           reaction['start_ms'], reaction['end_ms'], fps=info.fps)
            expected = next(w for w in report['coverage']['windows'] if w['kind'] == 'prefix'
                            and w['start_ms'] == reaction['start_ms'] and w['end_ms'] == reaction['end_ms'])
            if payload.window.model_dump(mode='json') != expected:
                raise ValueError('Rebuilt input sampling does not match the recorded baseline window')
            case_id = f'clip-{index + 1}-prefix-{reaction["end_ms"]:06d}'
            case_files.append(freeze_case(run_dir, case_id, payload, reaction, source['source_sha256']))
            if index == 0 and window_index == 0:
                control_case = freeze_case(run_dir, 'control-no-media', payload, None, source['source_sha256'], control=True)
        source.update({'duration_ms': report['duration_ms'], 'audit_sha256': sha256_file(report_path),
                       'chronological_path': str(chronological), 'chronological_sha256': sha256_file(chronological),
                       'transcript_path': str(transcript_path), 'transcript_sha256': sha256_file(transcript_path),
                       'baseline_evaluator': reactions['evaluator']})
    case_files.append(control_case)
    code_files = sorted((REPO / 'directorloop').rglob('*.py')) + [Path(__file__)]
    manifest = {
        'schema': 'directorloop-sponsor-screening-v1', 'created_at': utc_now_iso(), 'mode': 'prepared',
        'evidence_label': EVIDENCE_LABEL, 'selection_rule': 'first three distinct completed source hashes in staged filename order',
        'platform_outcomes_opened': False, 'automatic_model_promotion': False,
        'provider': 'wandb_inference', 'model': model, 'endpoint': 'https://api.inference.wandb.ai/v1',
        'max_calls': max_calls, 'max_output_tokens': max_output_tokens, 'concurrency': 1,
        'enable_thinking': False if disable_thinking else None,
        'sdk_retries': 0, 'schema_fallback': False, 'stop_on_first_failure': True,
        'sources': selected, 'cases': case_files, 'schema_sha256': hashlib.sha256(json_bytes(WINDOW_SCHEMA)).hexdigest(),
        'payload_node_hashes': current_payload, 'code_hashes': {str(p.relative_to(REPO)): sha256_file(p) for p in code_files},
        'dependencies': {name: importlib.metadata.version(name) for name in ('openai', 'weave', 'pydantic', 'numpy')},
        'comparison_note': 'Descriptive agreement with nine recorded Terra prefix readings; no human outcomes or accuracy claim. Frame extraction rebuilt from identical media and matching timestamps, not proof of byte-identical historical API payloads.',
        'grounding_note': 'Outputs preserve content-specific understanding and cause for review. Content grounding is not automatically verified; no-media control is inspected separately.',
    }
    freeze_json(run_dir / 'manifest.json', manifest)
    return manifest


def verify_inputs(run_dir: Path, manifest: dict[str, Any], *, verify_code: bool = True) -> None:
    for name, version in manifest.get('dependencies', {}).items():
        if importlib.metadata.version(name) != version:
            raise ValueError('Dependency changed after pilot freeze')
    if verify_code:
        for relative, digest in manifest['code_hashes'].items():
            if sha256_file(REPO / relative) != digest:
                raise ValueError('Code changed after pilot freeze; prepare a new run')
    for source in manifest['sources']:
        for path_key, hash_key in [('staged_path', 'source_sha256'), ('audit_path', 'audit_sha256'),
                                  ('chronological_path', 'chronological_sha256'), ('transcript_path', 'transcript_sha256'),
                                  ('baseline_receipt_path', 'baseline_receipt_sha256')]:
            if sha256_file(Path(source[path_key])) != source[hash_key]:
                raise ValueError('Source or baseline changed after pilot freeze')
    for descriptor in manifest['cases']:
        path = run_dir / descriptor['path']
        if sha256_file(path) != descriptor['sha256']:
            raise ValueError('Pilot case changed after freeze')
        case = json.loads(path.read_text())
        for frame in case['frames']:
            if sha256_file(run_dir / frame['path']) != frame['sha256']:
                raise ValueError('Pilot frame changed after freeze')


def prepare_remaining(source_run: Path, run_dir: Path, *, model: str, max_calls: int,
                      max_output_tokens: int, disable_thinking: bool,
                      ledger_path: Path = DEFAULT_OUTPUT / 'spend-ledger.db') -> dict[str, Any]:
    """New protocol, only genuinely unattempted cases; never replay a paid attempt."""
    source_manifest = json.loads((source_run / 'manifest.json').read_text())
    receipt = json.loads((source_run / 'comparison-receipt.json').read_text())
    if receipt['manifest_sha256'] != sha256_file(source_run / 'manifest.json'):
        raise ValueError('Source execution receipt does not match its frozen manifest')
    verify_inputs(source_run, source_manifest, verify_code=False)
    if model != source_manifest['model'] or max_calls != source_manifest['max_calls'] or max_output_tokens != source_manifest['max_output_tokens']:
        raise ValueError('Continuation preserves model, physical call ceiling and output token cap')
    if not disable_thinking:
        raise ValueError('This continuation requires the explicitly documented reasoning-disabled protocol')
    budget = receipt.get('spend_guard')
    if not budget or budget['max_physical_attempts'] != max_calls:
        raise ValueError('Continuation requires the original physical-request budget receipt')
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError('Continuation requires a new empty directory')
    if source_manifest.get('payload_node_hashes') != payload_node_hashes(REPO / 'directorloop/audit/review.py'):
        raise ValueError('Original prefix payload changed; cannot continue remaining cases')
    rows = {r['case_id']: r for r in receipt['results']}
    if set(rows) != {c['case_id'] for c in source_manifest['cases']}:
        raise ValueError('Source execution does not account for every frozen case')
    descriptors = [c for c in source_manifest['cases'] if rows[c['case_id']]['status'] == 'not_attempted']
    descriptors.sort(key=lambda c: (not c['control'], c['case_id']))
    if not descriptors:
        raise ValueError('No unattempted cases remain')
    run_dir.mkdir(parents=True, exist_ok=True)
    for descriptor in descriptors:
        case_path = source_run / descriptor['path']
        case = json.loads(case_path.read_text())
        for relative in [descriptor['path']] + [f['path'] for f in case['frames']]:
            target = run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as handle:
                handle.write((source_run / relative).read_bytes())
    code_files = sorted((REPO / 'directorloop').rglob('*.py')) + [Path(__file__)]
    manifest = {
        **source_manifest, 'created_at': utc_now_iso(), 'mode': 'prepared', 'cases': descriptors,
        'enable_thinking': False, 'code_hashes': {str(p.relative_to(REPO)): sha256_file(p) for p in code_files},
        'derived_from': {'run_dir': str(source_run), 'manifest_sha256': sha256_file(source_run / 'manifest.json'),
                         'receipt_sha256': sha256_file(source_run / 'comparison-receipt.json'),
                         'excluded_attempted_cases': [{'case_id': r['case_id'], 'status': r['status']} for r in receipt['results'] if r['status'] != 'not_attempted']},
        'protocol_change': {'change': 'Qwen thinking disabled; remaining unattempted cases only, with no-media control first',
                            'reason': 'Prior late-prefix request exhausted 2048 completion tokens with no answer; no completed or failed case is retried',
                            'documentation': 'https://docs.wandb.ai/inference/response-settings/reasoning',
                            'same_shared_budget_required': True},
        'shared_budget': {'ledger_path': str(ledger_path.resolve()), 'budget_id': budget['budget_id'],
                          'limit_usd': budget['limit_usd'], 'max_physical_attempts': budget['max_physical_attempts'],
                          'prior_physical_attempts': budget['physical_attempts']},
        'comparison_note': 'Descriptive agreement with recorded Terra prefix readings. This is a separately frozen reasoning-disabled screening protocol; results must not be pooled as one unchanged evaluator or human accuracy.',
    }
    freeze_json(run_dir / 'manifest.json', manifest)
    return manifest


def load_case_media(run_dir: Path, case: dict[str, Any]) -> ProbeMedia:
    return ProbeMedia(kind=case['media_kind'], duration_ms=case['duration_ms'], transcript=None,
                      frames=[SampledFrame(timestamp_ms=f['timestamp_ms'], jpeg=(run_dir / f['path']).read_bytes(),
                                           width=f['width'], height=f['height']) for f in case['frames']])


@traced('directorloop.screening.prefix', kind='agent', display=lambda i: f"Screen {i['case']['case_id']}")
def evaluate_case(provider: Any, run_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    result = provider.judge_json(load_case_media(run_dir, case), case['instruction'], WINDOW_SCHEMA)
    try:
        reply = ScreeningReply.model_validate(result.data)
        since = reply.waiting_since_s
        if since is not None and (not math.isfinite(since) or not 0 <= since * 1000 <= case['duration_ms']):
            raise ValueError('Model waiting timestamp crossed the visible prefix boundary')
        if reply.payoff != 'waiting' and since is not None:
            raise ValueError('Non-waiting model response supplied a waiting timestamp')
    except ValueError:
        return {'case_id': case['case_id'], 'status': 'failed', 'error': 'screening response failed schema or prefix-boundary validation',
                'schema_valid': False, 'returned_usage_available': result.input_tokens is not None and result.output_tokens is not None,
                'input_tokens': result.input_tokens, 'output_tokens': result.output_tokens, 'latency_ms': result.latency_ms}
    baseline = case['recorded_baseline']
    return {'case_id': case['case_id'], 'status': 'complete', 'mode': 'fresh', 'control': case['control'],
            'evidence_label': EVIDENCE_LABEL, 'judgment': reply.model_dump(),
            'schema_valid': True, 'has_observation_and_cause': bool(reply.understanding.strip() and reply.cause.strip()),
            'content_grounding_verified': False, 'frame_count': len(case['frames']),
            'latency_ms': result.latency_ms, 'input_tokens': result.input_tokens, 'output_tokens': result.output_tokens,
            'recorded_baseline': baseline, 'baseline_mode': 'recorded' if baseline else None,
            'attention_risk_agrees': reply.attention_risk == baseline['attention_risk'] if baseline else None,
            'reaction_agrees': reply.reaction == baseline['reaction'] if baseline else None,
            'weave_call_id': current_call_ref()[0], 'weave_url': current_call_ref()[1]}


@traced('directorloop.sponsor_screening', kind='agent', display='Sponsor screening pilot | 3 clips, 10 bounded calls')
def run_prepared(provider: Any, run_dir: Path, *, manifest: dict[str, Any], max_calls: int) -> dict[str, Any]:
    """Caller supplies an HTTP-guarded provider; stop sequentially on the first failure."""
    verify_inputs(run_dir, manifest)
    if max_calls != manifest['max_calls'] or max_calls < len(manifest['cases']):
        raise ValueError('Execution call limit differs from frozen pilot')
    freeze_json(run_dir / 'execution-started.json', {'started_at': utc_now_iso(), 'manifest_sha256': sha256_file(run_dir / 'manifest.json')})
    rows, failure = [], None
    for descriptor in manifest['cases']:
        if failure:
            rows.append({'case_id': descriptor['case_id'], 'status': 'not_attempted', 'reason': 'stopped after first failure'})
            continue
        case = json.loads((run_dir / descriptor['path']).read_text())
        try:
            row = evaluate_case(provider, run_dir, case)
            if row['status'] == 'failed':
                failure = row['error']
        except Exception as exc:  # noqa: BLE001 - first failure is terminal and persisted safely
            failure = safe_failure(exc)
            row = {'case_id': case['case_id'], 'status': 'failed', 'error': failure,
                   'returned_usage_available': False, 'failed_request_billed_usage': 'unknown'}
        freeze_json(run_dir / 'results' / f'{case["case_id"]}.json', row)
        rows.append(row)
        print(json.dumps({'case_id': row['case_id'], 'status': row['status'], 'checkpoint': str(run_dir / 'results' / f'{case["case_id"]}.json')}), flush=True)
    completed = [r for r in rows if r['status'] == 'complete']
    media_rows = [r for r in completed if not r['control']]
    receipt = {
        'status': 'failed' if failure else 'complete', 'ended_at': utc_now_iso(), 'error': failure,
        'evidence_label': EVIDENCE_LABEL, 'manifest_sha256': sha256_file(run_dir / 'manifest.json'),
        'completed_calls': len(completed), 'failed_calls': sum(r['status'] == 'failed' for r in rows),
        'not_attempted_calls': sum(r['status'] == 'not_attempted' for r in rows), 'results': rows,
        'descriptive_agreement': {'compared_windows': len(media_rows),
                                  'attention_risk_matches': sum(r['attention_risk_agrees'] for r in media_rows),
                                  'reaction_matches': sum(r['reaction_agrees'] for r in media_rows),
                                  'accuracy_against_humans': None, 'measured_retention': None},
        'model_promotion': 'not_performed', 'platform_outcomes_opened': False,
        'input_tokens': sum(r['input_tokens'] for r in completed) if all(r['input_tokens'] is not None for r in completed) and completed else None,
        'output_tokens': sum(r['output_tokens'] for r in completed) if all(r['output_tokens'] is not None for r in completed) and completed else None,
        'cost_usd': None, 'cost_note': 'Consult guarded provider ledger; this receipt is not an invoice',
        'weave_call_id': current_call_ref()[0], 'weave_url': current_call_ref()[1],
    }
    guard = getattr(provider, 'spend_guard', None)
    if guard is not None:
        receipt['spend_guard'] = guard.ledger.summary()
    freeze_json(run_dir / 'comparison-receipt.json', receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--batch-dir', type=Path, default=DEFAULT_BATCH)
    parser.add_argument('--staging-dir', type=Path, default=DEFAULT_STAGING)
    parser.add_argument('--model', required=True)
    parser.add_argument('--max-calls', type=int, default=10)
    parser.add_argument('--max-output-tokens', type=int, default=2048)
    parser.add_argument('--disable-thinking', action='store_true', help='Freeze documented Qwen reasoning-disabled request setting')
    parser.add_argument('--derive-remaining-from', type=Path, help='Prepare a new protocol containing only an earlier pilot\'s unattempted cases')
    parser.add_argument('--execute', action='store_true', help='Run an already frozen pilot through the guarded sponsor provider')
    parser.add_argument('--ledger', type=Path, default=DEFAULT_OUTPUT / 'spend-ledger.db')
    parser.add_argument('--budget-id', default='sponsor-pilot-20260912')
    parser.add_argument('--spend-cap-usd', default='2')
    args = parser.parse_args()
    run_dir = args.run_dir or DEFAULT_OUTPUT / new_id('pilot')
    if args.execute:
        from directorloop.config import get_settings
        from directorloop.observability.weave_ops import init_weave
        from directorloop.providers.openai_compat import OpenAICompatProvider
        from directorloop.providers.spend import SpendGuard, SpendLedger

        manifest = json.loads((run_dir / 'manifest.json').read_text())
        if args.model != manifest['model'] or args.max_output_tokens != manifest['max_output_tokens']:
            raise ValueError('Model/output cap differs from frozen pilot')
        if (False if args.disable_thinking else None) != manifest.get('enable_thinking'):
            raise ValueError('Thinking mode differs from frozen pilot')
        verify_inputs(run_dir, manifest)
        settings = get_settings()
        tracing = init_weave(settings)
        if not tracing.connected:
            raise ValueError('Pilot requires working Weave tracing before provider execution')
        ledger = SpendLedger(args.ledger, budget_id=args.budget_id, limit_usd=args.spend_cap_usd, max_attempts=args.max_calls)
        if manifest.get('shared_budget'):
            expected = manifest['shared_budget']
            current = ledger.summary()
            if (str(args.ledger.resolve()) != expected['ledger_path'] or args.budget_id != expected['budget_id']
                    or current['limit_usd'] != expected['limit_usd']
                    or current['physical_attempts'] < expected['prior_physical_attempts']):
                raise ValueError('Continuation must use the original shared ledger and previous attempt history')
        provider = OpenAICompatProvider(
            name='wandb_inference', api_key=settings.wandb_api_key, model=args.model,
            base_url=manifest['endpoint'], project=settings.weave_project_path(), vision=True,
            max_output_tokens=args.max_output_tokens, allow_compatibility_fallback=False,
            enable_thinking=manifest.get('enable_thinking'),
            spend_guard=SpendGuard(ledger, max_output_tokens=args.max_output_tokens),
        )
        receipt = run_prepared(provider, run_dir, manifest=manifest, max_calls=args.max_calls)
        import weave

        weave.finish()
        print(json.dumps({k: receipt[k] for k in ('status', 'completed_calls', 'failed_calls', 'not_attempted_calls', 'weave_url')}))
        return 0 if receipt['status'] == 'complete' else 1
    if args.derive_remaining_from:
        manifest = prepare_remaining(args.derive_remaining_from, run_dir, model=args.model,
                                     max_calls=args.max_calls, max_output_tokens=args.max_output_tokens,
                                     disable_thinking=args.disable_thinking, ledger_path=args.ledger)
    else:
        manifest = prepare(run_dir, args.batch_dir, args.staging_dir, model=args.model,
                           max_calls=args.max_calls, max_output_tokens=args.max_output_tokens, disable_thinking=args.disable_thinking)
    print(json.dumps({'status': 'prepared', 'run_dir': str(run_dir), 'cases': len(manifest['cases']), 'provider_calls': 0}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
