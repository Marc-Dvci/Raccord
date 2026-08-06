#!/usr/bin/env bash
# Mint a Grafana service-account token for the official MCP server.
#
#   docker compose up -d
#   ./tools/grafana_token.sh          # prints the token, and writes it to .env
#
# Doing this by hand means clicking through Administration -> Service accounts,
# which is fine once and tedious every time the stack is recreated. The local
# stack's admin credentials are the ones in docker-compose.yml and are not a
# secret; the token this produces is, which is why .env is gitignored.
set -euo pipefail

GRAFANA_URL="${GRAFANA_ADMIN_URL:-http://localhost:3000}"
ADMIN="${GRAFANA_ADMIN:-admin:accesspulse}"
SA_NAME="${SA_NAME:-accesspulse-mcp}"
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"

api() { curl -sS -u "$ADMIN" -H 'Content-Type: application/json' "$@"; }

# Wait for Grafana rather than failing on a race with `docker compose up -d`.
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "$GRAFANA_URL/api/health" >/dev/null 2>&1; then break; fi
  sleep 2
done

# Reuse the service account if it already exists: running this twice should not
# accumulate accounts.
sa_id="$(api "$GRAFANA_URL/api/serviceaccounts/search?query=$SA_NAME" \
  | python -c 'import json,sys; a=json.load(sys.stdin).get("serviceAccounts") or []; print(a[0]["id"] if a else "")')"

if [ -z "$sa_id" ]; then
  sa_id="$(api -X POST "$GRAFANA_URL/api/serviceaccounts" \
    -d "{\"name\":\"$SA_NAME\",\"role\":\"Admin\",\"isDisabled\":false}" \
    | python -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
fi

token="$(api -X POST "$GRAFANA_URL/api/serviceaccounts/$sa_id/tokens" \
  -d "{\"name\":\"$SA_NAME-$(date +%s)\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin)["key"])')"

echo "$token"

if [ -f "$ENV_FILE" ]; then
  # Replace the line rather than appending, so repeated runs leave one value.
  grep -v '^AP_GRAFANA_SERVICE_ACCOUNT_TOKEN=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
  mv "$ENV_FILE.tmp" "$ENV_FILE"
fi
echo "AP_GRAFANA_SERVICE_ACCOUNT_TOKEN=$token" >> "$ENV_FILE"
echo "written to $ENV_FILE" >&2
