import os
from sqlalchemy import create_engine
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import sessionmaker, Session
from app.data.models import *
from app.data.schema import *
from app.data.utils import run_migrations
from app.domain.cyclic_test_pressure_calculator import CyclicTestPressureCalculator
from app.domain.static_test_pressure_calculator import StaticTestPressureCalculator

from fastapi.middleware.cors import CORSMiddleware


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")

engine = create_engine(DATABASE_URL)
run_migrations(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
app = FastAPI()

# Dependency to get the session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Frontend origin
    allow_credentials=True,
    allow_methods=["GET","POST","PUT"],  # Allow all HTTP methods (POST, GET, etc.)
    allow_headers=["*"],  # Allow all headers
)


@app.get("/devices/", response_model=List[DeviceSchema])
def list_devices(db: Session = Depends(get_db)):
    return db.query(Device).all()


@app.post("/devices/", response_model=DeviceSchema)
def create_device(device: DeviceCreateSchema, db: Session = Depends(get_db)):
    db_device = Device(
        name=device.name,
    )
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    return db_device



@app.get("/devices/{device_id}/projects", response_model=List[ProjectSchema])
def get_projects_by_device_id(device_id: int, db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id).first()
    projects = db.query(Project).filter(Project.device == device).all()
    if not projects:
        raise HTTPException(status_code=404, detail="No projects found for this device_id")
    return projects
    
# # List all projects
# @app.get("/projects/", response_model=List[ProjectSchema])
# def list_projects(db: Session = Depends(get_db)):
#     return db.query(Project).all()

@app.post("/devices/{device_id}/projects/", response_model=ProjectSchema)
def create_project_for_device(device_id: int, project: ProjectCreateSchema, db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    db_project = Project(
        name=project.name,
        inward_design_pressure=project.inward_design_pressure,
        outward_design_pressure=project.outward_design_pressure,
        device_id=device_id,
        static_tests=[],
        infiltration_tests=[],
        missile_impact_tests=[],
        cyclic_tests=[],
    )
    db.add(db_project)
    db.commit()
    db.refresh(db_project)

    # Create 8 cyclic tests
    for i in range(8):
        h, l, c = CyclicTestPressureCalculator.get_cylcic_test_data(
            db_project.inward_design_pressure if i < 4 else db_project.outward_design_pressure,
            i,
        )
        cyclic_test = CyclicTest(
            type="inward" if i < 4 else "outward",
            cycles=c,
            low_pressure=l,
            high_pressure=h,
            index=i,
            project_id=db_project.id,
            finished=False,
        )
        db.add(cyclic_test)
    
    # Create 6 static tests
    for j in range(6):
        p, d = StaticTestPressureCalculator.get_static_test_data(db_project.inward_design_pressure if j < 3 else db_project.outward_design_pressure, j)
        static_test = StaticTest(
            pressure_factor='Structural Pressure',
            pressure=p,
            duration=d,
            type="inward" if j < 3 else "outward",
            index=j,
            project_id=db_project.id,
            finished=False,
        )
        db.add(static_test)
    
    db.commit()
    db.refresh(db_project)
    return db_project



@app.put("/projects/{project_id}", response_model=ProjectSchema)
def update_project(project_id: int, project_data: ProjectCreateSchema, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    db_project.name = project_data.name
    db_project.inward_design_pressure = project_data.inward_design_pressure
    db_project.outward_design_pressure = project_data.outward_design_pressure

    # Recalculate static tests
    for j in range(6):
        p, d = StaticTestPressureCalculator.get_static_test_data(
            db_project.inward_design_pressure if j < 3 else db_project.outward_design_pressure, j
        )
        static_test = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index == j).first()
        if static_test and not static_test.finished:
            static_test.pressure = p
            static_test.duration = d
        elif not static_test:
            new_static_test = StaticTest(
                pressure_factor='Structural Pressure',
                pressure=p,
                duration=d,
                type="inward" if j < 3 else "outward",
                index=j,
                project_id=project_id,
                finished=False
            )
            db.add(new_static_test)

    # Recalculate cyclic tests
    for i in range(8):
        h, l, c = CyclicTestPressureCalculator.get_cylcic_test_data(
            db_project.inward_design_pressure if i < 4 else db_project.outward_design_pressure, i
        )
        cyclic_test = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index == i).first()
        if cyclic_test and not cyclic_test.finished:
            cyclic_test.high_pressure = h
            cyclic_test.low_pressure = l
            cyclic_test.cycles = c
        elif not cyclic_test:
            new_cyclic_test = CyclicTest(
                type="inward" if i < 4 else "outward",
                cycles=c,
                low_pressure=l,
                high_pressure=h,
                index=i,
                project_id=project_id,
                finished=False
            )
            db.add(new_cyclic_test)
    

    db.commit()
    db.refresh(db_project)
    return db_project



