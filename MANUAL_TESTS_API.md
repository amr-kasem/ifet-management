# Manual test capture — internal LabOS API

**Audience: the LabOS UI developer.** This is an internal LabOS document. It is
not the Airtable interface contract and nothing here crosses that boundary —
these routes never call Airtable, and Airtable never calls them.

Eleven routes covering **Impact**, **Forced Entry** and **ANSI Z97.1**: the three
tests an operator enters by hand. Static Load and Cycles are unchanged and are
not described here.

Machine-readable: `src/management_service/openapi.json`, or `/docs` on a running
instance.

---

## 1. What these tests are

| Test | What happens | What LabOS records |
|---|---|---|
| **Impact** | A windborne-debris missile (ASTM E1886/E1996) is fired at the specimen, N times | How many impacts, whether each passed, and photographs |
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

## 2. The shape of every attempt

All three follow the same three phases. This is the part to get right; the
individual routes are mechanical once it is clear.

```
  create  ──────────►  finish  ──────────►  verdict
  operator starts      operator records     REVIEWER decides
  status: In Progress  status: Completed    test_result: Pass/Fail/
  test_result: Pending  or Aborted                       Inconclusive
                       test_result: STILL Pending
```

**The two words that look the same and are not:**

| Field | Whose | When | Meaning |
|---|---|---|---|
| `result` (boolean) | the **operator** | `finish` | "the specimen resisted / it did not" |
| `test_result` (string) | the **reviewer** | `verdict` | `Pending` → `Pass` \| `Fail` \| `Inconclusive` |

They are deliberately separate columns. Collapsing them would let an operator
certify their own work, and the review is a distinct act with a name and a
timestamp on it. **`operator_name` and `verdict_by` are stored separately even
when the same person does both.**

Neither is authenticated — LabOS has no user table. Both are declared names the
UI supplies. The UI should remember the last operator per device so it is not
retyped, but it cannot *prove* identity and should not imply that it does.

**Rules the API enforces, so the UI does not have to guess:**

1. A verdict before `finish` → **400**. Evidence freezes on termination, then it
   is reviewed.
2. A second verdict → **409**. The first stands; a change is a *new attempt*
   referring to the old one, never an edit.
3. A photo after the verdict → **409**. Adding substantive evidence to a
   reviewed attempt requires a correction.
4. `finish` on an already-finished attempt → **400**.
5. `retest_required` has **no default**. An unchecked box is not a decision.
6. Completion is explicit, or it is an abort with a reason. **Missing data is
   never a pass.**

`attempt_number` and `labos_attempt_id` are allocated **server-side**. Do not
send them. Every attempt is retained — running the same test again creates
attempt 2, it does not overwrite attempt 1.

---

## 3. Forced Entry and ANSI Z97.1 — 5 routes

Both live in one table, distinguished by `type`.

### `POST /projects/{project_id}/manual-tests/`
Start an attempt.

```json
{ "type": "Forced Entry",
  "required_option": "ASTM F588 Grade 40",
  "operator_name": "technician-1",
  "airtable_section_id": "recSec…" }
```
`type` is `"Forced Entry"` or `"ANSI Z97.1"` — anything else is **422**.
`required_option` is the grade or class; pre-filled from Airtable when the job
came from there, typed otherwise. The three `airtable_*` fields are optional and
absent for a LabOS-only job. → `200` with the full attempt.

### `GET /projects/{project_id}/manual-tests/?type=ANSI%20Z97.1`
List attempts, newest last, each with its `photos`. `type` filter optional.

### `PUT /manual-tests/{test_id}/finish`
```json
{ "result": true, "note": "no entry achieved", "testing_continued": "Stopped" }
```
or to abandon: `{ "abort_reason": "Equipment Fault" }`

`result` is **required** unless aborting → **400** otherwise. `test_result`
stays `Pending`.

### `PUT /manual-tests/{test_id}/verdict`
```json
{ "test_result": "Pass", "verdict_by": "reviewer-1",
  "retest_required": false, "rationale": "optional; appended to notes" }
```
`test_result` must be `Pass`, `Fail` or `Inconclusive` — **not** the Airtable
spellings `Passed`/`Failed`, which the sync layer translates later.

### `POST /manual-tests/{test_id}/photos`
`multipart/form-data`: `file` (required), `note` (optional). Optional for these
two test types.

---

## 4. Impact — 6 routes

Same three phases, plus shots.

### `POST /projects/{project_id}/impact-tests/`
```json
{ "missile": "Large Missile D", "missile_weight": 9.0,
  "operator_name": "technician-1" }
```
**Everything is optional.** The protocol fixes the missile, so requiring it per
attempt was retyping. `{}` is a valid body.

### `GET /projects/{project_id}/impact-tests/`
Each attempt carries its `shots` and `photos`.

### `POST /impact-tests/{test_id}/shots`
```json
{ "result": true }
```
`result` is the **only** required field — this is the "how many and whether each
passed" the product owner described. `area`, `velocity` and `note` are optional.
Omitting `result` is **422**: a shot without an outcome is not a shot.

Post one per impact. Refused once the attempt is finished.

### `PUT /impact-tests/{test_id}/finish`
Same body as the manual finish. Two extra preconditions when completing (not
when aborting):

- **at least one shot** → 400 otherwise
- **at least one photograph** → 400 otherwise

Impact is the only type that requires a photo, and it is required *here*, at
finish — not when publishing to Airtable. Attachments upload on their own
channel and may settle later, so making the upload a precondition for publishing
would let a queued file block a measured result.

### `PUT /impact-tests/{test_id}/verdict`
Identical to the manual verdict.

### `POST /impact-tests/{test_id}/photos`
Identical to the manual photo route. Upload **before** `finish`, since finish
requires one.

---

## 5. What the UI needs to supply, and where it comes from

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

## 6. Things that will bite

- **`test_result` is `Pending` after `finish`.** A screen showing "Completed"
  next to "Pending" is correct, not a bug. It means: tested, awaiting review.
- **`retest_required` is `null` until a verdict.** Render it as "not yet
  reviewed", never as an unchecked box.
- **Photos are `POST`-only.** There is no delete: evidence is append-only until
  the verdict, then frozen.
- **Errors are meant to be shown.** The `detail` strings say what to do next;
  surface them rather than replacing them with "something went wrong".
- **`GET /projects/{id}` still works and now includes these.** `ProjectSchema`
  embeds `missile_impact_tests`; its `missile` and shot `area`/`velocity` are
  now nullable, so handle `null`.

---

## 7. Running it locally

```bash
docker compose -f src/management_service/tests/postgres_harness/docker-compose.yaml up -d
```
Postgres on `127.0.0.1:15432`, disposable, `down -v` removes it. Point
`DATABASE_URL` at it and run `uvicorn app.main:app --reload` from
`src/management_service/`. Interactive docs at `/docs`.

Behaviour is pinned by `tests/test_manual_tests.py` — read it as executable
examples of every rule above.
