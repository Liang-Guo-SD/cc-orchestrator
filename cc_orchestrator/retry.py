from __future__ import annotations

from dataclasses import dataclass, field
from cc_orchestrator.models import Claims, VerificationTarget


@dataclass
class RetryAttempt:
    run_id: str
    claims: Claims
    failed_targets: list[tuple[VerificationTarget, str]]  # (target, actual_output)


def build_corrective_prompt(
    original_prompt: str,
    attempts: list[RetryAttempt],
) -> str:
    lines = [
        "ORIGINAL TASK (unchanged — implement this completely):",
        original_prompt,
        "",
        f"PRIOR FAILED ATTEMPTS ({len(attempts)}) — fresh context, do not continue these sessions:",
        "",
    ]
    for i, attempt in enumerate(attempts, 1):
        lines.append(f"Attempt {i} (run_id: {attempt.run_id}):")
        for vt, actual in attempt.failed_targets:
            lines.append(f"  Verification check: {vt.description}")
            lines.append(f"    Command:   {vt.command}")
            lines.append(f"    Expected:  {vt.expected_pattern}")
            lines.append(f"    Actual:    {actual.strip()}")
        lines.append("")
    lines.append(
        "Fix the specific discrepancies listed above, then re-run the full "
        "verification suite before reporting done."
    )
    return "\n".join(lines)
