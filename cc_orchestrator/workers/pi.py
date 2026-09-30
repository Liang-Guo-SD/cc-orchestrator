from cc_orchestrator.workers.opencode import OpenCodeAdapter


class PiAdapter(OpenCodeAdapter):
    """Pi Agent CLI adapter. Same subprocess pattern as OpenCode."""
    _agent_name = "pi"

    def extraction_patterns(self) -> dict[str, str]:
        return {}
