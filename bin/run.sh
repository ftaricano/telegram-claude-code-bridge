#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$APP_DIR"

# Supervisors often have a tiny default PATH; make common tool locations visible.
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "fatal: missing .env at $ENV_FILE" >&2
  exit 78
fi

# Load .env so shell-level validation sees the same values as pydantic/dotenv.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Optional macOS Keychain fallback. Prefer env/.env in portable deployments.
KEYCHAIN_SERVICE="${KEYCHAIN_SERVICE:-telegram-claude-code-bridge::TELEGRAM_BOT_TOKEN}"
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" && -x /usr/bin/security ]]; then
  TELEGRAM_BOT_TOKEN="$(/usr/bin/security find-generic-password \
    -s "$KEYCHAIN_SERVICE" \
    -a "$USER" -w 2>/dev/null || true)"
  export TELEGRAM_BOT_TOKEN
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  echo "fatal: TELEGRAM_BOT_TOKEN missing (not in .env, not in Keychain item '$KEYCHAIN_SERVICE')" >&2
  exit 78
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  echo "fatal: venv python not executable at $APP_DIR/.venv/bin/python" >&2
  exit 78
fi

# Production avoids development overrides that stretch Claude timeout to 600s.
# Force this after sourcing .env because the local checkout may keep ENVIRONMENT=development.
export ENVIRONMENT="production"
export CLAUDE_TIMEOUT_SECONDS="${CLAUDE_TIMEOUT_SECONDS:-300}"
export TELEGRAM_PROGRESS_EDIT_INTERVAL="${TELEGRAM_PROGRESS_EDIT_INTERVAL:-6}"
export TELEGRAM_PROGRESS_MAX_FAILURES="${TELEGRAM_PROGRESS_MAX_FAILURES:-1}"
export TELEGRAM_API_RETRY_ATTEMPTS="${TELEGRAM_API_RETRY_ATTEMPTS:-2}"
export STREAM_DRAFT_INTERVAL="${STREAM_DRAFT_INTERVAL:-1.5}"

# Hard guardrail: use Claude CLI/OAuth, not a paid Anthropic API key.
unset ANTHROPIC_API_KEY

# LaunchAgent does not inherit the interactive shell/keychain environment. Claude Code
# auth must be injected explicitly from the long-lived OAuth token created by
# `claude setup-token`; otherwise the SDK surfaces 401 / "Not logged in".
CLAUDE_OAUTH_TOKEN_FILE="${CLAUDE_OAUTH_TOKEN_FILE:-$HOME/.claude/claude_code_oauth_token}"
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" && -f "$CLAUDE_OAUTH_TOKEN_FILE" ]]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(tr -d '\r\n' < "$CLAUDE_OAUTH_TOKEN_FILE")"
  export CLAUDE_CODE_OAUTH_TOKEN
fi
if [[ -z "${ANTHROPIC_OAUTH_TOKEN:-}" && -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  ANTHROPIC_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN"
  export ANTHROPIC_OAUTH_TOKEN
fi
if [[ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]]; then
  echo "fatal: CLAUDE_CODE_OAUTH_TOKEN missing (run 'claude setup-token' and save token to $CLAUDE_OAUTH_TOKEN_FILE)" >&2
  exit 78
fi

echo "starting telegram-claude-code-bridge env=$ENVIRONMENT timeout=${CLAUDE_TIMEOUT_SECONDS}s progress_interval=${TELEGRAM_PROGRESS_EDIT_INTERVAL}s oauth_token_file=$CLAUDE_OAUTH_TOKEN_FILE"
exec "$APP_DIR/.venv/bin/python" -m src.main
