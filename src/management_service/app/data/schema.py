import datetime as _dt
from typing import List, Literal, Optional
from xmlrpc.client import Boolean
from pydantic import BaseModel


class DeflectionCreateSchema(BaseModel):
    deflection_gauge: str
    max_deflection: float
    permanent_deflection: float
    recovery: float
    class Config:
        from_attributes = True
        
class DeflectionSchema(DeflectionCreateSchema):
    id: int
    class Config:
        from_attributes = True

class StaticTestCreateSchema(BaseModel):
    pressure: float
    duration: int
    type: str
    class Config:
        from_attributes = True
        
class StaticTestUpdateSchema(BaseModel):
    # id: int
    type: str
    index: int
    duration: int
    pressure: float
    class Config:
        from_attributes = True
        
class StaticTestResultCreateSchema(BaseModel):
    """What the rig posts for one completed stage.

    **The lifecycle fields are optional and additive**, because production
    firmware sends `deflections` alone and must keep working. When they are
    supplied the route completes the attempt in the same call — which matches
    what actually happened, since the rig posts a *finished* stage rather than
    starting one (plan §4.5).

    Without `operator_name` the attempt stays `In Progress` and its terminal
    payload cannot be built: contract §4.5 requires an operator on a terminal
    write and LabOS does not invent one. That refusal is now **visible** in
    `GET /sync/failures` rather than silent, which is the honest behaviour for a
    rig that has not yet been told to send it (TC3).
    """

    deflections: List[DeflectionCreateSchema]
    operator_name: Optional[str] = None
    result: Optional[bool] = None
    testing_continued: Optional[str] = None
    note: Optional[str] = None

    class Config:
        from_attributes = True
        
class StaticTestResultSchema(StaticTestResultCreateSchema):
    id: int
    trial_number: int
    image_path: Optional[str] = None
    note: Optional[str] = None
    class Config:
        from_attributes = True
        
class StaticTestSchema(StaticTestCreateSchema):
    id: int
    finished: bool
    index: int
    trials: List[StaticTestResultSchema]
    preset: bool
    class Config:
        from_attributes = True

class InfiltrationTestCreateSchema(BaseModel):
    type: str
    pressure: float
class InfiltrationTestSchema(InfiltrationTestCreateSchema):
    id: int
    duration: float
    leakage: float

    class Config:
        from_attributes = True

class ShotCreateSchema(BaseModel):
    # Widened 2026-09-08 to match the columns. `result` stays required - a shot
    # without an outcome is not a shot - but area, velocity and note are
    # optional, because the protocol fixes the missile and its velocity and
    # requiring them per impact was retyping rather than data capture.
    #
    # This also keeps `ProjectSchema` serialisable: it embeds
    # MissileImpactTestSchema, which embeds this, so a required field here would
    # make every existing project route fail on any impact test recorded
    # without one.
    area: Optional[float] = None
    velocity: Optional[float] = None
    result: bool
    note: Optional[str] = None
    class Config:
        from_attributes = True
    
class ShotSchema(ShotCreateSchema):
    id: int
    # The ordinal the operator sees - impact 1, 2, 3 - not the database id.
    shot_number: Optional[int] = None

    class Config:
        from_attributes = True

class MissileImpactTestCreateSchema(BaseModel):
    # Widened with the columns, and for the same reason as ShotCreateSchema:
    # ProjectSchema embeds this, so a required field here breaks the existing
    # project routes for any attempt whose missile was not typed in.
    missile: Optional[str] = None
    missile_weight: Optional[float] = None
    class Config:
        from_attributes = True
        
class MissileImpactTestSchema(MissileImpactTestCreateSchema):
    id: int
    shots: List[ShotSchema]

    class Config:
        from_attributes = True



class CyclicTestCreateSchema(BaseModel):
    type: str
    cycles: int
    low_pressure: float
    high_pressure: float
    class Config:
        from_attributes = True
    
