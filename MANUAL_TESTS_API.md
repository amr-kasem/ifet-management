# Manual test capture — internal LabOS API

**Audience: the LabOS UI developer.** This is an internal LabOS document. It is
not the Airtable interface contract and nothing here crosses that boundary —
these routes never call Airtable, and Airtable never calls them.

Routes covering **Impact**, **Forced Entry** and **ANSI Z97.1**: the three
tests an operator enters by hand. Static Load and Cycles are unchanged and are
not described here.

Machine-readable: `src/management_service/openapi.json`, or `/docs` on a running
instance. Both are regenerated from the running app, not hand-written.

**What is actually verified**, as of 2026-09-08 — because "documented" and
"tested" are not the same thing:

- **All 18 routes are exercised by the suite.** `tests/route_coverage.py` records
  the matched route of every request the tests make and exits non-zero if any is
  unexercised, so a route added without a test fails rather than passes quietly.
  It was written because the honest answer to "are they all tested?" was 13 of 18.
- **246 tests + 84 subtests** against **PostgreSQL 13**, the version production
  runs — not SQLite.
- **The migration is rehearsed against populated tables**, not an empty database:
  P1 → M2 → `d1a6b93f2e57` in one ordered upgrade, with the 114-shot backfill,
  then rolled back.
- **Every route in this document is cross-checked against `openapi.json`.** The
  route tables here are not maintained by hand alone.

---

## 1. What these tests are

| Test | What happens | What LabOS records |
|---|---|---|
| **Impact** | A windborne-debris missile (ASTM E1886/E1996) is fired at the specimen, N times | **Numbered impacts — 1, 2, 3 — each with its own outcome and its own photographs** |
| **Forced Entry** | Specified loads and manipulation against the lock and sash (ASTM F588 / F476, AAMA 1304) | Pass or fail, against a named grade |
| **ANSI Z97.1** | A weighted bag is swung into the glazing; pass if it does not break, or breaks safely | Pass or fail, against a class |

Two things worth knowing before building screens:

- **ANSI Z97.1 is also an impact test**, but a different one — it qualifies the
  *glass*, which is why it is normally done first. Missile Impact qualifies the
  *assembly*. They are separate test types on purpose.
- **None of the three touches the rig.** No VFD, no valves, no pressure, no
  gauges, no MQTT. There is no "running" state to poll and no hardware to wait
  on — these are forms.

---

## 2. Two levels — the test, and attempts at it

This follows the shape the codebase already uses. It is not new structure:

```
static_tests          ──trials──►  static_test_results   ──►  deflections
cyclic_tests          ──trials──►  cyclic_test_results   ──►  deflections
manual_tests          ──trials──►  manual_test_results
missile_impact_tests  ──trials──►  impact_test_results   ──►  shots
```

**So: create the test, then start an attempt on it.** `POST …/trials` is the
"start" — the operator pressing the button — exactly as static and cyclic
record theirs.

The attempt row is a `TestResult`, which already carries `trial_number`
(= Attempt Number), `labos_attempt_id`, `labos_test_id`, the correction chain
and the review columns. That is why two levels rather than one flat row:
**`labos_test_id` is stable across every attempt at the same test**, and without
it "attempt 2 of the same test" cannot be expressed — nor can the Airtable
payload, which requires `LabOS Test ID` and `LabOS Attempt ID` as separate
fields on every phase.

### Attempt phases

```
  POST …/trials  ─────►  PUT /test-results/{id}/finish  ─────►  PUT …/verdict
  status: In Progress    status: Completed | Aborted            REVIEWER decides
  test_result: Pending   test_result: STILL Pending             Pass|Fail|Inconclusive
```

**Everything that acts on an attempt lives on `/test-results/{id}`** — terminate,
review, attach evidence. One route each, for all five test types, because it is
the same business whatever was tested. There is no per-type copy, and static and
cyclic inherit review and evidence the day they need it.

**The two words that look the same and are not:**

| Field | Whose | When | Meaning |
|---|---|---|---|
| `result` (boolean) | the **operator** | `finish` | "the specimen resisted / it did not" |
| `test_result` (string) | the **reviewer** | `verdict` | `Pending` → `Pass` \| `Fail` \| `Inconclusive` |

Separate columns on purpose. Collapsing them would let an operator certify their
own work. `operator_name` and `verdict_by` are likewise stored separately even
when the same person does both.

Neither is authenticated — LabOS has no user table. Both are declared names the
UI supplies; remember the last operator per device, but do not imply proof.

**Rules the API enforces, so the UI need not guess:**

