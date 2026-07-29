# Secrets & the `.env` migration — ifet-management

**P0 / Ref 42 (Epic IFET-32).** Credentials moved out of `compose.yaml` into a gitignored `.env` before an
`AIRTABLE_TOKEN` exists to leak.

> ## ⚠️ This is a breaking deploy for a production node. Read §2 before deploying.
> `compose.yaml` now uses `${VAR:?message}` for every credential, so **`docker compose up` refuses to start
> if `.env` is missing or incomplete** (verified: exit 1). That fail-fast is deliberate — it prevents a silent
> fallback to `password` — but on the **management node, which is production**, it means the stack will not
> come up until a correct `.env` exists on that node. **Create `.env` on the node first, validate, then take
> the new `compose.yaml`.** Never the other order.
>
> **Do not rebuild or recreate production containers to try this out.** Deployment is gated on §2.0: rehearse
> off-production, inform the deployment owner, agree a window. A test node is being requested from the
> manager; if it turns out not to run the management stack, the local rehearsal in §2.1 stands in for it.

---

## 0. Status — nothing is deployed by this change

| | |
|---|---|
| Where the change lives | Local clone only, branch `feature/labos-airtable`. |
| Production nodes touched | **None.** No SSH, no deploy, no container action was taken. |
| Can it reach production by itself? | **No.** Verified during the 2026-07-24 branch reconcile: no cron, systemd timer, Watchtower, or updater container pulls git or rebuilds anywhere on the fleet. Deploy is manual only. |
| Extra protection today | The node's `compose.yaml` is under `git update-index --skip-worktree` (set during the reconcile), so even a `git reset --mixed` leaves the node's live file alone. |
| Consequence of that protection | Repo and node **diverge** on `compose.yaml` until the deliberate migration in §2. That divergence is now documented rather than accidental. |

`src/management_service/app/config.py` is new but **inert**: nothing imports it yet (Ref 43's Airtable client
will). Note it lands live in the container without a rebuild, because `./src/management_service/app` is
**bind-mounted** to `/app/app` — so it must stay import-safe and side-effect-free, which it is (stdlib only,
no I/O at import, no exception on a missing token).

---

## 1. What is where

| Value | Lives in | Never in |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `DATABASE_URL` | `.env` on each node (gitignored, `chmod 600`) | git, `compose.yaml`, any UI config |
| `PGADMIN_DEFAULT_EMAIL` / `_PASSWORD` | `.env` | as above |
| `SICK_API_HOST_1` / `_2` | `.env` (node-specific — **production has these two swapped relative to the old repo literals**) | — |
| `AIRTABLE_TOKEN` | `.env`, server-side only, read once via `app/config.py` | **git, `deployment/config/config.json`, `src/ifet_ui_react/config.json`** — those two are served to the browser |
| `AIRTABLE_BASE_ID`, table name, allowlist, sync flag | `.env` (not secret, but environment-specific) | — |
| `LABOS_PUBLIC_ORIGIN` | `.env` | — |

`.env.example` is the committed template. `.gitignore` covers `.env`, `.env.*`, and `*.env`, with
`!.env.example` re-included.

**Guard:** `./deployment/scripts/check-secrets.sh` — fails on a tracked `.env`, an Airtable token pattern in
tracked content, a non-empty credential in either browser-served config, or a literal credential left in
`compose.yaml`. Run it before every commit and every deploy. Currently: **PASS**.

---

## 2. Migration runbook — **gated, not ready to run**

### 2.0 Prerequisites — all three, before anyone touches the production node

The management node **is production**. Recreating its containers is a scheduled operation, not a side effect
of merging this branch.

