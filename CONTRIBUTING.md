# Contributing to Telegram Claude Code Bridge

Thank you for your interest in contributing. This document provides the
expected development workflow for changes to this project.

## Development Status

This project is a public, usable bridge under active maintenance. The core bot,
Claude Code SDK integration, persistence layer, security middleware, project
thread routing, file handling, voice handling, and webhook surfaces are already
implemented.

Good contribution areas:

- Documentation improvements
- Tests for existing behavior
- Security hardening
- Reliability improvements for Telegram delivery and Claude Code sessions
- Small, focused feature improvements

## Getting Started

### Prerequisites

- Python 3.11 or higher (uv can install one automatically if missing)
- [uv](https://docs.astral.sh/uv/) for dependency management and virtualenvs
- Git for version control

### Setting Up Development Environment

1. **Fork and clone the repository**:
   ```bash
   git clone https://github.com/your-username/telegram-claude-code-bridge.git
   cd telegram-claude-code-bridge
   ```

2. **Install dependencies**:
   ```bash
   make dev
   ```
   Behind the scenes this runs `uv sync` and installs dependencies into
   `.venv/`. It also installs pre-commit hooks when available.

3. **Set up configuration**:
   ```bash
   cp .env.example .env
   # Edit .env with your development settings
   ```

4. **Verify setup**:
   ```bash
   make test
   make lint
   ```

## Development Workflow

### Before Starting Work

1. **Check existing issues** for similar work
2. **Create an issue** if none exists
3. **Comment on the issue** to indicate you're working on it
4. **Create a feature branch** from main

### Making Changes

1. **Follow the project structure**:
   ```
   src/
   |-- api/        # Optional HTTP/webhook server
   |-- bot/        # Telegram bot runtime
   |-- claude/     # Claude Code SDK integration
   |-- config/     # Configuration loading and validation
   |-- events/     # Event bus and event handlers
   |-- mcp/        # MCP server surface
   |-- projects/   # Project registry and topic mapping
   |-- security/   # Auth, validators, rate limiting, audit logging
   `-- storage/    # SQLite persistence and repositories
   ```

2. **Write tests** for new functionality:
   ```bash
   # Add tests in tests/unit/ or tests/integration/
   make test
   ```

3. **Follow code standards**:
   ```bash
   make format  # Auto-format code
   make lint    # Check code quality
   ```

4. **Update documentation** as needed

### Code Standards

#### Type Hints

All code must include comprehensive type hints:

```python
from typing import Optional, List, Dict, Any
from pathlib import Path

async def process_data(
    items: List[Dict[str, Any]],
    config: Optional[Path] = None
) -> bool:
    """Process data with optional config."""
    # Implementation
    return True
```

#### Error Handling

Use the custom exception hierarchy:

```python
from src.exceptions import ConfigurationError, SecurityError

try:
    # Some operation
    pass
except ValueError as e:
    raise ConfigurationError(f"Invalid configuration: {e}") from e
```

#### Logging

Use structured logging:

```python
import structlog

logger = structlog.get_logger()

def some_function():
    logger.info("Operation started", operation="example", user_id=123)
    # Implementation
```

#### Testing

Write comprehensive tests:

```python
import pytest
from src.config import create_test_config

@pytest.mark.asyncio
async def test_feature():
    """Test feature functionality."""
    config = create_test_config(debug=True)
    # Test implementation
    assert config.debug is True
```

## Contribution Types

### Documentation Improvements

- API documentation
- User guides
- Deployment guides
- Architecture documentation

### Testing Improvements

- Integration tests
- End-to-end tests
- Performance tests
- Security tests

### Runtime Improvements

- Telegram delivery resilience
- Topic session durability
- Local-only voice transcription
- Webhook authentication and replay protection
- MCP tool coverage

## Submitting Changes

### Pull Request Process

1. **Ensure tests pass**:
   ```bash
   make test
   make lint
   ```

2. **Update documentation** if needed

3. **Create pull request** with:
   - Clear title and description
   - Reference to related issue
   - List of changes made
   - Screenshots if UI-related

4. **Respond to review feedback** promptly

### Commit Message Format

Use conventional commits:

```
feat: add rate limiting functionality
fix: resolve configuration validation issue
docs: update development guide
test: add tests for authentication system
refactor: reorganize bot handlers
```

### Pull Request Template

```markdown
## Description
Brief description of changes made.

## Related Issue
Fixes #123

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Tests added/updated
- [ ] All tests pass
- [ ] Manual testing completed

## Checklist
- [ ] `make format` has been run (or pre-commit hooks are installed via `make dev`)
- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Documentation updated
- [ ] No breaking changes (or clearly documented)
```

## Code Review Guidelines

### For Contributors

- **Self-review** your code before submitting
- **Write clear commit messages** and PR descriptions
- **Respond promptly** to review feedback
- **Keep PRs focused** on a single change
- **Add tests** for new functionality

### For Reviewers

- **Be constructive** and helpful in feedback
- **Test functionality** when possible
- **Check for security implications**
- **Verify documentation updates**
- **Ensure tests are comprehensive**

## Issue Guidelines

### Bug Reports

```markdown
**Describe the bug**
A clear description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior.

**Expected behavior**
What you expected to happen.

**Environment**
- OS: [e.g. macOS, Linux]
- Python version: [e.g. 3.11]
- uv version: [e.g. 0.5.0]

**Additional context**
Any other context about the problem.
```

### Feature Requests

```markdown
**Is your feature request related to a problem?**
A clear description of what the problem is.

**Describe the solution you'd like**
A clear description of what you want to happen.

**Describe alternatives you've considered**
Alternative solutions or features you've considered.

**Additional context**
Any other context about the feature request.
```

## Security

### Reporting Security Issues

**Do not** create public issues for security vulnerabilities.

Instead:
1. Use GitHub private vulnerability reporting:
   https://github.com/ftaricano/telegram-claude-code-bridge/security/advisories/new
2. Include a detailed description, steps to reproduce, impact, and suggested
   mitigation.
3. Wait for maintainer acknowledgment before public disclosure.

### Security Guidelines

- **Never commit secrets** or credentials
- **Validate all inputs** thoroughly
- **Use parameterized queries** for database operations
- **Follow principle of least privilege**
- **Log security-relevant events**

## Development Environment

### Required Tools

- **uv**: Dependency management, virtualenv, Python toolchain
- **Black**: Code formatting
- **isort**: Import sorting
- **flake8**: Linting
- **mypy**: Type checking (`make typecheck`)
- **pytest**: Testing

### Recommended IDE Setup

#### VS Code
```json
{
    "python.defaultInterpreterPath": ".venv/bin/python",
    "python.formatting.provider": "black",
    "python.linting.enabled": true,
    "python.linting.flake8Enabled": true,
    "python.linting.mypyEnabled": true
}
```

#### PyCharm
- Point the project interpreter at `.venv/bin/python` (uv-managed)
- Enable Black formatting
- Enable flake8 and mypy inspections

## Community Guidelines

### Code of Conduct

- **Be respectful** and inclusive
- **Welcome newcomers** and help them get started
- **Give constructive feedback**
- **Focus on the code**, not the person
- **Assume good intentions**

### Communication

- **Use clear, concise language**
- **Provide context** in issues and PRs
- **Ask questions** when unsure
- **Share knowledge** and help others

## Getting Help

### Documentation
- Check `docs/` directory for guides
- Review existing code for patterns
- Read the configuration guide

### Asking Questions
- Search existing issues first
- Provide context and examples
- Include relevant environment details
- Be specific about what you've tried

### Debugging
- Use `make run-debug` for detailed logging
- Check test output with `make test`
- Run type checking with `make typecheck`

## Recognition

Contributors will be recognized in:
- `CHANGELOG.md` for their contributions
- Project documentation
- Release notes

Thank you for contributing to Telegram Claude Code Bridge.
