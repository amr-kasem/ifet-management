import uuid

from sqlalchemy import (
    Column, Integer, String, Float, ForeignKey, Boolean, DateTime, Text, JSON,
)
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()


# ---------------------------------------------------------------- Airtable ---
# P1 / Refs 44-45. The Airtable hierarchy is Project -> Mock-Up -> Protocol ->
# Protocol Section, and every attempt LabOS writes back must carry all four
# `rec…` IDs (write contract §4.1).
#
# Per pre-closed decision #2 these are **lightweight references, not mirrored
# tables**: Airtable owns that hierarchy and LabOS never writes it, so importing
# it into Postgres would create a second copy that can drift and that nothing is
# allowed to reconcile. We store the ID we were given, plus the display name so
# a human can read a LabOS screen without a round-trip.
#
# All nullable: projects created before the integration have no Airtable
# counterpart, and a LabOS-only project stays legal forever.


class AirtableProtocolRef:
    """Mixin — the Airtable Protocol + Section a test corresponds to.

    Mixed into StaticTest and CyclicTest rather than declared on a shared base,
    because those are separate tables and the alternative (a join table) buys
    nothing: the relationship is 1:1 and read-only.
    """

    airtable_protocol_id = Column(String, nullable=True, index=True)
    airtable_section_id = Column(String, nullable=True, index=True)
    # Airtable's `Section Name` — e.g. "DP (+) (PSF)". Also what the envelope
    # sends as `Test Name`.
    airtable_section_name = Column(String, nullable=True)


class ManualAttempt:
    """Mixin — one attempt at a manually-entered test.

    Impact, Forced Entry and ANSI Z97.1 do not touch the rig: no VFD, no valves,
    no pressure setpoint, no stage trials. `state_machine` handles exactly one
    command (`start`, mode `manual` or `cyclic`) and none of these three is
    either, so the firmware is not involved and needs no release.

    These columns are precisely what `app.airtable.envelope` consumes, so an
    attempt maps to the outbound payload without a translation layer. Static and
    cyclic take the same mixin later, which is why it is a mixin and not three
    copies.

    The three review columns are nullable **together**: an unreviewed attempt has
    not answered the retest question, and `bool(None)` would publish a decision
    nobody made (write contract §6).
    """

    labos_attempt_id = Column(String, nullable=False, unique=True, index=True,
                              default=lambda: str(uuid.uuid4()))
    attempt_number = Column(Integer, nullable=False, default=1)

    # In Progress -> Completed | Aborted. Never inferred from a timeout or a
    # disconnect: completion is explicit or it is an abort with a reason.
    status = Column(String, nullable=False, default="In Progress")
    abort_reason = Column(String, nullable=True)

    # Pending until the first review. Passed/Failed/Inconclusive are the
    # reviewer's, never the operator's.
    test_result = Column(String, nullable=False, default="Pending")

    # Declared identity, not authenticated - LabOS has no user table, and the
    # contract records this honestly as `identity_assurance = declared`.
    # Operator and reviewer are stored separately even when they are the same
    # person, because the review is a distinct act.
    operator_name = Column(String, nullable=True)
    verdict_by = Column(String, nullable=True)
    verdict_at = Column(DateTime(timezone=True), nullable=True)
    retest_required = Column(Boolean, nullable=True)

    testing_start_date = Column(DateTime(timezone=True), nullable=True)
    testing_end_date = Column(DateTime(timezone=True), nullable=True)
    testing_continued = Column(String, nullable=True)
    note = Column(Text, nullable=True)


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    
    turbo_mode = Column(Boolean, nullable=False)
    turbo_slave = Column(Boolean, nullable=False)
    turbo_charger = Column(Integer, ForeignKey('devices.id'), nullable=True)
    
    projects = relationship("Project", back_populates="device", cascade="all, delete-orphan")