1. A verdict before `finish` → **400**. Evidence freezes on termination.
2. A second verdict → **409**. The first stands; a change is a *new attempt*.
3. A photo after the verdict → **409**.
4. `finish` on an already-finished attempt → **400**.
5. An attempt on a finished test → **400**.
6. `retest_required` has **no default**. `null` means nobody has decided.
7. Completion is explicit, or an abort with a reason. **Missing data is never a
   pass.**

`trial_number`, `labos_attempt_id` and `labos_test_id` are allocated
server-side. Do not send them. Every attempt is retained.

## 3. Forced Entry and ANSI Z97.1

Both live in one table, distinguished by `type`.

| Route | |
|---|---|
| `POST /projects/{pid}/manual-tests/` | create the test — `type` is `Forced Entry` or `ANSI Z97.1` (anything else **422**), plus `required_option` (the grade or class) and the optional `airtable_*` links |
| `GET /projects/{pid}/manual-tests/` | list tests with their attempts; optional `?type=` |
| `POST /projects/{pid}/manual-tests/{id}/trials` | **start an attempt** — `{"operator_name": "technician-1"}` |
| `GET /projects/{pid}/manual-tests/{id}/trials` | attempts in order |
| `PUT /projects/{pid}/manual-tests/{id}/finish` | mark the test complete; no further attempts |

Then the shared attempt routes in §5.

## 4. Impact

An impact test is a **sequence**: impact 1, impact 2, impact 3 — each numbered,
each with its own pass/fail, each with its own photographs.

```
impact test
  └── attempt 1
        ├── impact 1   result: pass    photos: [ ]
        ├── impact 2   result: pass    photos: [ 1 ]
        └── impact 3   result: FAIL    photos: [ wide shot, corner detail, interior face ]
        └── attempt-level photos: [ specimen before ]
```

**Any impact can carry any number of photographs, including none.** `POST
/shots/{id}/photos` is repeatable; `photos` on each impact is a list, returned in
upload order. An attempt also has its own photographs — the specimen before
testing, the overall setup — which have `shot_id: null`.

| Route | |
|---|---|
| `POST /projects/{pid}/impact-tests/` | create the test. **Everything optional** — the protocol fixes the missile, so `{}` is valid |
| `GET /projects/{pid}/impact-tests/` | list with attempts |
| `POST /projects/{pid}/impact-tests/{id}/trials` | start an attempt |
| `GET /projects/{pid}/impact-tests/{id}/trials` | attempts in order |
| `PUT /projects/{pid}/impact-tests/{id}/finish` | mark the test complete |
| `POST /test-results/{aid}/shots` | **record one impact** — `{"result": true}`. `result` is the only required field; `area`, `velocity`, `note` optional. Omitting it is **422** |
| `GET /test-results/{aid}/shots` | the impacts **in order**, each with its photographs |
| `POST /shots/{sid}/photos` | photograph of **one specific impact**; call it once per photograph |

**`shot_number` is allocated server-side** — 1, 2, 3 in recording order,
restarting at 1 for each attempt. Not accepted from the client: one that chose
its own could number two impacts the same, or renumber a sequence already
photographed. Sending it is ignored, not rejected.

**Finishing an impact attempt** additionally requires at least one impact and at
least one photograph (not when aborting). A per-impact photograph counts, so
photographing the impacts satisfies it without a separate upload.

The photo requirement is enforced at finish, **not** when publishing to Airtable:
attachments deliver on their own channel and may settle later, so making an
upload a precondition for publishing would let a queued file block a measured
result.

## 5. The attempt routes — all five test types

| Route | |
|---|---|
| `PUT /test-results/{id}/finish` | `{"result": true, "note": "...", "testing_continued": "Stopped"}` or `{"abort_reason": "Equipment Fault"}`. `result` required to complete a manual attempt |
| `PUT /test-results/{id}/verdict` | `{"test_result": "Pass", "verdict_by": "reviewer-1", "retest_required": false, "rationale": "optional"}` |
| `POST /test-results/{id}/photos` | attempt-level evidence; `multipart/form-data` with `file` and optional `note` |
| `GET /test-results/{id}` | the attempt |
| `PUT /test-results/{id}` | amend the attempt's `note` / `image_path` only. **Pre-existing route** — it does not touch the verdict or any measurement, and it is not a way to edit a reviewed attempt. ⚠️ **`multipart/form-data`, not JSON** — it predates the JSON routes, so a JSON body is accepted, changes nothing, and still returns `200` |

