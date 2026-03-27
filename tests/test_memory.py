"""Tests for the memory module utilities."""

from remote_control.core.models import Memory


def test_extract_keywords_basic():
    from remote_control.core.memory import extract_keywords
    result = extract_keywords("fix the authentication bug in main.py")
    keywords = result.split(",")
    assert "fix" in keywords
    assert "authentication" in keywords
    assert "bug" in keywords
    assert "main.py" in keywords
    assert "the" not in keywords
    assert "in" not in keywords


def test_extract_keywords_empty():
    from remote_control.core.memory import extract_keywords
    assert extract_keywords("") == ""


def test_extract_keywords_deduplicates():
    from remote_control.core.memory import extract_keywords
    result = extract_keywords("fix fix fix the bug bug")
    keywords = result.split(",")
    assert len(keywords) == len(set(keywords))


def test_build_context_block():
    from remote_control.core.memory import build_context_block
    recent = [
        Memory(type="raw", content="Task: fix bug\nResult: Fixed it", created_at="2026-03-08T10:00:00"),
    ]
    keyword = [
        Memory(type="raw", content="Task: setup auth\nResult: Done", created_at="2026-03-05T10:00:00"),
    ]
    result = build_context_block(recent, keyword)
    assert "<context>" in result
    assert "</context>" in result
    assert "Recent Activity" in result
    assert "fix bug" in result
    assert "Related History" in result
    assert "setup auth" in result


def test_build_context_block_empty():
    from remote_control.core.memory import build_context_block
    assert build_context_block([], []) == ""


def test_build_context_block_truncates():
    from remote_control.core.memory import build_context_block
    recent = [
        Memory(type="raw", content="Task: x\nResult: " + "a" * 3000, created_at="2026-03-08T10:00:00"),
    ]
    result = build_context_block(recent, [], max_chars=500)
    assert len(result) <= 600  # some overhead for tags
