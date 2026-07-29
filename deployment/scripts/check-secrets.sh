#!/usr/bin/env bash
# Secret-hygiene guard for ifet-management (P0 / Ref 42).
#
# Run before every commit and before every deploy:
#     ./deployment/scripts/check-secrets.sh
#
# It fails on the four ways this repo can leak a credential:
#   1. .env committed or stageable
#   2. an Airtable token pattern anywhere in tracked files
#   3. a non-empty password/token in a config file the UI SERVES TO THE BROWSER
#   4. a plaintext credential left behind in compose.yaml
#
# Exit 0 = clean, 1 = a finding. No network, no writes.

set -uo pipefail
cd "$(dirname "$0")/../.." || exit 1

fail=0
note() { printf '  %s\n' "$1"; }
bad()  { printf '\033[31mFAIL\033[0m %s\n' "$1"; fail=1; }
ok()   { printf '\033[32m ok \033[0m %s\n' "$1"; }

# Files served to the browser — these must never hold a real secret.
UI_CONFIGS=(
  "deployment/config/config.json"
  "src/ifet_ui_react/config.json"
)

echo "== 1. .env must not be tracked =="
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  bad ".env is TRACKED by git. Remove it from the index: git rm --cached .env"
else
  ok ".env is not tracked"
fi
if [ -f .env ] && ! git check-ignore -q .env; then
  bad ".env exists but is NOT gitignored"
elif [ -f .env ]; then
  ok ".env exists and is gitignored"
else
  note "no local .env yet — cp .env.example .env before starting the stack"
fi

echo "== 2. no Airtable token in tracked files =="
# Airtable personal access tokens start with 'pat' followed by 14 chars, a dot,
# then a 64-char secret. Matching the prefix + length is enough to catch a paste.
if git grep -nIE 'pat[A-Za-z0-9]{14}\.[A-Za-z0-9]{40,}' -- . >/dev/null 2>&1; then
  bad "an Airtable token pattern appears in tracked content:"
  git grep -nIE 'pat[A-Za-z0-9]{14}\.[A-Za-z0-9]{10,}' -- . | sed 's/\(pat[A-Za-z0-9]\{6\}\).*/\1…REDACTED/' | head
else
  ok "no Airtable token pattern in tracked files"
fi

echo "== 3. browser-served configs hold no secrets =="
for f in "${UI_CONFIGS[@]}"; do
  [ -f "$f" ] || { note "$f not present, skipped"; continue; }
  # Any credential-shaped key with a NON-empty value is a finding. Empty
  # ("password": "") is the known-good state for these two files.
  if grep -nEi '"(password|passwd|token|secret|api[_-]?key)"[[:space:]]*:[[:space:]]*"[^"]+"' "$f" >/dev/null; then
    bad "$f contains a non-empty credential field:"
    grep -nEi '"(password|passwd|token|secret|api[_-]?key)"[[:space:]]*:' "$f" | sed 's/: *"[^"]*"/: "…REDACTED"/' | head
  else
    ok "$f clean (credential fields absent or empty)"
  fi
  if grep -qi 'airtable' "$f"; then
    bad "$f mentions Airtable — the token must never reach a browser-served file"
  fi
done

echo "== 4. compose.yaml uses .env, not literals =="
if grep -nE '^[[:space:]]*(POSTGRES_PASSWORD|PGADMIN_DEFAULT_PASSWORD|AIRTABLE_TOKEN):[[:space:]]*[^$[:space:]]' compose.yaml >/dev/null; then
  bad "compose.yaml still has a literal credential:"
  grep -nE '^[[:space:]]*(POSTGRES_PASSWORD|PGADMIN_DEFAULT_PASSWORD|AIRTABLE_TOKEN):' compose.yaml | head
else
  ok "compose.yaml credentials are all interpolated from the environment"
fi
if grep -nE 'postgresql://[^$"]*:[^$"@]+@' compose.yaml >/dev/null; then
  bad "compose.yaml contains an inline database URL with credentials"
else
  ok "no inline database URL with credentials in compose.yaml"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "secret hygiene: PASS"
else
  echo "secret hygiene: FAIL — fix the findings above before committing or deploying"
fi
exit "$fail"
