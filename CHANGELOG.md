# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Renamed to TapAgent for Telegram.** The project, package and repository are now
  `tapagent-telegram`, and the only console script is `tapagent-telegram` (the
  `telegram-claude-code-bridge`, `claude-code-telegram` and `claude-telegram-bot` scripts
  are gone). The old GitHub URL redirects to the new one. Behaviour is unchanged.

## [1.7.1] - 2026-09-25

### Security
- **Boundary checks also run inside subagents.** An agent definition in the project or
  user `.claude/agents/` directory can set `permissionMode` in its frontmatter, and Claude
  Code applied it to the subagent although the session was pinned to `default`. With
  `bypassPermissions` there, the subagent's tool calls never reached the `can_use_tool`
  callback, so its file tools and Bash could act outside `APPROVED_DIRECTORY`. The
  callback's checks now also run as an SDK `PreToolUse` hook on the guarded tools, which
  Claude Code calls in subagents too, whatever their permission mode. The hook only
  denies, so the callback still runs where it did before.
- **Allow rules in settings no longer skip the boundary checks.** A `permissions.allow`
  rule for a guarded tool in the project or user settings used to pre-approve it without
  the checks. The `PreToolUse` hook now checks those calls as well.

### Changed
- Reads inside the working directory that Claude Code allows without asking are now
  checked by the hook too, for the directory boundary only: a read path that resolves
  outside `APPROVED_DIRECTORY`, through `..`, `~` or a symlink, is denied. The path
  validator's patterns are not applied to these reads, so file names such as a Next.js
  `[...slug]` route or a `posts.$postId.tsx` route can still be read.

## [1.7.0] - 2026-09-25

