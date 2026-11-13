#!/usr/bin/env bash
# Thin wrapper: the installer lives at the repository root so that it can be piped from curl.
exec bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/install.sh" "$@"
