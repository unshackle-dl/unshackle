from copy import deepcopy
from dataclasses import dataclass, field
from typing import List, Dict, Any
from dacite import from_dict, Config as DaciteConfig


@dataclass
class RetryConfig:
    retries: int = 3
    backoff_multiplier: float = 1.0
    retry_statuses: List[int] = field(
        default_factory=lambda: [429, 500, 502, 503, 504]
    )
    retry_methods: List[str] = field(
        default_factory=lambda: ["GET", "POST"]
    )

    def __post_init__(self):
        self.retry_methods = [m.upper() for m in self.retry_methods]



@dataclass
class HttpClientConfig:
    type: str = "requests"
    headers: Dict[str, str] = field(default_factory=dict)
    retry: RetryConfig = field(default_factory=RetryConfig)

    # Adapter-specific extra parameters
    adapter_options: Dict[str, Any] = field(default_factory=dict)


def load_config(data: dict | None) -> HttpClientConfig:
    if data is None:
        return HttpClientConfig()

    return from_dict(
        data_class=HttpClientConfig,
        data=data,
        config=DaciteConfig(
            strict=True
        ),
    )
