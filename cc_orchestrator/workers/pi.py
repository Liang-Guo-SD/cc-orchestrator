from cc_orchestrator.workers.cli_adapter import CliAdapter


class PiAdapter(CliAdapter):
    """Pi Agent CLI adapter."""
    _agent_name = "pi"

    def extraction_patterns(self) -> dict[str, str]:
        return {}