class CyclicTestUpdateSchema(BaseModel):
    # id: int
    index: int
    cycles: int
    type: str
    low_pressure: float
    high_pressure: float
    class Config:
        from_attributes = True

class CyclicTestUpdateStatusSchema(BaseModel):

    current_cycle: int
    class Config:
        from_attributes = True

class CyclicTestResultCreateSchema(BaseModel):
    """As `StaticTestResultCreateSchema` — see its docstring for why these are
    optional and what happens when `operator_name` is absent."""

    deflections: List[DeflectionCreateSchema]
    operator_name: Optional[str] = None
    result: Optional[bool] = None
    testing_continued: Optional[str] = None
    note: Optional[str] = None

    class Config:
        from_attributes = True
        
class CyclicTestResultSchema(CyclicTestResultCreateSchema):
    id: int
    trial_number: int
    image_path: Optional[str] = None
    note: Optional[str] = None
    class Config:
        from_attributes = True
        
class CyclicTestSchema(CyclicTestCreateSchema):
    # id: int
    finished: bool
    index: int
    resume: bool
    current_cycle: int
    trials: List[CyclicTestResultSchema]
    preset: bool
    class Config:
        from_attributes = True

class TestResultUpdateSchema(BaseModel):
    note: Optional[str] = None
    class Config:
        from_attributes = True

class TestResultResponseSchema(BaseModel):
    id: int
    trial_number: int
    note: Optional[str] = None
    image_path: Optional[str] = None
    result: Optional[bool] = None
    class Config:
        from_attributes = True

class ProjectParentCreateSchema(BaseModel):
    name: str
    class Config:
        from_attributes = True

class ProjectParentSchema(ProjectParentCreateSchema):   
    id: int
    class Config:
        from_attributes = True

class ProjectCreateSchema(BaseModel):
    name: str
    inward_design_pressure: float
    outward_design_pressure: float
    parent_id: Optional[int] = None
    has_water_infiltration: bool = False
    class Config:
        from_attributes = True
       
class ProjectSchema(ProjectCreateSchema):
    id: int
    device_id: int  # New field added
    static_tests: List[StaticTestSchema]
    infiltration_tests: List[InfiltrationTestSchema]
    missile_impact_tests: List[MissileImpactTestSchema]
    # Added 2026-09-08 for consistency: every other test type on a project is
    # embedded here, so a UI that reads the project once was getting static,
    # cyclic, water and impact but silently not Forced Entry or ANSI.
    manual_tests: List["ManualTestSchema"] = []
    cyclic_tests: List[CyclicTestSchema]

    # Pre-filled parameters, exposed because pre-fill is pointless if the form
    # cannot read what was filled in. Response-only — they are on
    # `ProjectSchema` rather than `ProjectCreateSchema`, so creating a project
    # still takes exactly the fields a person types, and the import path fills
    # these afterwards from the requirement sections.
    #
    # Both nullable: a locally-typed job has no upstream requirement to pre-fill
    # them from, and absent is not zero.
    gauge_count: Optional[int] = None
    impact_count: Optional[int] = None
    # The Airtable identity, so a UI can show whether a job is linked and to
    # what. Never required — a project with no `airtable_*` id is a normal
    # standalone job (§4.6 Class 3).
    airtable_project_id: Optional[str] = None
    airtable_mockup_id: Optional[str] = None
    airtable_mockup_name: Optional[str] = None

    class Config:
        from_attributes = True


class DeviceCreateSchema(BaseModel):
    name: str

class DeviceSchema(DeviceCreateSchema):
    id: int
    projects: List[ProjectSchema]

    turbo_mode: Boolean
    turbo_slave: Boolean
    turbo_charger: Optional[int]

    class Config:
        from_attributes = True
        
class DeviceMiniSchema(DeviceCreateSchema):
    id: int
    turbo_mode: Boolean
    turbo_slave: Boolean
    turbo_charger: Optional[int]
    class Config:
        from_attributes = True
        
