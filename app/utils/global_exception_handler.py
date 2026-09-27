"""
Global exception handler for the application.
Handles all exceptions and returns appropriate HTTP responses.
"""

import logging

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global exception handler that catches all exceptions.
    - If it's an HTTPException, re-raise it to preserve the original status and detail.
    - Otherwise, log the error and return a 500 Internal Server Error.
    """
    # If it's already an HTTPException, return it with its original status and detail
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    # For all other exceptions, log the error and return 500
    logger.error(f"Unhandled exception: {exc}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Oops! Something went wrong."},
    )
