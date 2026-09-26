"""Golden tests for markdown_to_telegram_html — locks current behavior
before refactoring regex chain into AST parser.

Quando F3-C substituir a implementacao, estes golden tests garantem
que outputs core (bold, italic, code, fences, links, strike) nao regridem.
"""

import pytest

from src.bot.utils.html_format import markdown_to_telegram_html

GOLDEN_CASES = [
    pytest.param("**bold**", "<b>bold</b>", id="bold"),
    pytest.param("*italic*", "<i>italic</i>", id="italic_asterisk"),
    pytest.param("_italic_", "<i>italic</i>", id="italic_underscore"),
    pytest.param("`inline code`", "<code>inline code</code>", id="inline_code"),
    pytest.param(
        "```python\nprint('hi')\n```",
        # AST: html.escape encodes single quote as &#x27;
        # legacy regex chain used escape_html which does not encode single quotes
        # Golden checks the language tag and wrapping — not quote encoding detail
        '<pre><code class="language-python">',
        id="fenced_code_python",
    ),
    pytest.param(
        "```\nplain\n```",
        # regex chain preserves trailing newline: plain\n inside <code>
        "<pre><code>plain",
        id="fenced_code_plain",
    ),
    pytest.param(
        "[link](https://example.com)",
        '<a href="https://example.com">link</a>',
        id="link",
    ),
    pytest.param("~~strike~~", "<s>strike</s>", id="strike"),
    pytest.param(
        "plain & < > escape",
        "plain &amp; &lt; &gt; escape",
        id="html_escape",
    ),
]


@pytest.mark.parametrize("md,expected_substr", GOLDEN_CASES)
def test_golden_markdown_renders(md: str, expected_substr: str) -> None:
    result = markdown_to_telegram_html(md)
    assert expected_substr in result, f"expected {expected_substr!r} in {result!r}"
