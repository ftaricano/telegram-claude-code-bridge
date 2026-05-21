# Development Guide

This document provides detailed information for developers working on Telegram Claude Code Bridge.

## Getting Started

### Prerequisites

- Python 3.11 or higher (uv can install one automatically if missing)
- [uv](https://docs.astral.sh/uv/) for dependency management and virtualenvs
- Git for version control
- Claude Code CLI installed and authenticated through OAuth/subscription credentials

### Initial Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ftaricano/telegram-claude-code-bridge.git
   cd telegram-claude-code-bridge
   ```

2. **Install uv** (if not already installed):
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   # or: brew install uv
   ```

3. **Install dependencies**:
   ```bash
   make dev
   ```

4. **Set up pre-commit hooks** (optional but recommended):
   ```bash
   uv run pre-commit install
   ```

5. **Create configuration file**:
   ```bash
   cp .env.example .env
   # Edit .env with your development settings
   ```

## Development Workflow

### Daily Development

1. **Use `uv run` to invoke commands inside the project virtualenv**, or activate it manually:
   ```bash
   source .venv/bin/activate
   ```

2. **Run tests continuously during development**:
   ```bash
   make test
   ```

3. **Format code before committing**:
   ```bash
   make format
   ```

4. **Check code quality**:
   ```bash
   make lint
   ```

### Available Make Commands

```bash
make help          # Show all available commands
make install       # Install production dependencies only
make dev           # Install all dependencies including dev tools
make test          # Run full test suite with coverage
make lint          # Run format/import/lint checks
make typecheck     # Run mypy type checks
make format        # Auto-format all code
make clean         # Clean up generated files
make run           # Run the bot in normal mode
make run-debug     # Run the bot with debug logging

# Version management
make version       # Show current version
make bump-patch    # Bump patch version, commit, and tag
make bump-minor    # Bump minor version, commit, and tag
make bump-major    # Bump major version, commit, and tag
make release       # Push tag to trigger GitHub release workflow
```

## Project Architecture

### Package Structure

```
src/
|-- api/              # Optional FastAPI webhook server
|-- bot/              # Telegram bot runtime, handlers, middleware, features
|-- claude/           # Claude Code SDK integration and session handling
|-- config/           # Pydantic settings, environment loading, feature flags
|-- events/           # Event bus, event handlers, middleware, event types
|-- mcp/              # MCP server surface for Telegram tools
|-- notifications/    # Telegram notification delivery helpers
|-- projects/         # Project registry and thread mapping
|-- scheduler/        # Scheduled job support
|-- security/         # Auth, validators, rate limiting, audit logging
|-- storage/          # SQLite persistence, migrations, repositories
|-- utils/            # Constants and shared utilities
|-- exceptions.py     # Custom exception hierarchy
`-- main.py           # Application entry point
```

### Testing Structure

```
tests/
|-- unit/             # Unit tests, mostly mirroring src modules
|-- integration/      # Cross-component topic, goal, and AQ flow tests
|-- chaos/            # Durability and failure-mode tests
|-- conftest.py       # Shared pytest configuration
`-- test_idempotency.py
```

## Code Standards

### Code Style

We use strict code formatting and quality tools:

- **Black**: Code formatting with 88-character line length
- **isort**: Import sorting with Black compatibility
- **flake8**: Linting with 88-character line length
- **mypy**: Static type checking with strict settings (`make typecheck`)

### Type Hints

All code must include comprehensive type hints:

```python
from typing import Optional, List, Dict, Any
from pathlib import Path

def process_config(
    settings: Settings,
    overrides: Optional[Dict[str, Any]] = None
) -> Path:
    """Process configuration with optional overrides."""
    # Implementation
    return Path("/example")
```

### Error Handling

Use the custom exception hierarchy defined in `src/exceptions.py`:

```python
from src.exceptions import ConfigurationError, SecurityError

try:
    # Some operation
    pass
except ValueError as e:
    raise ConfigurationError(f"Invalid configuration: {e}") from e
```

### Logging

Use structured logging throughout:

```python
import structlog

logger = structlog.get_logger()

def some_function():
    logger.info("Operation started", operation="example", user_id=123)
    try:
        # Some operation
        logger.debug("Step completed", step="validation")
    except Exception as e:
        logger.error("Operation failed", error=str(e), operation="example")
        raise
```

## Testing Guidelines

### Test Organization

- **Unit tests**: Test individual functions and classes in isolation
- **Integration tests**: Test component interactions
- **End-to-end tests**: Test complete workflows (planned)

### Writing Tests

```python
import pytest
from src.config import create_test_config

def test_feature_with_config():
    """Test feature with specific configuration."""
    config = create_test_config(
        debug=True,
        claude_max_turns=5
    )

    # Test implementation
    assert config.debug is True
    assert config.claude_max_turns == 5

@pytest.mark.asyncio
async def test_async_feature():
    """Test async functionality."""
    # Test async code
    result = await some_async_function()
    assert result is not None
```

### Test Coverage

We aim for strong coverage on security-sensitive flows. Before submitting a PR,
run the same commands CI runs:

```bash
make lint
make test
```

Run `make typecheck` when changing typed runtime paths or public interfaces.

## Maintenance Focus

Prioritize small, well-tested changes in these areas:

- Telegram delivery durability and retry behavior
- Topic-scoped session persistence
- Claude Code SDK/OAuth behavior; direct Anthropic API key auth is unsupported
- Security validation for paths, files, webhooks, and tool allowlists
- Documentation that keeps public setup examples generic and secret-free

## Development Environment Configuration

### Required Environment Variables

For development, set these in your `.env` file:

```bash
# Required for basic functionality
TELEGRAM_BOT_TOKEN=test_token_for_development
TELEGRAM_BOT_USERNAME=test_bot
APPROVED_DIRECTORY=/path/to/your/test/projects

# Claude Authentication
# Use existing Claude CLI/OAuth auth. Direct Anthropic API keys are unsupported.

# Development settings
DEBUG=true
DEVELOPMENT_MODE=true
LOG_LEVEL=DEBUG
ENVIRONMENT=development

# Optional for testing specific features
ENABLE_GIT_INTEGRATION=true
ENABLE_FILE_UPLOADS=true
ENABLE_QUICK_ACTIONS=true
```

### Running in Development Mode

```bash
# Basic run with environment variables
export TELEGRAM_BOT_TOKEN=test_token
export TELEGRAM_BOT_USERNAME=test_bot
export APPROVED_DIRECTORY=/tmp/test_projects
make run-debug

# Or with .env file
make run-debug
```

The debug output will show:
- Configuration loading steps
- Environment overrides applied
- Feature flags enabled
- Validation results

## Version Management

### How versioning works

The version is defined in a single place: `pyproject.toml`. At runtime, `src/__init__.py` reads it via `importlib.metadata`. There is no hardcoded version string to keep in sync.

### Cutting a release

```bash
# Bump the version (choose one) - commits, tags, and pushes automatically
make bump-patch    # 1.2.0 -> 1.2.1
make bump-minor    # 1.2.0 -> 1.3.0
make bump-major    # 1.2.0 -> 2.0.0
```

`make bump-*` runs `uv version --bump <level>`, commits `pyproject.toml` and `uv.lock`, creates a git tag, and pushes both to GitHub. The tag push triggers the release workflow:

1. Runs the full lint + test suite
2. Creates a GitHub Release with auto-generated release notes
3. Updates the rolling `latest` git tag (stable releases only)

### Pre-releases

Tags containing `-rc`, `-beta`, or `-alpha` (e.g. `v1.3.0-rc1`) are marked as pre-releases on GitHub and do **not** update the `latest` tag.

## Contributing

### Before Submitting a PR

1. **Run the full test suite**:
   ```bash
   make test
   ```

2. **Check code quality**:
   ```bash
   make lint
   ```

3. **Format code**:
   ```bash
   make format
   ```

4. **Update documentation** if needed

5. **Add tests** for new functionality

### Commit Message Format

Use conventional commits:

```
feat: add rate limiting functionality
fix: resolve configuration validation issue
docs: update development guide
test: add tests for authentication system
```

### Code Review Guidelines

- All code must pass linting and type checking
- Test coverage should not decrease
- New features require documentation updates
- Security-related changes require extra review

## Common Development Tasks

### Adding a New Configuration Option

1. **Add to Settings class** in `src/config/settings.py`:
   ```python
   new_setting: bool = Field(False, description="Description of new setting")
   ```

2. **Add to .env.example** with documentation

3. **Add validation** if needed

4. **Write tests** in `tests/unit/test_config.py`

5. **Update documentation** in `docs/configuration.md`

### Adding a New Feature Flag

1. **Add property** to `FeatureFlags` class in `src/config/features.py`:
   ```python
   @property
   def new_feature_enabled(self) -> bool:
       return self.settings.enable_new_feature
   ```

2. **Add to enabled features list**

3. **Write tests**

### Debugging Configuration Issues

1. **Use debug logging**:
   ```bash
   make run-debug
   ```

2. **Check validation errors** in the logs

3. **Verify environment variables without printing secret values**:
   ```bash
   test -n "$TELEGRAM_BOT_TOKEN" && echo "TELEGRAM_BOT_TOKEN is set"
   command -v claude
   ```

4. **Test configuration loading**:
   ```python
   from src.config import load_config

   config = load_config()
   print(
       config.model_dump(
           exclude={
               "telegram_bot_token",
               "auth_token_secret",
               "mistral_api_key",
               "openai_api_key",
           }
       )
   )
   ```

## Troubleshooting

### Common Issues

1. **Import errors**: Make sure the project virtualenv is active (`source .venv/bin/activate`) or prefix commands with `uv run`

2. **Configuration validation errors**: Check that required environment variables are set

3. **Test failures**: Ensure test dependencies are installed (`make dev`)

4. **Type checking errors**: Run `make typecheck` to see detailed errors

5. **Dependency resolution issues**: Try `uv lock --refresh` to regenerate the lock file

### Getting Help

- Check the logs with `make run-debug`
- Review test output with `make test`
- Examine the implementation documentation in `docs/`
- Look at existing code patterns in the completed modules
