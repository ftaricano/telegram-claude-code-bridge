from unittest.mock import Mock

import pytest

from src.bot.utils.formatting import ResponseFormatter
from src.config.settings import Settings


@pytest.fixture()
def formatter():
    settings = Mock(spec=Settings)
    settings.enable_quick_actions = False
    return ResponseFormatter(settings)


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("Intro\n```python\nprint('missing close')", "unbalanced_fence"),
        ("This **bold marker never closes", "unbalanced_bold"),
        ("Use my_var_name in prose", "midword_underscore"),
    ],
)
def test_malformed_markdown_uses_raw_text_fallback(
    formatter, monkeypatch, text, pattern
):
    events = []
    monkeypatch.setattr(
        "src.bot.utils.formatting.logger.warning",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    messages = formatter.format_claude_response(text)

    assert len(messages) == 1
    assert messages[0].text == text
    assert messages[0].parse_mode is None
    assert events == [
        (
            "markdown_fallback",
            {
                "pattern_detected": pattern,
                "original_length": len(text),
                "fallback_strategy": "raw_text_no_parse_mode",
            },
        )
    ]


def test_bold_markers_inside_code_block_do_not_trigger_fallback(formatter):
    text = "```python\nprint('**not markdown**')\n```"

    messages = formatter.format_claude_response(text)

    assert messages[0].parse_mode == "HTML"
    assert "<pre>" in messages[0].text
