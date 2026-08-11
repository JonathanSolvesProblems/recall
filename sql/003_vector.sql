-- Unsay :: vector indexes and data residency
--
--   cockroach sql --insecure --host=localhost:26257 -d unsay -f sql/003_vector.sql
--
-- Kept separate from 002 because CockroachDB cannot run IMPORT INTO against a
-- table that already carries a vector index, and large batch inserts of VECTOR
-- values degrade while one is present. Bulk-load facts first, then run this.

USE unsay;

-- ---------------------------------------------------------------------------
-- Semantic retrieval over safety claims.
--
-- The `believed` prefix column is the point of this index. Without it, a
-- top-K search spends part of its budget on claims the system has already
-- retracted, and those get filtered out afterwards, so a query asking for 8
-- results can quietly come back with 3. Prefixing on `believed` means the K
-- nearest neighbours are drawn only from claims currently held to be true.
-- ---------------------------------------------------------------------------
CREATE VECTOR INDEX IF NOT EXISTS fact_semantic
    ON fact (believed, embedding vector_cosine_ops);

-- Recall of prior answers, used to detect when the agent is being asked
-- something it has already ruled on.
CREATE VECTOR INDEX IF NOT EXISTS decision_semantic
    ON decision (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- Data residency.
--
-- REGIONAL BY ROW gives every patient row a hidden `crdb_region` column and
-- domiciles the row in that region. An EU patient's memory lives in eu-west-1
-- because the storage layer puts it there, not because application code
-- remembered to. Reads from the home region are also local, so the common
-- path avoids a cross-ocean round trip.
-- ---------------------------------------------------------------------------
ALTER TABLE patient SET LOCALITY REGIONAL BY ROW;
