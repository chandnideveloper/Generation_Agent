import functools
import time
import traceback
from app.tableau.core.logging_utils import log_info, log_error

def robust_handler(func):
    """
    Decorator for detailed entry/exit logs and exception handling.
    """
    @functools.wraps(func)
    async def async_wrapper(*args, **kwargs):
        start_time = time.time()
        func_name = func.__name__
        log_info(f">>> Entering {func_name}")
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            log_info(f"<<< Exited {func_name} (Duration: {duration:.2f}s)")
            return result
        except Exception as e:
            log_error(f"!!! Exception in {func_name}: {str(e)}")
            return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

    return async_wrapper