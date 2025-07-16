from typing import List, Optional
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
    area: float
    velocity: float
    result: bool
    note: str
    class Config:
        from_attributes = True
    
class ShotSchema(ShotCreateSchema):
    id: int

    class Config:
        from_attributes = True

class MissileImpactTestCreateSchema(BaseModel):
    missile: str
    missile_weight: float
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

class ProjectCreateSchema(BaseModel):
    name: str
    inward_design_pressure: float
    outward_design_pressure: float
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
