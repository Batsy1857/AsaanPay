# AsaanPay

An educational synthetic payment-orchestration simulator for a DevOps architecture assignment. Planned stack: one FastAPI application, PostgreSQL, HTML/CSS/vanilla JS, and Docker Compose. No real financial transactions or payment-provider integrations occur.

Milestone 1 implements application startup, an asyncpg connection pool, `GET /api/health`, the initial one-table schema, containers, and verification tests. Checkout endpoints, issuer/risk simulation, orchestration, and UI are **not implemented**. See [architecture](docs/architecture.md) for contracts, decision matrix, state machine, benchmark methodology, tradeoffs, future file tree, and seven-day milestones.

## Start locally with Docker Compose

Prerequisites: Docker Engine/Desktop running and Docker Compose supporting `up --wait`. Run commands from the repository root. Create `.env` only if it does not already exist:

```sh
[ -f .env ] || cp .env.example .env
docker compose config --quiet
docker compose up --build -d --wait
docker compose ps
curl --fail-with-body http://localhost:8000/api/health
```

If port 8000 is already occupied, start with `API_PORT=18000 docker compose up --build -d --wait` and verify `http://localhost:18000/api/health`, or set `API_PORT=18000` in `.env` for subsequent runs.

Expected HTTP 200: `{"status":"ok","database":"connected"}`. If you change `API_PORT` in `.env`, use that port in host curl commands. The API is bound to localhost; PostgreSQL is accessible inside Compose. Use exactly one API instance and one Uvicorn worker.

PostgreSQL must become healthy before the API starts. The lifespan creates the pool and closes it on shutdown. Initial database connection/configuration failure stops API startup with a clear message. After startup, every health request runs `SELECT 1`; an unavailable database returns HTTP 503 with `{"status":"unhealthy","database":"unavailable"}`. The query and pool wait are bounded to three seconds. Health does not verify table existence.

## Verify Milestone 1

```sh
docker compose exec api python -m unittest discover -s tests -v
docker compose exec -e RUN_DB_TESTS=1 api python -m unittest discover -s tests -v
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT 1;"'
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d payment_attempts"'
docker compose logs --tail=100 api db
```

The default suite mocks database access and checks health success/failure/recovery, startup failure, pool cleanup, and absent checkout routes. The opt-in live test connects to PostgreSQL, verifies schema defaults, and checks unique-key/positive-amount constraints using fixtures rolled back afterward. Skipped live tests are not evidence of database connectivity.

Optional outage check on this local sandbox:

```sh
docker compose stop db
curl -i http://localhost:8000/api/health
docker compose start db
# Once PostgreSQL is healthy again, expect HTTP 200:
curl -i http://localhost:8000/api/health
```

Run the unit suite without Docker using Python 3.13:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Shutdown and reset

Stop and remove containers while preserving the named database volume:

```sh
docker compose down
```

`db/schema.sql` is mounted into PostgreSQL's initialization directory. It runs only when the database data directory is empty. Initialization scripts **do not automatically rerun on existing volumes**; rebuilding the API or restarting PostgreSQL does not update the schema. Changing `.env` credentials also does not rewrite existing database users.

For an intentional fresh local sandbox only, the following **deletes this Compose project's database volume and all its attempts**. Back up anything needed first. This reset is never performed automatically:

```sh
docker compose down --volumes
docker compose up --build -d --wait
```

For existing data that must be retained, review and apply an explicit SQL change instead of resetting. There is no ORM or Alembic.

## Resolved future checkout behavior

`POST /api/checkouts` will wait for execution and return 201 with the terminal state. Same-key identical normalized requests return 200 when finished or 202 when unfinished; changed payloads return 409. `GET /api/checkouts/{id}` will retrieve attempts or return 404. Invalid inputs return 422. These routes remain planned only.

`Idempotency-Key` identifies an immutable SHA-256 fingerprint of canonical `amount_pkr`, `card_preset`, `billing_country`, `execution_mode`, and `workload_profile`. Database transactions will remain closed during simulator waits. Merchant country is PK. Presets are approved/declined; risk is VERIFY for amount >= 50000 PKR or non-PK billing, otherwise CLEAR. These are fictional sandbox rules. Amounts use NUMERIC(14,2); canonical decimal formatting and validation are specified in the architecture.

| Issuer | Risk | State |
| --- | --- | --- |
| APPROVED | CLEAR | SUCCEEDED |
| APPROVED | VERIFY | VERIFICATION_REQUIRED |
| DECLINED | Either valid result | DECLINED |
| Technical failure/timeout | Any | ERROR |

Both sequential and concurrent modes must produce identical decisions. Per-branch timeout is two seconds. Workload profiles use explicitly **artificial waits**: lightweight 0/0 ms, balanced 250/250 ms, optional asymmetric 100/400 ms. Benchmarks will measure orchestration/API latency, actual branch overlap, correctness, median, and P95 using paired deterministic inputs and fresh keys.

Future startup recovery may mark unfinished attempts ERROR with an interruption reason after database connectivity is established, without rerunning operations. **This simple policy is unsafe for multiple independent API instances.** Recovery writes are not part of Milestone 1. Milestone 2 has not begun.
