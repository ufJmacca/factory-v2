import sys

data = sys.stdin.read()
if not data:
    raise SystemExit("expected at least one record")
records = data.split("\n")
print(sum(bool(record) for record in records))