`test_result` must be `Pass`, `Fail` or `Inconclusive` — **not** the Airtable
spellings `Passed`/`Failed`, which the sync layer translates later.

## 6. How these routes relate to the static/cyclic ones you already use

Same shape, with two deliberate differences worth knowing before you write the
client.

| | Static / Cyclic | Manual / Impact |
|---|---|---|
| Create the test | `POST /projects/{pid}/static_tests/` | `POST /projects/{pid}/manual-tests/` |
| Address a test by | `{index}` — its ordinal, 0–5 or 0–7 | **`{test_id}`** |
| Start / record an attempt | `POST …/{index}/trials` | `POST …/{test_id}/trials` |
| List attempts | `GET …/trials` | `GET …/trials` |
| Finish the test | `PUT …/{index}/finish` | `PUT …/{test_id}/finish` |
| Embedded in `ProjectSchema` | yes | yes |

**Why `{test_id}` and not `{index}`.** Static and cyclic tests are generated
automatically from the design pressures — six and eight of them — so the ordinal
is meaningful and stable. Manual and impact tests are created by an operator on
demand, so an index would be an arbitrary counter with nothing to anchor it.

**⚠️ `POST …/trials` means something different here.** On static and cyclic, the
rig posts a *finished* trial in one call, with its deflections. On these, it
**starts** an attempt: you get back an `In Progress` attempt, then record impacts
and photographs against it, then `PUT /test-results/{id}/finish`. Same path
suffix, two-step instead of one — because a person filling in a form is not an
instrument reporting a completed measurement.

**Everything project-level still works unchanged.** `GET /devices/{id}/projects/`
returns the project with `static_tests`, `cyclic_tests`, `infiltration_tests`,
`missile_impact_tests` **and now `manual_tests`**, so a screen that loads the
project once sees all five test types.

## 7. What the UI needs to supply, and where it comes from

| Field | Airtable-linked job | LabOS-only job |
|---|---|---|
| `project_id` | from the picker | from the picker |
| `type` | from `Requirement Code` (`FORCED_ENTRY` → Forced Entry, `ANSI_IMPACT` → ANSI Z97.1) | operator chooses |
| `required_option` | `Required Option` | operator types |
| `missile`, `missile_weight` | ~~`Missile Type`, `Missile Weight`~~ **withdrawn 2026-09-10** — never pre-filled again | operator types, or leaves blank |
| `impact_family` | **not yours to send** — resolved from the bound section's requirement code | operator chooses `SMI` or `LMI` |
| `impact_level` | operator chooses `D` or `E`, LMI only | same |
| `target_velocity` | operator enters, ft/s | same |
| `operator_name` | remembered per device | remembered per device |
| `airtable_*` | from the mirror | omit |

**Both columns must work.** A LabOS-only job has no Airtable identity and is
fully testable — that is the normal mode, not a degraded one.

### Impact classification — built 2026-09-10, **not yet deployed**

The routes below behave this way in the codebase now. **Nothing is deployed**, and the two Airtable
fields do not exist in any base yet — so build against this, but do not expect it from a running
instance until the deploy lands.

Impact requirements no longer arrive from Airtable as `Missile Type` / `Missile Weight` /
`Impact Velocity`. Two LabOS-owned values replace them:

| Field | Type | Values | Who sets it |
|---|---|---|---|
| `impact_family` | string | `SMI` · `LMI` | **The importer**, from the section's `IMPACT_SMI`/`IMPACT_LMI` code, on any Airtable-bound test. Operator-set only on a LabOS-only test |
| `impact_level` | string | `D` · `E` | Operator. **LMI only** |
| `target_velocity` | number | ft/s | Operator. Not derived from anything |

`impact_classification` comes back on both the test and the project payload as a **read-only derived
value** — `SMI`, `LMI Level D` or `LMI Level E`. Render it; never compute it, and never send it.

**`PATCH /projects/{pid}/impact-tests/{id}`** — new. This is how the two values arrive, because
neither is required at creation:

- `impact_level`, `target_velocity` — editable until an attempt **completes**, then `409`.
- `impact_family` — only on a LabOS-only test, and only until **any** attempt exists, aborted
  included. On a bound test it is `400`. Any other key is `422`.

**Two new `400`s on `PUT /test-results/{id}/finish`**, for Impact attempts without `abort_reason`:
the test must have a resolvable classification, and a `target_velocity`. An abort needs neither.

**What the screen needs.** For an `IMPACT_SMI` section there is **no family control** — show "SMI",
read-only. For `IMPACT_LMI`, a required D/E choice. Never a three-option picker: the SMI/LMI half is
Airtable's answer and offering it invites a contradiction the API will reject anyway.

