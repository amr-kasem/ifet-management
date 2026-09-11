# Manual test capture — internal LabOS API

**Audience: the LabOS UI developer.** This is an internal LabOS document. It is
not the Airtable interface contract and nothing here crosses that boundary —
these routes never call Airtable, and Airtable never calls them.

> ⚠️ **This is the API reference, not the UI implementation contract.** What the
> screens must do — which workflow obtains its test object how, which controls
> exist, what the operator may and may not be offered — is
> `ifet-firmware/docs/labos-airtable/contract/tc5-ui-developer-handoff-2026-09-11.md`,
> and **that document wins on any domain question**. This one describes the
> routes underneath it in more depth. Where the two ever disagree, the handoff
> is right and this file is a bug.

Routes covering **Impact**, **Forced Entry** and **ANSI Z97.1**: the three
tests an operator enters by hand. Static Load and Cycles are unchanged and are
not described here.

Machine-readable: `src/management_service/openapi.json`, or `/docs` on a running
instance. Both are regenerated from the running app, not hand-written.

**What is actually verified**, as of 2026-09-11 — because "documented" and
"tested" are not the same thing:

- **The manual-test surface is now 20 routes**, not the 18 of 2026-09-08:
  `POST /test-results/{id}/correct` and `PATCH /projects/{pid}/impact-tests/{id}`
  were added after that count was written. `route_coverage.py` reports 25,
  which is these 20 plus the five `/sync` routes it also watches.
- **All of them are exercised by the suite.** `tests/route_coverage.py` records
  the matched route of every request the tests make and exits non-zero if any is
  unexercised, so a route added without a test fails rather than passes quietly.
  It was written because the honest answer to "are they all tested?" was 13 of 18.
- **433 tests + 108 subtests** against **PostgreSQL 13**, the version production
  runs — not SQLite. Verified 2026-09-11, every suite green.
  ⚠️ **Run the suites per file.** Collecting all 14 into one pytest process
  exhausts `postgres:13`'s 100 connections part-way through and reports
  `FATAL: sorry, too many clients already` as a wave of unrelated failures.
  That is the harness, not the code: each file passes on its own.
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
- **None of the three *commands* the rig, but all three *occupy* it.** No VFD,
  no valves, no pressure, no gauges — `report-api` has no MQTT client at all, so
  there is no "running" state to poll and no hardware to wait on. The data entry
  is a form.
  **What is not a form: the rig lock.** All four attempt types share
  `attempts.rig_is_busy`, which is keyed on the project's `device_id`. So
  starting a Forced Entry attempt on a rig with an open Cycles attempt is a
  `409`, and an open Impact attempt will block a Static Load one the same way.
  One rig holds one specimen and runs one test at a time; that is a physical
  fact, and these tests are inside it rather than beside it.

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

**The sequence is made of attempts, not of shots inside one attempt.**
(Delivery plan §4.5a, product owner 2026-09-08.) This is the one thing in this
document most likely to be remembered wrongly, because it used to be the other
way round:

```
impact test
  ├── attempt 1  ──►  impact 1   result: pass   photos: [ setup, face ]   ──► Airtable row 1
  ├── attempt 2  ──►  impact 2   result: pass   photos: [ face ]          ──► Airtable row 2
  └── attempt 3  ──►  impact 3   result: FAIL   photos: [ wide, corner ]  ──► Airtable row 3
```

**One physical impact = one attempt = one `Shot` = one Airtable row.** Five
impacts are five attempts and five rows, each separately finished and separately
reviewed. There is no attempt that holds a sequence.

| Route | |
|---|---|
| `POST /projects/{pid}/impact-tests/` | create the test. **Everything optional** — `{}` is valid. ⚠️ **Not for an imported job**: the importer already created it. See §7 |
| `GET /projects/{pid}/impact-tests/` | list with attempts |
| `PATCH /projects/{pid}/impact-tests/{id}` | set `impact_level` / `target_velocity` (and `impact_family` on a LabOS-only test). See §7 |
| `POST /projects/{pid}/impact-tests/{id}/trials` | **start one impact** |
| `GET /projects/{pid}/impact-tests/{id}/trials` | attempts in order |
| `PUT /projects/{pid}/impact-tests/{id}/finish` | mark the **whole test** complete — not one impact. See below |
| `POST /test-results/{aid}/shots` | **record this attempt's one impact** — `{"result": true}`. `result` is the only required field; `area`, `velocity`, `note` optional. Omitting it is **422** |
| `GET /test-results/{aid}/shots` | this attempt's impact |
| `POST /shots/{sid}/photos` | photograph of that impact; call it once per photograph |

