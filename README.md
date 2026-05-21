# Telegram Claude Code Bridge

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/ftaricano/telegram-claude-code-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/ftaricano/telegram-claude-code-bridge/actions/workflows/ci.yml)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-OAuth%2FCLI-6B46C1.svg)](https://claude.ai/code)

Telegram Claude Code Bridge connects Telegram chats and forum topics to
Claude Code. It is built for remote development workflows where each Telegram
topic can keep its own Claude session, durable delivery state, project context,
and operator controls.

This is an independent fork and evolution of
[RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram).
It is not an official Anthropic or Telegram project.

## Why This Fork

- Topic-scoped Claude sessions for Telegram forum workflows
- Persistent SQLite state for topic sessions, delivery checkpoints, goals, and context summaries
- Durable Telegram delivery with replayable checkpoints and delivery metrics
- Long-context compaction per topic
- AskUserQuestion support through Telegram inline buttons
- Claude Code authentication through CLI/OAuth, not Anthropic API keys
- Security controls for approved directories, file uploads, webhooks, tool allowlists, and token redaction

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for local development
- Claude Code CLI installed and authenticated with OAuth/subscription credentials
- Telegram bot token from [@BotFather](https://t.me/botfather)
- At least one allowed Telegram user ID for production use

Direct Anthropic API key authentication is intentionally unsupported.

## Install

From source:

```bash
git clone https://github.com/ftaricano/telegram-claude-code-bridge.git
cd telegram-claude-code-bridge
make dev
```

Run tests and lint before making changes:

```bash
make test
make lint
```

## Quick Start

Create local configuration:

```bash
cp .env.example .env
```

Minimum required settings:

```bash
TELEGRAM_BOT_TOKEN=<TELEGRAM_BOT_TOKEN>
TELEGRAM_BOT_USERNAME=<YOUR_BOT_USERNAME>
APPROVED_DIRECTORY=/absolute/path/to/projects
ALLOWED_USERS=123456789
USE_SDK=true
```

Start the bot:

```bash
make run
```

For debug output:

```bash
make run-debug
```

## Core Modes

### Agentic Mode

Agentic mode is the default. Users send natural-language requests and Claude
Code works inside the configured approved directory.

Useful commands:

```text
/start
/new
/status
/goal
/verbose
/context
/compact
/sync_threads
```

### Topic Sessions

Each Telegram forum topic maps to one persisted Claude session. Messages in
the same topic resume the same SDK session; messages in another topic use a
different session. `/new` resets only the current topic.

Enable project thread routing with:

```bash
ENABLE_PROJECT_THREADS=true
PROJECT_THREADS_MODE=private
PROJECTS_CONFIG_PATH=config/projects.example.yaml
```

Use `config/projects.example.yaml` as the public template. Keep personal
registries such as `config/projects.local.yaml` out of Git.

### Classic Mode

Set `AGENTIC_MODE=false` to use the older command-style interface with
directory navigation, quick actions, Git helpers, and session export commands.

## Configuration

The main configuration surface is `.env`. The public template is
[.env.example](.env.example).

Common settings:

```bash
CLAUDE_TIMEOUT_SECONDS=300
CLAUDE_MAX_COST_PER_USER=10.0
CLAUDE_MAX_COST_PER_REQUEST=5.0
CLAUDE_ALLOWED_TOOLS=Read,Write,Edit,Bash,Glob,Grep,LS,Task,TodoRead,TodoWrite,WebSearch,Skill,AskUserQuestion
RATE_LIMIT_REQUESTS=10
RATE_LIMIT_WINDOW=60
DATABASE_URL=sqlite:///data/bot.db
```

Optional voice transcription:

```bash
ENABLE_VOICE_MESSAGES=true
VOICE_PROVIDER=local
WHISPER_CPP_MODEL_PATH=base
```

Cloud voice providers require their own API keys:

```bash
MISTRAL_API_KEY=<MISTRAL_API_KEY>
OPENAI_API_KEY=<OPENAI_API_KEY>
```

## Webhooks And Automation

The bridge can run a FastAPI webhook server and route events into Claude.

```bash
ENABLE_API_SERVER=true
API_SERVER_PORT=8080
GITHUB_WEBHOOK_SECRET=<GITHUB_WEBHOOK_SECRET>
WEBHOOK_API_SECRET=<WEBHOOK_API_SECRET>
```

GitHub webhooks use HMAC-SHA256 verification. Generic webhooks require
`Authorization: Bearer <WEBHOOK_API_SECRET>`.

## Security

Security is a first-class concern because this bridge connects a chat surface
to an agent that can read and modify local files.

Important controls:

- Telegram access is restricted by `ALLOWED_USERS` unless development mode is explicitly enabled.
- Claude file operations are constrained to `APPROVED_DIRECTORY`.
- Bash and file tool calls are validated before execution.
- `.env`, local MCP config, SQLite databases, logs, coverage, and project-specific registries are ignored by Git.
- Telegram bot tokens in HTTP client logs are redacted by a root logging filter.
- Claude Code uses CLI/OAuth credentials. Do not set `ANTHROPIC_API_KEY`.

Never commit:

```text
.env
config/mcp.json
config/projects.local.yaml
config/projects.ferd.yaml
data/
logs/
htmlcov/
*.db
```

See [SECURITY.md](SECURITY.md) for the threat model, production checklist, and
private vulnerability reporting guidance.

## Development

```bash
make dev       # Install dependencies and hooks
make format    # Black + isort
make lint      # Black check + isort check + flake8
make typecheck # mypy type checks
make test      # Pytest with coverage
make clean     # Remove generated local artifacts
```

CI runs lint and tests on pushes and pull requests to `main`.

## Documentation

- [Setup guide](docs/setup.md)
- [Configuration reference](docs/configuration.md)
- [Development guide](docs/development.md)
- [Tools reference](docs/tools.md)
- [Local whisper.cpp setup](docs/local-whisper-cpp.md)

## Attribution

This project is derived from
[RichardAtCT/claude-code-telegram](https://github.com/RichardAtCT/claude-code-telegram),
originally authored by Richard Atkinson. This fork is maintained by Fernando
Taricano and includes substantial changes for topic-scoped Claude Code
sessions, durable delivery, and operator workflows.

See [NOTICE](NOTICE) for attribution and redistribution notes.

## License

MIT License. See [LICENSE](LICENSE).