| Gate | Status | Owner |
|---|---|---|
| **A · Rehearsed somewhere that is not production** — the full `.env` + new `compose.yaml` path brought up and verified on a throwaway or test environment | ✅ **DONE 2026-07-29, off-node** — see §2.1a for the evidence | LabOS |
| **B · Deployment/production owner informed and a maintenance window agreed** — services `db`, `pgadmin`, `report-api`, and both `sick_gateway`s restart, so no test may be mid-run | ☐ open | LabOS → manager / production owner |
| **C · A reachable test node** to deploy to first | ☐ **blocked** — the `test` node has been offline since at least 2026-07-24 (connection refused, re-confirmed 2026-07-26). The manager is being asked to bring it up and grant SSH. Also confirm **whether it runs the management stack at all** — it has historically been a firmware rig, and if it has no management deployment, gate A must be satisfied locally instead (§2.1). | manager |

Until A **and** B are green, this branch stays unmerged-to-`latest` and undeployed. The node keeps running its
current stack; the `skip-worktree` on its `compose.yaml` means nothing here can reach it by accident.

### 2.1 Rehearsal without a node (satisfies gate A on its own)

The whole point of the `${VAR:?…}` change is that it is verifiable off-node, at zero risk:

```
cp .env.example .env && edit            # throwaway values, never a node's
docker compose config --quiet           # exit 0 = interpolation resolves
docker compose config | grep -E 'AIRTABLE_|SICK_API_HOST|POSTGRES_USER'
rm .env && docker compose config --quiet ; echo $?   # exit 1 = fail-fast works
```
For a fuller rehearsal, bring up just the two services that consume the credentials, on non-conflicting host
ports and an isolated project name, then tear down:
```
docker compose -p labos-rehearsal -f compose.yaml -f <override-with-alt-ports> up -d db report-api
docker compose -p labos-rehearsal logs --tail=40 report-api    # expect no auth errors
docker compose -p labos-rehearsal down -v                      # -v drops the throwaway volume ONLY for this project
```
⚠️ `down -v` is safe **only** with the `-p labos-rehearsal` project name, which owns its own volumes. Never run
`down -v` against the default project on a node.

### 2.1a Rehearsal evidence — executed 2026-07-29, zero production contact

Run in the local clone with a throwaway `.env`, project name `labos-rehearsal`, and `report-api` remapped to
host port 18000 so nothing collided. Results:

| Check | Result |
|---|---|
| `docker compose config --quiet` with a complete `.env` | exit **0** |
| Same with `.env` absent | exit **1**, message `required variable POSTGRES_USER is missing a value: set POSTGRES_USER in .env (see .env.example)` — fail-fast confirmed |
| `up -d db report-api` | both containers **Up**; image built clean |
| `report-api` env inside the container | `DATABASE_URL` + all five `AIRTABLE_*` + `LABOS_PUBLIC_ORIGIN` present and correctly interpolated from `.env` |
| Database auth with `.env`-supplied credentials | **no auth error**; `Application startup complete` on every worker |
| `GET /docs` | **HTTP 200** |
| `python3 -m app.config` inside the container | `{'base_id': 'appYBTqIL43pmS0xN', …, 'sync_enabled': False, 'token_present': False, 'public_origin': None}` → `state: not configured (missing: AIRTABLE_TOKEN)`. **The token is never printed**, and a blank token is a valid non-fatal state. |
| Teardown | `down -v` on the isolated project only; **0** rehearsal containers, **0** rehearsal volumes left; throwaway `.env` deleted |

**What this does and does not prove.** It proves the interpolation, the fail-fast, the container-level env
wiring, the DB auth path, and that `app/config.py` is import-safe with no token. It does **not** prove
anything about the node's *own* credential values or its `skip-worktree` swap — those are §2.2 steps 1–6 and
still need gates B and C.

### 2.2 The node steps

Ordered. Steps 1–5 are read-only or additive; nothing changes what is running until step 6.
**Run these on the test node first (gate C), then on production only inside the agreed window (gate B).**

**1. Read the live values (read-only).** They are the source of truth, not this repo:
```
grep -nE 'POSTGRES_|PGADMIN_|DATABASE_URL|SICK_API_HOST' /home/labadm/ifet-management/compose.yaml
```

