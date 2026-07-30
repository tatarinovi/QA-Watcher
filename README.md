# QA Watcher

QA Watcher is a read-only GitLab deployment watcher with a durable FIFO queue and
a Streamable HTTP MCP server. It verifies what was actually deployed, persists
factual rollout events, and lets an MCP client such as
[Hermes Agent](https://github.com/NousResearch/hermes-agent) claim and complete
QA analysis work.

The watcher never launches an AI model, Hermes, Git, or a shell command. Optional
Telegram notifications are deterministic and use GitLab metadata only.

> [!IMPORTANT]
> QA Watcher is an alpha, single-replica local service. Its MCP endpoint has no
> remote authentication and must not be exposed to a LAN or the public internet.

## Highlights

- Explicit numeric GitLab Project ID allowlist; no group discovery.
- Exact deployment, deployable job, canonical pipeline, SHA, environment, and
  branch/ref validation.
- Independent per-project and per-environment FIFO queues.
- Durable schema-v3 JSON state with atomic replacement and file locking.
- Expiring claims, opaque claim tokens, recoverable report completion, and
  idempotent completion receipts.
- Seven bounded MCP tools over Streamable HTTP at `/mcp`.
- Isolated read-only replay of recent real GitLab deployment history.
- Optional deterministic Telegram notifications with exact associated MR
  metadata and fail-closed fallback behavior.
- No AI/LLM dependency or credential in the watcher runtime.

## Architecture

```text
GitLab REST API
      |
      | read-only polling
      v
+---------------------------+
| QA Watcher                |
|                           |
| verify rollout facts      |
| persist FIFO + reports    |
| optional Telegram notice  |
| expose /mcp               |
+-------------+-------------+
              |
              | localhost MCP pull
              v
       Hermes / MCP client
```

A deployment is accepted only when all authoritative GitLab facts agree. The
canonical environment-to-ref mapping defaults to:

```text
dev   -> develop
test  -> testing
stage -> stable
prod  -> master
```

The branch names are configurable through `DEV_BRANCH`, `TEST_BRANCH`,
`STAGE_BRANCH`, and `PROD_BRANCH`. GitLab UI status `passed` corresponds to API
status `success`; deployment environment and pipeline ref are validated
independently.

## Requirements

- Docker with Docker Compose, or Python 3.12+
- [uv](https://docs.astral.sh/uv/) for local development
- A GitLab token with the minimum read-only API permissions needed to access the
  configured projects

## Configuration

Copy the public template and fill in local values:

```bash
cp .env.example .env
```

Never commit `.env`. The important settings are:

| Variable | Required | Purpose |
| --- | --- | --- |
| `GITLAB_URL` | yes | GitLab base URL, for example `https://gitlab.example.com` |
| `GITLAB_PROJECT_IDS` | yes | CSV allowlist of numeric Project IDs |
| `GITLAB_TOKEN` | yes | Read-only GitLab API token |
| `DEV_BRANCH` | no | Expected pipeline ref for `dev` |
| `TEST_BRANCH` | no | Expected pipeline ref for `test` |
| `STAGE_BRANCH` | no | Expected pipeline ref for `stage` |
| `PROD_BRANCH` | no | Expected pipeline ref for `prod` |
| `POLL_INTERVAL_SECONDS` | no | Polling interval; default `60` |
| `TELEGRAM_BOT_TOKEN` | no | Enables Telegram when paired with `TELEGRAM_CHAT_ID` |
| `TELEGRAM_CHAT_ID` | no | Telegram destination chat |
| `TELEGRAM_MESSAGE_THREAD_ID` | no | Positive topic ID for a supergroup topic |
| `BIND_HOST` | no | Defaults to `127.0.0.1` |
| `PORT` | no | Defaults to `8000` |

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` must be both present or both absent.
A topic ID without those credentials is rejected.

## Run with Docker Compose

```bash
docker compose up --build -d
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/ready
```

Compose publishes the service on host loopback only and stores state and reports
in named volumes. The container listens on `0.0.0.0` only inside its isolated
network namespace; host publication remains `127.0.0.1`.

## Run locally

```bash
uv sync --frozen
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV \
  uv run python -m app.cli serve
```

Useful administrative commands:

```bash
uv run python -m app.cli poll
uv run python -m app.cli list-pending
uv run python -m app.cli show-state
```

## MCP integration

The Streamable HTTP endpoint is:

```text
http://127.0.0.1:8787/mcp
```

For current Hermes Agent versions:

```bash
hermes mcp add qa-release --url http://127.0.0.1:8787/mcp
hermes mcp test qa-release
```

Restart Hermes after adding the server so the tools are discovered. QA Watcher
exposes exactly seven tools:

1. `list_pending_deployments`
2. `claim_deployment`
3. `get_deployment_context`
4. `get_changed_files`
5. `get_release_metadata`
6. `mark_analysis_completed`
7. `get_report`

The contracts, limits, queue ordering, and report format are documented in
[`docs/contracts.md`](docs/contracts.md) and
[`docs/storage.md`](docs/storage.md).

## Read-only replay

Replay verifies the GitLab-to-MCP path without writing to GitLab or touching
production state. The selected project must be in `GITLAB_PROJECT_IDS`.

```bash
uv run python -m app.cli replay \
  --project-id 123 \
  --environment test \
  --count 3 \
  --port 18788
```

Replay performs a bounded history query, revalidates canonical pipeline facts,
and creates disposable state and reports under the system temporary directory.
The first deployment is a silent baseline; later transitions become available
at `http://127.0.0.1:18788/mcp`. It does not create a GitLab deployment or
pipeline.

## Telegram notifications

Telegram is optional and contains no AI-generated text. After a newly actionable
rollout is durably persisted, the watcher sends one factual message:

```text
Успешный деплой
Проект: <GitLab project name>
Площадка: <dev|test|stage|prod>
Версия: <associated merge request title>
Описание: <associated merge request description>
```

The associated merge request is used only when exactly one complete entry has
`merge_commit_sha` or `squash_commit_sha` equal to the authoritative deployment
SHA and targets the expected branch. Ambiguous, incomplete, malformed, or
paginated metadata fails closed to:

```text
Версия: <commit title>
Описание: —
```

Values are HTML-escaped, meaningful whitespace and line breaks are preserved,
and output is bounded to Telegram's 4096 UTF-16-unit parsed-text limit. Initial
baseline/backfill, already-known deployments, and same-SHA redeployments do not
send notifications.

Delivery is intentionally single-attempt best effort. A Telegram transport
failure cannot roll back durable GitLab ingestion, but a message can be lost if
the process or Telegram fails after ingestion. There is no durable notification
outbox or retry queue in this release.

## Security model

- GitLab access is read-only and limited to the explicit Project ID allowlist.
- Tokens and chat identifiers are read only from the environment and are not
  persisted in state or reports.
- Repository URLs returned by GitLab are sanitized before persistence or MCP
  exposure.
- Unknown MCP `Host` and browser `Origin` values are rejected.
- Non-loopback bind requires an explicit unsafe override, but does not add
  authentication.
- State and report files are created with restrictive permissions.
- The watcher does not execute repository code, Git hooks, shell commands, or
  LLM requests.

See [`docs/threat-model.md`](docs/threat-model.md) and
[`SECURITY.md`](SECURITY.md) before deployment.

## Current limitations

- One continuously running watcher replica per state store.
- Local JSON storage rather than a distributed database.
- Local loopback MCP only; no remote authentication.
- Telegram delivery is best effort without a durable retry/outbox.
- The first confirmed deployment in a new project/environment partition is a
  silent baseline.
- Replay is a read-only verification tool, not a deployment simulator.

## Development

```bash
uv sync --frozen
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run pytest -q
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run ruff check .
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV uv run mypy app
```

Contribution and private vulnerability-reporting guidance is available in
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`SECURITY.md`](SECURITY.md).

## License

QA Watcher is released under the [MIT License](LICENSE).