@app.put("/projects/{project_id}/cyclic_tests", response_model=ProjectSchema)
def update_cyclic_tests(project_id: int, cyclic_tests_data: List[CyclicTestUpdateSchema], db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Update cyclic tests
    for cyclic_test_data in cyclic_tests_data:
        cyclic_test = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index == cyclic_test_data.index).first()
        if cyclic_test and not cyclic_test.finished:
            cyclic_test.type = cyclic_test_data.type
            cyclic_test.cycles = cyclic_test_data.cycles
            cyclic_test.low_pressure = cyclic_test_data.low_pressure
            cyclic_test.high_pressure = cyclic_test_data.high_pressure
            cyclic_test.index = cyclic_test_data.index
        elif not cyclic_test:
            new_cyclic_test = CyclicTest(
                type=cyclic_test_data.type,
                cycles=cyclic_test_data.cycles,
                low_pressure=cyclic_test_data.low_pressure,
                high_pressure=cyclic_test_data.high_pressure,
                index=cyclic_test_data.index,
                project_id=project_id
            )
            db.add(new_cyclic_test)

    db.commit()
    db.refresh(db_project)
    return db_project

@app.put("/projects/{project_id}/static_tests", response_model=ProjectSchema)
def update_static_tests(project_id: int, static_tests_data: List[StaticTestUpdateSchema], db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update static tests
    for static_test_data in static_tests_data:
        static_test : StaticTest = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index == static_test_data.index).first()
        if static_test and not static_test.finished:
            static_test.pressure_factor = static_test_data.pressure_factor
            static_test.pressure = static_test_data.pressure
            static_test.duration = static_test_data.duration
            static_test.type = static_test_data.type
            static_test.index = static_test_data.index
        elif not static_test:
            new_static_test = StaticTest(
                pressure_factor=static_test_data.pressure_factor,
                pressure=static_test_data.pressure,
                duration=static_test_data.duration,
                type=static_test_data.type,
                index=static_test_data.index,
                project_id=project_id
            )
            db.add(new_static_test)

    db.commit()
    db.refresh(db_project)
    return db_project

