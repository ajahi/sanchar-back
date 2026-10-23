"""Failure logging that stays cheap when the app is overloaded."""
import sys

from sqlalchemy.exc import TimeoutError as PoolTimeout


def log_failure(log, msg: str, *args) -> None:
    """log.exception for an unexpected error; a one-liner for DB-pool exhaustion. Call inside `except`.

    Formatting a traceback costs milliseconds of CPU (it re-parses source lines). Under overload every
    request hits the same pool timeout, and thousands of tracebacks took ~40% of the CPU in the
    load-test profile, slowing the loop further and causing more timeouts.
    """
    if isinstance(sys.exc_info()[1], PoolTimeout):
        log.error(msg + ": DB pool exhausted", *args)
    else:
        log.exception(msg, *args)