**Careful with the two velocities.** `target_velocity` is per *test* and operator-entered.
`shots.velocity` is the achieved velocity of one impact, unchanged, and stays per shot. Different
fields, different meanings — they must not share a control.

**Unchanged:** two-step start-then-finish, one attempt per impact, `shot_number`, `Impact Result`,
the photograph requirement, and impact location on the shot.

---

## 8. Things that will bite

- **`test_result` is `Pending` after `finish`.** A screen showing "Completed"
  next to "Pending" is correct, not a bug. It means: tested, awaiting review.
- **`retest_required` is `null` until a verdict.** Render it as "not yet
  reviewed", never as an unchecked box.
- **Photos are `POST`-only.** There is no delete: evidence is append-only until
  the verdict, then frozen.
- **Errors are meant to be shown.** The `detail` strings say what to do next;
  surface them rather than replacing them with "something went wrong".
- **A test with no attempts is normal.** It means created, not yet started.
- **Handle `null` on the project payload.** `missile`, `missile_weight` and a
  shot's `area` / `velocity` / `note` are all nullable now — and `missile` /
  `missile_weight` stay nullable permanently: they become history-only fields
  once Impact Classification lands, never pre-filled from Airtable again. The project route is
  `GET /devices/{id}/projects/`; there is no `GET /projects/{id}`.
- **Render `shot_number`, never the shot `id`.** They are unrelated numbers, and
  the id is meaningless to an operator.
- **`shot_id` says where a photograph belongs.** Set → it shows that impact.
  Null → it is attempt-level. The attempt's own `photos` list contains **both**,
  because a per-impact photograph is still evidence of the attempt — so when
  rendering an impact's gallery, read the impact's list, not the attempt's, or
  the same file appears twice.
- **A photograph count of zero is normal.** Impacts need not each be
  photographed; the attempt only needs at least one photograph overall to
  finish.

---

## 9. Why Forced Entry and ANSI carry so little — and whose decision that was

Both are recorded as **pass or fail against a named grade or class**, with notes
and optional photographs. That is the whole model today.

**Where that came from, stated plainly:** the product owner's message lists
*"Forced-entry results"* and *"ANSI Z97.1 results"* as things LabOS sends back,
but does **not** specify their shape. The pass/fail decision is IFET's, given on
2026-09-07. So if sub-detail is wanted later — a per-attempt-point breakdown for
Forced Entry, or drop height and class for ANSI — that is revisiting our own
decision, not a change of his requirement.

The schema is built to allow it: both live in `manual_tests` with a `type`
discriminator, so adding type-specific columns or a JSON detail block is
additive. `required_option` already carries the grade or class as free text.

**No dedicated Airtable scalar is added for either** — `Test Type` and
`Test Result` are both single-selects there, so their reporting filters and
groups both workflows natively. That stays true whatever detail we add locally.

---

## 10. Running it locally

```bash
docker compose -f src/management_service/tests/postgres_harness/docker-compose.yaml up -d
```
Postgres on `127.0.0.1:15432`, disposable, `down -v` removes it. Point
`DATABASE_URL` at it and run `uvicorn app.main:app --reload` from
`src/management_service/`. Interactive docs at `/docs`.

Behaviour is pinned by `tests/test_manual_tests.py` — read it as executable
examples of every rule above.

---

## 11. Beyond the manual tests — the rest of the surface, added 2026-09-08

This document covers the three manual test types. The API grew the same day, and
these are the routes a UI needs that are **not** described above. Full detail is
in `openapi.json` / `/docs`; this is the map.

### Picking a job from Airtable, and pre-filling from it

**Every one of these reads a local mirror, never Airtable.** A picker at a rig
cannot depend on someone else's API being up, so an empty mirror gives an empty
list rather than a spinner or a 502.

| Route | |
|---|---|
| `GET /airtable/projects` | jobs available to import. Each carries `mirrored_at` — a stale mirror is a fact worth showing |
| `GET /airtable/projects/{rec}/specimens` | with `imported_project_id`, so the UI can say "already imported" instead of letting someone import twice and wonder why nothing changed |
| `GET /airtable/specimens/{rec}/protocols` | |
| `GET /airtable/protocols/{rec}/sections` | each section with `executable`, `applicability`, and **`refused`** — the reason LabOS will not run it. **Show `refused`.** It is the one thing the operator can actually fix |
| `POST /airtable/refresh` | the only route that calls Airtable. Deliberately a button, not a side effect of reading |
| `POST /airtable/import/plan` | what an import *would* do — executable, unconfirmed and refused sections — without doing it |
| `POST /airtable/import` | creates the project. **Idempotent on the mock-up**: a repeat returns the same project |

