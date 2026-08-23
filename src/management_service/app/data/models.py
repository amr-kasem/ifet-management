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
    name = Column(String, nullable=False, unique=True)
    
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

    device = relationship("Device", back_populates="projects")
    parent = relationship("ProjectParent", back_populates="projects", foreign_keys=[parent_id])
    static_tests = relationship("StaticTest", back_populates="project", cascade="all, delete-orphan")
    infiltration_tests = relationship("InfiltrationTest", back_populates="project", cascade="all, delete-orphan")
    missile_impact_tests = relationship("MissileImpactTest", back_populates="project", cascade="all, delete-orphan")
    cyclic_tests = relationship("CyclicTest", back_populates="project", cascade="all, delete-orphan")

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

class MissileImpactTest(Base):
    __tablename__ = "missile_impact_tests"

    id = Column(Integer, primary_key=True, index=True)
    missile = Column(String, nullable=False)
    missile_weight = Column(Float, nullable=False)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="missile_impact_tests")

    shots = relationship("Shot", back_populates="missile_impact_test", cascade="all, delete-orphan")

class Shot(Base):
    __tablename__ = "shots"

    id = Column(Integer, primary_key=True, index=True)
    area = Column(Float, nullable=False)
    velocity = Column(Float, nullable=False)
    result = Column(Boolean, nullable=False)
    note = Column(String, nullable=False)

    missile_impact_test_id = Column(Integer, ForeignKey('missile_impact_tests.id'))
    missile_impact_test = relationship("MissileImpactTest", back_populates="shots")

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