class ProjectParent(Base):
    __tablename__ = "project_parents"
    id = Column(Integer, primary_key=True, index=True)
    # Airtable's `IFET job number`. Display and human reconciliation only —
    # it is hand-typed, so a renumber or a typo would silently re-point a job's
    # results if it were the key. The `rec…` id below is what routes.
    name = Column(String, nullable=False, unique=True)

    # P1 covered `projects`; the job level was missed. Nullable: a LabOS-only
    # job has no Airtable counterpart and stays legal forever.
    airtable_project_id = Column(String, nullable=True, index=True)
    
    projects = relationship("Project", back_populates="parent", foreign_keys="[Project.parent_id]")
    
class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    parent_id = Column(Integer, ForeignKey('project_parents.id'), nullable=True)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=False)
    inward_design_pressure = Column(Float, nullable=False)
    outward_design_pressure = Column(Float, nullable=False)

    # P1 / Ref 44 — Airtable linkage. See the AirtableProtocolRef note above.
    airtable_project_id = Column(String, nullable=True, index=True)
    airtable_mockup_id = Column(String, nullable=True, index=True)
    airtable_mockup_name = Column(String, nullable=True)

    # Class 2 (plan §4.6) — dual-source. The operator types these, or Airtable
    # pre-fills them from `GAUGE_COUNT` / `IMPACT_LMI`+`IMPACT_SMI`. Nullable
    # because absent is a legitimate state: in the production base `# Dials` and
    # `Impact` are blank on most sections, and a missing requirement must never
    # read as zero.
    gauge_count = Column(Integer, nullable=True)
    impact_count = Column(Integer, nullable=True)

    # Class 3 — Airtable-only display metadata: Product Type, Height (Inches),
    # Width (Inches), Service line. All four already exist and are populated in
    # their base, and all four are shown to the operator so they are not
    # retyped. **Never executed from** — nothing here reaches a rig.
    airtable_meta = Column(JSON, nullable=True)

    device = relationship("Device", back_populates="projects")
    parent = relationship("ProjectParent", back_populates="projects", foreign_keys=[parent_id])
    static_tests = relationship("StaticTest", back_populates="project", cascade="all, delete-orphan")
    infiltration_tests = relationship("InfiltrationTest", back_populates="project", cascade="all, delete-orphan")
    missile_impact_tests = relationship("MissileImpactTest", back_populates="project", cascade="all, delete-orphan")
    cyclic_tests = relationship("CyclicTest", back_populates="project", cascade="all, delete-orphan")
    manual_tests = relationship("ManualTest", back_populates="project", cascade="all, delete-orphan")

class StaticTest(Base, AirtableProtocolRef):
    __tablename__ = "static_tests"
    finished = Column(Boolean, nullable=False)
    id = Column(Integer, primary_key=True, index=True)
    index = Column(Integer, nullable=False)
    pressure_factor = Column(String, nullable=False)
    pressure = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    preset = Column(Boolean, nullable=False, default=False)
    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="static_tests")
    trials = relationship("StaticTestResult", back_populates="static_test", cascade="all, delete-orphan")

class Deflection(Base):
    __tablename__ = "deflections"

    id = Column(Integer, primary_key=True, index=True)
    deflection_gauge = Column(String, nullable=False)
    max_deflection = Column(Float, nullable=False)
    permanent_deflection = Column(Float, nullable=False)
    recovery = Column(Float, nullable=False)

    test_id = Column(Integer, ForeignKey('test_results.id'))
    test = relationship("TestResult", back_populates="deflections")

class InfiltrationTest(Base):
    __tablename__ = "infiltration_tests"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False)
    pressure = Column(Float, nullable=False)
    duration = Column(Float, nullable=True)
    leakage = Column(Float, nullable=True)
    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="infiltration_tests")

