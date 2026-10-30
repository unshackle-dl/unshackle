from typing import Any, Collection, Dict, Iterable

FLAT_DL_KEYS = frozenset({"title", "service", "title_id", "profile", "proxy", "no_proxy"})


def service_options(
    data: Dict[str, Any], names: Collection[str], flat_excluded: Iterable[str] = FLAT_DL_KEYS
) -> Dict[str, Any]:
    """A request's service options named in ``names``: from its ``service_params`` object, else its flat keys.

    With ``service_params`` present every flat key is a dl value, so it never fills a service option of the same name.
    Without it, the flat keys in ``flat_excluded`` stay dl values.
    """
    nested = data.get("service_params")
    if isinstance(nested, dict):
        return {k: v for k, v in nested.items() if k in names}
    excluded = set(flat_excluded)
    return {k: v for k, v in data.items() if k in names and k not in excluded}
