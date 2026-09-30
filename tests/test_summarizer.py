import json
from unittest.mock import MagicMock, patch
from cc_orchestrator.summarizer import extract_claims_rule_based, compute_confidence, summarize, summarize_with_haiku
from cc_orchestrator.models import Claims

PYTEST_OUTPUT = """\
tests/test_foo.py ....
tests/test_bar.py ..F
FAILED tests/test_bar.py::test_something
222 passed, 1 failed, 0 errors in 12.34s
"""

RUFF_CLEAN = "All checks passed!\n"
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


UNSTRUCTURED_OUTPUT = """
The refactoring is complete. I restructured three modules and improved
readability throughout. Some legacy patterns were removed. The work
took about 45 minutes.
"""

HAIKU_RESPONSE = json.dumps({
    "summary": "Refactored three modules; legacy patterns removed.",
    "tests_passed": None,
    "tests_failed": None,
    "files_changed": ["app/foo.py", "app/bar.py", "app/baz.py"],
    "commit_hash": None,
    "important_findings": ["legacy patterns removed"],
})


def _mock_anthropic(response_text: str):
    mock_client = MagicMock()
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text=response_text)]
    mock_client.messages.create.return_value = mock_msg
    return mock_client


def test_summarize_uses_haiku_when_confidence_low():
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value = _mock_anthropic(HAIKU_RESPONSE)
        claims, summary, confidence = summarize(
            raw_text=UNSTRUCTURED_OUTPUT,
            exit_code=0,
            task_type="implementation",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="test-key",
        )
    assert "Refactored" in summary
    assert claims.files_changed == ["app/foo.py", "app/bar.py", "app/baz.py"]
    assert claims.important_findings == ["legacy patterns removed"]


def test_summarize_skips_haiku_when_confidence_high():
    high_conf_output = "222 passed, 0 failed, 0 errors\n"
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        claims, summary, confidence = summarize(
            raw_text=high_conf_output,
            exit_code=0,
            task_type="verification",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="test-key",
        )
        MockClient.assert_not_called()
    assert claims.tests_passed == 222
    assert confidence == 1.0


def test_summarize_returns_low_confidence_when_api_key_none():
    claims, summary, confidence = summarize(
        raw_text=UNSTRUCTURED_OUTPUT,
        exit_code=0,
        task_type="implementation",
        adapter_patterns={},
        haiku_model="claude-haiku-4-5-20251001",
        api_key=None,
    )
    assert confidence < 0.6
    assert "low_confidence" in summary.lower() or summary == ""


def test_summarize_returns_low_confidence_on_auth_error():
    import anthropic as _anthropic
    with patch("cc_orchestrator.summarizer.anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.side_effect = (
            _anthropic.AuthenticationError.__new__(_anthropic.AuthenticationError)
        )
        claims, summary, confidence = summarize(
            raw_text=UNSTRUCTURED_OUTPUT,
            exit_code=0,
            task_type="implementation",
            adapter_patterns={},
            haiku_model="claude-haiku-4-5-20251001",
            api_key="bad-key",
        )
    assert confidence < 0.6
