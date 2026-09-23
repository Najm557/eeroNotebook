class OpenNotebookError(Exception):
    """Base exception class for Open Notebook errors."""

    pass


class DatabaseOperationError(OpenNotebookError):
    """Raised when a database operation fails."""

    pass


class UnsupportedTypeException(OpenNotebookError):
    """Raised when an unsupported type is provided."""

    pass


class InvalidInputError(OpenNotebookError):
    """Raised when invalid input is provided."""

    pass


class NotFoundError(OpenNotebookError):
    """Raised when a requested resource is not found."""

    pass


class AuthenticationError(OpenNotebookError):
    """Raised when there's an authentication problem."""

    pass


class AccessDeniedError(OpenNotebookError):
    """Raised when an authenticated member may see a record but not change it.

    Reserved for a Viewer refused a write (spec task 6.2). A member with no
    access at all gets NotFoundError instead, so a response never confirms that
    a Notebook exists to somebody who has neither ownership nor a Share
    (Requirement 7.4). Hiding existence from a Viewer would achieve nothing -
    they can already read it - and would read as a defect rather than a refusal.
    """

    pass


class AccessUnavailableError(OpenNotebookError):
    """Raised when an access check could not be completed.

    Distinct from AccessDeniedError on purpose. "The database is unreachable"
    must not be answerable as "you have no access": the second is a 404 or a 403
    that looks like a passed check, and an outage would silently turn every
    access decision into a denial the caller cannot tell from a real one. Answers
    503, matching what task 5.3 does for an unreadable identity store.
    """

    pass


class ConfigurationError(OpenNotebookError):
    """Raised when there's a configuration problem."""

    pass


class ExternalServiceError(OpenNotebookError):
    """Raised when an external service (e.g., AI model) fails."""

    pass


class RateLimitError(OpenNotebookError):
    """Raised when a rate limit is exceeded."""

    pass


class FileOperationError(OpenNotebookError):
    """Raised when a file operation fails."""

    pass


class NetworkError(OpenNotebookError):
    """Raised when a network operation fails."""

    pass


class NoTranscriptFound(OpenNotebookError):
    """Raised when no transcript is found for a video."""

    pass
