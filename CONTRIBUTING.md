# Contributing to lobster-cc

Thanks for your interest in contributing! This guide covers the basics.

## Dev Setup

```bash
git clone https://github.com/ahaNotAki/lobster-cc.git
cd lobster-cc
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
python -m pytest tests/ -v
```

## Linting

```bash
ruff check src/ tests/
```

We use [ruff](https://docs.astral.sh/ruff/) with a 100-character line length limit.

## Pull Request Process

1. Fork the repository and create a feature branch.
2. Make your changes and add tests where appropriate.
3. Ensure all tests pass (`pytest`) and linting is clean (`ruff check`).
4. Open a pull request against `main` with a clear description of your changes.

## Code Style

- Formatter/linter: ruff
- Max line length: 100 characters
- Type hints encouraged but not strictly enforced
- Write docstrings for public functions and classes

## Reporting Issues

Use the GitHub issue templates for bug reports and feature requests.
