from cc_orchestrator.workers.opencode import OpenCodeAdapter


class QoderAdapter(OpenCodeAdapter):
    """Qoder CLI adapter. Same subprocess pattern as OpenCode."""

    def extraction_patterns(self) -> dict[str, str]:
        return {}
