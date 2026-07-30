# Contributing

Contributions are welcome through focused issues and pull requests.

## Development setup

Requirements:

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

```bash
uv sync --frozen
```

Run the quality gates before opening a pull request:

```bash
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run pytest -q
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run ruff check .
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run mypy app
```

## Pull requests

- Keep changes scoped and include regression tests for behavior changes.
- Preserve the read-only GitLab boundary and fail closed on ambiguous data.
- Do not add AI/LLM calls to the watcher runtime.
- Do not commit `.env`, tokens, chat IDs, private URLs, production payloads,
  state files, generated reports, or local repository paths.
- Use synthetic hosts such as `gitlab.example.com` and synthetic numeric IDs in
  tests and documentation.
- Document intentional compatibility or durability changes.

Security vulnerabilities must be reported privately according to
[`SECURITY.md`](SECURITY.md), not through a public issue.
