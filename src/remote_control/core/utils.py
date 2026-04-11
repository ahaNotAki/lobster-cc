"""Utility functions shared across modules."""


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