@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_id}/finish", response_model=CyclicTestSchema)
def finish_cyclic_test(project_id: int, cyclic_test_id: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test = db.query(CyclicTest).filter(CyclicTest.id == cyclic_test_id, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")

    cyclic_test.finished = True
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

@app.put("/projects/{project_id}/static_tests/{static_test_id}/finish", response_model=StaticTestSchema)
def finish_static_test(project_id: int, static_test_id: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    static_test = db.query(StaticTest).filter(StaticTest.id == static_test_id, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="Static test not found")

    # Check if previous tests are finished
    previous_tests = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index < static_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous static tests are not finished")

    static_test.finished = True
    db.commit()
    db.refresh(static_test)
    return static_test


# @app.post("/projects/", response_model=ProjectSchema)
# def create_project(project: ProjectCreateSchema, db: Session = Depends(get_db)):
#     db_project = Project(
#         name=project.name,
#         device_id=project.device_id,
#         static_tests=[],
#         infiltration_tests=[],
#         missile_impact_tests=[],
#         cyclic_tests=[],
#     )
#     db.add(db_project)
#     db.commit()
#     db.refresh(db_project)

#     # Create 8 cyclic tests
#     for i in range(8):
#         cyclic_test = CyclicTest(
#             type="inward" if i < 4 else "outward",
#             cycles=0,  # Initialize with 0 cycles
#             low_pressure=0.0,  # Initialize with 0.0
#             high_pressure=0.0,  # Initialize with 0.0
#             project_id=db_project.id
#         )
#         db.add(cyclic_test)
    
#     # Create 6 static tests
#     for _ in range(6):
#         static_test = StaticTest(
#             pressure_factor=0.0,  # Initialize with 0.0
#             pressure=0.0,  # Initialize with 0.0
#             project_id=db_project.id
#         )
#         db.add(static_test)
    
#     db.commit()
#     db.refresh(db_project)
#     return db_project
# Add StaticTest to Project
# @app.post("/projects/{project_id}/static-tests/", response_model=StaticTestSchema)
# def add_static_test(project_id: int, static_test_data: StaticTestCreateSchema, db: Session = Depends(get_db)):
#     project = db.query(Project).filter(Project.id == project_id).first()
#     if not project:
#         raise HTTPException(status_code=404, detail="Project not found")
    
#     new_static_test = StaticTest(**static_test_data.dict(), project_id=project_id)
#     db.add(new_static_test)
#     db.commit()
#     db.refresh(new_static_test)
#     return new_static_test

# Update a specific StaticTest
@app.put("/static-tests/{static_test_id}/", response_model=StaticTestSchema)
def update_static_test(static_test_id: int, static_test_data: StaticTestUpdateSchema, db: Session = Depends(get_db)):
    static_test = db.query(StaticTest).filter(StaticTest.id == static_test_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")
    
    if static_test.finished:
        raise HTTPException(status_code=400, detail="Cannot update a finished StaticTest")
    
    for key, value in static_test_data.dict().items():
        setattr(static_test, key, value)
    db.commit()
    db.refresh(static_test)
    return static_test
# # Delete a StaticTest
# @app.delete("/static-tests/{static_test_id}/", response_model=dict)
# def delete_static_test(static_test_id: int, db: Session = Depends(get_db)):
#     static_test = db.query(StaticTest).filter(StaticTest.id == static_test_id).first()
#     if not static_test:
#         raise HTTPException(status_code=404, detail="StaticTest not found")
    
#     db.delete(static_test)
#     db.commit()
#     return {"detail": "StaticTest deleted successfully"}


# Create a Deflection within a StaticTest
@app.post("/static-tests/{static_test_id}/deflections/", response_model=DeflectionSchema)
def create_deflection(static_test_id: int, deflection_data: DeflectionCreateSchema, db: Session = Depends(get_db)):
    static_test = db.query(StaticTest).filter(StaticTest.id == static_test_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")

    new_deflection = Deflection(**deflection_data.dict(), static_test_id=static_test_id)
    db.add(new_deflection)
    db.commit()
    db.refresh(new_deflection)
    return new_deflection

# Update a Deflection
@app.put("/deflections/{deflection_id}/", response_model=DeflectionSchema)
def update_deflection(deflection_id: int, deflection_data: DeflectionCreateSchema, db: Session = Depends(get_db)):
    deflection = db.query(Deflection).filter(Deflection.id == deflection_id).first()
    if not deflection:
        raise HTTPException(status_code=404, detail="Deflection not found")

    for key, value in deflection_data.dict().items():
        setattr(deflection, key, value)
    db.commit()
    db.refresh(deflection)
    return deflection

# Delete a Deflection
@app.delete("/deflections/{deflection_id}/", response_model=dict)
def delete_deflection(deflection_id: int, db: Session = Depends(get_db)):
    deflection = db.query(Deflection).filter(Deflection.id == deflection_id).first()
    if not deflection:
        raise HTTPException(status_code=404, detail="Deflection not found")

    db.delete(deflection)
    db.commit()
    return {"detail": "Deflection deleted successfully"}


# Create an InfiltrationTest within a Project
# @app.post("/projects/{project_id}/infiltration-tests/", response_model=InfiltrationTestSchema)
# def create_infiltration_test(project_id: int, infiltration_test_data: InfiltrationTestCreateSchema, db: Session = Depends(get_db)):
#     project = db.query(Project).filter(Project.id == project_id).first()
#     if not project:
#         raise HTTPException(status_code=404, detail="Project not found")

#     new_infiltration_test = InfiltrationTest(**infiltration_test_data.dict(), project_id=project_id)
#     db.add(new_infiltration_test)
#     db.commit()
#     db.refresh(new_infiltration_test)
#     return new_infiltration_test

# Update a specific InfiltrationTest
# @app.put("/infiltration-tests/{infiltration_test_id}/", response_model=InfiltrationTestSchema)
# def update_infiltration_test(infiltration_test_id: int, infiltration_test_data: InfiltrationTestCreateSchema, db: Session = Depends(get_db)):
#     infiltration_test = db.query(InfiltrationTest).filter(InfiltrationTest.id == infiltration_test_id).first()
#     if not infiltration_test:
#         raise HTTPException(status_code=404, detail="InfiltrationTest not found")

#     for key, value in infiltration_test_data.dict().items():
#         setattr(infiltration_test, key, value)
#     db.commit()
#     db.refresh(infiltration_test)
#     return infiltration_test

# Delete an InfiltrationTest
# @app.delete("/infiltration-tests/{infiltration_test_id}/", response_model=dict)
# def delete_infiltration_test(infiltration_test_id: int, db: Session = Depends(get_db)):
#     infiltration_test = db.query(InfiltrationTest).filter(InfiltrationTest.id == infiltration_test_id).first()
#     if not infiltration_test:
#         raise HTTPException(status_code=404, detail="InfiltrationTest not found")

#     db.delete(infiltration_test)
#     db.commit()
#     return {"detail": "InfiltrationTest deleted successfully"}



# Create a CyclicTest within a Project
# @app.post("/projects/{project_id}/cyclic-tests/", response_model=CyclicTestSchema)
# def create_cyclic_test(project_id: int, cyclic_test_data: CyclicTestCreateSchema, db: Session = Depends(get_db)):
#     project = db.query(Project).filter(Project.id == project_id).first()
#     if not project:
#         raise HTTPException(status_code=404, detail="Project not found")

#     new_cyclic_test = CyclicTest(**cyclic_test_data.dict(), project_id=project_id)
#     db.add(new_cyclic_test)
#     db.commit()
#     db.refresh(new_cyclic_test)
#     return new_cyclic_test

# Update a specific CyclicTest
@app.put("/cyclic-tests/{cyclic_test_id}/", response_model=CyclicTestSchema)
def update_cyclic_test(cyclic_test_id: int, cyclic_test_data: CyclicTestUpdateSchema, db: Session = Depends(get_db)):
    cyclic_test = db.query(CyclicTest).filter(CyclicTest.id == cyclic_test_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="CyclicTest not found")
    if cyclic_test.finished:
        raise HTTPException(status_code=400, detail="Cannot update a finished CyclicTest")

    for key, value in cyclic_test_data.dict().items():
        setattr(cyclic_test, key, value)
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

# Delete a CyclicTest
# @app.delete("/cyclic-tests/{cyclic_test_id}/", response_model=dict)
# def delete_cyclic_test(cyclic_test_id: int, db: Session = Depends(get_db)):
#     cyclic_test = db.query(CyclicTest).filter(CyclicTest.id == cyclic_test_id).first()
#     if not cyclic_test:
#         raise HTTPException(status_code=404, detail="CyclicTest not found")

#     db.delete(cyclic_test)
#     db.commit()
#     return {"detail": "CyclicTest deleted successfully"}



# Create a MissileImpactTest within a Project
# @app.post("/projects/{project_id}/missile-impact-tests/", response_model=MissileImpactTestSchema)
# def create_missile_impact_test(project_id: int, missile_impact_test_data: MissileImpactTestCreateSchema, db: Session = Depends(get_db)):
#     project = db.query(Project).filter(Project.id == project_id).first()
#     if not project:
#         raise HTTPException(status_code=404, detail="Project not found")

#     new_missile_impact_test = MissileImpactTest(**missile_impact_test_data.dict(), project_id=project_id)
#     db.add(new_missile_impact_test)
#     db.commit()
#     db.refresh(new_missile_impact_test)
#     return new_missile_impact_test

# Update a specific MissileImpactTest
# @app.put("/missile-impact-tests/{missile_impact_test_id}/", response_model=MissileImpactTestSchema)
# def update_missile_impact_test(missile_impact_test_id: int, missile_impact_test_data: MissileImpactTestCreateSchema, db: Session = Depends(get_db)):
#     missile_impact_test = db.query(MissileImpactTest).filter(MissileImpactTest.id == missile_impact_test_id).first()
#     if not missile_impact_test:
#         raise HTTPException(status_code=404, detail="MissileImpactTest not found")

#     for key, value in missile_impact_test_data.dict().items():
#         setattr(missile_impact_test, key, value)
#     db.commit()
#     db.refresh(missile_impact_test)
#     return missile_impact_test

# Delete a MissileImpactTest
# @app.delete("/missile-impact-tests/{missile_impact_test_id}/", response_model=dict)
# def delete_missile_impact_test(missile_impact_test_id: int, db: Session = Depends(get_db)):
#     missile_impact_test = db.query(MissileImpactTest).filter(MissileImpactTest.id == missile_impact_test_id).first()
#     if not missile_impact_test:
#         raise HTTPException(status_code=404, detail="MissileImpactTest not found")

#     db.delete(missile_impact_test)
#     db.commit()
#     return {"detail": "MissileImpactTest deleted successfully"}

# Create a Shot within a MissileImpactTest
# @app.post("/missile-impact-tests/{missile_impact_test_id}/shots/", response_model=ShotSchema)
# def create_shot(missile_impact_test_id: int, shot_data: ShotCreateSchema, db: Session = Depends(get_db)):
#     missile_impact_test = db.query(MissileImpactTest).filter(MissileImpactTest.id == missile_impact_test_id).first()
#     if not missile_impact_test:
#         raise HTTPException(status_code=404, detail="MissileImpactTest not found")

#     new_shot = Shot(**shot_data.dict(), missile_impact_test_id=missile_impact_test_id)
#     db.add(new_shot)
#     db.commit()
#     db.refresh(new_shot)
#     return new_shot

# Update a specific Shot
# @app.put("/shots/{shot_id}/", response_model=ShotSchema)
# def update_shot(shot_id: int, shot_data: ShotCreateSchema, db: Session = Depends(get_db)):
#     shot = db.query(Shot).filter(Shot.id == shot_id).first()
#     if not shot:
#         raise HTTPException(status_code=404, detail="Shot not found")

#     for key, value in shot_data.dict().items():
#         setattr(shot, key, value)
#     db.commit()
#     db.refresh(shot)
#     return shot

# Delete a Shot
# @app.delete("/shots/{shot_id}/", response_model=dict)
# def delete_shot(shot_id: int, db: Session = Depends(get_db)):
#     shot = db.query(Shot).filter(Shot.id == shot_id).first()
#     if not shot:
#         raise HTTPException(status_code=404, detail="Shot not found")

#     db.delete(shot)
#     db.commit()
#     return {"detail": "Shot deleted successfully"}


