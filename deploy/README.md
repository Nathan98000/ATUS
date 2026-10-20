# deploy/

Support files for the local production stack (`docker-compose.prod.yml`) and
its free-tier emulation (`docker-compose.emulation.yml`). The usage guide is
in [docs/deployment.md](../docs/deployment.md#0-rehearse-the-whole-stack-locally).

| Path | Used by | Purpose |
|---|---|---|
| `postgres/initdb/10-roles.sh` | `db` (first start) | creates `neondb_owner` (non-superuser owner), `atus_api` (read-only, ADR-011 role defaults) and database `atus` |
| `postgres/grant_api_role.sql` | `seed`, `restore` | re-applies the API role's SELECT grants after a load or restore |
| `stack/seed_fixture.py` | `seed` | loads the hand-computed fixture dataset as the owner |
| `stack/restore_dump.sh` | `restore` (profile `dump`) | `pg_restore --no-owner --no-acl` of a serving dump, grants, `VACUUM (ANALYZE)` |
| `stack/smoke_probe.py` | `smoke`, `scripts/smoke_stack.sh` | stdlib HTTP checks run inside the stack network |
| `stack/stack.env.example` | you | template for the stack's gitignored env file |
| `pgbouncer/` | `pgbouncer` (emulation profile) | PgBouncer in transaction mode with Neon's pooler settings |
| `edge/` | `edge` (profile) | the Worker under `wrangler dev`, local mode |
| `emulation/neon_settings.py` | you, tests | Neon's compute settings for any CU range |

Nothing here is used in production: Render runs the image, Neon is restored
by the release scripts, and the Worker is deployed by CI.
