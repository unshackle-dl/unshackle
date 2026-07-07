import re

# The actual regex from resolve.py line 77
pattern = r"^[a-z]+:.+$"
tests = [
    "v2ray:us",          # contains digit '2' — should NOT match [a-z]+
    "nordvpn:us",        # all letters — should match
    "basic:us",          # all letters — should match
    "expressvpn:us",     # all letters — should match
    "v2ray:vmess://x",   # contains digit — should NOT match
]
for t in tests:
    m = re.match(pattern, t, re.IGNORECASE)
    print(f"{t!r:40} -> match={bool(m)}")
