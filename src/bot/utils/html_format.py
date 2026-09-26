"""HTML formatting utilities for Telegram messages.

Telegram's HTML mode only requires escaping 3 characters (<, >, &) vs the many
ambiguous Markdown v1 metacharacters, making it far more robust for rendering
Claude's output which contains underscores, asterisks, brackets, etc.
"""

import hashlib
import re
from typing import List, Tuple

import structlog

from ._markdown_ast import render_to_telegram_html as _render_ast

logger = structlog.get_logger()


def escape_html(text: str) -> str:
    """Escape the 3 HTML-special characters for Telegram.

    This replaces all 3 _escape_markdown functions previously scattered
    across the codebase.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def markdown_to_telegram_html(text: str) -> str:
    """F3-C: AST-based conversion. Tolerates partial input.

    Legacy regex chain available as markdown_to_telegram_html_legacy
    for rollback if F3-C causes regressions.
    """
    return _render_ast(text)


def markdown_to_telegram_html_legacy(text: str) -> str:
    """LEGACY regex chain — kept for rollback. Use markdown_to_telegram_html.

    Converts Claude markdown to Telegram-compatible HTML via 8-pass regex chain
    with placeholder integrity checks. Order: fenced code blocks → inline code
    → HTML-escape → bold → italic → links → headers → strikethrough → restore.
    """
    original_text = text
    original_text_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()
    placeholders: List[Tuple[str, str, str]] = []
    placeholder_counter = 0

    def _make_placeholder(html_content: str, placeholder_type: str) -> str:
        nonlocal placeholder_counter
        key = f"\x00PH{placeholder_counter}\x00"
        placeholder_counter += 1
        placeholders.append((key, html_content, placeholder_type))
        return key

    def _placeholders_intact(pass_name: str) -> bool:
        for key, _html_content, placeholder_type in placeholders:
            if key not in text:
                logger.warning(
                    "placeholder_corrupted",
                    pass_name=pass_name,
                    placeholder_type=placeholder_type,
                    original_text_hash=original_text_hash,
                )
                return False
        return True

    # --- 1. Extract fenced code blocks ---
    def _replace_fenced(m: re.Match) -> str:  # type: ignore[type-arg]
        lang = m.group(1) or ""
        code = m.group(2)
        escaped_code = escape_html(code)
        if lang:
            html = f'<pre><code class="language-{escape_html(lang)}">{escaped_code}</code></pre>'
        else:
            html = f"<pre><code>{escaped_code}</code></pre>"
        return _make_placeholder(html, "code_fence")

    text = re.sub(
        r"```(\w+)?\n(.*?)```",
        _replace_fenced,
        text,
        flags=re.DOTALL,
    )
    if not _placeholders_intact("fenced_code"):
        return escape_html(original_text)

    # --- 2. Extract inline code ---
    def _replace_inline_code(m: re.Match) -> str:  # type: ignore[type-arg]
        code = m.group(1)
        escaped_code = escape_html(code)
        return _make_placeholder(f"<code>{escaped_code}</code>", "inline_code")

    text = re.sub(r"`([^`\n]+)`", _replace_inline_code, text)
    if not _placeholders_intact("inline_code"):
        return escape_html(original_text)

    # --- 3. HTML-escape remaining text ---
    text = escape_html(text)
    if not _placeholders_intact("html_escape"):
        return escape_html(original_text)

    # --- 4. Bold: **text** or __text__ ---
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    if not _placeholders_intact("bold_asterisk"):
        return escape_html(original_text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    if not _placeholders_intact("bold_underscore"):
        return escape_html(original_text)

    # --- 5. Italic: *text* (require non-space after/before) ---
    text = re.sub(r"\*(\S.*?\S|\S)\*", r"<i>\1</i>", text)
    if not _placeholders_intact("italic_asterisk"):
        return escape_html(original_text)
    # _text_ only at word boundaries (avoid my_var_name)
    text = re.sub(r"(?<!\w)_(\S.*?\S|\S)_(?!\w)", r"<i>\1</i>", text)
    if not _placeholders_intact("italic_underscore"):
        return escape_html(original_text)

    # --- 6. Links: [text](url) ---
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )
    if not _placeholders_intact("links"):
        return escape_html(original_text)

    # --- 7. Headers: # Header -> <b>Header</b> ---
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    if not _placeholders_intact("headers"):
        return escape_html(original_text)

    # --- 8. Strikethrough: ~~text~~ ---
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    if not _placeholders_intact("strikethrough"):
        return escape_html(original_text)

    # --- 9. Restore placeholders ---
    for key, html_content, _placeholder_type in placeholders:
        text = text.replace(key, html_content)

    return text
