import re

import pytest

from src.bot.utils.html_format import (
    escape_html,
)
from src.bot.utils.html_format import (
    markdown_to_telegram_html_legacy as markdown_to_telegram_html,
)


@pytest.mark.parametrize(
    ("corrupted_pattern", "pass_name"),
    [
        (r"\*\*(.+?)\*\*", "bold_asterisk"),
        (r"__(.+?)__", "bold_underscore"),
        (r"\*(\S.*?\S|\S)\*", "italic_asterisk"),
        (r"\[([^\]]+)\]\(([^)]+)\)", "links"),
    ],
)
def test_placeholder_corruption_logs_and_falls_back_to_escaped_raw(
    monkeypatch, corrupted_pattern, pass_name
):
    events = []
    original_re_sub = re.sub
    original_text = (
        "```python\nprint('safe')\n```\n\n"
        "**bold** __bold__ *italic* [link](https://example.com)"
    )

    def corrupting_sub(pattern, repl, string, count=0, flags=0):
        result = original_re_sub(pattern, repl, string, count=count, flags=flags)
        if pattern == corrupted_pattern:
            return result.replace("\x00PH0\x00", "\x00BROKEN0\x00")
        return result

    monkeypatch.setattr("src.bot.utils.html_format.re.sub", corrupting_sub)
    monkeypatch.setattr(
        "src.bot.utils.html_format.logger.warning",
        lambda event, **kwargs: events.append((event, kwargs)),
    )

    result = markdown_to_telegram_html(original_text)

    assert result == escape_html(original_text)
    assert len(events) == 1
    event, kwargs = events[0]
    assert event == "placeholder_corrupted"
    assert kwargs["pass_name"] == pass_name
    assert kwargs["placeholder_type"] == "code_fence"
    assert len(kwargs["original_text_hash"]) == 64
    assert "safe" not in kwargs.values()
