"""Read-time boundary flags cannot rewrite records or prematurely finish jobs."""
import pytest

from directorloop.domain.ids import utc_now_iso
from directorloop.screening import runner as R
from directorloop.screening.models import ScreenJudgment, ScreenReport, ScreenWindow
from tests.unit.test_api_screening import screening_client as screening_client
from tests.unit.test_screening_citation_guards import judgment_data


@pytest.mark.parametrize('status', ['complete','running'])
def test_existing_report_boundary_flags_preserve_raw_evidence_and_job_state(screening_client, status):
    client, services, source, _ = screening_client
    judgment=judgment_data(timestamps=[1000],kind='visible_fact',attention='medium')
    judgment['suggestion']='The video ends mid-sentence before the solution.'
    report=ScreenReport(id='screen_abc_def',video_id='source-one',artifact_path=str(source),duration_ms=7000,
        created_at=utc_now_iso(),status=status,protocol={'coverage':'full'},
        windows=[ScreenWindow(start_ms=0,end_ms=2000,status='complete',judgment=ScreenJudgment.model_validate(judgment),raw_output=judgment)])
    R.save_screening(services.data,report)
    path=services.data/'screenings'/'screen_abc_def.json'
    before=path.read_bytes()
    response=client.get('/api/screenings/screen_abc_def')
    assert response.status_code==200, response.text
    result=response.json()
    assert result['status']==('needs_review' if status=='complete' else 'running')
    assert result['recorded_status']==status
    window=result['windows'][0]
    assert window['status']=='needs_review' and window['recorded_status']=='complete'
    assert window['judgment']['attention_risk']=='medium'
    assert window['raw_output']==judgment
    assert any('Intermediate checkpoint' in issue for issue in window['validation_issues'])
    assert window['display_reason']=='The video continues past this checkpoint. This finding needs review.'
    assert result['view_validation']['semantic_grounding_verified'] is False
    assert path.read_bytes()==before
    assert services.screening_ledger.summary()['physical_attempts']==0
