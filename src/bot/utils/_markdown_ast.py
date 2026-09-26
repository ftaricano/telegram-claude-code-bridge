"""F3-C: AST-based markdown→Telegram HTML conversion.

Substitui regex chain fragil de markdown_to_telegram_html_legacy
por parser AST baseado em markdown-it-py. Tolera input parcial
(fence aberto, markdown malformado mid-streaming).

Telegram HTML subset: https://core.telegram.org/bots/api#html-style
"""

from __future__ import annotations

from html import escape as _html_escape

from markdown_it import MarkdownIt
from markdown_it.token import Token

_md = MarkdownIt("commonmark", {"breaks": True, "html": False, "linkify": False})
_md.enable("strikethrough")


def render_to_telegram_html(text: str) -> str:
    """Convert markdown→Telegram HTML via AST. Tolerant to partial input."""
    if not text:
        return ""
    try:
        tokens = _md.parse(text)
    except Exception:
        return _html_escape(text)
    return _tokens_to_html(tokens)


def _tokens_to_html(tokens: list[Token]) -> str:
    out: list[str] = []
    for token in tokens:
        out.append(_token_to_html(token))
    return "".join(out).strip()


def _token_to_html(token: Token) -> str:
    """Map a markdown-it token to Telegram HTML.

    Telegram supports: <b>, <strong>, <i>, <em>, <u>, <s>, <strike>, <del>,
    <code>, <pre>, <a href=...>, <blockquote>, <tg-spoiler>, <br>.
    Anything else: render as escaped text.
    """
    t = token.type

    # Inline container — recurse into children
    if t == "inline" and token.children:
        return "".join(_token_to_html(child) for child in token.children)

    # Text node
    if t == "text":
        return _html_escape(token.content)

    # Soft break / hardbreak → newline
    if t == "softbreak":
        return "\n"
    if t == "hardbreak":
        return "\n"

    # Bold
    if t == "strong_open":
        return "<b>"
    if t == "strong_close":
        return "</b>"

    # Italic
    if t == "em_open":
        return "<i>"
    if t == "em_close":
        return "</i>"

    # Strike (~~text~~ via plugin)
    if t == "s_open":
        return "<s>"
    if t == "s_close":
        return "</s>"

    # Inline code
    if t == "code_inline":
        return f"<code>{_html_escape(token.content)}</code>"

    # Fenced code
    if t == "fence":
        lang = (token.info or "").strip()
        content = token.content.rstrip("\n")
        if lang:
            return f'<pre><code class="language-{_html_escape(lang)}">{_html_escape(content)}</code></pre>'
        return f"<pre><code>{_html_escape(content)}</code></pre>"

    # Indented code block
    if t == "code_block":
        content = token.content.rstrip("\n")
        return f"<pre><code>{_html_escape(content)}</code></pre>"

    # Link
    if t == "link_open":
        href = token.attrGet("href") or ""
        return f'<a href="{_html_escape(href)}">'
    if t == "link_close":
        return "</a>"

    # Blockquote
    if t == "blockquote_open":
        return "<blockquote>"
    if t == "blockquote_close":
        return "</blockquote>"

    # Paragraphs — separate with double newline
    if t == "paragraph_open":
        return ""
    if t == "paragraph_close":
        return "\n\n"

    # Lists — render as plain text with bullet/number, no Telegram-incompatible tags
    if t == "bullet_list_open":
        return ""
    if t == "bullet_list_close":
        return "\n"
    if t == "ordered_list_open":
        return ""
    if t == "ordered_list_close":
        return "\n"
    if t == "list_item_open":
        return "• "
    if t == "list_item_close":
        return "\n"

    # Heading — render as bold (Telegram has no native H1-H6)
    if t == "heading_open":
        return "<b>"
    if t == "heading_close":
        return "</b>\n"

    # Horizontal rule
    if t == "hr":
        return "\n———\n"

    # Unknown — render content escaped if any
    if token.content:
        return _html_escape(token.content)
    return ""
