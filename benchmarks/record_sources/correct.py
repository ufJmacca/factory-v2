import sys

records = sys.stdin.read().split("\n")
print(sum(bool(record) for record in records))
