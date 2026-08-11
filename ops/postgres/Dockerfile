# Postgres 18 + pgvector + pgTAP.
#
# pgTAP exists here for one reason: Suite 2 (04-EVALUATION.md §3) is specified to
# be implemented TWICE — "pgTAP at the database layer and API-level through the
# real gateway" — because "policy regressions are silent: no error, just wrong
# rows". Only the API half existed, which meant a refactor that stopped calling
# db.scoped() would keep the Python suite green while isolation was gone.
#
# The database half asserts things the API half structurally cannot: that RLS is
# enabled AND forced on every table including ones added later, and the grant
# ladder, which needs privileges the application role deliberately does not have.
#
# The base tag stays pinned for the reason the compose file states: an unpinned
# tag is a supply-chain decision you did not make.
FROM pgvector/pgvector:0.8.2-pg18

# PGDG already configured in the base image, so this is one package rather than a
# source build with a toolchain left behind in the layer.
# curl is for the backup job, which runs from this image and has to call
# Prometheus's snapshot API. The base image ships neither curl nor wget, and the
# backup silently logged "admin API unavailable" and skipped the snapshot —
# reporting a missing TOOL as a missing FEATURE, which is the kind of message
# that sends you to fix the wrong thing.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-18-pgtap curl \
 && rm -rf /var/lib/apt/lists/*
