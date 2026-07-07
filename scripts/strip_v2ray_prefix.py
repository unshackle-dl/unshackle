"""One-off script: drop the 'v2ray: ' prefix from error/log messages in v2ray.py.

This matches the convention used by every other proxy provider (NordVPN, Gluetun,
Basic, etc.) which do NOT prefix their messages with the module name — the logger
name (proxies.v2ray) already identifies the source.
"""
import re
from pathlib import Path

path = Path("/home/z/my-project/unshackle/unshackle/core/proxies/v2ray.py")
content = path.read_text(encoding="utf-8")

# Drop "v2ray: " from log calls: log.xxx("v2ray: ...") -> log.xxx("...")
# and log.xxx("v2ray: countries[%s] ...") -> log.xxx("countries[%s] ...")
content = re.sub(r'(log\.\w+\()"v2ray: ', r'\1"', content)

# Drop "v2ray: " from raise messages: raise X("v2ray: ...") -> raise X("...")
# Handle f-strings too: raise X(f"v2ray: ...") -> raise X(f"...")
content = re.sub(r'(raise \w+\(f?)"v2ray: ', r'\1"', content)

path.write_text(content, encoding="utf-8")
print("Done. Remaining 'v2ray: ' occurrences:")
for i, line in enumerate(content.splitlines(), 1):
    if '"v2ray: ' in line:
        print(f"  {i}: {line.strip()}")
