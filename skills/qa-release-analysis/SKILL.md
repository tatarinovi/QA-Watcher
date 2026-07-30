---
name: qa-release-analysis
description: Use when analyzing QA deployments from the QA Watcher MCP queue.
version: 1.0.0
author: QA Watcher
license: MIT
---

# QA Release Analysis

QA Watcher is a pull-only factual queue. Hermes owns repository mapping, local Git analysis, Markdown authoring, and completion. Watcher must never receive a local path or launch Git, shell, Hermes, or an LLM.

## Setup

```bash
hermes mcp add qa-release --url http://127.0.0.1:8787/mcp
hermes mcp test qa-release
```

Install a private repository mapping from `templates/repositories.example.yaml`. Match both `gitlab_host` and numeric `project_id`; a project name alone is insufficient.

The server exposes exactly:

- `mcp_qa_release_list_pending_deployments`
- `mcp_qa_release_claim_deployment`
- `mcp_qa_release_get_deployment_context`
- `mcp_qa_release_get_changed_files`
- `mcp_qa_release_get_release_metadata`
- `mcp_qa_release_mark_analysis_completed`
- `mcp_qa_release_get_report`

## Workflow

1. Call `mcp_qa_release_list_pending_deployments`. Select only the oldest item with `claimable=true`; FIFO is independent per `(project_id, environment)`.
2. Claim it with `mcp_qa_release_claim_deployment`, always passing both `project_id` and `deployment_id` from the selected queue item. Keep the opaque token only in transient MCP-call context. Never write it to a file, report, command argument, log, or summary. Renew with the same tool, project ID, deployment ID, and token before expiry.
3. Fetch `mcp_qa_release_get_deployment_context`, `mcp_qa_release_get_release_metadata`, and every page from `mcp_qa_release_get_changed_files`. Always pass the same `project_id` with `deployment_id` to every call. Preserve truncation, timeout, and incomplete limitations.
4. Resolve `(gitlab_host, project_id)` to the local clone and verify `git remote -v` matches the credential-free repository URLs from MCP.
5. Verify exact objects without checkout:

```bash
git -C <repo> cat-file -e '<old_sha>^{commit}'
git -C <repo> cat-file -e '<new_sha>^{commit}'
```

If an object is absent, fetch only the exact SHA from the verified remote after user approval. Never substitute a branch/tag tip, initialize submodules, or run hooks.

6. Analyze the direct pair, never a cumulative range or branch head:

```bash
git -C <repo> diff --name-status <old_sha> <new_sha>
git -C <repo> diff --stat <old_sha> <new_sha>
git -C <repo> diff --no-ext-diff --no-textconv <old_sha> <new_sha> -- <bounded-paths>
```

The analyzed identity is exactly `old_sha..new_sha`. For `3.20 → 3.21 → 3.22`, analyze `3.20..3.21`, complete it, then analyze `3.21..3.22`; never use `3.20..3.22`.

7. Build Markdown containing these exact standalone headings once and in this order:

```markdown
# QA Impact Analysis

## Summary

## Changed Areas

## Risk Analysis

## Recommended Checks

## Regression Focus
```

Include project ID, environment, deployment/pipeline IDs, exact SHA pair, outcome, evidence, and limitations. Exclude credentials, claim token, local paths, raw unbounded diffs, and unsupported claims. Limits are 100,000 characters and 512,000 UTF-8 bytes.

8. Call `mcp_qa_release_mark_analysis_completed` with the same `project_id`, deployment ID, token, and `analysis_outcome=complete` or `partial`. On an uncertain response, retry only the identical payload; changed content must produce `COMPLETION_PAYLOAD_MISMATCH`.
9. Read all chunks using `mcp_qa_release_get_report`, always passing `project_id` with the deployment ID. Verify reconstructed content/digest, then list pending again. Only the next event in the same project/environment partition should become claimable.

## Fail Closed

- `CLAIM_CONFLICT`: another worker owns it; stop.
- `PREDECESSOR_NOT_COMPLETED`: return to the indicated predecessor.
- `CLAIM_TOKEN_INVALID` or `CLAIM_EXPIRED`: stop; never guess or replay a token.
- `COMPLETION_PAYLOAD_MISMATCH`: retrieve the existing report and investigate.
- `ALREADY_COMPLETED`: retrieve the report; do not create another analysis.
- `STORAGE_ERROR`: preserve local findings and ask an operator to restore durability.

## Checklist

- [ ] Oldest claimable event selected in its project/environment partition
- [ ] Project ID passed with every identity-sensitive MCP call
- [ ] Token exists only in MCP calls
- [ ] Every factual page and limitation captured
- [ ] Host + project ID + Git remote positively verified
- [ ] Exact commit objects verified; no fallback/checkout/hooks/submodules
- [ ] Direct bounded old-to-new diff analyzed
- [ ] Exact report headings and limits satisfied
- [ ] Completion receipt and chunked report verified