**A second `POST …/shots` on the same attempt is `409`**, not a second impact.
The message says so: *"One attempt is one impact — start a new attempt on this
test to record the next one."* The invariant is a database constraint
(`uq_shots_attempt_number`), not a rule in the route.

**`shot_number` mirrors `attempt.trial_number`** — it is **not** a counter inside
the attempt, and it does **not** restart at 1. Impact 3 is attempt 3 and carries
`shot_number = 3`. Allocated server-side; sending one is ignored, not rejected.

**`Impact Number` on the Airtable row is `attempt.trial_number`** — the same
value, published under its own name, and omitted entirely on the other four test
types rather than sent as 0.

### Two different "finish" operations, and they are not interchangeable

| | Route | Means |
|---|---|---|
| **Attempt finish** | `PUT /test-results/{aid}/finish` | *this impact* is done. Called once per impact |
| **Test finish** | `PUT /projects/{pid}/impact-tests/{id}/finish` | *the whole impact test* is done. Called once, at the end |

`PUT …/impact-tests/{id}/finish` sets `finished = True`, and after it
`POST …/trials` returns **400** — so it closes the test to further impacts. It
is the operator declaring the sequence over.

⚠️ **Nothing checks it against the required impact count.** `projects.impact_count`
is accumulated at import from the `IMPACT_LMI` / `IMPACT_SMI` sections'
`Required Value`, and it is carried in the published JSON as a requirement — but
no route compares it to the number of attempts. Finishing after three of five
impacts is accepted. **If the screen is to warn, the warning is the screen's**;
the API will not refuse it.

### What an impact attempt needs before it can be completed

`PUT /test-results/{aid}/finish` with no `abort_reason` refuses with **400**
unless all four hold:

1. **Exactly one impact** on the attempt — not "at least one". Zero has nothing
   to report; two is a shape the constraint already refused.
2. **At least one photograph on the attempt.** A per-impact photograph counts —
   it carries the attempt id as well as the shot id — so photographing the impact
   satisfies this without a separate upload.
3. **A resolvable `impact_classification`** on the test (§7).
4. **A `target_velocity`** on the test (§7).

**An abort needs none of them.** `{"abort_reason": "Equipment Fault"}` is always
accepted on an open attempt.

`result` is **not** required in the finish body for an impact attempt: the
attempt's outcome is its impact's outcome, copied from the shot. Send it and it
is honoured; omit it and it is derived. For Forced Entry and ANSI it *is*
required, because there is no shot to derive it from.

The photograph requirement is enforced at finish, **not** when publishing to
Airtable: attachments deliver on their own channel and may settle later, so
making an upload a precondition for publishing would let a queued file block a
measured result.

### Photographs on an impact attempt

Any impact may carry **any number** of photographs, and an attempt may also carry
its own — the specimen before testing, the overall setup — with `shot_id: null`.
The attempt's `photos` list contains **both**, which is what makes a per-impact
photograph satisfy the finish gate.

⚠️ **"Zero photographs on an impact" is no longer a normal state.** It was, when
one attempt held several impacts and only the attempt needed evidence. Now the
attempt *is* the impact, so an impact with no photograph is an attempt that
cannot be completed — only aborted.

Photographs may be added between termination and the verdict. After the verdict
they are **409**: evidence is frozen, and adding to it requires a correction.

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

### ⚠️ First: for an imported job, do not create the test at all

**`POST /airtable/import` already creates the Forced Entry, ANSI Z97.1 and Impact
tests**, one per executable Protocol Section, in `importer.bind`. They come back
on the import response — `manual_tests[]` and `missile_impact_tests[]` on
`ProjectSchema` — already carrying `airtable_protocol_id`,
`airtable_section_id`, `airtable_section_name`, `required_option` and, for
Impact, a frozen `impact_family`.

So for Airtable-bound work the UI **selects an existing test and starts an
attempt on it**. Calling `POST …/manual-tests/` or `POST …/impact-tests/`
afterwards creates a **second, parallel test** with its own `labos_test_id`,
which publishes as an unrelated group of rows in Airtable. Nothing refuses it —
it is a legal call — so this is a rule the screen has to keep.

| | Airtable-bound job | LabOS-only job |
|---|---|---|
| Where the test comes from | **the import.** Select it | **`POST …/manual-tests/` or `POST …/impact-tests/`.** Create it |
| `type` (FE / ANSI) | set from `Requirement Code` | operator chooses |
| `impact_family` | frozen by the importer from `IMPACT_SMI` / `IMPACT_LMI` | operator chooses, write-once |
| `required_option` | copied from `Required Option`, may be blank | operator types, may be blank |