`POST /airtable/import` takes `{device_id, project_record_id,
specimen_record_id, protocol_record_id, name?}` and returns the same
`ProjectSchema` as a typed project — because it goes through the *same* create
route. `gauge_count`, `impact_count` and the `airtable_*` ids are now on that
response, so a pre-filled form can read what was filled in.

**Which rig is LabOS's alone.** `device_id` is an operator's decision about a
physical machine; Airtable has no opinion on it.

### Sync status — the operator's "did it reach Airtable?"

| Route | |
|---|---|
| `GET /sync/status` | `status` is one of **`Synced` · `Pending` · `Sync Failed` · `Retry Required`** — the Airtable team's own four words. Also `attachment_backlog`, `attachment_parked`, `artifacts_needing_reconciliation`, `failed_publications`, `worker_alive` |
| `GET /sync/queue` | one row per pending write, with `channel` (`record` or `attachment`) |
| `POST /sync/queue/{id}/retry` | un-park one entry. Re-enables eligibility; sends nothing |
| `GET /sync/failures` | payloads LabOS **refused to queue**. Invisible in `/sync/queue` by construction — they never got an entry |
| `POST /sync/failures/{id}/repair` | rebuild and re-queue one. `409` while it is still refused, with the current reason |

Two things worth building around:

- **Attachments have their own channel.** A photograph stuck in delivery cannot
  hold up a verdict, and a parked attachment does not drag the headline status to
  `Retry Required`. Show `attachment_backlog` separately from `failed_publications`.
- **`/sync/failures` needs a surface.** These are results that were saved
  correctly and could not be described to Airtable. Before this route existed the
  headline read `Synced` while such an attempt had never been published.

### Two behaviours that will change your screens

- **Start is idempotent.** `POST …/trials` on a test that already has an open
  attempt returns **that attempt**, not a new one — a double-click cannot become
  two certification records. A retest requires the previous attempt to be
  terminal, so a "Retest" button must finish or abort first.
- **One active run per rig.** Starting a test on a rig that is already running a
  different one returns **409**. Surface the message; it names the blocking
  attempt.

### Run start, for Static Load and Cycles

`PUT /projects/{pid}/static_tests/{index}/start` and
`PUT /projects/{pid}/cyclic_tests/{index}/start` accept
`{"operator_name": "..."}`. **Send it.** The rig's callback carries only
deflections, so the operator declared here is what lets the attempt complete and
publish — without it the attempt stays `In Progress` and the refusal shows up in
`/sync/failures`.

### Corrections — and why a correction is not a retest

`POST /test-results/{id}/correct` — added 2026-09-08 (TC1g), and **the screen has to tell the two
apart**, because Airtable cannot.

A **retest** is a new attempt at the same test: the first one happened, and its record stands. A
**correction** says the earlier attempt's record is wrong — mis-typed, recorded against the wrong
specimen, attributed to the wrong operator. Both produce a new attempt; only one of them means
"disregard the previous row".

| | |
|---|---|
| Body | `{"reason": "..."}` — required, and it is the operator's words, not a code |
| Returns | a new attempt, **open**, which is then recorded and finished through the ordinary routes |
| Sets | `corrects_attempt_id` → the corrected attempt's `LabOS Attempt ID`, and `correction_reason` |
| Publishes as | `Corrects Attempt ID` and `Correction Reason` on the new row. Both are **blank on an ordinary attempt and on a retest** |
| Works on | a terminal attempt, **aborted included**, and on a test that is already `finished` |

Three refusals, and each means something different on a screen:

| | `400` when |
|---|---|
| The original is still open | An open attempt is *finished* correctly, not corrected — its evidence is not frozen yet, so there is nothing to supersede. Offer "finish" or "abort", not "correct" |
| The test already has an open attempt | A correction creates one, and two open attempts make "the current attempt" ambiguous. Resolve the open one first |
| The attempt predates the per-type attempt tables | Its parent test cannot be resolved. Historical rows are not correctable through this route |

**Nothing is deleted or edited.** Attempts are append-only: the original row stays in Airtable exactly
as it was, and the correction points at it. That is what lets somebody reading their base six months
later see both what was recorded and what replaced it.

**Do not offer "correct" as a synonym for "try again".** A correction that is really a retest puts a
`Corrects Attempt ID` on a row that supersedes nothing, and there is no route that takes it back.
