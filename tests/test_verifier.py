from cc_orchestrator.verifier import generate_targets
from cc_orchestrator.models import Claims


def test_generates_pytest_target_when_tests_passed_known():
    claims = Claims(exit_code=0, tests_passed=222, tests_failed=0)
    targets = generate_targets(claims, working_dir="/proj")
    commands = [t.command for t in targets]
    assert any("pytest" in c for c in commands)
    patterns = [t.expected_pattern for t in targets]
    assert any("222 passed" in p for p in patterns)


def test_generates_commit_hash_target():
    claims = Claims(exit_code=0, commit_hash="abc1234")
    targets = generate_targets(claims, working_dir="/proj")
    commands = [t.command for t in targets]
    assert any("abc1234" in c for c in commands)
    patterns = [t.expected_pattern for t in targets]
    assert any("changed" in p for p in patterns)


def test_no_targets_for_empty_claims():
    claims = Claims(exit_code=0)
    targets = generate_targets(claims, working_dir="/proj")
    assert targets == []


def test_failed_tests_generates_no_pass_target():
    claims = Claims(exit_code=1, tests_passed=None, tests_failed=3)
    targets = generate_targets(claims, working_dir="/proj")
    # tests_passed is None, so no pytest target generated at all
    assert targets == []


def test_working_dir_used_in_pytest_command():
    claims = Claims(exit_code=0, tests_passed=5, tests_failed=0)
    targets = generate_targets(claims, working_dir="/my/project")
    pytest_targets = [t for t in targets if "pytest" in t.command]
    assert pytest_targets  # at least one pytest target exists
