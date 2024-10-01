from typing import List, Optional
from pydantic import BaseModel


class DeflectionCreateSchema(BaseModel):
    deflection_gauge: int
    max_deflection: float
    permanent_deflection: float
    recovery: float

class DeflectionSchema(DeflectionCreateSchema):
    id: int
    class Config:
        orm_mode = True

class StaticTestCreateSchema(BaseModel):
    pressure_factor: str
    pressure: float
    index: int
    duration: int
    type: str

class StaticTestUpdateSchema(BaseModel):
    # id: int
    type: str
    index: int
    duration: int
    pressure: float

class StaticTestSchema(StaticTestCreateSchema):
    id: int
    deflections: List[DeflectionSchema]
    finished: bool
    class Config:
        orm_mode = True
        
class InfiltrationTestCreateSchema(BaseModel):
    type: str
    pressure: Optional[float]
class InfiltrationTestSchema(InfiltrationTestCreateSchema):
    id: int
    duration: float
    leakage: float

    class Config:
        orm_mode = True

class ShotCreateSchema(BaseModel):
    area: float
    velocity: float
    result: bool
    note: str
    
class ShotSchema(ShotCreateSchema):
    id: int

    class Config:
        orm_mode = True

class MissileImpactTestCreateSchema(BaseModel):
    missile: str
    missile_weight: float
    
class MissileImpactTestSchema(MissileImpactTestCreateSchema):
    id: int
    shots: List[ShotSchema]

    class Config:
        orm_mode = True



class CyclicTestCreateSchema(BaseModel):
    type: str
    cycles: int
    low_pressure: float
    high_pressure: float
    
class CyclicTestUpdateSchema(BaseModel):
    # id: int
    index: int
    cycles: int
    type: str
    low_pressure: float
    high_pressure: float
    
class CyclicTestSchema(CyclicTestCreateSchema):
    # id: int
    finished: bool
    index: int
    deflection: Optional[float]
    permanent_set: Optional[float]
    result: Optional[bool]
    note: Optional[str]

    class Config:
        orm_mode = True

class ProjectCreateSchema(BaseModel):
    name: str
    inward_design_pressure: float
    outward_design_pressure: float
       
class ProjectSchema(ProjectCreateSchema):
    id: int
    device_id: int  # New field added
    static_tests: List[StaticTestSchema]
    infiltration_tests: List[InfiltrationTestSchema]
    missile_impact_tests: List[MissileImpactTestSchema]
    cyclic_tests: List[CyclicTestSchema]

    class Config:
        orm_mode = True


class DeviceCreateSchema(BaseModel):
    name: str

class DeviceSchema(DeviceCreateSchema):
    id: int
    projects: List[ProjectSchema]

    class Config:
        orm_mode = True