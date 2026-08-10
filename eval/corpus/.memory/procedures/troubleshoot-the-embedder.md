---
id: PROC-0004
title: Troubleshoot the embedding service
status: active
date: 2026-08-10
---

# Troubleshoot the embedding service

## Symptom: /readyz reports embeddings degraded

The API tolerates a cold embedder by design. Retrieval degrades to the lexical
arm and writes still land, without a vector, to be backfilled later. Readiness
does not fail.

Check the provider first: `curl -s localhost:11434/api/tags` for Ollama.

## Symptom: containers cannot reach Ollama but the host can

`OLLAMA_HOST` defaults to `127.0.0.1`, which is not reachable from inside a
container. Set `OLLAMA_HOST=0.0.0.0:11434`. The tell is that
`curl localhost:11434` succeeds on the host while every container gets connection
refused.

## Symptom: the embedder restarts forever with no error message

Check the exit code, not the container state:
`docker events --filter container=<name> --filter event=die`

Exit code 137 is SIGKILL. If no memory limit is set on the service, Docker
reports `OOMKilled=false` even though the host kernel killed it, so the container
looks like it is restarting for no reason.

TEI allocates the maximum batch shape during warmup, and attention is O(n^2). At
`--max-batch-tokens 16384` on an XLM-RoBERTa-large model that is far more memory
than a laptop VM has. TEI also refuses to start when `--max-batch-tokens` is below
the model's `max_input_length` unless `--auto-truncate` is set, so there is no
small-and-lossless setting. Either raise the VM memory, accept truncation, or use
a provider that sizes a KV cache to the actual context instead.

## Symptom: dimension errors on insert

The vector column is `halfvec(1024)` and the HNSW index is built for it. The
provider abstraction checks dimensions on the way out precisely so a model
returning 768 fails at the provider with a clear message rather than at the
INSERT with a type error far from the cause. Changing embedding model requires a
migration, not a settings change.
