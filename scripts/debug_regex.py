import re

pattern = r'^[a-z]+:.+$'
test_str = 'v2ray:us'
print(f"pattern: {pattern!r}")
print(f"test_str: {test_str!r}")
print(f"len(pattern): {len(pattern)}")
for i, c in enumerate(pattern):
    print(f"  [{i}] {c!r} (ord={ord(c)})")
m = re.match(pattern, test_str, re.IGNORECASE)
print(f"match: {m}")
