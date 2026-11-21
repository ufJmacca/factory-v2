#!/bin/sh
set -eu

chmod 0700 /logs/verifier
exec /opt/venv/bin/python -EB /tests/grade.py
