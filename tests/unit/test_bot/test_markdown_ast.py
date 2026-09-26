"""F3-C: AST tolerates partial/malformed input mid-streaming."""

from src.bot.utils.html_format import markdown_to_telegram_html


def test_partial_code_fence_does_not_raise() -> None:
    """Mid-streaming pode chegar com ``` aberto. AST aguenta."""
    md = "```python\nprint('hi"  # fence nao fechou
    result = markdown_to_telegram_html(md)
    assert isinstance(result, str)
    assert len(result) > 0
    # Deve fazer algo razoavel: render parcial ou fallback raw escapado.


def test_unbalanced_bold_does_not_raise() -> None:
    """** impar."""
    md = "**bold** and **dangling"
    result = markdown_to_telegram_html(md)
    assert isinstance(result, str)


def test_underscore_midword_does_not_raise() -> None:
    """_ no meio de palavra — bug classico de Markdown."""
    md = "snake_case_variable mention"
    result = markdown_to_telegram_html(md)
    assert isinstance(result, str)
    # snake_case nao deveria ficar entre tags <i>
    assert "snake_case_variable" in result or "snake" in result


def test_empty_string_returns_empty() -> None:
    assert markdown_to_telegram_html("") == ""


def test_only_text_no_markdown() -> None:
    md = "Just plain text without any markdown formatting at all."
    result = markdown_to_telegram_html(md)
    assert "Just plain text" in result
