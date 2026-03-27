"""Memory utilities — keyword extraction and context building for task history recall."""

from __future__ import annotations

import re

from remote_control.core.models import Memory

# Common English stopwords to filter from keywords
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of", "and",
    "or", "but", "not", "with", "this", "that", "from", "by", "as", "be", "was",
    "were", "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "shall", "so", "if",
    "then", "than", "too", "very", "just", "about", "up", "out", "no", "all",
    "my", "your", "its", "our", "their", "me", "him", "her", "us", "them",
    "what", "which", "who", "when", "where", "how", "why", "i", "you", "he",
    "she", "we", "they", "am", "are",
})

_MIN_KEYWORD_LEN = 2


def extract_keywords(text: str) -> str:
    """Extract keywords from text. Returns comma-separated string."""
    if not text.strip():
        return ""
    # \w matches Unicode word chars (Latin, CJK, etc.) plus underscore
    tokens = re.findall(r"[\w.]+", text.lower())
    seen: set[str] = set()
    keywords: list[str] = []
    for token in tokens:
        if token and token not in _STOPWORDS and len(token) >= _MIN_KEYWORD_LEN and token not in seen:
            seen.add(token)
            keywords.append(token)
    return ",".join(keywords)


def clean_message(message: str) -> str:
    """Strip system-injected prefixes ([System:...] and <context>...</context>) from a message."""
    text = message
    if text.startswith("[System:"):
        idx = text.find("]\n\n")
        if idx > 0:
            text = text[idx + 3:]
    if text.startswith("<context>"):
        idx = text.find("</context>")
        if idx > 0:
            text = text[idx + 10:].lstrip()
    return text.strip()


def build_context_block(
    recent: list[Memory],
    keyword_matches: list[Memory],
    max_chars: int = 2000,
) -> str:
    """Build a context block to prepend to the user's message."""
    if not recent and not keyword_matches:
        return ""

    parts: list[str] = ["<context>"]
    current_len = 10

    if recent:
        parts.append("## Recent Activity")
        current_len += 20
        for mem in recent:
            first_line = mem.content.split("\n")[0][:200]
            line = f"- {first_line}"
            if current_len + len(line) > max_chars:
                break
            parts.append(line)
            current_len += len(line)

    if keyword_matches:
        parts.append("")
        parts.append("## Related History")
        current_len += 20
        for mem in keyword_matches:
            first_line = mem.content.split("\n")[0][:200]
            line = f"- {first_line}"
            if current_len + len(line) > max_chars:
                break
            parts.append(line)
            current_len += len(line)

    parts.append("</context>")
    return "\n".join(parts)


