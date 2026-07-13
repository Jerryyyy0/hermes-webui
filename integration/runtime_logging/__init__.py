"""Runtime log persistence for direct ``server.py`` launches."""

from integration.runtime_logging.sink import RuntimeLogSetup, setup_runtime_logging

__all__ = ["RuntimeLogSetup", "setup_runtime_logging"]
