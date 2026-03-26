"""Custom exception hierarchy for bracket_team."""


class BracketTeamError(Exception):
    """Base exception for all bracket_team errors."""


class LLMError(BracketTeamError):
    """Base exception for LLM-related errors."""

    def __init__(self, message: str, role: str | None = None, attempt: int | None = None):
        super().__init__(message)
        self.role = role
        self.attempt = attempt


class LLMRetryExhaustedError(LLMError):
    """Raised when all retry attempts for an LLM call have been exhausted."""


class LLMValidationError(LLMError):
    """Raised when the LLM response fails schema validation after all retries."""


class FatalLLMError(LLMError):
    """Raised for non-retryable fatal LLM errors (billing, auth, etc.).
    Not swallowed by individual analyst wrappers — propagates to the pipeline.
    """


class PipelineError(BracketTeamError):
    """Raised when the matchup pipeline encounters an unrecoverable error."""

    def __init__(
        self,
        message: str,
        run_id: int | None = None,
        matchup_id: int | None = None,
    ):
        super().__init__(message)
        self.run_id = run_id
        self.matchup_id = matchup_id


class BracketImportError(BracketTeamError):
    """Raised when bracket import data is invalid."""
