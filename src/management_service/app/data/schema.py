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
    deflections: List[DeflectionCreateSchema]
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
    deflections: List[DeflectionCreateSchema]
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
    cyclic_tests: List[CyclicTestSchema]

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
# Impact, Forced Entry and ANSI Z97.1. Delivery plan §4.5. None of the three
# touches the rig, so nothing here has a setpoint, a duration or a stage.
#
# Three phases, matching the write contract: create -> finish -> verdict.
# `Test Result` is Pending from create until a named reviewer records a verdict,
# and the operator's `result` boolean is a different thing from the reviewer's
# `test_result` - different people, different moments.

MANUAL_TEST_TYPES = ("Forced Entry", "ANSI Z97.1")


class ManualTestCreateSchema(BaseModel):
    """Start a Forced Entry or ANSI Z97.1 attempt.

    `attempt_number`, `labos_attempt_id` and `status` are allocated
    server-side - a client that could choose its own attempt number could
    silently overwrite an earlier one.
    """

    type: Literal["Forced Entry", "ANSI Z97.1"]
    # The grade or class the protocol requires, e.g. "ASTM F588 Grade 40" or
    # "Class A". Pre-filled from Airtable's `Required Option` when the job came
    # from there, typed by the operator when it did not.
    required_option: Optional[str] = None
    operator_name: Optional[str] = None
    # Airtable linkage, all optional: a LabOS-only test has none and stays legal.
    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    class Config:
        from_attributes = True


class ManualTestFinishSchema(BaseModel):
    """Terminate. Explicit completion, or an abort with a reason - never inferred.

    `result` is the operator's recorded outcome. It is not the verdict: the
    verdict is a separate act by a named reviewer (write contract §4).
    """

    result: Optional[bool] = None
    note: Optional[str] = None
    testing_continued: Optional[str] = None
    abort_reason: Optional[str] = None

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


class PhotoSchema(BaseModel):
    id: int
    filename: str
    note: Optional[str] = None
    created_at: Optional[_dt.datetime] = None

    class Config:
        from_attributes = True


class ManualTestSchema(BaseModel):
    id: int
    project_id: int
    type: str
    required_option: Optional[str] = None
    result: Optional[bool] = None

    labos_attempt_id: str
    attempt_number: int
    status: str
    test_result: str
    abort_reason: Optional[str] = None

    operator_name: Optional[str] = None
    verdict_by: Optional[str] = None
    verdict_at: Optional[_dt.datetime] = None
    retest_required: Optional[bool] = None

    testing_start_date: Optional[_dt.datetime] = None
    testing_end_date: Optional[_dt.datetime] = None
    testing_continued: Optional[str] = None
    note: Optional[str] = None

    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    photos: List[PhotoSchema] = []

    class Config:
        from_attributes = True


# ------------------------------------------------------------------ impact ---
#
# `MissileImpactTestCreateSchema` and `ShotCreateSchema` already exist above and
# are kept, because report generation reads them. These add the attempt identity
# the existing pair never had, and relax what a shot must carry.


class ImpactTestCreateSchema(BaseModel):
    """Start a missile impact attempt.

    Missile and weight are optional: the protocol normally fixes them, and
    requiring them per attempt was retyping rather than data capture. When the
    job came from Airtable they are pre-filled from `Missile Type` and
    `Missile Weight`.
    """

    missile: Optional[str] = None
    missile_weight: Optional[float] = None
    operator_name: Optional[str] = None
    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    class Config:
        from_attributes = True


class ShotRecordSchema(BaseModel):
    """One impact. Pass or fail is the whole requirement; the rest is optional.

    `result` has no default on purpose. A shot without an outcome is not a shot,
    and a default of False would silently record a failure nobody observed.
    """

    result: bool
    area: Optional[float] = None
    velocity: Optional[float] = None
    note: Optional[str] = None

    class Config:
        from_attributes = True


class ImpactTestSchema(BaseModel):
    id: int
    project_id: int
    missile: Optional[str] = None
    missile_weight: Optional[float] = None

    labos_attempt_id: str
    attempt_number: int
    status: str
    test_result: str
    abort_reason: Optional[str] = None

    operator_name: Optional[str] = None
    verdict_by: Optional[str] = None
    verdict_at: Optional[_dt.datetime] = None
    retest_required: Optional[bool] = None

    testing_start_date: Optional[_dt.datetime] = None
    testing_end_date: Optional[_dt.datetime] = None
    testing_continued: Optional[str] = None
    note: Optional[str] = None

    airtable_protocol_id: Optional[str] = None
    airtable_section_id: Optional[str] = None
    airtable_section_name: Optional[str] = None

    shots: List[ShotSchema] = []
    photos: List[PhotoSchema] = []

    class Config:
        from_attributes = True
