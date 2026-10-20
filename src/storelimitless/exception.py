"""Exception hierarchy raised by the StoreLimitless Python client."""


class StoreLimitlessError(Exception):
    """Base class for errors raised by the StoreLimitless client."""


class StoreLimitlessConnectionError(StoreLimitlessError):
    """Raised when the StoreLimitless server cannot be reached or ready."""


class StoreLimitlessResponseError(StoreLimitlessError):
    """Raised when a successful HTTP response does not match the expected schema."""


class StoreLimitlessHTTPError(StoreLimitlessError):
    """Raised for non-success HTTP responses from the StoreLimitless server."""

    def __init__(self, status_code: int, message: str) -> None:
        """Store the HTTP status code and the server-provided error message."""

        self.status_code: int = status_code
        super().__init__(message)
