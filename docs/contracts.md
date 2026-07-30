# QA Watcher contracts

Contract version 1 uses state schema version 3 and a versioned success/error
envelope. Events are identified by `(project_id, deployment_id)` and ordered by
`(finished_at, project_id, deployment_id)`. Each project/environment partition
has an independent FIFO queue. Identity-sensitive tools accept optional
`project_id`; a deployment-ID-only call works only when the ID is globally
unique and otherwise returns `AMBIGUOUS_DEPLOYMENT_ID`.
The previous SHA is the immediately preceding verified successful deployment;
transitions are never collapsed.

The report contract requires these standalone headings in this order:

1. `# QA Impact Analysis`
2. `## Summary`
3. `## Changed Areas`
4. `## Risk Analysis`
5. `## Recommended Checks`
6. `## Regression Focus`

Reports are limited to 100,000 characters and 512,000 UTF-8 bytes. List limits
are 1–100, changed-file limits 1–200, and report chunks 1–20,000 characters.
Cursors are opaque.

State transitions are `pending → claimed → completed`; an expired claim becomes
claimable again. Initial baselines and same-SHA deployments are terminal
completed events without reports.
