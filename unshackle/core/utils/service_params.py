from typing import Any, Collection, Dict, Iterable

import click

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


def cast_service_options(params: Iterable[click.Parameter], ctx: Any, options: Dict[str, Any]) -> Dict[str, Any]:
    """``options`` with each value cast by the click parameter of the same name; raises ``click.BadParameter``.

    A name that no parameter declares keeps its value. A JSON object, an array for a single-value text option
    (click would stringify it) and a boolean for a number are refused before the cast. ``convert()`` only promises
    ``BadParameter`` for strings, so any other error of a JSON value becomes one too.
    """
    by_name = {p.name: p for p in params if p.name}
    cast: Dict[str, Any] = {}
    for k, v in options.items():
        param = by_name.get(k)
        if param is None:
            cast[k] = v
            continue
        if isinstance(v, dict) or (
            isinstance(v, list)
            and not param.multiple
            and param.nargs == 1
            and isinstance(param.type, click.types.StringParamType)
        ):
            raise click.BadParameter("expected a single value, not a JSON object or array", ctx=ctx, param=param)
        if isinstance(v, bool) and isinstance(param.type, (click.types.IntParamType, click.types.FloatParamType)):
            raise click.BadParameter("expected a number, not a boolean", ctx=ctx, param=param)
        try:
            cast[k] = param.type_cast_value(ctx, v)
        except (TypeError, ValueError, AttributeError, OverflowError) as e:
            raise click.BadParameter(f"not a valid {param.type.name} value", ctx=ctx, param=param) from e
    return cast
