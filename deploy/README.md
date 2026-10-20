# deploy/

Support files for the local production stack (`docker-compose.prod.yml`), its
free-tier emulation (`docker-compose.emulation.yml`) and the CI checks on the
image and packaging files (`ci/`). The usage guide is in
[docs/deployment.md](../docs/deployment.md#0-rehearse-the-whole-stack-locally).

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
| `ci/check_image.py` | CI image job, you | gate on the built image: numeric non-root user, no build tools, a `/health` check that never touches the database (proved against a logging Postgres) |
| `ci/lint.sh` | CI, you | `docker build --check` on every Dockerfile, `docker compose config` on every compose set, shell syntax; hadolint/shellcheck when installed |

Nothing here is used in production: Render runs the image, Neon is restored
by the release scripts, and the Worker is deployed by CI. The two `ci/`
scripts are what CI runs on the image and the packaging files before anything
is pushed.