class DeviceTurboMaster(BaseModel):
    slave_id: int
    turbo_mode: bool
    class Config:
        from_attributes = True
    
class DeviceTurboSlave(BaseModel):
    slave_mode: bool
    class Config:
        from_attributes = True


# ---------------------------------------------------- manual test capture ---
#
# Impact, Forced Entry and ANSI Z97.1. Delivery plan §4.5.
#
# Two levels, matching the rest of the codebase: a **test** row, and **attempt**
# rows under it. `TestResult` already is the attempt record — `trial_number` is
# Attempt Number, and it carries both UUIDs, the lifecycle, the correction chain
# and the review columns — so `AttemptSchema` below serialises it for every test
# type rather than each type declaring its own.

MANUAL_TEST_TYPES = ("Forced Entry", "ANSI Z97.1")


class PhotoSchema(BaseModel):
    id: int
    filename: str
    note: Optional[str] = None
    created_at: Optional[_dt.datetime] = None
    # Set when the photograph shows one specific impact; None when it belongs
    # to the attempt as a whole.
    shot_id: Optional[int] = None

    class Config:
        from_attributes = True


class ShotRecordSchema(BaseModel):
    """One impact. Pass or fail is the whole requirement; the rest is optional.

    `result` has no default on purpose. An impact without an outcome is not an
    impact, and a default of False would record a failure nobody observed.

    `shot_number` is **not** accepted: it is allocated server-side in the order
    impacts are recorded. A client that chose its own could number two the same,
    or renumber a sequence someone has already photographed.
    """

    result: bool
    area: Optional[float] = None
    velocity: Optional[float] = None
    note: Optional[str] = None

    class Config:
        from_attributes = True


class ShotDetailSchema(BaseModel):
    """A recorded impact with its own photographs."""

    id: int
    # The ordinal the operator sees — impact 1, 2, 3 — not the database id.
    shot_number: int
    result: bool
    area: Optional[float] = None
    velocity: Optional[float] = None
    note: Optional[str] = None
    photos: List[PhotoSchema] = []

    class Config:
        from_attributes = True


class AttemptStartSchema(BaseModel):
    """Begin an attempt. Identity and the attempt number are server-side."""

    operator_name: Optional[str] = None

    class Config:
        from_attributes = True


class AttemptFinishSchema(BaseModel):
    """Terminate. Explicit completion, or an abort with a reason — never inferred.

    `result` is the **operator's** recorded outcome, and is required to complete
    a Forced Entry or ANSI attempt. It is not the verdict: the verdict is a
    separate act by a named reviewer (write contract §4).
    """

    result: Optional[bool] = None
    note: Optional[str] = None
    testing_continued: Optional[str] = None
    abort_reason: Optional[str] = None

    class Config:
        from_attributes = True


class AttemptCorrectSchema(BaseModel):
    """Supersede a recorded result: a new attempt naming the one it replaces.

    `reason` is required and not defaulted. Contract §4.1 puts it in the
    always-required set beside `Corrects Attempt ID`, because a correction with
    no stated reason is indistinguishable downstream from a retest — and the
    change document's argument to the Airtable team is precisely that those two
    must never be confusable.

    No `result` here. A correction starts open and is recorded and finished
    through the ordinary paths, so there is one lifecycle rather than two: the
    correction is a normal attempt that happens to name its predecessor.
    """

    reason: str
    # Who is making the correction, which need not be who ran the original.
    operator_name: Optional[str] = None

    class Config:
        from_attributes = True


class VerdictSchema(BaseModel):
    """The first review. Recorded once, by someone with a name.

    All four travel together by contract §6. `retest_required` is required
    rather than defaulted: an unchecked box is not a decision, and defaulting it
    to False would publish an answer nobody gave.
    """

    test_result: Literal["Pass", "Fail", "Inconclusive"]
    verdict_by: str
    retest_required: bool
    rationale: Optional[str] = None

    class Config:
        from_attributes = True


