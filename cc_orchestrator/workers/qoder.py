from cc_orchestrator.workers.cli_adapter import CliAdapter


class QoderAdapter(CliAdapter):
    """Qoder CLI adapter."""
    _agent_name = "qoder"

    def extraction_patterns(self) -> dict[str, str]:
        return {}
