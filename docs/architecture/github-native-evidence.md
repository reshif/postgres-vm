# GitHub-Native Evidence Architecture

## Authority

The source repository is authoritative for code and ordinary engineering docs at
an exact commit SHA. A private `-evidence` repository is authoritative for
reviewed, durable assertions. The database is a rebuildable projection for
retrieval, graph queries, operations, and evaluations; it is never the sole
place an agent authors durable project truth.

The legacy `.memory` tree is an import-only compatibility source during
migration. New durable knowledge must not depend on a checkout mounted into the
runtime stack.

## Repositories

For every registered project, bind one GitHub source repository and one private
evidence repository:

```
organization/project
organization/project-evidence
```

The two repositories must be distinct and each repository belongs to exactly
one GitHub-native project binding. This keeps signed webhook routing and all
later evidence scopes unambiguous.

The evidence repository contains reviewable assertions, extraction policy,
and evaluation cases. A GitHub App has read access to the source repository and
write-by-pull-request access to the evidence repository. It never force-pushes
or writes directly to the default branch.

## Operator Setup

Install the GitHub App on the source and evidence repositories, then bind the
project without creating a host-local knowledge directory:

```text
memory init --github --org <organization> --project <project> \
  --evidence-repo https://github.com/<organization>/<project>-evidence \
  --installation-id <GitHub-App-installation-id>
```

The command stores only the project scope and repository identities in
`.memory-platform/binding.json`. It creates neither `.memory/` nor a source
checkout. GitHub remains the durable authority and all content is fetched by
immutable SHA through the App.

An assertion is a Markdown file under `assertions/` with flat frontmatter:

```text
---
id: service-storage
subject: service
predicate: uses
object: PostgreSQL
type: decision
state: accepted
confidence: 0.960
evidence:
  - github://github.com/organization/project@<immutable-sha>:src/storage.py
---
```

The sidecar importer refuses an assertion without immutable supporting blobs.
It records the assertion file as provenance, links every cited artifact, and
supersedes the earlier source revision atomically. Removing an active assertion
from a synced sidecar revision retracts it rather than leaving stale knowledge
active. It does not copy arbitrary Git content into the database or retrieval
corpus.

## Ingestion Contract

1. GitHub signs a webhook delivery.
2. The API verifies `X-Hub-Signature-256` before parsing JSON.
3. It persists a bounded, normalized envelope containing repository identity,
   event type, exact SHA/reference, and a payload hash. It does not persist raw
   webhook bodies, PR bodies, or workflow logs.
4. A worker creates immutable provenance artifacts and later fetches required
   Git blobs by exact SHA through the GitHub App.
5. Deterministic parsers build the code and document graph from those blobs.
6. Curators or policy-approved automation create reviewed assertions through
   pull requests in the evidence repository.
7. The projection only exposes accepted assertions and their cited artifacts to
   retrieval. Proposed and contested assertions remain visible to curation, not
   as answers.

## Evidence Rules

- Every assertion carries source repository, source path, source revision,
  validity interval, review state, and one or more supporting artifacts.
- An artifact is immutable and content-addressed where content is retained.
- Agent sessions are short-lived `agent_episode` evidence. They can propose an
  assertion but cannot promote their own output to accepted knowledge.
- Graph nodes and edges are derived read models. Code edges come from
  deterministic parsers; claim edges require evidence and review state.
- A query with no accepted, directly relevant evidence returns an explicit
  no-evidence result. Baseline project constraints are not answer evidence.

## Retention

Keep assertions and their source references for the project retention period.
Retain bounded delivery metadata and hashes for audit and replay. Do not retain
full workflow logs or arbitrary webhook text in the knowledge plane; link to
the provider URL with access control instead.

## Migration Gates

1. Register GitHub binding and evidence repository for a pilot project.
2. Replay Git history to a shadow projection at exact SHAs.
3. Compare retrieval, provenance, and graph coverage against the legacy path.
4. Make GitHub projection primary only after the parity evaluation passes.
5. Disable legacy poll ingestion, remove source-checkout mounts, and retain the
   legacy importer only as an explicit migration command until its sunset date.
