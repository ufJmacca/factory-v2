#!/bin/sh
set -eu

mkdir -p /logs/agent
exec 2>/logs/agent/boot.log

setpriv --reuid 10001 --regid 10001 --clear-groups /bin/sh -ec '
    git init --quiet --template= --initial-branch=work /work/product
    cd /work/product
    git -c fetch.fsckObjects=true fetch --quiet --no-tags /seed/base.bundle refs/heads/submission
    git reset --quiet --hard FETCH_HEAD
'
touch /tmp/ready
exec sleep infinity
