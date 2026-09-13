from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from directorloop.audit.models import InspectedWindow
from directorloop.providers.base import CompletionResult, ProbeMedia, ProviderError

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/sponsor_screening_pilot.py'
spec = importlib.util.spec_from_file_location('sponsor_screening_pilot', SCRIPT)
pilot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pilot
spec.loader.exec_module(pilot)


def valid_reply(**overrides):
    return {'understanding': 'A hand points at a green cup.', 'expectation': 'The cup will be used.',
            'open_question': 'What is in it?', 'reaction': 'engaged', 'attention_risk': 'low',
            'cause': 'The green cup is centered in the frame.', 'payoff': 'waiting', 'waiting_since_s': 0.5,
            **overrides}


def make_run(tmp_path, count=3):
    cases = []
    for index in range(count):
        row = {'case_id': f'case-{index}', 'media_kind': 'none', 'duration_ms': 2000,
               'frames': [], 'instruction': 'Describe only supplied media.',
               'recorded_baseline': valid_reply(), 'control': False}
        path = tmp_path / f'case-{index}.json'
        cases.append({'path': path.name, 'sha256': pilot.freeze_json(path, row), 'case_id': row['case_id'], 'control': False})
    manifest = {'code_hashes': {}, 'sources': [], 'cases': cases, 'max_calls': count}
    pilot.freeze_json(tmp_path / 'manifest.json', manifest)
    return manifest


def test_selection_uses_staged_identity_and_complete_receipts_without_metadata(tmp_path):
    staging = tmp_path / 'staging'
    batch = tmp_path / 'batch'
    staging.mkdir()
    (batch / 'results').mkdir(parents=True)
    for name, status in [('c', 'complete'), ('a', 'complete'), ('d', 'complete'), ('b', 'failed')]:
        media = staging / f'{name}.mp4'
        media.write_bytes(name.encode())
        (staging / f'{name}.info.json').write_text('INVALID JSON; outcomes must never be opened')
        pilot.freeze_json(batch / 'results' / f'{name}.json', {'status': status,
                          'artifact_sha256': pilot.sha256_file(media), 'audit_path': f'{name}-audit.json'})
    selected = pilot.select_sources(batch, staging)
    assert [row['staged_identity'] for row in selected] == ['a.mp4', 'c.mp4', 'd.mp4']


def test_no_media_control_withholds_frames_words_and_audio(tmp_path):
    window = InspectedWindow(kind='prefix', start_ms=0, end_ms=2000, frame_timestamps_ms=[], frame_width=448)
    payload = SimpleNamespace(media=ProbeMedia(kind='frames'), instruction='secret transcript detail green cup', window=window)
    descriptor = pilot.freeze_case(tmp_path, 'control', payload, None, 'f' * 64, control=True)
    case = json.loads((tmp_path / descriptor['path']).read_text())
    assert case['media_kind'] == 'none'
    assert case['frames'] == []
    assert case['recorded_baseline'] is None
    assert 'green cup' not in case['instruction']
    assert 'withheld: no transcript supplied' in case['instruction']
    assert 'withheld: no audio measurements supplied' in case['instruction']
    assert pilot.load_case_media(tmp_path, case).transcript is None


def test_schema_rejects_missing_fields_and_invalid_categories():
    with pytest.raises(ValidationError):
        pilot.ScreeningReply.model_validate({'understanding': 'A cup'})
    with pytest.raises(ValidationError):
        pilot.ScreeningReply.model_validate(valid_reply(attention_risk='99%'))
    with pytest.raises(ValidationError):
        pilot.ScreeningReply.model_validate(valid_reply(waiting_since_s=True))


def test_tampered_case_stops_before_provider_call(tmp_path):
    manifest = make_run(tmp_path)
    (tmp_path / 'case-0.json').write_text('{}')
    provider = SimpleNamespace(judge_json=lambda *_: pytest.fail('provider must not be called'))
    with pytest.raises(ValueError, match='changed after freeze'):
        pilot.run_prepared(provider, tmp_path, manifest=manifest, max_calls=3)
    assert not (tmp_path / 'execution-started.json').exists()


def test_quota_failure_stops_all_unscheduled_calls(tmp_path):
    manifest = make_run(tmp_path)
    calls = []

    def judge(*args):
        calls.append(args)
        raise ProviderError('provider credits exhausted; no automatic retry')

    receipt = pilot.run_prepared(SimpleNamespace(judge_json=judge), tmp_path, manifest=manifest, max_calls=3)
    assert len(calls) == 1
    assert receipt['failed_calls'] == 1
    assert receipt['not_attempted_calls'] == 2
    assert receipt['completed_calls'] == 0
    assert receipt['input_tokens'] is None
    assert receipt['platform_outcomes_opened'] is False
    assert 'credits exhausted' in receipt['error']
    assert (tmp_path / 'results/case-0.json').is_file()


