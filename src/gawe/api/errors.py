"""Errors the service raises. The HTTP layer maps each to a status code; messages are safe."""


class ApiError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequestError(ApiError):
    status = 422
    code = "bad_request"


class ForbiddenError(ApiError):
    status = 403
    code = "forbidden"


class NotFoundError(ApiError):
    """Also used for another tenant's resources, so existence is never revealed."""

    status = 404
    code = "not_found"


class ConflictError(ApiError):
    status = 409
    code = "conflict"


class UnauthorizedError(ApiError):
    status = 401
    code = "unauthorized"


class TooManyRequestsError(ApiError):
    status = 429
    code = "too_many_requests"

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after
