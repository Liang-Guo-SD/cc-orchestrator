from cc_orchestrator.workers.opencode import OpenCodeAdapter


class QoderAdapter(OpenCodeAdapter):
    """Qoder CLI adapter. Same subprocess pattern as OpenCode."""
    _agent_name = "qoder"

    def extraction_patterns(self) -> dict[str, str]:
        return {}