def test_complete_receipt_describes_recorded_model_agreement_only(tmp_path):
    manifest = make_run(tmp_path)
    provider = SimpleNamespace(judge_json=lambda *_: CompletionResult(data=valid_reply(), latency_ms=12,
                               model='sponsor-test', input_tokens=100, output_tokens=20))
    receipt = pilot.run_prepared(provider, tmp_path, manifest=manifest, max_calls=3)
    assert receipt['status'] == 'complete'
    assert receipt['completed_calls'] == 3
    assert receipt['input_tokens'] == 300
    assert receipt['descriptive_agreement']['attention_risk_matches'] == 3
    assert receipt['descriptive_agreement']['accuracy_against_humans'] is None
    assert receipt['descriptive_agreement']['measured_retention'] is None
    assert receipt['model_promotion'] == 'not_performed'
    assert all(r['baseline_mode'] == 'recorded' and r['content_grounding_verified'] is False for r in receipt['results'])
    with pytest.raises(FileExistsError):
        pilot.run_prepared(provider, tmp_path, manifest=manifest, max_calls=3)


def test_future_waiting_timestamp_fails_without_extra_calls(tmp_path):
    manifest = make_run(tmp_path)
    calls = []

    def judge(*args):
        calls.append(args)
        return CompletionResult(data=valid_reply(waiting_since_s=3), latency_ms=1, model='test')

    receipt = pilot.run_prepared(SimpleNamespace(judge_json=judge), tmp_path, manifest=manifest, max_calls=3)
    assert len(calls) == 1
    assert receipt['status'] == 'failed'


def test_payload_parity_requires_all_frozen_nodes(tmp_path):
    source = tmp_path / 'bad.py'
    source.write_text('WINDOW_SCHEMA = {}\n')
    with pytest.raises(ValueError, match='complete frozen prefix'):
        pilot.payload_node_hashes(source)


def make_continuation_source(tmp_path):
    source = tmp_path / 'v1'
    source.mkdir()
    manifest = make_run(source, count=4)
    manifest.update({'model': 'Qwen/test', 'max_output_tokens': 2048,
                     'payload_node_hashes': pilot.payload_node_hashes(pilot.REPO / 'directorloop/audit/review.py')})
    manifest['cases'][3]['control'] = True
    (source / 'manifest.json').write_bytes(pilot.json_bytes(manifest))
    receipt = {'manifest_sha256': pilot.sha256_file(source / 'manifest.json'),
               'spend_guard': {'budget_id': 'shared-test', 'limit_usd': 2.0, 'max_physical_attempts': 4, 'physical_attempts': 2},
               'results': [{'case_id': f'case-{i}', 'status': status} for i, status in enumerate(
                   ['complete', 'failed', 'not_attempted', 'not_attempted'])]}
    pilot.freeze_json(source / 'comparison-receipt.json', receipt)
    return source


def test_continuation_only_unattempted_control_first_with_explicit_protocol(tmp_path):
    source = make_continuation_source(tmp_path)
    source_hash = pilot.sha256_file(source / 'manifest.json')
    remaining = pilot.prepare_remaining(source, tmp_path / 'v2', model='Qwen/test', max_calls=4,
                                         max_output_tokens=2048, disable_thinking=True)
    assert [case['case_id'] for case in remaining['cases']] == ['case-3', 'case-2']
    assert remaining['enable_thinking'] is False
    assert remaining['protocol_change']['same_shared_budget_required'] is True
    assert remaining['shared_budget']['prior_physical_attempts'] == 2
    assert remaining['shared_budget']['budget_id'] == 'shared-test'
    assert [r['status'] for r in remaining['derived_from']['excluded_attempted_cases']] == ['complete', 'failed']
    assert pilot.sha256_file(source / 'manifest.json') == source_hash
    assert not (tmp_path / 'v2' / 'case-0.json').exists()
    pilot.verify_inputs(tmp_path / 'v2', remaining)


def test_continuation_rejects_silent_protocol_change(tmp_path):
    source = make_continuation_source(tmp_path)
    with pytest.raises(ValueError, match='explicitly documented'):
        pilot.prepare_remaining(source, tmp_path / 'v2', model='Qwen/test', max_calls=4,
                                max_output_tokens=2048, disable_thinking=False)
    with pytest.raises(ValueError, match='preserves model'):
        pilot.prepare_remaining(source, tmp_path / 'v2', model='Qwen/different', max_calls=4,
                                max_output_tokens=2048, disable_thinking=True)
