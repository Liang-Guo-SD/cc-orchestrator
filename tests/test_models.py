from cc_orchestrator.models import Claims, VerificationTarget, RunResult
from cc_orchestrator.workers.base import AdapterResult


def test_claims_defaults_all_none():
    c = Claims(exit_code=0)
    assert c.tests_passed is None
    assert c.tests_failed is None
    assert c.tests_error is None
    assert c.files_changed is None
    assert c.commit_hash is None
    assert c.important_findings == []


def test_claims_can_be_fully_populated():
    c = Claims(
        tests_passed=222,
        tests_failed=0,
        tests_error=0,
        files_changed=["app/foo.py"],
        commit_hash="abc1234",
        exit_code=0,
        important_findings=["all green"],
    )
    assert c.tests_passed == 222
    assert c.commit_hash == "abc1234"


def test_verification_target_fields():
    vt = VerificationTarget(
        command="pytest -q 2>&1 | tail -1",
        expected_pattern="222 passed",
        description="suite green",
    )
    assert vt.command.startswith("pytest")
    assert "222" in vt.expected_pattern


def test_run_result_parent_run_id_defaults_none():
    from cc_orchestrator.models import Claims
    r = RunResult(
        run_id="run-001",
        agent="pi",
        status="completed",
        summary="all good",
        claims=Claims(exit_code=0),
        verification_targets=[],
        raw_output_path="/tmp/run-001/output.txt",
    )
    assert r.parent_run_id is None


def test_adapter_result_fields():
    ar = AdapterResult(exit_code=0, session_id="sess-abc")
    assert ar.exit_code == 0
    assert ar.session_id == "sess-abc"
