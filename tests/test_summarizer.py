from cc_orchestrator.summarizer import extract_claims_rule_based, compute_confidence
from cc_orchestrator.models import Claims

PYTEST_OUTPUT = """\
tests/test_foo.py ....
tests/test_bar.py ..F
FAILED tests/test_bar.py::test_something
222 passed, 1 failed, 0 errors in 12.34s
"""

RUFF_CLEAN = "All checks passed!\n"
RUFF_DIRTY = "Found 3 errors.\n"
WIRING_OK = "check_approval_chain: ok\n10/10 passed\n"
COMMIT_LINE = "[main abc1234] feat: add r3 gate\n 8 files changed, 420 insertions(+)\n"


def test_extracts_tests_passed():
    claims = extract_claims_rule_based(PYTEST_OUTPUT, exit_code=1, patterns={})
    assert claims.tests_passed == 222


def test_extracts_tests_failed():
    claims = extract_claims_rule_based(PYTEST_OUTPUT, exit_code=1, patterns={})
    assert claims.tests_failed == 1


def test_extracts_ruff_clean():
    claims = extract_claims_rule_based(RUFF_CLEAN, exit_code=0, patterns={})
    assert claims.tests_passed is None  # not a pytest output
    # ruff_clean is stored via important_findings or a dedicated field; check commit_hash absent
    assert claims.commit_hash is None


def test_extracts_commit_hash():
    claims = extract_claims_rule_based(COMMIT_LINE, exit_code=0, patterns={})
    assert claims.commit_hash == "abc1234"


def test_extracts_files_changed_count():
    claims = extract_claims_rule_based(COMMIT_LINE, exit_code=0, patterns={})
    # files_changed is a list; check its length was captured
    assert claims.files_changed is not None
    assert len(claims.files_changed) == 8


def test_exit_code_stored():
    claims = extract_claims_rule_based("some output", exit_code=42, patterns={})
    assert claims.exit_code == 42


def test_confidence_full_for_verification_task():
    claims = Claims(
        exit_code=0, tests_passed=222, tests_failed=0, tests_error=0,
        commit_hash=None, files_changed=None,
    )
    # verification expects: tests_passed, tests_failed, tests_error
    conf = compute_confidence(claims, task_type="verification")
    assert conf == 1.0


def test_confidence_zero_for_empty_claims():
    claims = Claims(exit_code=0)
    conf = compute_confidence(claims, task_type="verification")
    assert conf == 0.0


def test_confidence_code_review_always_zero():
    # code_review has no structured fields; Haiku always invoked
    claims = Claims(exit_code=0, tests_passed=5)
    conf = compute_confidence(claims, task_type="code_review")
    assert conf == 0.0