class AttemptSchema(BaseModel):
    """One attempt at any test type — the serialised `TestResult`."""

    id: int
    # `trial_number` already means what the contract calls Attempt Number, so it
    # is reused rather than duplicated.
    trial_number: int
    labos_attempt_id: Optional[str] = None
    labos_test_id: Optional[str] = None

    test_type: Optional[str] = None
    test_name: Optional[str] = None
    status: Optional[str] = None
    # The reviewer's verdict: Pending until the first review.
    test_result: Optional[str] = None
    # The operator's recorded outcome — a different field, on purpose.
    result: Optional[bool] = None
    abort_reason: Optional[str] = None

    operator_name: Optional[str] = None
    verdict_by: Optional[str] = None
    verdict_at: Optional[_dt.datetime] = None
    retest_required: Optional[bool] = None
    result_rationale: Optional[str] = None

    # The correction chain, exposed 2026-09-08 with the correction route. Both
    # are `None` on an ordinary attempt and on a retest, and populated together
    # on a correction — which is the whole distinction the change document
    # promises the Airtable team, so a client showing an attempt should be able
    # to see it without a second request.
    corrects_attempt_id: Optional[str] = None
    correction_reason: Optional[str] = None

    testing_start_date: Optional[_dt.datetime] = None
    testing_end_date: Optional[_dt.datetime] = None
    testing_continued: Optional[str] = None
    note: Optional[str] = None

    photos: List[PhotoSchema] = []

    class Config:
        from_attributes = True


class ManualTestCreateSchema(BaseModel):
    """Create a Forced Entry or ANSI Z97.1 test. Attempts start separately."""

    type: Literal["Forced Entry", "ANSI Z97.1"]
    # The grade or class the protocol requires, e.g. "ASTM F588 Grade 40" or
    # "Class A". Pre-filled from Airtable's `Required Option` when the job came
    # from there, typed by the operator when it did not.
    required_option: Optional[str] = None
    # Airtable linkage, all optional: a LabOS-only test has none and stays legal.
    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    class Config:
        from_attributes = True


class ManualTestSchema(BaseModel):
    id: int
    project_id: int
    type: str
    required_option: Optional[str] = None
    finished: bool

    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    trials: List[AttemptSchema] = []

    class Config:
        from_attributes = True


class ImpactTestCreateSchema(BaseModel):
    """Create a missile impact test.

    Missile and weight are optional: the protocol normally fixes them, and
    requiring them was retyping rather than data capture. Pre-filled from
    `Missile Type` and `Missile Weight` when the job came from Airtable.
    """

    missile: Optional[str] = None
    missile_weight: Optional[float] = None
    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    class Config:
        from_attributes = True


class ImpactTestSchema(BaseModel):
    id: int
    project_id: int
    missile: Optional[str] = None
    missile_weight: Optional[float] = None
    finished: bool

    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    trials: List[AttemptSchema] = []

    class Config:
        from_attributes = True


# ProjectSchema forward-references ManualTestSchema, which is declared below it.
ProjectSchema.model_rebuild()


class RunStartSchema(BaseModel):
    """Who is running this test. Declared, never authenticated (contract §4).

    Sent by the UI at run start, before hardware moves. The rig's trial callback
    then inherits it, which is why capturing the operator needs no firmware
    change: it is not a fact the rig has.
    """

    operator_name: Optional[str] = None

    class Config:
        from_attributes = True


class ImportRequestSchema(BaseModel):
    """Which Airtable hierarchy to import, and onto which rig.

    `device_id` is LabOS's alone: which rig will run this is an operator's
    decision about a physical machine, and Airtable has no opinion on it
    (§4.6 Class 1). `name` overrides the specimen name for the local project,
    for the case where the mock-up name is not what the lab calls it.
    """

    device_id: int
    project_record_id: str
    specimen_record_id: str
    protocol_record_id: str
    name: Optional[str] = None

    class Config:
        from_attributes = True
