def get_session() -> requests.Session:
    """
    Creates a Python-requests Session, adds common headers
    from config, cookies, retry handler, and a proxy if available.
    :returns: Prepared Python-requests Session
    """
    session = requests.Session()
    session.headers.update(config.headers)
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(total=15, backoff_factor=0.2, status_forcelist=[429, 500, 502, 503, 504]),
            pool_block=True,
        ),
    )
    session.mount("http://", session.adapters["https://"])
    return session
