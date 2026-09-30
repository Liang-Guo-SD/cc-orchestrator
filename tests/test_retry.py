from cc_orchestrator.retry import build_corrective_prompt, RetryAttempt
from cc_orchestrator.models import Claims, VerificationTarget


def _make_attempt(run_id: str, pattern: str, actual: str) -> RetryAttempt:
    vt = VerificationTarget(
        command="pytest -q 2>&1 | tail -1",
        expected_pattern=pattern,
        description="suite green",
    )
    claims = Claims(exit_code=1, tests_failed=3)
    return RetryAttempt(run_id=run_id, claims=claims, failed_targets=[(vt, actual)])


def test_corrective_prompt_contains_original_task():
    original = "Run verification suite and confirm all tests pass."
    attempt = _make_attempt("run-001", "222 passed", "3 failed, 219 passed")
    prompt = build_corrective_prompt(original, [attempt])
    assert original in prompt


def test_corrective_prompt_contains_discrepancy():
    original = "Run verification suite."
    attempt = _make_attempt("run-001", "222 passed", "3 failed, 219 passed")
    prompt = build_corrective_prompt(original, [attempt])
    assert "222 passed" in prompt   # expected
    assert "3 failed" in prompt     # actual


def test_corrective_prompt_includes_all_prior_attempts():
    original = "Run verification suite."
    attempts = [
        _make_attempt("run-001", "222 passed", "3 failed"),
        _make_attempt("run-002", "222 passed", "1 failed"),
    ]
    prompt = build_corrective_prompt(original, attempts)
    assert "run-001" in prompt
    assert "run-002" in prompt


def test_corrective_prompt_instructs_new_session():
    original = "Run tests."
    attempt = _make_attempt("run-001", "0 failed", "2 failed")
    prompt = build_corrective_prompt(original, [attempt])
    # Prompt should note this is a fresh context attempt
    assert "fresh" in prompt.lower() or "new" in prompt.lower()
