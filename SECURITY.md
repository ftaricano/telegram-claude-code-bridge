# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 1.6.x   | Current development |

## Security Model

TapAgent for Telegram implements a defense-in-depth security model with multiple layers:

### 1. Authentication & Authorization
- **User Whitelist**: Only pre-approved Telegram user IDs can access the bot
- **Token-Based Auth**: Optional token-based authentication for additional security
- **Session Management**: Secure session handling with timeout and cleanup

### 2. Directory Boundaries
- **Approved Directory**: All operations confined to a pre-configured directory tree
- **Path Validation**: Prevents directory traversal attacks (../../../etc/passwd)
- **Permission Checks**: Validates file system permissions before operations
- **Pre-execution Tool Checks**: Claude's own tool calls are validated before they run
  via the SDK `can_use_tool` callback: tools outside `CLAUDE_ALLOWED_TOOLS` are refused,
  file paths are checked for `Read`, `Write`, `Edit`, `MultiEdit`, `NotebookRead` and
  `NotebookEdit`, and `Bash` commands are checked for directory escapes. See
  *Tool Call Enforcement* below.

### 3. Input Validation
- **Command Sanitization**: All user inputs sanitized to prevent injection attacks
- **File Type Validation**: Only allowed file types can be uploaded
- **Path Sanitization**: Removes dangerous characters and patterns (`;`, `&&`, `$()`, `..`)
- **Secret File Protection**: Blocks access to `.env`, `.ssh`, `id_rsa`, `.pem` files

### 4. Rate Limiting
- **Request Rate Limiting**: Token bucket algorithm prevents abuse with configurable limits
- **Cost-Based Limiting**: Tracks and limits Claude usage costs per user
- **Burst Protection**: Configurable burst capacity prevents spike attacks

### 5. Audit Logging
- **Authentication Events**: All login attempts and auth failures logged
- **Command Execution**: All commands and file operations logged
- **Security Violations**: Path traversal attempts, injection attempts, and other violations logged
- **Risk Assessment**: Automatic severity classification for security events

### 6. Webhook Authentication
- **GitHub HMAC-SHA256**: Webhook payloads verified against `X-Hub-Signature-256` header using a shared secret
- **Generic Bearer Token**: Non-GitHub providers authenticated via `Authorization: Bearer <token>` header
- **Deduplication**: Atomic `INSERT OR IGNORE` on delivery ID prevents replay attacks
- **Event Security Middleware**: Validates webhook events before handler processing

## Current Security Status

All planned security features are implemented and active:

- Multi-provider authentication system (whitelist + token)
- Rate limiting with token bucket algorithm (request and cost-based)
- Input validation with path traversal, command injection, and zip bomb protection
- Directory isolation with approved directory boundaries
- Security audit logging with risk assessment and event tracking
- Bot middleware framework (auth, rate limit, security, burst protection)
- Webhook signature verification (GitHub HMAC-SHA256, generic Bearer token)
- Event security middleware for webhook and scheduled event validation
- Configuration security via Pydantic validators and SecretStr
- Pre-execution checks on Claude's tool calls (`can_use_tool`)
- Proxy credentials are masked before the proxy URL is logged

### Tool Call Enforcement

The `can_use_tool` callback in `src/claude/sdk_integration.py` enforces the tool
allowlist and the approved-directory boundary on tool calls that Claude itself
initiates.

The callback only runs when the Claude CLI sends a permission request, and the CLI
resolves its allow rules before asking. To keep the checks live,
`ClaudeSDKManager.execute_command` removes the guarded tools (`Read`, `Write`, `Edit`,
`MultiEdit`, `NotebookRead`, `NotebookEdit`, `Bash`) from the `allowed_tools` it passes
to the SDK and disables `autoAllowBashIfSandboxed`. The callback then allows a guarded
tool only when it is in `CLAUDE_ALLOWED_TOOLS` and passes the boundary checks, and it
refuses any other tool that is not in `CLAUDE_ALLOWED_TOOLS` or that is in
`CLAUDE_DISALLOWED_TOOLS`.

The same checks also run as an SDK `PreToolUse` hook on the guarded tools. Claude Code
calls hooks on every tool call, including calls from subagents and calls to tools that
an allow rule pre-approves, where the callback is never consulted. The hook only denies:
when the checks pass it returns no decision, the permission request still reaches the
callback, and the callback runs the same checks again.

