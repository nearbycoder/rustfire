#!/bin/sh
set -eu

chown rustfire:rustfire /data
exec gosu rustfire "$@"
