# Manual test capture — internal LabOS API

**Audience: the LabOS UI developer.** This is an internal LabOS document. It is
not the Airtable interface contract and nothing here crosses that boundary —
these routes never call Airtable, and Airtable never calls them.

Routes covering **Impact**, **Forced Entry** and **ANSI Z97.1**: the three
tests an operator enters by hand. Static Load and Cycles are unchanged and are
not described here.

Machine-readable: `src/management_service/openapi.json`, or `/docs` on a running
instance.

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
| `PUT /test-results/{id}` | amend the attempt's `note` / `image_path` only. **Pre-existing route** — it does not touch the verdict or any measurement, and it is not a way to edit a reviewed attempt |

`test_result` must be `Pass`, `Fail` or `Inconclusive` — **not** the Airtable
spellings `Passed`/`Failed`, which the sync layer translates later.

## 6. What the UI needs to supply, and where it comes from

| Field | Airtable-linked job | LabOS-only job |
|---|---|---|
| `project_id` | from the picker | from the picker |
| `type` | from `Requirement Code` (`FORCED_ENTRY` → Forced Entry, `ANSI_IMPACT` → ANSI Z97.1) | operator chooses |
| `required_option` | `Required Option` | operator types |
| `missile`, `missile_weight` | `Missile Type`, `Missile Weight` | operator types, or leaves blank |
| `operator_name` | remembered per device | remembered per device |
| `airtable_*` | from the mirror | omit |

**Both columns must work.** A LabOS-only job has no Airtable identity and is
fully testable — that is the normal mode, not a degraded one.

---

## 7. Things that will bite

- **`test_result` is `Pending` after `finish`.** A screen showing "Completed"
  next to "Pending" is correct, not a bug. It means: tested, awaiting review.
- **`retest_required` is `null` until a verdict.** Render it as "not yet
  reviewed", never as an unchecked box.
- **Photos are `POST`-only.** There is no delete: evidence is append-only until
  the verdict, then frozen.
- **Errors are meant to be shown.** The `detail` strings say what to do next;
  surface them rather than replacing them with "something went wrong".
- **A test with no attempts is normal.** It means created, not yet started.
- **`GET /projects/{id}` still works and now includes these.** `ProjectSchema`
  embeds `missile_impact_tests`; its `missile` and shot `area`/`velocity` are
  now nullable, so handle `null`.
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

## 8. Why Forced Entry and ANSI carry so little — and whose decision that was

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

## 9. Running it locally

```bash
docker compose -f src/management_service/tests/postgres_harness/docker-compose.yaml up -d
```
Postgres on `127.0.0.1:15432`, disposable, `down -v` removes it. Point
`DATABASE_URL` at it and run `uvicorn app.main:app --reload` from
`src/management_service/`. Interactive docs at `/docs`.

Behaviour is pinned by `tests/test_manual_tests.py` — read it as executable
examples of every rule above.
