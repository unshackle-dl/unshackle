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
