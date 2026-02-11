class NetworkError(Exception):
    """Base unified network error."""


class NetworkHTTPError(NetworkError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(f"HTTP error {status_code}")
