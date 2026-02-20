# Happy Jumper Runbook

## Routine operations

1. Check service health endpoint:
   - `GET /health` should return `ok`.
2. Inspect Railway logs for worker errors and restart loops.
3. Confirm key bot flows in Discord:
   - 99k/Happy Jump signup + start flow
   - insurance signup/payment/claims
   - raffle create, buy, draw, and panels

## Incident: bot not responding

1. Check Railway deployment status and process restarts.
2. Check `/health` endpoint:
   - `db_not_initialized` => DB startup/config issue
   - `db_unhealthy` => DB connectivity/query issue
3. Validate environment variables are present and non-empty.
4. Review logs for recent exceptions (secrets are redacted by structured logging).

## Incident: database saturation / slowdowns

1. Look for `db_acquire_timeout` warning events.
2. Confirm DB instance health (connections, CPU, I/O).
3. Restart service to clear stuck in-memory workers if needed.
4. If persistent, reduce concurrent bot load and escalate DB capacity.

## Incident: background worker instability

1. Look for repeated `Supervised task failed` lines.
2. Confirm heartbeat logs from monitor tasks (`jump_monitor heartbeat ...`).
3. Restart deployment to rehydrate worker state from DB.

## Safe restart procedure

1. Trigger Railway redeploy/restart.
2. Wait for healthy status.
3. Validate `/health` and run Discord smoke checks.

## Change management checklist

- `ruff check .`
- `pytest -q`
- If touching startup/lifecycle, also validate bot startup and shutdown behavior in logs.
