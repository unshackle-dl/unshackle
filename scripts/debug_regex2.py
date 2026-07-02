import re

# Test various forms
print("=== Without re.IGNORECASE ===")
print(f"  match with ^...$: {re.match(r'^[a-z]+:.+$', 'v2ray:us')}")
print(f"  match without ^...$: {re.match(r'[a-z]+:.+', 'v2ray:us')}")
print(f"  fullmatch: {re.fullmatch(r'[a-z]+:.+', 'v2ray:us')}")
print(f"  search: {re.search(r'[a-z]+:.+', 'v2ray:us')}")

print("\n=== With re.IGNORECASE ===")
print(f"  match with ^...$: {re.match(r'^[a-z]+:.+$', 'v2ray:us', re.IGNORECASE)}")
print(f"  match without ^...$: {re.match(r'[a-z]+:.+', 'v2ray:us', re.IGNORECASE)}")

print("\n=== Different pattern: [a-zA-Z]+ ===")
print(f"  match: {re.match(r'^[a-zA-Z]+:.+$', 'v2ray:us')}")
print(f"  match IGNORECASE: {re.match(r'^[a-zA-Z]+:.+$', 'v2ray:us', re.IGNORECASE)}")

print("\n=== Simplified ===")
print(f"  ^[a-z]+: : {re.match(r'^[a-z]+:', 'v2ray:us')}")
print(f"  ^[a-z]+:. : {re.match(r'^[a-z]+:.', 'v2ray:us')}")
print(f"  ^[a-z]+:.+ : {re.match(r'^[a-z]+:.+', 'v2ray:us')}")
