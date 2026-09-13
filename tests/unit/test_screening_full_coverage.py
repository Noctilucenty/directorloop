from types import SimpleNamespace

import numpy as np
import pytest

from directorloop.audit.review import Word
from directorloop.creative.signals import VideoSignals
from directorloop.screening import runner
from tests.unit.test_screening import Frames, Provider


@pytest.mark.parametrize('duration',[1,1500,4000,6000,31637,60000,180000])
def test_full_coverage_is_contiguous_bounded_and_exact(duration):
    windows=runner.screening_windows(duration,'full')
    assert windows[0][0]==0 and windows[-1][1]==duration
    assert len(windows)<=8
    assert all(a<b for a,b in windows)
    assert all(left[1]==right[0] for left,right in zip(windows,windows[1:],strict=False))
    assert sum(b-a for a,b in windows)==duration


def test_full_payload_has_current_and_earlier_frames_without_future_words():
    words=[Word(f'w{i}',i*500,(i+1)*500) for i in range(360)]
    signals=VideoSignals(180000,448,796,[],[],np.zeros(0))
    payload=runner._full_payload(Frames(None,180000),signals,words,100000,130000,fps=30)
    assert len(payload.media.frames)<=24
    assert all(f.timestamp_ms<130000 for f in payload.media.frames)
    assert payload.window.transcript_words==260
    assert 'w0' in payload.instruction and 'w259' in payload.instruction
    assert 'w260' not in payload.instruction
    current=[f for f in payload.media.frames if f.width==448]
    assert current[0].timestamp_ms<102000 and current[-1].timestamp_ms>128000


def test_full_run_keeps_evidence_and_protocol_and_stops_on_failure(tmp_path,monkeypatch):
    source=tmp_path/'source.mp4'
    source.write_bytes(b'fake local media')
    monkeypatch.setattr(runner,'inspect_media',lambda _:SimpleNamespace(duration_ms=31637,fps=30,width=448,height=796,has_audio=False))
    monkeypatch.setattr(runner,'FrameCache',Frames)
    provider=Provider()
    report=runner.run_screening('v',source,provider,tmp_path/'data',coverage='full')
    assert report.status=='needs_review' and report.model_calls==8
    assert all('checklist is incomplete' in w.validation_issues[-1] for w in report.windows)
    assert report.protocol['coverage']=='full'
    assert report.protocol['max_prefix_calls']==8
    assert report.windows[0].start_ms==0 and report.windows[-1].end_ms==31637
    assert all(len(w.frame_timestamps_ms)<=24 for w in report.windows)
    assert not report.automatic_edit_allowed and not report.semantic_grounding_verified
    assert 'Every timeline section' in report.limitations[0]
    # Concurrent requests may start out of order; each media prefix owns its cutoff.
    assert sorted(media.duration_ms for media,_,_ in provider.calls)==[w.end_ms for w in report.windows]
    assert all(f.timestamp_ms<media.duration_ms for media,_,_ in provider.calls for f in media.frames)


def test_review_dimensions_do_not_claim_unobserved_audio_or_captions():
    from directorloop.screening.models import ScreenJudgment
    from tests.unit.test_screening import reply
    data=reply(166,review_checks=[
        {'aspect':'voice_delivery','status':'clear','reason':'Confident voice','observation_indices':[0]},
        {'aspect':'caption_readability','status':'clear','reason':'Readable text','observation_indices':[0]},
    ])
    issues=runner.validate_evidence(ScreenJudgment.model_validate(data),[166],'')
    assert any('native audio was not supplied' in v for v in issues)
    assert any('readable caption evidence missing' in v for v in issues)


def test_full_schema_requires_all_dimensions_without_changing_quick():
    assert 'review_checks' in runner.FULL_SCREENING_SCHEMA['required']
    assert runner.FULL_SCREENING_SCHEMA['properties']['review_checks']['minItems']==8
    assert 'review_checks' not in runner.SCREENING_SCHEMA['required']
