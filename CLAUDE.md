# ifet-management — project instructions

The management node for the IFET test lab: Postgres, `report-api` (FastAPI), the React UI served by httpd, the
MQTT broker, and two SICK gateway services. This node **is production.**

**Read `../ifet-firmware/docs/INDEX.md` first** — it maps every document across both repos and Notion and says
which one is authoritative for which subject. Secret handling and the deploy runbook are in
`deployment/SECRETS.md`.

## Branch reality

- **Production runs `latest`.** `main` is stale (Oct 2024) — never branch from it.
- Integration work is on `feature/labos-airtable`, cut from `latest`.

## Before touching anything that runs

1. **`compose.yaml` now requires a `.env`.** Every credential is interpolated with `${VAR:?…}`, so compose
   **refuses to start** if a value is missing — deliberately, to prevent a silent fallback to a default
   password. On the node, `.env` must exist *before* the new `compose.yaml` lands. Runbook: `deployment/SECRETS.md` §2.
2. **Do not rotate the database password during the `.env` migration.** Postgres only applies
   `POSTGRES_PASSWORD` when it initialises a data directory; the node's volume already exists, so a new value
   would just break `report-api` auth. Rotation is a separate operation (`ALTER USER` plus the `.env` edit) in a
   maintenance window.
3. **Never rebuild or recreate containers here without notice and an agreed window.** Rehearse off-production
   with an isolated compose project name and throwaway values.
4. On the node, `compose.yaml` and `config/fstab` are `git update-index --skip-worktree`. Leave them that way
   unless the runbook says otherwise.
5. `report-api`'s `app/` is **bind-mounted**, so a Python change there is live in the container without a
   rebuild. Keep new modules import-safe and side-effect-free.

## Secrets

- Everything server-side, in a gitignored `.env`. `.env.example` is the committed template.
- **`deployment/config/config.json` and `src/ifet_ui_react/config.json` are served to the browser.** Never put
  a credential — least of all the Airtable token — in either. Their `mqtt.password` fields are empty and must
  stay empty.
- Read Airtable settings through `app/config.py`, not `os.getenv` — it is the single entry point, it never
  prints the token, and it enforces the one-table write allowlist (an Airtable token is scoped per *base*, not
  per table, so that allowlist is what actually keeps us out of their read-only tables).
- **Run `./deployment/scripts/check-secrets.sh` before every commit and every deploy.**

## Working practice

- Commit every day's work, including SSH sessions, and update the matching Notion page the same day.
- **Every commit is authored `gad <abdulrahmanashraf.gad@gmail.com>` with no assistant attribution** — no
  `Co-Authored-By`, no "Generated with", no session links. This node's git identity is unset, so pass the
  author explicitly when committing there.