Reload the project with `GET /devices/{id}/projects/` to see them; there is no
`GET /projects/{id}`.

### Then, per field

| Field | Airtable-linked job | LabOS-only job |
|---|---|---|
| `project_id` | from the picker | from the picker |
| `type` | from `Requirement Code` (`FORCED_ENTRY` → Forced Entry, `ANSI_IMPACT` → ANSI Z97.1) | operator chooses |
| `required_option` | `Required Option`, copied verbatim — **may be blank, and blank is legal** | operator types, or leaves blank |
| `missile`, `missile_weight` | ~~`Missile Type`, `Missile Weight`~~ **withdrawn 2026-09-10** — never pre-filled again | operator types, or leaves blank |
| `impact_family` | **not yours to send** — resolved from the bound section's requirement code | operator chooses `SMI` or `LMI` |
| `impact_level` | operator chooses `D` or `E`, LMI only | same |
| `target_velocity` | operator enters, ft/s | same |
| `operator_name` | remembered per device | remembered per device |
| `airtable_*` | from the mirror | omit |

**Both columns must work.** A LabOS-only job has no Airtable identity and is
fully testable — that is the normal mode, not a degraded one.

### Impact classification — implemented, verified in Testing, not deployed to Production

Status, stated precisely, because "not deployed" on its own has misled before:

| | |
|---|---|
| **Implemented in the current branch** | yes — `feature/labos-airtable`, and the routes below behave this way now |
| **Verified against the live Testing base** | yes — TA7 real-wire probe, 64/64, 2026-09-11 |
| **Deployed to the `management` node** | **no** |
| **Applied to the Production Airtable base** | **no.** Production is untouched at 142 fields |

`Impact Classification` `fldMY7DiiuP9kbQbL` and `Target Impact Velocity`
`fldhywP9YpsmoWWT1` **do exist — in the Testing base**, applied 2026-09-11. They
are deliberately absent from Production until the schema request is accepted, and
`preflight` asserts that absence on every run. So build against this; just do not
expect it from the *production* node until the deploy lands.

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
- **⚠️ A photograph count of zero is no longer normal on an impact.** That was
  true while one attempt held several impacts. One attempt is now one impact, so
  an impact with no photograph is an attempt that can only be aborted, never
  completed. Forced Entry and ANSI are the opposite: they need **no** photograph
  to finish, only a `result`.

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

**⚠️ Two dedicated Airtable scalars now exist — this paragraph said the opposite
until 2026-09-11.** DG8 was closed on the reasoning that `Test Type` and
`Test Result` are both single-selects and filter natively. **The product owner
reopened it on 2026-09-10** — question 2 came back **NO**, *"they are different
under different standards"* — so TA6 added:

| Field | Table | Populated for | Testing field ID |
|---|---|---|---|
| `Forced Entry Result` | `LabOS Raw Data Table` | `Test Type = Forced Entry` only | `fldAHuPzZHZEj0Cjt` |
| `ANSI Result` | `LabOS Raw Data Table` | `Test Type = ANSI Z97.1` only | `fldmCKJV95N9uL7xt` |

**Alongside `Test Result`, not instead of it.** `Test Result` still carries the
verdict for all five types. The dedicated field is *the same value on its own
axis* — same four options, same `Pass`→`Passed` wire translation, same
`Pending`-at-terminal lifecycle — projected by type in `mapping.py`. On the other
four types the key is **omitted entirely**, never sent blank.

**Nothing changes for the UI.** There is one verdict control; the backend
projects it. Do not offer a second result field, and do not ask the operator for
a per-standard result — it is derived from the reviewer's `test_result`, not
entered.

`Failure Notes` was **not** added: he named two fields, and a third on inference
is not his decision. Applied to the Testing base 2026-09-11 (162 → 164);
Production untouched at 142.

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
| `GET /sync/status` | `status` is one of **`Synced` · `Pending` · `Sync Failed` · `Retry Required`** — the Airtable team's own four words. Also `led`, `attachment_backlog`, `attachment_parked`, `artifacts_needing_reconciliation`, `failed_publications`, `queue_depth`, `parked`, `blocked_attempts`, `revision`, `last_push_ok_at` / `last_pull_ok_at` / `last_push_error` / `last_pull_error`, and — for liveness — **`worker_alive` plus `heartbeat_age_seconds`**. ⚠️ There is no `worker_heartbeat_at`; this document named one until 2026-09-11. ⚠️ **A dead worker reads as `Sync Failed` with an empty queue**, which is correct and is not a failed result: check `worker_alive` before showing the headline as a data problem |
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
