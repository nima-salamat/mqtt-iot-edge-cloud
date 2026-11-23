#!/usr/bin/env bash
# Load the Compose .env file into the current Bash process without sourcing it
# as shell code. Docker Compose owns .env parsing; Python shell-quotes values
# before they are exported to the deployment scripts.
load_compose_env() {
  local env_file="${1:-.env}"
  local compose_file="${2:-docker-compose.yml}"
  local compose_output
  local shell_exports

  [[ -f "$env_file" ]] || {
    echo "ERROR: Environment file not found: $env_file" >&2
    return 1
  }
  command -v docker >/dev/null 2>&1 || {
    echo "ERROR: Docker is required to parse the Compose environment." >&2
    return 1
  }
  command -v python3 >/dev/null 2>&1 || {
    echo "ERROR: python3 is required to safely export Compose variables." >&2
    return 1
  }

  if ! compose_output="$(docker compose --env-file "$env_file" -f "$compose_file" config --environment)"; then
    echo "ERROR: Docker Compose could not parse $env_file. Check its KEY=VALUE syntax." >&2
    return 1
  fi

  if ! shell_exports="$(printf '%s\n' "$compose_output" | python3 -c '
import re
import shlex
import sys

for raw_line in sys.stdin:
    line = raw_line.rstrip("\r\n")
    if not line or "=" not in line:
        continue
    key, value = line.split("=", 1)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise SystemExit("Invalid environment variable name returned by Docker Compose")
    print("export " + key + "=" + shlex.quote(value))
')"; then
    echo "ERROR: Could not safely export the Compose environment." >&2
    return 1
  fi

  # Values are shell-quoted by Python's shlex.quote; no .env value is
  # evaluated as shell syntax.
  eval "$shell_exports"
}