**When it is active:** whenever a `SecurityValidator` is wired (the default for the
bot). `DISABLE_TOOL_VALIDATION=true` turns off the name-based allow/deny lists, but the
path and Bash boundary checks keep running.

**Limits:**

- Direct use of `ClaudeSDKManager` without a `SecurityValidator` has no callback.
- Tool calls may read, but never write, Claude Code configuration: anything in a
  `.claude` directory at any depth (settings, hooks, skills, commands, agents), `.claude.json`,
  `.mcp.json`, and anything in the user config directory (`~/.claude/`, or
  `CLAUDE_CONFIG_DIR`) other than `plans/` and `todos/`. Names are compared
  case-insensitively, as macOS and Windows filesystems do. This holds inside
  `APPROVED_DIRECTORY` too, and through symlinks. File tools refuse to write a file that
  has other hard links, and Bash may not create hard links (`ln` without `-s`, `link`,
  `cp -l`). Edit these files yourself.
- The SDK loads only the `project` setting source by default.
  `CLAUDE_LOAD_USER_SETTINGS=true` also loads the `user` source. An allow rule in a loaded
  settings file pre-approves a tool before the callback is consulted. For the guarded
  tools the `PreToolUse` hook still runs the checks. Any other tool pre-approved there is
  not checked against `CLAUDE_ALLOWED_TOOLS`.
- While the callback is wired, the bridge starts Claude Code with the `default` permission
  mode. Without an explicit mode, Claude Code would take `permissions.defaultMode` from the
  loaded settings, and `bypassPermissions` there approves every tool without consulting
  the callback. The explicit mode takes precedence, so a `permissions.defaultMode` in the
  project or user settings no longer turns the checks off.
- An agent definition in a loaded `.claude/agents/` directory (project, or user with
  `CLAUDE_LOAD_USER_SETTINGS=true`) may set `permissionMode` in its frontmatter, and Claude
  Code applies it to the subagent even though the session runs in `default` mode. With
  `bypassPermissions` there, the subagent never consults the callback. The boundary checks
  still run inside the subagent through the `PreToolUse` hook, but its tools outside the
  guarded set are not checked against `CLAUDE_ALLOWED_TOOLS`.
- Because the hook runs on every call to a guarded tool, it also checks reads inside the
  working directory that Claude Code allows without asking. For reads the hook checks only
  the directory boundary, after resolving `~`, `..` and symlinks, and not the path
  validator's patterns, so a file such as `[...slug]/page.tsx` can be read. Writes, and
  reads that Claude Code asks the callback about, still go through the full path validator.
- The Bash boundary check is static and best effort. It follows `cd`, `~` and `$HOME`,
  output redirection, `sh -c` and quoted paths in inline interpreter code (`python -c`),
  but it cannot see into scripts run from files, archive extraction or version-control
  checkouts. For those, the OS sandbox (`SANDBOX_ENABLED`) is the boundary: while the
  callback is wired, Bash may not ask to run outside it (`allowUnsandboxedCommands` is
  off, and a Bash call with `dangerouslyDisableSandbox` is refused).
- `SANDBOX_EXCLUDED_COMMANDS` is empty by default, and any entry removes the OS boundary
  for Bash:
  - Claude Code splits a command line into its commands. When any of them matches an
    entry, the **whole line** runs outside the sandbox, not only that command.
  - Tools such as `make`, `npm`, `docker` and `git` run code from files the agent can
    write (Makefiles, package scripts, Dockerfiles, hooks, aliases), so even a command
    run on its own can execute agent-written code outside the sandbox.
  - An entry without a wildcard matches only the exact command line: `git` matches
    `git`, not `git status`. `git:*` matches `git` with any arguments.
  - While the callback is wired, a Bash line that joins an excluded command with other
    commands is refused. An excluded command on its own still runs outside the sandbox.
- With `SANDBOX_ENABLED=false` there is no OS boundary for Bash. The static check alone is
  not a boundary, so keep the sandbox on wherever the bot takes untrusted input.
- `Glob`, `Grep` and `LS` are read-only and are not path-checked.

## Security Configuration

### Required Security Settings

