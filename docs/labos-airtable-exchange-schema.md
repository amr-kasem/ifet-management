# LabOS ↔ Airtable — Exchange Schema (derived from implementation)

## Inventory (what was read / what could not be found)

1. Read in full: `ifet-management/src/management_service/app/data/models.py` (SQLAlchemy ORM, 105 lines) — the only real schema definition in the codebase.
2. Read in full: `ifet-management/src/management_service/app/data/schema.py` (Pydantic I/O schemas, 123 lines) and `app/data/utils.py`.
3. Read in full: `ifet-management/src/management_service/app/main.py` (556 lines) — every FastAPI route, to determine which of the ORM's capabilities are actually reachable over HTTP.
4. Read `ifet-management/src/management_service/app/utils/populate_db.py` and both `app/domain/*_pressure_calculator.py` files.
5. Read `ifet-firmware/src/state_machine/api/api.py` (the firmware's HTTP client into the management service) and `ifet-firmware/src/state_machine/state_machine.py`, `sensor_ownership.py`, and the MQTT-using node scripts under `src/serial_service`, `src/valves_node`, `src/fake_*`.
6. Extracted literal strings from the built (minified) React bundle `ifet-management/src/ifet_ui_react/static/js/main.2bcaf809.js` — no frontend source is checked into either repo, so this bundle is the only evidence of what the UI actually calls and labels.
7. Searched (`git grep`, plain grep, `git log --all`) both repositories, all branches (`main`/`dev`, plus the current feature branches), for `airtable`, `contract`, `integrations`, `sync`, `queue`, `merge_key`, `external_id`, `record_id`, `mqtt` (management side), `report`, `photo`, `upload`, `csv`, `export`, `operator`, `section`, `protocol`, `mock`, `sick`.
8. **`ifet-firmware/docs/labos-airtable-write-contract-v0.3.md` does NOT exist** — not on the current branch, not on `dev`, not anywhere in git history of either repo. `ifet-firmware/docs/` contains only `README.md` (Modbus/pin configuration notes).
9. **`app/integrations/airtable/` does NOT exist** in `ifet-management` (or anywhere) — there is no Airtable client, no `listProjects`/`listMockups`/`listProtocols`/`getTestRequirements`, no write path, no sync queue, no MQTT-to-Airtable bridge, no SICK-gateway-to-Airtable bridge, no report API. The MQTT and "SICK" (`sick/sensors/...`, `sick/release/#`) traffic in `ifet-firmware` is internal real-time sensor/valve telemetry between firmware nodes and the live UI — it never touches Airtable and has no persistence.
10. Consequently, this document treats **contract v0.3 as unavailable** rather than inferring its content. §7 (Divergences) compares the *code* against *itself* (DB model vs. live API vs. firmware client vs. UI calls) since there is no external contract to diverge from. §2–§6 describe only what exists; §3/§4 (the READ/WRITE Airtable schemas the task template calls for) are marked NOT IMPLEMENTED throughout, because zero lines of Airtable integration code exist in either repository at HEAD of `claude/labos-airtable-schema-doc-bwokgn`, `dev` (firmware) or `main` (management).

---

## §1 Scope

**What would need to cross the boundary, if the integration existed:** project/test identity (device, project, and the five test-type records below), the parameters LabOS needs to run a test (design pressures, pressure/cycle schedules), and the results LabOS produces (deflections, shots, pass/fail, notes).

**What deliberately does not cross, per the actual code:** there is no billing, pricing, invoicing, or scheduling concept anywhere in `ifet-management` or `ifet-firmware` — no model, no field, no endpoint referencing money, invoices, or calendar scheduling. This is consistent with the task's exclusion list and requires no further action; it is absent by omission, not by an explicit guard.

**What crosses today:** nothing. There is no Airtable client, credential, base ID, or table ID anywhere in either repository (§ Inventory, items 8–9). The "boundary" described in §3–§6 below is therefore hypothetical, built from the local schema that *would* need to be exposed if a sync were implemented.

---

## §2 Identifiers

| Key | Owner today | Format | Minted where | Status |
|---|---|---|---|---|
| `Device.id` | LabOS (Postgres) | auto-increment `INTEGER` | DB default, via `Device(name=...)` insert, `models.py:9`, used at `main.py:47-52` | Implemented — but purely local; nothing maps it to an external system |
| `Project.id` | LabOS (Postgres) | auto-increment `INTEGER` | `models.py:16`, inserted `main.py:76-88` | Implemented, local only |
| `StaticTest.id` / `.index` | LabOS (Postgres) | `id`: auto-increment; `index`: application-assigned `0..5` (`main.py:108,115`, `StaticTestPressureCalculator.get_static_test_data`, `static_test_pressure_calculator.py:11-13`) | `main.py:91-119` (creation loop) | Implemented. `index` (not `id`) is the key the code itself uses to re-identify a static test slot across updates — see merge key below |
| `CyclicTest.id` / `.index` | LabOS (Postgres) | same pattern, `index` `0..7` | `main.py:90-105`, `cyclic_test_pressure_calculator.py` | Implemented, same caveat |
| `Deflection.id` | LabOS (Postgres) | auto-increment | `main.py:365-375` (`create_deflection`) | Implemented, local only |
| `InfiltrationTest.id`, `MissileImpactTest.id`, `Shot.id` | LabOS (Postgres) | auto-increment | declared in `models.py:53-86`; **no live route creates them** (§4, §5) | Model exists; no code path mints a real row today |
| Airtable record ID (any table) | N/A | N/A | N/A | **NOT IMPLEMENTED** — no field in `models.py` stores an Airtable record ID, no schema in `schema.py` exposes one, no code reads or writes one |
| Idempotency / merge key for a project or test between LabOS and Airtable | N/A | N/A | N/A | **NOT IMPLEMENTED**. The only "merge key" that exists anywhere in the code is *internal*: `(project_id, index)` is what `update_static_tests`/`update_cyclic_tests` use to decide whether to update an existing row or insert a new one (`main.py:142-156`, `194-210`, `224-240`). There is no equivalent for matching a LabOS row to an Airtable row — Airtable integration would have to invent this from scratch. |

Note on the firmware's own identifiers: `device_id` in `ifet-firmware/deployment/config/*.json` (e.g. `"device_id": "device2"`) is a **string** configured per physical rig, distinct from the management service's integer `Device.id`. Nothing in the code maps one to the other; the firmware `Api` client (`ifet-firmware/src/state_machine/api/api.py`) is passed a `project_id`/`device_id` as opaque strings by its caller, and that caller was not found in either repo (the state machine's own use of the `Api` class was not located within the code read — see §7).

---

## §3 READ schema (Airtable → LabOS)

**NOT IMPLEMENTED.** There is no code in either repository that reads from Airtable. No `listProjects`, `listMockups`, `listProtocols`, `getTestRequirements`, or any HTTP call to `api.airtable.com` exists. Consequently there is no "what LabOS does if a field is missing" behavior to document — LabOS today has no code path that expects Airtable-sourced data at all. All project/test setup in LabOS is created directly through the FastAPI UI-facing endpoints (`POST /devices/{device_id}/projects/`, `main.py:70-123`), with the two design-pressure values as the only externally supplied inputs; everything else (the 6 static-test rows and 8 cyclic-test rows, their pressures, durations, and cycle counts) is computed locally by `StaticTestPressureCalculator`/`CyclicTestPressureCalculator`.

If a "mock-up"/"protocol"/"section"/"operator" concept is meant to originate in Airtable, note that **none of these four concepts exist anywhere in the LabOS data model** (`models.py`) or its API (`main.py`) — see § Inventory item 7. Any such fields the Airtable base already has are simply not consumed.

---

## §4 WRITE schema (LabOS → Airtable)

**NOT IMPLEMENTED.** There is no result envelope, no serializer, no outbound HTTP call to Airtable anywhere in the code. The table below instead documents the *local* fields that exist and would be the only candidate source data for such an envelope, since the task requires every write-schema claim to cite real LabOS data or say NOT IMPLEMENTED.

| Local field (file:line) | Type (as declared) | Units | Reachable via API today? | Per-test-type applicability | Null-vs-absent |
|---|---|---|---|---|---|
| `Project.name` (`models.py:17`) | `String`, `NOT NULL` | — | Yes — set/read on every project route (`main.py:76-88`, `128-136`) | all | Cannot be null (DB constraint); Pydantic `ProjectCreateSchema.name: str` requires it |
| `Project.inward_design_pressure` / `.outward_design_pressure` (`models.py:19-20`) | `Float`, `NOT NULL` | Unlabeled in code (no unit column/comment/UI string found — see §7) | Yes | Static, Cyclic (feeds `StaticTestPressureCalculator`/`CyclicTestPressureCalculator`) | Cannot be null |
| `StaticTest.type` (`models.py:35`) | `String`, `NOT NULL`, value `"inward"`/`"outward"` (`main.py:114`) | — | Yes, via bulk `PUT /projects/{id}/static_tests` (`main.py:216-244`) and single `PUT /static-tests/{id}/` (`main.py:338-351`) | Static only | Required, always set by the creation loop |
| `StaticTest.pressure_factor` (`models.py:32`) | `String`, `NOT NULL`, value hardcoded `"Structural Pressure"` (`main.py:111`) | — | Yes (same routes) | Static only | Required; currently only one literal value is ever produced |
| `StaticTest.pressure` (`models.py:33`) | `Float`, `NOT NULL` | Unlabeled (see §7) | Yes | Static only | Required |
| `StaticTest.duration` (`models.py:34`) | `Integer`, `NOT NULL`, hardcoded `30` by `StaticTestPressureCalculator.get_static_test_data` (`static_test_pressure_calculator.py:13`) | Assumed seconds (unconfirmed — see §7) | Yes | Static only | Required |
| `StaticTest.index` (`models.py:31`) | `Integer`, `NOT NULL`, `0..5` | — | Yes | Static only | Required; doubles as the merge key (§2) |
| `StaticTest.finished` (`models.py:29`) | `Boolean`, `NOT NULL` | — | Settable only via `PUT /projects/{project_id}/static_tests/{static_test_id}/finish` (`main.py:266-284`), one-way (no "unfinish" route) | Static only | Required; defaults to `False` at creation (`main.py:117`) |
| `Deflection.deflection_gauge` (`models.py:45`) | `Integer`, `NOT NULL` | Gauge number/id, not a physical unit | Yes, via `POST /static-tests/{id}/deflections/` (`main.py:365-375`) — **one deflection per call**, not a batch | Static only | Required |
| `Deflection.max_deflection` / `.permanent_deflection` / `.recovery` (`models.py:46-48`) | `Float`, `NOT NULL` each | Unlabeled (frontend bundle labels the *columns* "Max Deflection", "Permanent Deflection", "Recovery" — no unit string found, see §7) | Yes (same route) | Static only | Required |
| `CyclicTest.type` (`models.py:94`) | `String`, `NOT NULL`, `"inward"`/`"outward"` | — | Yes, bulk `PUT /projects/{id}/cyclic_tests` (`main.py:187-214`) and single `PUT /cyclic-tests/{id}/` (`main.py:455-467`) | Cyclic only | Required |
| `CyclicTest.cycles` (`models.py:95`) | `Integer`, `NOT NULL`, from `CyclicTestPressureCalculator.CYCLE_COUNT` (`cyclic_test_pressure_calculator.py:4`) | count | Yes | Cyclic only | Required |
| `CyclicTest.low_pressure` / `.high_pressure` (`models.py:96-97`) | `Float`, `NOT NULL` | Unlabeled | Yes | Cyclic only | Required |
| `CyclicTest.index` (`models.py:93`) | `Integer`, `NOT NULL`, `0..7` | — | Yes | Cyclic only | Required; merge key |
| `CyclicTest.finished` (`models.py:91`) | `Boolean`, `NOT NULL` | — | Set via `PUT /projects/{id}/cyclic_tests/{id}/finish` (`main.py:246-264`), one-way | Cyclic only | Required |
| `CyclicTest.deflection` (`models.py:98`) | `Float`, **nullable** | Unlabeled | Only through the generic `PUT /cyclic-tests/{id}/` which requires `CyclicTestUpdateSchema` (`schema.py:79-85`) — that schema has **no `deflection`, `permanent_set`, `result`, or `note` field**, so these four columns are **write-only from the DB's point of view but unreachable from any HTTP route** | Cyclic only | Can be absent/`NULL`; no code ever sets it |
| `CyclicTest.permanent_set` (`models.py:99`) | `Float`, **nullable** | Unlabeled | Same as above — unreachable | Cyclic only | Can be `NULL`; never set by any route |
| `CyclicTest.result` (`models.py:100`) | `Boolean`, **nullable** | — | Unreachable (see above) | Cyclic only | Can be `NULL`; never set |
| `CyclicTest.note` (`models.py:101`) | `String`, **nullable** | — | Unreachable (see above) | Cyclic only | Can be `NULL`; never set |
| `InfiltrationTest.*` (`models.py:53-62`) | see §5 | — | **No live create or update route** — both are commented out (`main.py:402-426`) | Forced-Entry / infiltration-shaped test | Model has data (from `populate_db.py` fixtures only); no production code path writes it |
| `MissileImpactTest.*`, `Shot.*` (`models.py:64-86`) | see §5 | — | **No live create or update route** — all commented out (`main.py:482-554`) | Impact | Same as above |

Because no Airtable envelope exists, there is no code-defined "canonical snake_case field name" to map to; the column names above (already snake_case, from SQLAlchemy) are the closest available vocabulary, and are used as-is in §5.

---

## §5 Per-test-type matrix

The task specifies five test types (Static Load, Cycles, Impact, Forced Entry, ANSI Z97.1). The code only implements **four** test-type tables, and only **two** of those have a working write path. "Forced Entry" and "ANSI Z97.1" are not distinguished anywhere in code — both would have to be values of `InfiltrationTest.type` (a free `String`), but no such literal values were found (only fixture placeholders `"Type A"/"Type B"/"Type C"` in `populate_db.py:61`, which is itself dead code — see §7). "ANSI Z97.1" (a glazing/impact-safety standard) has no representation at all; it is not `MissileImpactTest` and not `InfiltrationTest` in this code.

Legend: **R** = required (NOT NULL, and a live route sets it) · **O** = optional (nullable, or nullable-with-default) · **U** = column exists but is unreachable by any HTTP route (write-only in theory, dead in practice) · **N/A** = does not apply to this test type · **✗** = test type/record cannot be created at all today

| Field | Static Load | Cycles | Impact | Forced Entry | ANSI Z97.1 |
|---|---|---|---|---|---|
| `type` (inward/outward) | R | R | N/A | R (free string, model) | N/A |
| `pressure_factor` | R | N/A | N/A | N/A | N/A |
| `pressure` | R | N/A | N/A | R (model; `Optional[float]` in create schema, `NOT NULL` in ORM — contradiction, §7) | N/A |
| `low_pressure` / `high_pressure` | N/A | R | N/A | N/A | N/A |
| `duration` | R | N/A | N/A | O (nullable, model) | N/A |
| `cycles` | N/A | R | N/A | N/A | N/A |
| `index` | R | R | N/A | N/A | N/A |
| `finished` | R | R | N/A | N/A | N/A |
| `deflection_gauge`/`max_deflection`/`permanent_deflection`/`recovery` (Deflection rows) | R | N/A | N/A | N/A | N/A |
| `deflection` (single, on CyclicTest) | N/A | U | N/A | N/A | N/A |
| `permanent_set` | N/A | U | N/A | N/A | N/A |
| `result` | N/A | U | ✗ (model has `Shot.result`, R by column, but the whole `MissileImpactTest`/`Shot` record can never be created) | N/A | N/A |
| `note` | N/A | U | ✗ (`Shot.note`, same caveat) | N/A | N/A |
| `missile` / `missile_weight` | N/A | N/A | ✗ | N/A | N/A |
| `area` / `velocity` (Shot) | N/A | N/A | ✗ | N/A | N/A |
| `leakage` | N/A | N/A | N/A | O (nullable, model; never set — no write route) | N/A |
| ANSI Z97.1-specific fields | N/A | N/A | N/A | N/A | **NOT IMPLEMENTED — no such record type exists** |

---

## §6 Cardinality & structure

- **Project → StaticTest / CyclicTest**: one-to-many, plain foreign key (`static_test.project_id`, `cyclic_test.project_id`, `models.py:36,103`), not a JSON blob. Rows are pre-created in fixed batches at project-creation time (6 static, 8 cyclic — `main.py:91-119`), each with a stable `index`. On the Airtable side this shape maps naturally to two separate linked tables (one row per static/cyclic test slot, linked to the project), **not** a single multi-select or JSON field — the fixed `index` values (0–5, 0–7) would need to survive the round trip if Airtable is ever the source of truth for re-creating them.
- **StaticTest → Deflection**: one-to-many (`deflection.static_test_id`, `models.py:50`), **repeating rows**, created one at a time via `POST /static-tests/{id}/deflections/` (`main.py:365-375`) — there is no batch-insert endpoint despite the firmware's `Api.finish_static_test`/`Api.finish_cyclic_test` (`ifet-firmware/.../api.py:28-71`) building a list of deflections and POSTing them as a single JSON array to endpoints (`/projects/{id}/static_tests/{index}/trials`, `/projects/{id}/cyclic-tests/{index}/trials`) that **do not exist in `main.py` at all** (see §7). Each `Deflection` row is a flat record (gauge id + 3 floats) — no photo, no attachment field anywhere on it.
- **MissileImpactTest → Shot**: modeled identically (one-to-many, `shot.missile_impact_test_id`, `models.py:85`), but the whole subtree is unreachable in the live API (§4, §5) — there is no runtime cardinality to report because no code path ever populates it beyond the throwaway `populate_db.py` fixture data (which itself references a stale import path `app.models`/`app.utils` that no longer exists under `app.data.*`, confirming this script is not run against current code).
- **Photos**: **NOT IMPLEMENTED.** No column, no attachment array, no file-upload endpoint, no static-file serving path exists in `ifet-management`. If Airtable's base has an attachments field for photos, LabOS produces nothing to fill it.
- **Operators / Sections / Protocols / Mock-ups**: **NOT IMPLEMENTED.** These four concepts named in the task's method section do not appear as models, columns, or endpoints anywhere in either repository (§ Inventory item 7). Any Airtable base field of these kinds has no local counterpart to populate it from, nor any local field that would need one of these as a foreign key.

---

## §7 Divergences

There is no `labos-airtable-write-contract-v0.3.md` to diverge from (§ Inventory item 8), so this section instead reports the internal contradictions found by reading the DB model, the live API, the firmware's own HTTP client, and the built frontend side by side — these are the divergences that would have to be resolved *before* any Airtable contract could be written against this code honestly.

| # | Divergence | Evidence | Which side is "correct" and why |
|---|---|---|---|
| 1 | Firmware's `Api` client posts to `/projects/{id}/static_tests/{index}/trials` and `/projects/{id}/cyclic-tests/{index}/trials`; no such routes exist in `main.py`. | `ifet-firmware/src/state_machine/api/api.py:31,57` vs. full route list in `ifet-management/src/management_service/app/main.py` | The **management service's actual routes** are ground truth for what LabOS can persist today (`POST /static-tests/{id}/deflections/`, singular). The firmware client is either stale or talks to an unmerged/undeployed version of the API — either way, deflection results from a live test run currently cannot reach the DB through this client as written. |
| 2 | Firmware `Api` client also calls `PUT /projects/{id}/cyclic_tests/{id}/start`, `PUT .../reset`, `PUT .../update_status`, `GET /projects/{id}/next-cyclic-test`, `GET /devices/{id}` — none exist in `main.py`. | `api.py:16-20,48-88` vs. `main.py` route table | Same as #1 — the deployed API is the source of truth; the firmware client's expectations are ahead of (or diverged from) it. |
| 3 | Frontend bundle calls `GET/POST http://localhost:8000/static/inward` and `.../static/outward` for reading/writing deflection tables; no `/static/*` route exists in `main.py`. | extracted string in `ifet-management/src/ifet_ui_react/static/js/main.2bcaf809.js` (search `localhost:8000/static/`) vs. `main.py` route table | The backend routes are correct; this UI code path is dead/broken against the current backend. Deflection entry through the shipped UI build does not work. |
| 4 | `InfiltrationTestCreateSchema.pressure` is `Optional[float]` (`schema.py:39`) but `InfiltrationTest.pressure` is `Column(Float, nullable=False)` (`models.py:58`). | `schema.py:37-39` vs. `models.py:56-62` | The ORM (`nullable=False`) should be treated as correct intent, since it is moot regardless — there is no live route that constructs an `InfiltrationTest` from the create schema, so the contradiction is currently unreachable, but it must be fixed before this table is exposed to any client, Airtable included. |
| 5 | `CyclicTestSchema`/`CyclicTestUpdateSchema` (used by every reachable cyclic-test write route) omit `deflection`, `permanent_set`, `result`, `note` — even though the ORM defines them as real, nullable columns meant to hold a cyclic test's outcome. | `schema.py:79-97` vs. `models.py:98-101`, and no other route sets them | This looks like a gap, not a design choice: `StaticTest` has an equivalent outcome path (`Deflection` rows), but `CyclicTest` has no way to record its result at all through the API. Treat the ORM column set as the intended shape and the schema as incomplete. |
| 6 | Units are never declared anywhere in code for `pressure`, `low_pressure`, `high_pressure`, `deflection`, `max_deflection`/`permanent_deflection`/`recovery` — no column comment, no docstring, no UI unit label was found (the frontend's Modbus sensor config carries units like `"PSF"` for raw sensor input in `ifet-firmware/deployment/config/config2.json`, but that is a *sensor calibration* unit, not a confirmed unit for the *stored test-result* fields). | `models.py` (no comments), `deployment/config/config2.json` (`"unit": "PSF"` on raw pressure sensors only) | Cannot be resolved from code. This must be confirmed with whoever owns the physical test procedure — do not assume PSF for the stored/report-level pressure fields just because the raw sensor input happens to use it. |
| 7 | `populate_db.py` imports `from app.models import ...` / `from app.utils import run_migrations`, but the real modules are `app.data.models` / `app.data.utils`. | `ifet-management/src/management_service/app/utils/populate_db.py:4-5` vs. actual package layout `app/data/models.py`, `app/data/utils.py` | The package layout (`app/data/*`) is correct/current; `populate_db.py` is stale and cannot run, so any assumption based on its fixture data (e.g., infiltration `type` values `"Type A"/"Type B"/"Type C"`) does not describe real production data. |

---

## §8 Asks for the Airtable team

Because no integration code exists yet, these are asks for what the Airtable base would need to expose, prioritized by what LabOS can actually supply today vs. what requires LabOS-side work first.

**P0 — required for even the currently-working slice (Project, Static Load, Cycles headers) to sync:**
1. A stable place to store a LabOS-minted identifier per Project/StaticTest/CyclicTest row (e.g. a text field holding the local integer `id`), since LabOS has no field of its own reserved for an Airtable record ID and no merge key exists today (§2). Decide *now* which system owns the canonical ID — this determines the merge key design, and nothing in the current code makes that choice for you.
2. Explicit unit fields (or units baked into field names, e.g. `pressure_psf`) for every numeric result field in §4 — `inward_design_pressure`, `outward_design_pressure`, `pressure`, `low_pressure`, `high_pressure`, `max_deflection`, `permanent_deflection`, `recovery` — since the LabOS code carries no units for any of these (§7, #6). Airtable cannot safely display or validate these without agreeing on units with LabOS's engineers first.
3. Two linked tables (or one table with a `test_kind` discriminator) mirroring `StaticTest` (index 0–5, type inward/outward, pressure_factor, pressure, duration, finished) and `CyclicTest` (index 0–7, type, cycles, low_pressure, high_pressure, finished), keyed by the fixed `index` per project — Airtable must be able to hold exactly the 6+8 fixed slots LabOS always creates (§6), not an arbitrary-length list.

**P1 — needed once the known gaps in §7 are fixed on the LabOS side:**
4. A repeating "Deflection" sub-table/linked records under Static Load results (gauge id + 3 floats), matching the shape LabOS already has (`Deflection`, §6) — but note LabOS's own write path for this is one-row-at-a-time and the shipped UI can't reach it at all (§7 #3), so don't build against this until LabOS fixes the `/static/inward`↔`/static-tests/.../deflections/` mismatch.
5. Fields for CyclicTest outcome (`deflection`, `permanent_set`, `result`, `note`) — these exist in the LabOS DB but are currently unreachable by any API route (§7 #5); Airtable should not expect to receive them until LabOS adds a write path.

**P2 — speculative, contingent on LabOS building features that don't exist at all today:**
6. Tables/fields for Impact (missile + shots) and Forced-Entry/Infiltration results — the LabOS models exist but have zero working create/update routes (§4, §5); do not build Airtable-side automation against these until LabOS ships the corresponding endpoints.
7. Whatever structure is needed for "ANSI Z97.1" results — LabOS has no representation of this test type whatsoever (§5); this requires new LabOS modeling work before any Airtable field can be mapped to it.
8. Attachment fields for photos, and any structure for Operator/Section/Protocol/Mock-up records — none of these have a LabOS counterpart today (§6); confirm with LabOS whether these are even planned before reserving base structure for them.
