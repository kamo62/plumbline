"""Shared API errors for backend API clients."""

class ADORequestError(Exception):
    """Custom exception for Azure DevOps API errors"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ADOPermanentError(ADORequestError):
    """A request or response that retrying unchanged will not fix."""


class ADOTransientError(ADORequestError):
    """A temporary HTTP or transport failure after the retry budget."""
