from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from .exceptions import NetworkError
from .config import RetryConfig


def build_retry(config: RetryConfig):
    return retry(
        stop=stop_after_attempt(config.retries),
        wait=wait_exponential(multiplier=config.backoff_multiplier),
        retry=retry_if_exception_type(NetworkError),
        reraise=True,
    )