class MissileImpactTest(Base, AirtableProtocolRef, ManualAttempt):
    """Windborne-debris missile impact (ASTM E1886/E1996).

    **Already in production use — 39 tests and 114 shots on the live node.** So
    this is additive against real rows, not greenfield: nothing is dropped and
    nothing is narrowed. `missile` and `missile_weight` were `NOT NULL` and are
    widened, because the business shape is "how many impacts, whether each
    passed, and a few photographs" and an operator should not be forced to type
    metadata the protocol already fixes.

    Not to be confused with ANSI Z97.1, which is also an impact test — a
    bag-drop safety-glazing test — and lives in `manual_tests`. The contract
    keeps `IMPACT_LMI`/`IMPACT_SMI` and `ANSI_IMPACT` as separate requirement
    codes for exactly this reason.
    """

    __tablename__ = "missile_impact_tests"

    id = Column(Integer, primary_key=True, index=True)
    missile = Column(String, nullable=True)
    missile_weight = Column(Float, nullable=True)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="missile_impact_tests")

    shots = relationship("Shot", back_populates="missile_impact_test", cascade="all, delete-orphan")
    photos = relationship("TestPhoto", back_populates="missile_impact_test",
                          cascade="all, delete-orphan")

class Shot(Base):
    __tablename__ = "shots"

    id = Column(Integer, primary_key=True, index=True)
    # Widened 2026-09-08: the operator records pass/fail per impact and may skip
    # the rest. The protocol normally fixes missile and velocity, so requiring
    # them per shot was retyping, not data capture.
    area = Column(Float, nullable=True)
    velocity = Column(Float, nullable=True)
    result = Column(Boolean, nullable=False)
    note = Column(String, nullable=True)

    missile_impact_test_id = Column(Integer, ForeignKey('missile_impact_tests.id'))
    missile_impact_test = relationship("MissileImpactTest", back_populates="shots")

class ManualTest(Base, AirtableProtocolRef, ManualAttempt):
    """Forced Entry and ANSI Z97.1 — one table, discriminated by `type`.

    Both are recorded as a pass/fail outcome against a named class or grade,
    with notes and optional photographs. The shape is identical, so two tables
    would be duplication rather than fidelity to the repo pattern - and
    `StaticTest` already carries a `type` column, so this is the pattern.

    ANSI Z97.1 is a bag-drop safety-glazing test and is normally performed first
    on a specimen. That ordering is **informational only** and deliberately not
    enforced: a hard block would eventually stop legitimate work, and there is
    no override in this design.
    """

    __tablename__ = "manual_tests"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)

    # "Forced Entry" | "ANSI Z97.1" - the Airtable `Test Type` spelling verbatim.
    type = Column(String, nullable=False, index=True)

    # The grade or class the section requires, e.g. "ASTM F588 Grade 40" or
    # "Class A". Free text because Airtable's `Required Option` is free text and
    # LabOS does not invent an option set the requirement side does not have.
    required_option = Column(String, nullable=True)

    # The operator's recorded outcome. Distinct from `test_result`, which is the
    # reviewer's verdict - they are different people and different moments.
    result = Column(Boolean, nullable=True)

    project = relationship("Project", back_populates="manual_tests")
    photos = relationship("TestPhoto", back_populates="manual_test",
                          cascade="all, delete-orphan")


class TestPhoto(Base):
    """Photographic evidence for a manual test.

    Two nullable foreign keys rather than a polymorphic owner column: explicit
    beats clever here, the set of owners is two and closed, and a real FK keeps
    the database able to enforce it.

    Enabled for all three manual types and required by none at the schema level.
    A *failed* Forced Entry or ANSI is exactly when someone wants a photograph,
    and evidence cannot be added once an attempt is reviewed. Impact's
    requirement is enforced on the finish path, not here - see
    `contract.REQUIRED_EVIDENCE_BY_TEST_TYPE`.

    Originals stay on the node under `uploads/`; Airtable receives a downscaled
    preview through the attachment channel.
    """

    __tablename__ = "test_photos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    path = Column(String, nullable=False)
    note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=True)

    manual_test_id = Column(Integer, ForeignKey("manual_tests.id"), nullable=True)
    missile_impact_test_id = Column(Integer, ForeignKey("missile_impact_tests.id"),
                                    nullable=True)

    manual_test = relationship("ManualTest", back_populates="photos")
    missile_impact_test = relationship("MissileImpactTest", back_populates="photos")


