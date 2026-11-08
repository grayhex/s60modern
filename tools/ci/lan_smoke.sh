#!/usr/bin/env bash
# Home LAN mode smoke test against a built image (issue #72). Usage: tools/ci/lan_smoke.sh <image>
# Needs Docker and a non-loopback IPv4 address on this machine (the CI runner has one).
set -euo pipefail
image="${1:?image}"
ip="$(hostname -I | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | grep -v '^127\.' | head -1)"
test -n "$ip" || { echo "no LAN address, skipping"; exit 0; }
password="ci-$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')"
name=symport-lan-smoke
cleanup() { docker logs "$name" 2>&1 | tail -n 20 || true; docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "--- fail closed: LAN mode without a password must not start"
set +e
docker run --rm -e SYMPORT_ACCESS_MODE=lan -e SYMPORT_BIND="$ip" "$image" >/tmp/lan-nopass.log 2>&1
code=$?
set -e
test "$code" -eq 78 || { cat /tmp/lan-nopass.log; echo "expected exit 78, got $code"; exit 1; }
grep -qi "admin password" /tmp/lan-nopass.log

echo "--- fail closed: 0.0.0.0 alone must not open anything"
set +e
docker run --rm -e SYMPORT_BIND=0.0.0.0 "$image" >/tmp/lan-wildcard.log 2>&1
code=$?
set -e
test "$code" -eq 78 || { cat /tmp/lan-wildcard.log; echo "expected exit 78, got $code"; exit 1; }

echo "--- Home LAN mode on $ip"
docker run -d --name "$name" -e SYMPORT_ACCESS_MODE=lan -e SYMPORT_BIND="$ip" -e SYMPORT_ADMIN_PASSWORD="$password" \
  -p "$ip:8765:8765" "$image" >/dev/null
for _ in $(seq 1 40); do
  if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2).read()" 2>/dev/null; then ready=1; break; fi
  sleep 2
done
test "${ready:-0}" -eq 1
base="http://$ip:8765"
login_page="$(curl --fail --silent "$base/")"
grep -q 'login.js' <<<"$login_page"
! grep -q 'symport-token' <<<"$login_page"
test "$(curl -s -o /dev/null -w '%{http_code}' "$base/api/state")" = 401
test "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example:8765' "$base/api/state")" = 403
test "$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -H "Origin: $base" -d '{"password":"wrong-wrong-wrong"}' "$base/api/auth/login")" = 401
jar="$(mktemp)"
test "$(curl -s -o /dev/null -w '%{http_code}' -c "$jar" -H 'Content-Type: application/json' -H "Origin: $base" -d "{\"password\":\"$password\"}" "$base/api/auth/login")" = 200
grep -q HttpOnly "$jar"
curl --fail --silent -b "$jar" "$base/api/state" | python3 -c 'import json,sys; assert json.load(sys.stdin)["schema"]=="symport.web/1"'
workspace="$(curl --fail --silent -b "$jar" "$base/")"
grep -q 'name="symport-auth" content="lan"' <<<"$workspace"
# the LAN port is not published on loopback
! curl --silent --max-time 3 "http://127.0.0.1:8765/healthz" >/dev/null
status="$(docker inspect --format '{{.State.Status}}' "$name")"
test "$status" = running
echo "Home LAN smoke test passed"
