from __future__ import annotations

from cc_orchestrator.models import Claims, VerificationTarget


def generate_targets(claims: Claims, working_dir: str) -> list[VerificationTarget]:
    """Generate shell commands Claude Code can run independently to verify claims."""
    targets: list[VerificationTarget] = []

    if claims.tests_passed is not None and (claims.tests_failed is None or claims.tests_failed == 0):
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && .venv/bin/python -m pytest -q 2>&1 | tail -1",
            expected_pattern=f"{claims.tests_passed} passed",
            description="pytest suite fully green",
        ))
    elif claims.tests_failed is not None and claims.tests_failed > 0:
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && .venv/bin/python -m pytest -q 2>&1 | tail -1",
            expected_pattern="failed",
            description=f"{claims.tests_failed} test(s) expected to fail",
        ))

    if claims.commit_hash:
        targets.append(VerificationTarget(
            command=f"cd {working_dir} && git show --stat {claims.commit_hash}",
            expected_pattern="files changed",
            description=f"commit {claims.commit_hash} exists with file changes",
        ))

    return targets