**2. Back up the live file:**
```
cp /home/labadm/ifet-management/compose.yaml ~/mgmt-compose-backup-$(date +%F).yaml
```

**3. Write `/home/labadm/ifet-management/.env` with those values copied verbatim.**
> 🔒 **Do not rotate the database password in this step.** Postgres applies `POSTGRES_PASSWORD` only when it
> *initialises* a data directory. The node's `postgres_data` volume already exists, so a new password in
> `.env` would **not** change the database — it would only make `report-api` fail authentication. Rotation is
> a separate, deliberate operation: `ALTER USER … WITH PASSWORD …` inside psql **and** the `.env` update,
> together, in a maintenance window.

Keep `DATABASE_URL` consistent with `POSTGRES_USER`/`POSTGRES_PASSWORD`, percent-encoding any URL-reserved
characters. Then:
```
chmod 600 /home/labadm/ifet-management/.env
```

**4. Validate before swapping anything** — this reads `.env` + the *new* compose and resolves interpolation
without starting or stopping a single container:
```
docker compose config --quiet   # must exit 0
docker compose config | grep -E 'SICK_API_HOST|POSTGRES_USER|AIRTABLE_'   # eyeball the resolved values
```
If this fails, stop. The live stack is still running and untouched.

**5. Sync the repo refs (never rewrites live files):**
```
git fetch --prune origin
git reset --mixed origin/feature/labos-airtable    # HEAD + index only. NEVER --hard
```

**6. Take the new `compose.yaml` — the one genuinely mutating step:**
```
git update-index --no-skip-worktree compose.yaml
git diff compose.yaml          # review: expect ONLY literal → ${VAR} substitutions
git checkout -- compose.yaml
docker compose config --quiet  # re-validate against the real new file
docker compose up -d           # recreates only services whose env changed
```
Expect a **brief restart of `db`, `pgadmin`, `report-api`, and both `sick_gateway` services** — their
environment changed. Do this in a maintenance window, not mid-test. The `postgres_data` volume is named and
persists across recreation, so **no data loss**; and `config/fstab` keeps its own `skip-worktree`.

**7. Verify:**
```
docker compose ps                       # all services Up
docker compose logs --tail=40 report-api  # no auth errors
curl -sf localhost:8000/docs >/dev/null && echo api-ok
curl -sf localhost/config.json >/dev/null && echo ui-ok
./deployment/scripts/check-secrets.sh   # must PASS
python3 -m app.config                   # optional, inside report-api: prints redacted state
```

**8. Rollback (any failure):**
```
cp ~/mgmt-compose-backup-<date>.yaml compose.yaml
git update-index --skip-worktree compose.yaml
docker compose up -d
```
Rollback needs no database work, because step 3 changed no credential — only where they are read from. That
is precisely why rotation is kept out of this migration.

---

## 3. Standing rules

1. **Secrets are server-side only.** `deployment/config/config.json` and `src/ifet_ui_react/config.json` are
   served to the browser. Their `mqtt.password` fields are empty today and must stay that way. An Airtable
   token in either file is a live credential published to every UI visitor.
2. **Never `docker cp` a hot patch into a production container.** Deploy = rebuild/recreate from a clean
   checkout, per the production-deploy caution.
3. **Never `git checkout`, `reset --hard`, `stash pop`, `clean`, `pull`, or `merge`** on a node except the
   single reviewed `checkout -- compose.yaml` in §2 step 6. Those commands rewrite working-tree files, which
   on this node means the live bind-mounted configs the running containers read.
4. **Token rotation** (the Airtable PAT leaked in the vendor's PDF is being revoked): update `.env`, then
   restart `report-api` only — `docker compose up -d report-api`. No rebuild, no other service touched.
5. **Run the guard before every commit and deploy.** It is cheap and it catches the exact class of mistake
   that leaked the vendor's token.