This fork diverged from upstream
[claude-code-telegram](https://github.com/overwirehq/claude-code-telegram) at 1.6.0.
Upstream's own 1.6.1 has different content.

### Security
- **Tool calls can no longer write Claude Code configuration.** `Write`, `Edit`,
  `MultiEdit`, `NotebookEdit` and Bash are denied on anything in a `.claude` directory
  at any depth (settings, hooks, skills, commands, agents), `.claude.json`, `.mcp.json`
  and the user config directory (`~/.claude/` or `CLAUDE_CONFIG_DIR`, except `plans/` and
  `todos/`), inside `APPROVED_DIRECTORY` too, through symlinks and whatever the letter
  case. `~/.claude/settings.json` is no longer
  treated as an internal path that skips the approved-directory check. An allow rule
  written there would have pre-approved tools on the next session. Reading these files
  is still allowed.
- **The Bash boundary check follows more of the shell.** It tracks `cd` across a command
  line, expands `~` and `$HOME`, checks output redirection targets, subshells, `sh -c`
  scripts and quoted paths in inline interpreter code, splits on newlines and on
  separators without spaces, joins backslash-newline continuations, and treats
  `chmod`, `chown`, `chgrp` and `truncate` as filesystem-modifying.
- **Sandboxed Bash cannot opt out of the sandbox** while the `can_use_tool` callback is
  wired: `allowUnsandboxedCommands` is off, and the callback refuses a Bash call with
  `dangerouslyDisableSandbox`. It also refuses a Bash line that joins a command listed
  in `SANDBOX_EXCLUDED_COMMANDS` with other commands, since Claude Code would run the
  whole line outside the sandbox.
- **Hard links:** file tools refuse to write a file that has other hard links, Bash may
  not create hard links (`ln` without `-s`, `link`, `cp -l`), and a hard link to the
  project settings counts as agent configuration.
- **Settings can no longer switch the permission mode.** While the `can_use_tool`
  callback is wired, the SDK is started with `permission_mode="default"`. Without it,
  Claude Code took `permissions.defaultMode` from the loaded project or user settings,
  and `bypassPermissions` there approved every tool without consulting the callback.
  A `permissions.defaultMode` in those settings no longer turns the checks off.
- File tool paths that start with `~` are expanded before the approved-directory check.
- Proxy URLs without a scheme and user-only proxy tokens are masked in logs, and an
  invalid proxy URL no longer puts its credentials in the startup error.
- **Tool boundary checks now run on the default configuration.** Guarded tools
  (`Read`, `Write`, `Edit`, `MultiEdit`, `NotebookRead`, `NotebookEdit`, `Bash`) were
  passed to the SDK as pre-approved, so the `can_use_tool` callback was never consulted
  for them and the `APPROVED_DIRECTORY` path and Bash boundary checks did not run.
  They are now withheld from the SDK allow rules, and `autoAllowBashIfSandboxed` is
  disabled while the callback is wired. `MultiEdit`, `NotebookRead` and `NotebookEdit`
  paths (including `notebook_path`) are validated as well. Ported from upstream
  [claude-code-telegram](https://github.com/overwirehq/claude-code-telegram) (MIT)
  [PR #220](https://github.com/overwirehq/claude-code-telegram/pull/220), closing
  upstream [#219](https://github.com/overwirehq/claude-code-telegram/issues/219).
- **Proxy credentials are no longer logged.** The `Proxy configured` log entry now
  masks the password in `HTTPS_PROXY`/`HTTP_PROXY`. Ported from upstream
  [PR #218](https://github.com/overwirehq/claude-code-telegram/pull/218), closing
  upstream [#169](https://github.com/overwirehq/claude-code-telegram/issues/169).

### Changed
- **`SANDBOX_EXCLUDED_COMMANDS` is now empty by default** (it was `git`, `npm`, `pip`,
  `poetry`, `make`, `docker`). Claude Code runs a whole command line outside the sandbox
  when any command in it matches an entry, and these tools run code from files the agent
  can write, so any entry removes the Bash sandbox boundary. An entry without a wildcard
  matches only the exact command (`git` matches `git`, not `git status`), so commands
  such as `git status` or `make test` already ran in the sandbox and are not affected.
  `git:*` matches a command with any arguments. Read SECURITY.md before adding an entry.
- **The `can_use_tool` callback refuses tools outside `CLAUDE_ALLOWED_TOOLS`** and
  tools listed in `CLAUDE_DISALLOWED_TOOLS`. Tools that are not listed, such as MCP
  tools or plan-mode tools, must now be added to `CLAUDE_ALLOWED_TOOLS`; MCP server
  rules (`mcp__<server>`, `mcp__<server>__*`) are supported. No MCP tool is
  pre-approved by default. `DISABLE_TOOL_VALIDATION=true` skips these name checks,
  while the path and Bash boundary checks keep running.
- **Only the project Claude Code settings are loaded by default**, as upstream does.
  Set `CLAUDE_LOAD_USER_SETTINGS=true` to also load the user settings
  (`~/.claude/settings.json`), as before.

## [1.6.0] - 2026-03-30

### Added
- **Image/screenshot analysis**: Images sent to the bot are now passed as multimodal content blocks via the SDK, enabling Claude to actually see and analyze them (#168, closes #137)
- **Exponential backoff retry**: Transient `CLIConnectionError` failures are automatically retried with exponential backoff (1s → 3s → 9s, capped at 30s). MCP config errors and timeouts are correctly excluded (#170, closes #60)
- **Local whisper.cpp voice transcription**: New `VOICE_PROVIDER=local` option for offline voice transcription via whisper.cpp + ffmpeg. No API keys required (#158)
- **`make run-watch`**: Auto-restart during development via watchfiles (#158)
- **Inline Stop button**: Cancel running Claude requests with a ⏹ button in the progress message (#122)
- **Slash command passthrough**: Unknown `/commands` in agentic mode are forwarded to Claude as prompts (#131)
- **Proxy support**: Explicit proxy configuration for httpx client via `HTTPS_PROXY`/`HTTP_PROXY` env vars (#166)

### Fixed
- **Empty responses**: "(No content to display)" after tool-heavy tasks — added missing `StreamUpdate` helper methods, fixed `ConversationEnhancer` call signature, and added fallback for tool-only responses (#136, closes #135)
- **ThinkingBlock raw output**: `ThinkingBlock` objects no longer print as raw Python objects — proper `isinstance` checks extract `.thinking` text (#162, closes #161)

## [1.5.0] - 2026-03-04

### Added
- **Voice Message Transcription**: Send voice messages for automatic transcription and Claude processing. Dual provider support: Mistral Voxtral (default) and OpenAI Whisper (#106)
- **`/restart` command**: Restart bot process from Telegram, plus `set_my_commands` timing fix for reliable command registration on startup (#112)
- **Streaming partial responses**: Stream Claude's output in real-time via Telegram `sendMessageDraft` API. Enable with `ENABLE_STREAM_DRAFTS=true` (#123)

### Fixed
- **`/actions` crash**: Corrected `SessionModel` constructor argument in `get_suggestions` (#125, closes #119)
- **Model config ignored**: `claude_model` setting now passed to SDK `ClaudeAgentOptions`. Default deferred to CLI instead of hardcoded sonnet (#121)

### Documentation
- Linux `aiolimiter` DBus installation workaround (#124)

## [1.4.0] - 2026-02-27

### Added
- **Outbound image support**: Claude can now auto-detect and send images to Telegram, plus MCP `send_image_to_user` tool (#99)
- **CLAUDE.md loading**: Project-level CLAUDE.md files are loaded from the working directory and appended to the system prompt
- **Configurable reply quoting**: `REPLY_QUOTE` setting controls message quoting behavior, centralized via PTB Defaults (#111)
- **`max_budget_usd` cost cap**: Per-request cost limit passed to SDK via `ClaudeAgentOptions` (#95)
- **`Skill` and `AskUserQuestion`** added to default allowed tools (#85, #87)
- **Documentation site**: Docs index and README linking (#92)

### Changed
- **ToolMonitor replaced with SDK `can_use_tool` callback**: Security validation now uses the native SDK hook instead of a custom wrapper. `SecurityValidator` wired directly into `ClaudeAgentOptions.can_use_tool` (#62)
- **`DISABLE_TOOL_VALIDATION=true`** now passes `allowed_tools=None` to the SDK, fully bypassing tool name validation
- **Phase 5 cleanup**: `src/claude/` reduced from 2,774 to 1,316 lines (#96)
- **PTB `AIORateLimiter`** replaces manual sync-local `RetryAfter` retry (#86)
- **Project thread sync throttling**: Configurable `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` to avoid Telegram API rate limits (#84)
- **GitHub Actions upgraded** to latest versions for Node 24 compatibility (#67, #68)

### Fixed
- **Empty `CLAUDE_CLI_PATH` causing Permission denied**: Empty string coerced to `None` so SDK auto-discovers the CLI
- **Session resume failing** with generic exit code 1 (#94)
- **Progress message deletion crash**: Bot no longer stops mid-response when progress message deletion fails (#107)
- **General topic routing**: Messages in the General topic of forum supergroups now route correctly (#110)
- **Session ownership enforcement**: `load_session` and `get_or_create_session` now validate ownership (#83)
- **Bash boundary enforcement**: `cd` and chained commands checked against directory boundary (#69)
- **Handler robustness**: Potential `UnboundLocalError` resolved in message handlers (#66)
- **Claude Code internal paths**: `~/.claude/plans/` and `todos/` allowed in tool validation (#89)
- **`Topic_not_modified` treated as success** in topic sync instead of raising an error
- **Test fixes**: `is_forum=False` set on MagicMock chats to prevent test failures (#110)

### Previously Added
- **Agentic Mode** (default interaction model):
  - `MessageOrchestrator` routes messages to agentic (3 commands) or classic (13 commands) handlers based on `AGENTIC_MODE` setting
  - Natural language conversation with Claude -- no terminal commands needed
  - Automatic session persistence per user/project directory
- **Event-Driven Platform**:
  - `EventBus` -- async pub/sub system with typed event subscriptions (UserMessage, Webhook, Scheduled, AgentResponse)
  - `AgentHandler` -- bridges events to `ClaudeIntegration.run_command()` for webhook and scheduled event processing
  - `EventSecurityMiddleware` -- validates events before handler processing
- **Webhook API Server** (FastAPI):
  - `POST /webhooks/{provider}` endpoint for GitHub, Notion, and generic providers
  - GitHub HMAC-SHA256 signature verification
  - Generic Bearer token authentication
  - Atomic deduplication via `webhook_events` table
  - Health check at `GET /health`
- **Job Scheduler** (APScheduler):
  - Cron-based job scheduling with persistent storage in `scheduled_jobs` table
  - Jobs publish `ScheduledEvent` to event bus on trigger
  - Add, remove, and list jobs programmatically
- **Notification Service**:
  - Subscribes to `AgentResponseEvent` for Telegram delivery
  - Per-chat rate limiting (1 msg/sec) to respect Telegram limits
  - Message splitting at 4096 char boundary
  - Broadcast to configurable default chat IDs
- **Database Migration 3**: `scheduled_jobs` and `webhook_events` tables, WAL mode enabled
- **Automatic Session Resumption**: Sessions are now automatically resumed per user+directory
  - SDK integration passes `resume` parameter to Claude Code for real session continuity
  - Session IDs extracted from Claude's `ResultMessage` instead of generated locally
  - `/cd` looks up and resumes existing sessions for the target directory
  - Auto-resume from SQLite database survives bot restarts
  - Graceful fallback to fresh session when resume fails
  - `/new` and `/end` are the only ways to explicitly clear session context

### Recently Completed

#### Storage Layer Implementation (TODO-6) - 2025-06-06
- **SQLite Database with Complete Schema**:
  - 7 core tables: users, sessions, messages, tool_usage, audit_log, user_tokens, cost_tracking
  - Foreign key relationships and proper indexing for performance
  - Migration system with schema versioning and automatic upgrades
  - Connection pooling for efficient database resource management
- **Repository Pattern Data Access Layer**:
  - UserRepository, SessionRepository, MessageRepository, ToolUsageRepository
  - AuditLogRepository, CostTrackingRepository, AnalyticsRepository
- **Persistent Session Management**:
  - SQLiteSessionStorage replacing in-memory storage
  - Session persistence across bot restarts and deployments
- **Analytics and Reporting System**:
  - User dashboards with usage statistics and cost tracking
  - Admin dashboards with system-wide analytics

#### Telegram Bot Core (TODO-4) - 2025-06-06
- Complete Telegram bot with command routing, message parsing, inline keyboards
- Navigation commands: /cd, /ls, /pwd for directory management
- Session commands: /new, /continue, /status for Claude sessions
- File upload support, progress indicators, response formatting

#### Claude Code Integration (TODO-5) - 2025-06-06
- Async process execution with timeout handling
- Session state management and cross-conversation continuity
- Streaming JSON output parsing, tool call extraction
- Cost tracking and usage monitoring

#### Authentication & Security Framework (TODO-3) - 2025-06-05
- Multi-provider authentication (whitelist + token)
- Rate limiting with token bucket algorithm
- Input validation, path traversal prevention
- Security audit logging with risk assessment
- Bot middleware framework (auth, rate limit, security, burst protection)

## [0.1.0] - 2025-06-05

### Added

#### Project Foundation (TODO-1)
- Complete project structure with Poetry dependency management
- Exception hierarchy, structured logging, testing framework
- Code quality tools: Black, isort, flake8, mypy with strict settings

#### Configuration System (TODO-2)
- Pydantic Settings v2 with environment variable loading
- Environment-specific overrides (development, testing, production)
- Feature flags system for dynamic functionality control
- Comprehensive validation with cross-field dependencies

## Development Status

- **TODO-1**: Project Structure & Core Setup -- Complete
- **TODO-2**: Configuration Management -- Complete
- **TODO-3**: Authentication & Security Framework -- Complete
- **TODO-4**: Telegram Bot Core -- Complete
- **TODO-5**: Claude Code Integration -- Complete
- **TODO-6**: Storage & Persistence -- Complete
- **TODO-7**: Advanced Features -- Complete (agentic platform, webhooks, scheduler, notifications)
- **TODO-8**: Complete Testing Suite -- In progress
- **TODO-9**: Deployment & Documentation -- In progress
