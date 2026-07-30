# Threat model

QA Watcher is a loopback-only factual context provider. It accepts only known
Host values and absent or loopback Origins. It has no AI client, outbound Hermes
lifecycle calls, local clone, git subprocess, arbitrary filesystem tool, full
diff response, or host repository path. The only service credential is the
read-only GitLab token; tokens, headers, report bodies, diff bodies, and claim
tokens must never be logged or returned.

Canonical pipeline verification uses only
`deployment.deployable.pipeline.id`. Redirect following and pipelines-by-SHA
fallback are forbidden. Repository URLs are portable and sanitized.

Claims use 256-bit opaque tokens with only SHA-256 hashes persisted and
constant-time comparisons. Reports are untrusted Markdown and are stored, not
rendered. Remote exposure is outside the MVP trust boundary.

