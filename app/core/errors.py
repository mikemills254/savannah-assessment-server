from fastapi import status


class AppError(Exception):
    """Base for errors that services raise and the API turns into a JSON
    response ({"detail": ...}) with the class's status code; see main.py.

    Services raise these instead of HTTPException so they stay free of HTTP
    and can be reused from scripts and tests.
    """

    status_code: int = status.HTTP_400_BAD_REQUEST
    detail: str = "Bad request"

    def __init__(self, detail: str | None = None):
        if detail is not None:
            self.detail = detail
        super().__init__(self.detail)


class InvalidRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    detail = "Not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    detail = "Conflict"
