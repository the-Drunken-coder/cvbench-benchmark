class CVBenchError(Exception):
    """Base error surfaced by the CLI."""


class ConfigurationError(CVBenchError):
    """A benchmark or system definition is invalid."""


class ProtocolError(CVBenchError):
    """A frame or output record violates the protocol."""


class RuntimeFailure(CVBenchError):
    """The system under test could not complete a run."""
