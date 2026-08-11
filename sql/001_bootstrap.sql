-- Recall :: cluster bootstrap
--
-- Run once against a freshly initialized cluster:
--   cockroach sql --insecure --host=localhost:26257 -f sql/001_bootstrap.sql

-- Vector indexing is gated behind a cluster setting.
SET CLUSTER SETTING feature.vector_index.enabled = true;

CREATE DATABASE IF NOT EXISTS recall;

-- ---------------------------------------------------------------------------
-- Multi-region topology.
--
-- SURVIVE REGION FAILURE places 5 replicas so that no single region holds a
-- majority. Losing an entire region costs latency, never availability, and
-- never a committed write. This is the property the live demo removes a
-- region to prove.
--
-- Requires an Enterprise Free licence (free for companies under $10M revenue,
-- issued from the CockroachDB Cloud console). Without one, skip this block and
-- the rest of the schema still works on a single-region cluster.
-- ---------------------------------------------------------------------------
ALTER DATABASE recall SET PRIMARY REGION "us-east-1";
ALTER DATABASE recall ADD REGION "us-west-2";
ALTER DATABASE recall ADD REGION "eu-west-1";
ALTER DATABASE recall SURVIVE REGION FAILURE;

-- ---------------------------------------------------------------------------
-- Garbage-collection window.
--
-- CockroachDB's own docs are explicit that gc.ttlseconds "is not meant to be a
-- solution for long-term retention of history; for that you should handle
-- versioning in the schema design at the application layer." Recall does
-- exactly that: the `fact` table below is bitemporal, so point-in-time recall
-- is exact and unbounded.
--
-- MVCC time-travel is a complementary fast path, not the mechanism. 25 hours
-- is the largest value Cockroach Labs regularly tests, and it buys a
-- same-day AS OF SYSTEM TIME replay window for recent-incident forensics.
-- ---------------------------------------------------------------------------
ALTER DATABASE recall CONFIGURE ZONE USING gc.ttlseconds = 90000;
