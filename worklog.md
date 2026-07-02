---
Task ID: v2ray-integration
Agent: main
Task: Add V2Ray/Xray proxy provider to the unshackle repo (downloaded from https://github.com/unshackle-dl/unshackle/archive/refs/heads/dev.zip), keeping full backward compatibility with the existing NordVPN/ExpressVPN/Gluetun/etc. integrations. Production-quality changes suitable for a GitHub PR. Strictly Python — no React/Next.js.

Work Log:
- Downloaded and extracted unshackle dev branch to /home/z/my-project/unshackle/
- Analyzed the existing proxy provider architecture: Proxy base class, NordVPN/ExpressVPN/ProtonVPN/SurfsharkVPN/WindscribeVPN/Hola/Gluetun/Basic providers, resolve.py orchestration, binaries.py discovery, ip_info.py utilities, existing test patterns
- Designed V2Ray provider following existing patterns (Proxy subclass, server_map config, atexit cleanup like Gluetun, binary discovery like Hola)
- Added Xray + V2Ray binary discovery to unshackle/core/binaries.py (Xray preferred, V2ray fallback)
- Implemented unshackle/core/proxies/v2ray.py (~1440 lines):
  * URI parsers for vmess://, vless:// (incl. Reality + XTLS Vision flow), trojan://, ss:// (SIP002 + legacy + plaintext)
  * Subscription fetcher (base64 + plain, lenient per-line parsing)
  * V2Ray/Xray JSON config-file loader (extracts vmess/vless/trojan/shadowsocks outbounds)
  * Country detection heuristic (flag emoji, 2-letter codes, full names, UK alias, TLD fallback)
  * V2Ray/Xray config builder (SOCKS5 + HTTP inbounds, single outbound, geoip:private bypass routing)
  * V2Ray class with full lifecycle: port allocation, subprocess spawn with 0600 temp config file, readiness polling, optional IP verification via get_ip_info, atexit cleanup, secure config file deletion
  * Query formats: country (us), country:index (us:1), country:remark (us:tokyo), remark substring (tokyo), server_map aliases
- Registered V2Ray in unshackle/core/proxies/__init__.py and wired it into resolve.initialize_proxy_providers()
- Wrote tests/core/test_v2ray.py with 83 unit tests covering: URI parsing (all 4 protocols, edge cases, error paths), subscription fetching (base64/plain/empty/HTTP errors), config file loading, country detection (parametrized), config building, provider construction (all 3 server sources + dedup + inline dicts), server selection (all query formats + aliases + UK normalization + fuzzy country names), get_proxy lifecycle (mocked subprocess, port allocation, reuse, startup failure, cleanup, kill on hang), and resolve.py integration
- Added docs/V2RAY.md (full reference: prerequisites, quick start, server sources, query format, country detection, all provider options, supported protocols with examples, subprocess lifecycle, troubleshooting)
- Updated docs/NETWORK_CONFIG.md to add a v2ray section next to the existing gluetun section
- Updated mkdocs.yml nav to include the new V2RAY.md page

Stage Summary:
- Files added:
  * unshackle/core/proxies/v2ray.py (new V2Ray provider, ~1440 lines)
  * tests/core/test_v2ray.py (83 unit tests, ~1035 lines)
  * docs/V2RAY.md (full reference doc)
- Files modified (minimal, surgical):
  * unshackle/core/binaries.py — added Xray + V2Ray discovery + __all__ entries
  * unshackle/core/proxies/__init__.py — added V2Ray to imports + __all__
  * unshackle/core/proxies/resolve.py — added 2-line V2Ray instantiation block, matching the existing pattern
  * docs/NETWORK_CONFIG.md — added a v2ray section
  * mkdocs.yml — added V2RAY.md to nav
- Test results: 524 passed (441 pre-existing + 83 new), 0 failures, 0 regressions
- Lint: ruff + isort + mypy all pass on the new/modified files
- Backward compatibility: zero changes to existing provider code; the only edits to existing files are additive (new imports, new __all__ entries, new conditional instantiation block in resolve.py)
- No new runtime dependencies required (uses existing requests[socks], pycountry, rich, appdirs already in pyproject.toml)

---
Task ID: v2ray-basic-style-and-direct-uri
Agent: main
Task: Add two new V2Ray usage modes requested by the user: (1) basic-style per-country YAML assignment (`countries:` map, used with `--proxy v2ray:us`), and (2) direct V2Ray URI on the CLI (`--proxy v2ray:vmess://...`). Also write the commit/PR message.

Work Log:
- Added `countries` parameter to V2Ray.__init__ — a dict mapping country code / alias to a single URI or list of URIs (mirrors Basic's `**countries` pattern)
- Implemented `_load_country_map()` to parse the YAML map into `dict[str, list[V2RayServer]]`, skipping unparseable URIs with a warning and forcing the server's country to match the map key (when the key is a valid ISO code) so IP verification compares against the user's intent
- Updated `_select_server()` to check `_country_servers` first (by normalised country, raw head, and arbitrary alias), then fall back to the flat list — with random pick for the countries map (matching Basic) and deterministic first-match for the flat list (preserving existing test expectations)
- Added `_is_v2ray_uri()` helper to detect vmess:// / vless:// / trojan:// / ss:// URI queries
- Updated `get_proxy()` to detect direct-URI queries and spawn a one-shot subprocess via the new `_spawn_for_server()` method (shared between country/remark selection and direct-URI mode for identical lifecycle behaviour)
- Loosened the "no servers configured" check so direct-URI mode works with a completely empty YAML config
- Added `country_servers` read-only property for introspection
- Updated `__repr__` to count both the flat list and the countries map
- Fixed a critical pre-existing bug in resolve_proxy: the provider-prefix regex `^[a-z]+:.+$` didn't match `v2ray` (because of the digit `2`), so `--proxy v2ray:us` was silently falling through to the try-every-provider loop and raising a confusing "No proxy provider had a proxy" error. Changed to `^[a-z][a-z0-9]*:.+$` (letter first, then letters/digits) so provider names with digits are recognised. Verified the fix doesn't break any existing provider (nordvpn, expressvpn, basic, etc. all still route correctly).
- Added 30 new unit tests covering: countries-map loading (single URI, list, unparseable-skipping, non-dict rejection, non-string-non-list skipping, country-override, arbitrary alias keys), selection from the countries map (by country, by index, by alias, priority over flat list), repr counting both pools, get_proxy with countries map, _is_v2ray_uri detection for all 4 schemes + whitespace + non-URI strings, direct-URI get_proxy for vmess/vless/trojan/ss (one-shot spawn, reuse for same URI, different URIs get different subprocesses, unparseable URI raises, coexistence with country-query mode), and resolve_proxy routing (v2ray:us, v2ray:vmess://..., v2ray:vless://..., v2ray:ss://..., v2ray-not-found error, nordvpn:us backward compat, bare `us` falls through to try-every-provider)
- Updated docs/V2RAY.md with new "Quick Start" options A-E (countries map, subscription, config_path, inline list, direct URI), a new `countries` section under Server Sources, a new "Direct URI on the CLI" section, an expanded Query Format table (added alias + URI rows), and the countries row in the Provider Options table
- Updated docs/NETWORK_CONFIG.md v2ray section to mention the countries map and direct-URI CLI mode
- Wrote COMMIT_MESSAGE.txt with the PR description ("Added native V2Ray support")

Stage Summary:
- All 554 tests pass (524 from previous commit + 30 new), 0 failures, 0 regressions
- ruff + isort + mypy all clean on the changed files
- Three usage modes now supported:
  1. basic-style countries map: `countries: {us: [vmess://..., vless://...]}` + `--proxy v2ray:us`
  2. direct URI on CLI: `--proxy v2ray:vmess://...` (no YAML needed)
  3. existing flat-list / subscription / config_path modes (unchanged)
- The resolve_proxy regex fix is a bonus bug-fix that was blocking ALL `v2ray:*` queries (not just the new modes)
