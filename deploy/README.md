# Deploying the demo

The demo URL has to stay reachable for the whole judging window
(19 August to 15 September), which is roughly four weeks, not four days. That
rules out running it from a laptop and makes idle cost the thing to optimise
for, not peak throughput.

## Shape, and why

| Piece | Choice | Cost |
|---|---|---|
| Database | CockroachDB Cloud **Basic** | free to 10 GiB / 50M RUs |
| App | **Lambda + function URL** | pennies at demo traffic, nothing when idle |
| Models | Bedrock, on demand | cents |

The alternative shapes were considered and rejected on cost:

- **9-node cluster on EC2.** A single `t3.xlarge` is about $120/month. That one
  decision would cost more than the entire rest of the project.
- **ECS Fargate / App Runner.** Around $15-30/month for a container that is
  idle almost all of the time, because judging traffic is a handful of visits.

Lambda has no idle charge, so a page nobody is looking at costs nothing. The
tradeoff is a cold start of a few seconds on the first request after a quiet
period, which is acceptable for a demo and is why `unsay/lambda_handler.py`
keeps the connection pool at 0..2 rather than the local default of 2..16: a
frozen execution environment otherwise holds connections open against a Basic
cluster's limit while doing no work.

## The region-failure demo stays local, deliberately

`SURVIVE REGION FAILURE` needs a multi-region cluster, which on CockroachDB
Cloud is an Advanced-tier feature with custom pricing. The 9-node
docker-compose cluster does the real thing for free.

So the split is:

- **Hosted demo**: the product, on Cloud Basic, up for the judging window.
- **Region-kill demo**: recorded locally against the real 9-node cluster.

Nothing about the region-kill shot needs to be hosted, and it is a genuine
failure of a genuine cluster either way.

## Steps

1. Create a **Basic** cluster at <https://cockroachlabs.cloud/signup>, then
   copy the connection string from Connect → General connection string.

2. Apply the schema:

   ```bash
   cockroach sql --url "$UNSAY_CLOUD_DSN" -f sql/002_schema.sql
   cockroach sql --url "$UNSAY_CLOUD_DSN" -f sql/003_vector.sql
   ```

   Skip `001_bootstrap.sql`: it sets multi-region topology and a cluster
   setting, neither of which applies to a single-region Basic cluster. If
   `CREATE VECTOR INDEX` is rejected, the vector index is unavailable on that
   tier and the fallback is a Standard cluster.

3. Load the corpus (this is the openFDA ingest, pointed at Cloud):

   ```bash
   UNSAY_DSN="$UNSAY_CLOUD_DSN" python -m unsay.cli ingest-recalls --since 2025-01-01 --limit 400
   ```

4. Build and deploy:

   ```bash
   bash deploy/build_lambda.sh
   bash deploy/deploy_lambda.sh
   ```

   `build_lambda.sh` fetches Linux wheels rather than building locally, since
   the packages are compiled and a Windows or macOS wheel will not run on
   Lambda.

## Keeping it cheap

- Set a **zero-spend or $50 budget alert** before deploying.
- Lambda memory at 512 MB is enough; more memory costs more per millisecond.
- The `/api/demo/supersede` endpoint writes to the database, so leave the
  hosted demo pointed at a corpus you are happy for strangers to mutate. It
  cannot delete anything: the schema has no destructive endpoint.