```bash
# Base directory for all operations (CRITICAL)
APPROVED_DIRECTORY=/path/to/approved/projects

# User access control
ALLOWED_USERS=123456789,987654321  # Telegram user IDs

# Optional: Token-based authentication
ENABLE_TOKEN_AUTH=true
AUTH_TOKEN_SECRET=<AUTH_TOKEN_SECRET>  # Generate with: openssl rand -hex 32
```

### Webhook Security Settings

```bash
# GitHub webhook signature verification
GITHUB_WEBHOOK_SECRET=<GITHUB_WEBHOOK_SECRET>

# Generic webhook Bearer token
WEBHOOK_API_SECRET=<WEBHOOK_API_SECRET>

# API server (required for webhooks)
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
```

### Recommended Security Settings

```bash
# Strict rate limiting for production
RATE_LIMIT_REQUESTS=5
RATE_LIMIT_WINDOW=60
RATE_LIMIT_BURST=10

# Cost controls
CLAUDE_MAX_COST_PER_USER=5.0

# Security features
ENABLE_TELEMETRY=true  # For security monitoring
LOG_LEVEL=INFO         # Capture security events

# Environment
ENVIRONMENT=production  # Enables strict security defaults
```

## Security Best Practices

### For Administrators

1. **Directory Configuration**
   ```bash
   # Use minimal necessary permissions
   chmod 755 /path/to/approved/projects

   # Avoid sensitive directories
   # Don't use: /, /home, /etc, /var
   # Use: /home/user/projects, /opt/bot-projects
   ```

2. **Token Management**
   ```bash
   # Generate secure secrets
   openssl rand -hex 32

   # Store in environment, never in code
   export AUTH_TOKEN_SECRET="generated-secret"
   export GITHUB_WEBHOOK_SECRET="generated-secret"
   export WEBHOOK_API_SECRET="generated-secret"
   ```

3. **User Management**
   ```bash
   # Get Telegram User ID: message @userinfobot
   # Add to whitelist
   export ALLOWED_USERS="123456789,987654321"
   ```

4. **Monitoring**
   ```bash
   # Enable logging and monitoring
   export LOG_LEVEL=INFO
   export ENABLE_TELEMETRY=true

   # Monitor logs for security events
   tail -f bot.log | grep -i "security\|auth\|violation"
   ```

### For Developers

1. **Never Commit Secrets** -- use `.gitignore` for `.env`, `*.key`, `*.pem`
2. **Use Type Safety** -- all functions must have type hints (`mypy --strict`)
3. **Validate All Inputs** -- use `SecurityValidator` for user-provided paths and commands
4. **Log Security Events** -- use structlog with `violation_type` and `user_id` context

## Threat Model

### Threats We Protect Against

1. **Directory Traversal** (High Priority) -- path traversal, symlink attacks
2. **Command Injection** (High Priority) -- shell injection, env var injection
3. **Unauthorized Access** (Medium Priority) -- non-whitelisted users, token replay
4. **Resource Abuse** (Medium Priority) -- rate limit bypass, cost limit violations
5. **Webhook Forgery** (Medium Priority) -- unsigned payloads, replay attacks
6. **Information Disclosure** (Low Priority) -- sensitive file exposure, error leakage

### Threats Outside Scope

- Network-level attacks (handled by hosting infrastructure)
- Telegram API vulnerabilities (handled by Telegram)
- Host OS security (handled by system administration)

## Reporting a Vulnerability

**Do not create public GitHub issues for security vulnerabilities.**

For security issues, use GitHub private vulnerability reporting:
https://github.com/ftaricano/tapagent-telegram/security/advisories/new

Include: description, steps to reproduce, potential impact, and suggested mitigation.

### Response Process

1. **Acknowledgment** within 48 hours
2. **Initial assessment** within 1 week
3. **Fix development** as soon as possible
4. **Security advisory** published after fix

## Production Checklist

- [ ] `APPROVED_DIRECTORY` properly configured and restricted
- [ ] `ALLOWED_USERS` whitelist configured
- [ ] Rate limiting enabled and configured
- [ ] Logging enabled and monitored
- [ ] Authentication tokens properly secured
- [ ] `GITHUB_WEBHOOK_SECRET` set (if using GitHub webhooks)
- [ ] `WEBHOOK_API_SECRET` set (if using generic webhooks)
- [ ] API server behind reverse proxy with TLS (if enabled)
- [ ] Environment variables properly configured
- [ ] File permissions properly set
- [ ] Network access properly restricted
- [ ] All dependencies updated to latest secure versions