class CyclicTest(Base, AirtableProtocolRef):
    __tablename__ = "cyclic_tests"

    finished = Column(Boolean, nullable=False)
    id = Column(Integer, primary_key=True, index=True)
    index = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    cycles = Column(Integer, nullable=False)
    low_pressure = Column(Float, nullable=False)
    high_pressure = Column(Float, nullable=False)
    resume = Column(Boolean, nullable=False)
    current_cycle = Column(Integer, nullable=False)
    preset = Column(Boolean, nullable=False, default=False)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="cyclic_tests")
    
    trials = relationship("CyclicTestResult", back_populates="cyclic_test", cascade="all, delete-orphan")

class TestResult(Base):
    """One **attempt** at one test. Append-only (P1 / Ref 46).

    Write contract §3: an attempt moves `In Progress` -> `Completed` / `Aborted`
    and is then **final**. A re-run is a new attempt row; a *correction* is also
    a new attempt row, but one that names the attempt it supersedes. Nothing is
    ever edited in place, because this table is the evidence behind a
    certification report — see §3.1 for the worked example that motivated it.

    `trial_number` predates the integration and already means what the contract
    calls `Attempt Number`, so it is reused rather than duplicated.

    Every column added below is nullable or defaulted: production has live rows,
    and this migration must not be able to fail on them.
    """

    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True, index=True)
    trial_number = Column(Integer, nullable=False)      # = contract `Attempt Number`
    result = Column(Boolean, nullable=True)             # legacy pass/fail; see test_result
    note = Column(String, nullable=True)
    image_path = Column(String, nullable=True)
    deflections = relationship("Deflection", back_populates="test", cascade="all, delete-orphan")

    # -- identity (contract §4.1) -------------------------------------------
    # The merge key. Airtable upserts on this, so it must be stable, unique and
    # plain — a UUID, not a counter: two rigs run concurrently and "001" would
    # collide across jobs (contract §10.22).
    # `default` rather than a bare column: it fires on ANY ORM insert, so a
    # write path nobody remembered to update still produces a valid merge key
    # instead of a row that can never be synced. The DB column stays nullable
    # for one release — existing rows are backfilled by the migration, and the
    # NOT NULL is a follow-up once every insert path is confirmed covered.
    labos_attempt_id = Column(String, nullable=True, unique=True, index=True,
                              default=lambda: str(uuid.uuid4()))
    # Stable across every attempt at the same test — this is what makes
    # "attempt 2 of the same test" expressible at all.
    labos_test_id = Column(String, nullable=True, index=True)
    schema_version = Column(String, nullable=True)

    # -- correction chain (contract §3.1) -----------------------------------
    # Points at the labos_attempt_id this attempt supersedes. Absent on a
    # retest, set on a correction — which is the ONLY thing distinguishing the
    # two, and why a boolean "corrected" flag would not do: a flag cannot
    # express a chain (a3 -> a2 -> a1) or say which row is current.
    corrects_attempt_id = Column(String, nullable=True, index=True)
    correction_reason = Column(Text, nullable=True)

    # -- lifecycle (contract §4.3) ------------------------------------------
    status = Column(String, nullable=True)              # In Progress | Completed | Aborted
    test_type = Column(String, nullable=True)           # contract option set
    test_name = Column(String, nullable=True)
    test_result = Column(String, nullable=True)         # Pass | Fail | Inconclusive
    abort_reason = Column(String, nullable=True)
    retest_required = Column(Boolean, nullable=False, server_default="false", default=False)
    testing_continued = Column(String, nullable=True)   # Continued | Stopped
    # Stamped when the attempt reaches a terminal state. Its presence IS the
    # lock: the sync worker refuses to re-write an attempt that has one, so
    # immutability is a checkable fact rather than a convention (§3).
    terminal_at = Column(DateTime(timezone=True), nullable=True)

    # -- measurements (contract §4.4) ---------------------------------------
    measured_value = Column(Float, nullable=True)
    unit = Column(String, nullable=True)
    max_pressure_achieved = Column(Float, nullable=True)
    deflection_value = Column(Float, nullable=True)
    deflection_unit = Column(String, nullable=True)
    impact_result = Column(String, nullable=True)
    cycles_required = Column(Integer, nullable=True)
    cycles_completed = Column(Integer, nullable=True)

    # -- what we were testing AGAINST ---------------------------------------
    # Copied from the Airtable Protocol Section at attempt creation, not looked
    # up at report time. Two reasons. It makes the row self-contained evidence:
    # "this attempt was run against 60 PSF" survives any later edit upstream.
    # And it is the only defence available against contract §10.19 — their
    # extractor currently mis-populates requirement values, so when they fix it
    # these columns are what let us identify, retrospectively, which attempts
    # ran against a wrong requirement. Without them that question is unanswerable.
    required_value = Column(Float, nullable=True)
    required_unit = Column(String, nullable=True)

    # -- timing & people (contract §4.5) ------------------------------------
    # timezone=True throughout: contract §4.5 requires ISO 8601 **UTC**, and the
    # envelope refuses a naive datetime rather than guessing a zone. A
    # TIMESTAMP WITHOUT TIME ZONE column would silently make that guess at the
    # storage layer instead, which is the same bug one level down.
    testing_start_date = Column(DateTime(timezone=True), nullable=True)
    testing_end_date = Column(DateTime(timezone=True), nullable=True)
    operator_name = Column(String, nullable=True)

    # -- artifacts & metadata (contract §4.6) -------------------------------
    photo_links = Column(Text, nullable=True)           # newline-separated URLs
    report_link = Column(String, nullable=True)
    excel_file_link = Column(String, nullable=True)
    test_rig = Column(String, nullable=True)            # System 1 | System 2
    labos_version = Column(String, nullable=True)
    result_rationale = Column(Text, nullable=True)
    labos_created_at = Column(DateTime(timezone=True), nullable=True)
    labos_updated_at = Column(DateTime(timezone=True), nullable=True)

    # -- the JSON columns (internal-plan gap D) ------------------------------
    # `result_detail` is the payload for contract §6's extensibility valve — the
    # per-test-type detail plus every field the Airtable team has not created a
    # column for, so nothing is silently dropped while we wait (§10.15).
    result_detail = Column(JSON, nullable=True)
    # The requirement block this attempt was run against, cached verbatim from
    # Airtable. Kept raw and whole so that if their structure changes, or a
    # value is later found wrong, the original is still on the record.
    required_params = Column(JSON, nullable=True)

    # -- sync visibility -----------------------------------------------------
    # The durable queue and worker are W4 / Ref 55. These columns land now
    # because they are attempt state, not queue state, and the DoD requires the
    # sync status to be visible per attempt.
    airtable_sync_state = Column(String, nullable=True)   # Pending|Synced|Sync Failed|Retry Required
    airtable_record_id = Column(String, nullable=True)    # the rec… Airtable returned
    airtable_synced_at = Column(DateTime(timezone=True), nullable=True)
    airtable_sync_error = Column(Text, nullable=True)

    # ---------------------------------------------------------------- helpers
    TERMINAL_STATUSES = ("Completed", "Aborted")

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL_STATUSES

    @property
    def is_correction(self):
        """A correction supersedes a specific earlier attempt; a retest does not."""
        return bool(self.corrects_attempt_id)

class CyclicTestResult(TestResult):
    __tablename__ = "cyclic_test_results"
    id = Column(Integer, ForeignKey('test_results.id'), primary_key=True, index=True)
    cyclic_test_id = Column(Integer, ForeignKey('cyclic_tests.id'))
    cyclic_test = relationship("CyclicTest", back_populates="trials")

class StaticTestResult(TestResult):
    __tablename__ = "static_test_results"
    id = Column(Integer, ForeignKey('test_results.id'), primary_key=True, index=True)
    static_test_id = Column(Integer, ForeignKey('static_tests.id'))
    static_test = relationship("StaticTest", back_populates="trials")