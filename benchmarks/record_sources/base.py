import sys

records = sys.stdin.read().split("\n")[:-1]
print(sum(bool(record) for record in records))
