from __future__ import annotations

import logging
import re
import sys
from typing import Any, Optional

import click
import yaml
from rich.padding import Padding
from rich.rule import Rule
from rich.tree import Tree

from unshackle.commands.dl import dl
from unshackle.core.config import config
from unshackle.core.console import console
from unshackle.core.constants import context_settings
from unshackle.core.proxies.proxy import Proxy
from unshackle.core.proxies.resolve import REGION, describe_proxy, load_proxy_providers, pick_proxy, split_proxy_query
from unshackle.core.service import Service
from unshackle.core.services import Services
from unshackle.core.utils.click_types import ContextData
from unshackle.core.utils.collections import merge_dict
from unshackle.core.utils.redact import mask_proxy


@click.command(
    short_help="Search for titles from a Service.",
    cls=Services,
    context_settings=context_settings,
)
@click.option(
    "-p", "--profile", type=str, default=None, help="Profile to use for Credentials and Cookies (if available)."
)
@click.option(
    "--proxy",
    type=str,
    default=None,
    help="Proxy URI to use. If a 2-letter country is provided, it will try get a proxy from the config.",
)
@click.option("--no-proxy", is_flag=True, default=False, help="Force disable all proxy use.")
@click.pass_context
def search(ctx: click.Context, no_proxy: bool, profile: Optional[str] = None, proxy: Optional[str] = None):
    if not ctx.invoked_subcommand:
        raise ValueError("A subcommand to invoke was not specified, the main code cannot continue.")

    log = logging.getLogger("search")

    service = Services.get_tag(ctx.invoked_subcommand)
    profile = profile

    if profile:
        log.info(f"Using profile: '{profile}'")

    with console.status("Loading Service Config...", spinner="dots"):
        service_config_path = Services.get_path(service) / config.filenames.config
        if service_config_path.exists():
            service_config = yaml.safe_load(service_config_path.read_text(encoding="utf8"))
            log.info("Service Config loaded")
        else:
            service_config = {}
        merge_dict(config.services.get(service), service_config)

    proxy_providers: list[Proxy] = []
    if no_proxy:
        ctx.params["proxy"] = None
    else:
        with console.status("Loading Proxy Providers...", spinner="dots"):
            proxy_providers = load_proxy_providers()

        if proxy:
            try:
                requested_provider, query = split_proxy_query(proxy, proxy_providers)
            except ValueError as e:
                log.error(str(e))
                sys.exit(1)
            if re.fullmatch(rf"(?:[a-z]+:)?{REGION}", query, re.IGNORECASE):
                query = query.lower()
                with console.status(f"Getting a Proxy to {query}...", spinner="dots"):
                    try:
                        proxy_provider, proxy_uri = pick_proxy(proxy_providers, query, requested_provider)
                    except ValueError as e:
                        log.error(str(e))
                        sys.exit(1)
                    ctx.params["proxy"] = proxy_uri
                    log.info(describe_proxy(proxy_provider, proxy_uri, query))
            else:
                log.info(f"Using explicit Proxy: {mask_proxy(ctx.params['proxy'])}")

    ctx.obj = ContextData(config=service_config, cdm=None, proxy_providers=proxy_providers, profile=profile)


@search.result_callback()
def result(service: Service, profile: Optional[str] = None, **_: Any) -> None:
    log = logging.getLogger("search")

    service_tag = service.__class__.__name__

    with console.status("Authenticating with Service...", spinner="dots"):
        cookies = dl.get_cookie_jar(service_tag, profile)
        credential = dl.get_credentials(service_tag, profile)
        service.authenticate(cookies, credential)
        if cookies or credential:
            log.info("Authenticated with Service")

    search_results = Tree("Search Results", hide_root=True)
    with console.status("Searching...", spinner="dots"):
        for result in service.search():
            result_text = f"[bold text]{result.title}[/]"
            if result.url:
                result_text = f"[link={result.url}]{result_text}[/link]"
            if result.label:
                result_text += f"  [pink]{result.label}[/]"
            if result.description:
                result_text += f"\n[text2]{result.description}[/]"
            result_text += f"\n[bright_black]id: {result.id}[/]"
            search_results.add(result_text + "\n")

    cookie_file = dl.get_cookie_path(service_tag, profile)
    if cookie_file:
        dl.save_cookies(cookie_file, service.session.cookies)

    console.print(Padding(Rule(f"[rule.text]{len(search_results.children)} Search Results"), (1, 2)))

    if search_results.children:
        console.print(Padding(search_results, (0, 5)))
    else:
        console.print(
            Padding("[bold text]No matches[/]\n[bright_black]Please check spelling and search again....[/]", (0, 5))
        )
