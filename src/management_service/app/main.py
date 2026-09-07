import os
from typing import Optional
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker, Session
from app.data.models import *
from app.data.schema import *
from app.data import attempts
from app.domain.cyclic_test_pressure_calculator import CyclicTestPressureCalculator
from app.domain.static_test_pressure_calculator import StaticTestPressureCalculator
import logging
import uuid
import shutil
from pathlib import Path
from fastapi.staticfiles import StaticFiles
import tempfile

from fastapi.middleware.cors import CORSMiddleware

from app.utils.pdf_utils import create_test_report_pdf

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")

engine = create_engine(DATABASE_URL)
# Migrations are handled by startup script

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
app = FastAPI()

# Create uploads directory if it doesn't exist.
#
# Configurable since 2026-09-08. It was a hard-coded relative path created at
# import time, which meant `app.main` could not be imported anywhere the working
# directory is read-only — including the test harness, where the repo is mounted
# `:ro`. Originals live here and stay local: Airtable receives only a downscaled
# preview (write contract §6), so this directory is the evidence of record and
# belongs on a bind mount, not inside the image.
uploads_dir = Path(os.getenv("LABOS_UPLOADS_DIR", "uploads"))
uploads_dir.mkdir(parents=True, exist_ok=True)

# Mount static files for uploaded images
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

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
    allow_methods=["GET","POST","PUT", "DELETE"],  # Allow all HTTP methods (POST, GET, etc.)
    allow_headers=["*"],  # Allow all headers
)


@app.get("/devices/", response_model=List[DeviceSchema])
def list_devices(db: Session = Depends(get_db)):
    return db.query(Device).all()

@app.get("/devices/{device_id}", response_model=DeviceMiniSchema)
def get_device(device_id: int,db: Session = Depends(get_db)):
    device: Device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="No Device found")
    return device


@app.post("/devices/", response_model=DeviceSchema)
def create_device(device: DeviceCreateSchema, db: Session = Depends(get_db)):
    db_device = Device(
        name=device.name,
        turbo_mode=False,
        turbo_slave=False,
        turbo_charger=None,
    )
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    return db_device

@app.put("/devices/{device_id}/", response_model=DeviceSchema)
def update_device(device: DeviceSchema, db: Session = Depends(get_db)):
    db_device = Device(
        name=device.name,
        turbo_mode=device.turbo_mode,
        turbo_slave=device.turbo_slave,
        turbo_charger=device.turbo_charger,
    )
    db.add(db_device)
    db.commit()
    db.refresh(db_device)
    return db_device

@app.get("/devices/{device_id}/projects/", response_model=List[ProjectSchema])
def get_projects_by_device_id(device_id: int, parent_id: Optional[int] = None, db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id).first()
    query = db.query(Project).filter(Project.device == device)
    
    if parent_id is not None:
        query = query.filter(Project.parent_id == parent_id)
    
    projects = query.all()
    if not projects:
        raise HTTPException(status_code=404, detail="No projects found for this device_id")
    
    # Sort by id descending to get most recent first
    sorted_by_id = sorted(projects, key=lambda x: x.id, reverse=True)
    # Get the two most recent
    most_recent = sorted_by_id[:2]
    # Get the rest and sort alphabetically by name
    rest = sorted_by_id[2:]
    rest_sorted = sorted(rest, key=lambda x: x.name)
    # Combine: most recent two first, then rest alphabetically
    return most_recent + rest_sorted

# # List all projects
# @app.get("/projects/", response_model=List[ProjectSchema])
# def list_projects(db: Session = Depends(get_db)):
#     return db.query(Project).all()

@app.post("/devices/{device_id}/projects/", response_model=ProjectSchema)
def create_project_for_device(device_id: int, project: ProjectCreateSchema, db: Session = Depends(get_db)):
    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    duplicate = db.query(Project).filter(Project.name == project.name, Project.parent_id == project.parent_id).first()
    if duplicate:
        duplicate_data = {
            "id": duplicate.id,
            "name": duplicate.name,
            "parent_id": duplicate.parent_id,
            "device_id": duplicate.device_id
        }
        logger.error(f"Error - Duplicate specimen found: {duplicate.parent_id, duplicate.name} for Project: {project.parent_id, project.name}")
        raise HTTPException(status_code=400, detail="Specimen with this name already exists for this Project")

    try:

        db_project = Project(
            name=project.name,
            inward_design_pressure=project.inward_design_pressure,
            outward_design_pressure=project.outward_design_pressure,
            device_id=device_id,
            parent_id=project.parent_id,
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
                resume=False,
                current_cycle=0,
                preset=True,
            )
            db.add(cyclic_test)
    except IntegrityError as e:
        db.rollback()
        if "UNIQUE constraint failed" in str(e) or "duplicate key value" in str(e):
            raise HTTPException(status_code=400, detail=f"Unique Contstraint Failed: Specimen with name '{project.name}' already exists")
        else:
            raise HTTPException(status_code=400, detail="Database constraint violation")

    
    # Create 6 static tests
    for j in range(6):
        p, d = StaticTestPressureCalculator.get_static_test_data(db_project.outward_design_pressure if j % 2 else db_project.inward_design_pressure, j)
        static_test = StaticTest(
            pressure_factor='Structural Pressure',
            pressure=p,
            duration=d,
            type="outward" if j % 2 else "inward",
            index=j + (1 if project.has_water_infiltration and j > 3 else 0),
            project_id=db_project.id,
            finished=False,
            preset=True,
        )
        db.add(static_test)

    if project.has_water_infiltration:
        water_infiltration_test = StaticTest(
            pressure_factor='Structural Pressure',
            pressure=db_project.inward_design_pressure * 0.15,
            duration=900,
            type="inward",
            index=4,
            project_id=db_project.id,
            finished=False,
            preset=True,
        )
        db.add(water_infiltration_test)
    
    db.commit()
    db.refresh(db_project)
    return db_project



@app.put("/projects/{project_id}", response_model=ProjectSchema)
def update_project(project_id: int, project_data: ProjectCreateSchema, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    duplicate = db.query(Project).filter(Project.name == project_data.name and Project.parent_id == db_project.parent_id).first()
    if duplicate:
        raise HTTPException(status_code=400, detail="Specimen with this name already exists for this device")
    db_project.name = project_data.name
    db_project.inward_design_pressure = project_data.inward_design_pressure
    db_project.outward_design_pressure = project_data.outward_design_pressure

    # Recalculate static tests
    for j in range(6):
        p, d = StaticTestPressureCalculator.get_static_test_data(
            db_project.outward_design_pressure if j % 2 else db_project.inward_design_pressure, j
        )
        static_test = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index == j, StaticTest.preset == True).first()
        if static_test and not static_test.finished:
            static_test.pressure = p
            static_test.duration = d
        elif not static_test:
            new_static_test = StaticTest(
                pressure_factor='Structural Pressure',
                pressure=p,
                duration=d,
                type="outward" if j % 2 else "inward",
                index=j,
                project_id=project_id,
                finished=False,
                preset=True,
            )
            db.add(new_static_test)

    # Recalculate cyclic tests
    for i in range(8):
        h, l, c = CyclicTestPressureCalculator.get_cylcic_test_data(
            db_project.inward_design_pressure if i < 4 else db_project.outward_design_pressure, i
        )
        cyclic_test = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index == i, CyclicTest.preset == True).first()
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
                finished=False,
                resume=False,
                current_cycle=0,
                preset=True,
            )
            db.add(new_cyclic_test)
    

    db.commit()
    db.refresh(db_project)
    return db_project


@app.post("/projects/{project_id}/static_tests/", response_model=StaticTestSchema)  
def create_static_test(project_id: int, static_test_data: StaticTestCreateSchema, db: Session = Depends(get_db)):
    db_project : Project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")    
    
    static_test = StaticTest(
        project_id=project_id,
        index=len(db_project.static_tests),
        type=static_test_data.type,
        preset=False,
        finished=False,
        duration=static_test_data.duration,
        pressure=static_test_data.pressure,
        pressure_factor="",
    )
    
    db.add(static_test)
    db.commit()
    db.refresh(static_test)
    return static_test

@app.put("/projects/{project_id}/static_tests", response_model=ProjectSchema)
def update_static_tests(project_id: int, static_tests_data: List[StaticTestUpdateSchema], db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Update static tests
    for static_test_data in static_tests_data:
        static_test : StaticTest = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index == static_test_data.index).first()
        if static_test and not static_test.finished:
            static_test.pressure = static_test_data.pressure
            static_test.duration = static_test_data.duration
            static_test.type = static_test_data.type
            static_test.index = static_test_data.index
        elif not static_test:
            new_static_test = StaticTest(
                pressure=static_test_data.pressure,
                duration=static_test_data.duration,
                type=static_test_data.type,
                index=static_test_data.index,
                project_id=project_id,
                preset=False,
            )
            db.add(new_static_test)

    db.commit()
    db.refresh(db_project)
    return db_project
# Get a specific StaticTest
@app.get("/projects/{project_id}/static-tests/{static_test_index}/", response_model=StaticTestSchema)
def get_static_test(project_id: int, static_test_index: int, db: Session = Depends(get_db)):
    static_test = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")
    return static_test

# Update a specific StaticTest
@app.put("/projects/{project_id}/static-tests/{static_test_index}/", response_model=StaticTestSchema)
def update_static_test(project_id: int, static_test_index: int, static_test_data: StaticTestUpdateSchema, db: Session = Depends(get_db)):
    static_test = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")
    
    if static_test.finished:
        raise HTTPException(status_code=400, detail="Cannot update a finished StaticTest")
    
    for key, value in static_test_data.dict().items():
        setattr(static_test, key, value)
    db.commit()
    db.refresh(static_test)
    return static_test


# Delete a StaticTest
@app.delete("/projects/{project_id}/static-tests/{static_test_index}/", response_model=dict)
def delete_static_test(project_id: int, static_test_index: int, db: Session = Depends(get_db)):
    static_test : StaticTest = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")
    if static_test.preset:
        raise HTTPException(status_code=400, detail="Cannot delete a preset StaticTest")
    if static_test.trials:
        raise HTTPException(status_code=400, detail="Cannot delete a StaticTest with trials")
    
    db.delete(static_test)
    db.commit()
    return {"detail": "StaticTest deleted successfully"}


@app.post("/projects/{project_id}/static_tests/{static_test_index}/trials", response_model=StaticTestResultSchema)
def create_static_test_trial(project_id: int, static_test_index: int, trial_data: StaticTestResultCreateSchema, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    static_test : StaticTest = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="Static test not found")
    
    
    new_trial = StaticTestResult(
        static_test_id=static_test.id,
        trial_number=len(static_test.trials)+1,
        result=None,
        note=None,
        # P1 / Ref 46 — an attempt is born with its Airtable identity and in the
        # In Progress state, not stamped later. The merge key has to exist
        # before anything can reference the attempt, and `labos_test_id` is
        # taken from the sibling attempts so a retest joins its own group.
        **attempts.begin(
            static_test.trials,
            test_type="Static Load",
            test_name=static_test.airtable_section_name,
        ),
    )   
    
    db.add(new_trial)
    db.flush()  # Flush to get the new ID without committing
    
    for d in trial_data.deflections:
        new_deflection = Deflection(
            deflection_gauge=d.deflection_gauge,
            max_deflection=d.max_deflection,
            permanent_deflection=d.permanent_deflection,
            recovery=d.recovery,
            test_id=new_trial.id,
        )
        db.add(new_deflection)
        
    db.commit()
    db.refresh(new_trial)
    return new_trial


@app.get("/projects/{project_id}/static_tests/{static_test_index}/trials", response_model=List[StaticTestResultSchema])
def get_static_test_trials(project_id: int, static_test_index: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    static_test = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    if not static_test:
        raise HTTPException(status_code=404, detail="Static test not found")
    
    return static_test.trials



@app.put("/projects/{project_id}/static_tests/{static_test_index}/finish", response_model=StaticTestSchema)
def finish_static_test(project_id: int, static_test_index: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    static_test = db.query(StaticTest).filter(StaticTest.index == static_test_index, StaticTest.project_id == project_id).first()
    static_test_result = db.query(StaticTestResult).filter(StaticTestResult.static_test_id == static_test.id).first()
    if not static_test_result:
        raise HTTPException(status_code=404, detail="Static test has no results, please add at least one trial")
    if not static_test:
        raise HTTPException(status_code=404, detail="Static test not found")

    # # Check if previous tests are finished
    # previous_tests = db.query(StaticTest).filter(StaticTest.project_id == project_id, StaticTest.index < static_test.index).all()
    # if any(not test.finished for test in previous_tests):
    #     raise HTTPException(status_code=400, detail="Previous static tests are not finished")

    static_test.finished = True
    db.commit()
    db.refresh(static_test)
    return static_test






@app.post("/projects/{project_id}/cyclic-tests/", response_model=CyclicTestSchema)
def create_cyclic_test(project_id: int, cyclic_test_data: CyclicTestCreateSchema, db: Session = Depends(get_db)):
    db_project : Project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    cyclic_test = CyclicTest(
        project_id=project_id,
        index=len(db_project.cyclic_tests),
        type=cyclic_test_data.type,
        cycles=cyclic_test_data.cycles,
        low_pressure=cyclic_test_data.low_pressure,
        high_pressure=cyclic_test_data.high_pressure,
        preset=False,
        finished=False,
        resume=False,
        current_cycle=0,
    )
    
    db.add(cyclic_test)
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

@app.get("/projects/{project_id}/cyclic-tests/{cyclic_test_index}/", response_model=CyclicTestSchema)
def get_cyclic_test(project_id: int, cyclic_test_index: int, db: Session = Depends(get_db)):
    cyclic_test = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="StaticTest not found")
    return cyclic_test

@app.put("/projects/{project_id}/cyclic_tests", response_model=ProjectSchema)
def update_cyclic_tests(project_id: int, cyclic_tests_data: List[CyclicTestUpdateSchema], db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    # Update cyclic tests
    for cyclic_test_data in cyclic_tests_data:
        cyclic_test : CyclicTest = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index == cyclic_test_data.index).first()
        if cyclic_test and not cyclic_test.finished:
            cyclic_test.type = cyclic_test_data.type
            cyclic_test.cycles = cyclic_test_data.cycles
            cyclic_test.low_pressure = cyclic_test_data.low_pressure
            cyclic_test.high_pressure = cyclic_test_data.high_pressure
        elif not cyclic_test:
            new_cyclic_test = CyclicTest(
                type=cyclic_test_data.type,
                cycles=cyclic_test_data.cycles,
                low_pressure=cyclic_test_data.low_pressure,
                high_pressure=cyclic_test_data.high_pressure,
                index=cyclic_test_data.index,
                project_id=project_id,
                resume=False,
                current_cycle=0,
                preset=False,
            )
            db.add(new_cyclic_test)

    db.commit()
    db.refresh(db_project)
    return db_project



# Update a specific CyclicTest
@app.put("/projects/{project_id}/cyclic-tests/{cyclic_test_index}/", response_model=CyclicTestSchema)
def update_cyclic_test(project_id: int, cyclic_test_index: int, cyclic_test_data: CyclicTestUpdateSchema, db: Session = Depends(get_db)):
    cyclic_test = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="CyclicTest not found")
    if cyclic_test.finished:
        raise HTTPException(status_code=400, detail="Cannot update a finished CyclicTest")

    for key, value in cyclic_test_data.dict().items():
        setattr(cyclic_test, key, value)
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

@app.delete("/projects/{project_id}/cyclic-tests/{cyclic_test_index}/", response_model=dict)
def delete_cyclic_test(project_id: int, cyclic_test_index: int, db: Session = Depends(get_db)):
    cyclic_test : CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="CyclicTest not found")
    if cyclic_test.preset:
        raise HTTPException(status_code=400, detail="Cannot delete a preset CyclicTest")
    if cyclic_test.trials:
        raise HTTPException(status_code=400, detail="Cannot delete a CyclicTest with trials")

    db.delete(cyclic_test)
    db.commit()
    return {"detail": "CyclicTest deleted successfully"}

    
@app.post("/projects/{project_id}/cyclic-tests/{cyclic_test_index}/trials", response_model=CyclicTestResultSchema)
def create_cyclic_test_trial(project_id: int, cyclic_test_index: int, trial_data: CyclicTestResultCreateSchema, db: Session = Depends(get_db)):
    cyclic_test : CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="CyclicTest not found")
    
    cyclic_test.resume = False
    
    db.commit()
    db.refresh(cyclic_test)
    
    new_trial = CyclicTestResult(
        cyclic_test_id=cyclic_test.id,
        trial_number=len(cyclic_test.trials)+1,
        result=None,
        note=None,
        # See the static-load endpoint above. "Cycles" is the contract §4.3
        # option name for this test type; the LabOS word is "cyclic".
        **attempts.begin(
            cyclic_test.trials,
            test_type="Cycles",
            test_name=cyclic_test.airtable_section_name,
        ),
    )

    db.add(new_trial)
    db.flush()
    
    for d in trial_data.deflections:
        new_deflection = Deflection(
            deflection_gauge=d.deflection_gauge,
            max_deflection=d.max_deflection,
            permanent_deflection=d.permanent_deflection,
            recovery=d.recovery,
            test_id=new_trial.id,
        )
        db.add(new_deflection)  
        
    db.commit()
    db.refresh(new_trial)
    return new_trial


@app.get("/projects/{project_id}/cyclic-tests/{cyclic_test_index}/trials", response_model=List[CyclicTestResultSchema])
def get_cyclic_test_trials(project_id: int, cyclic_test_index: int, db: Session = Depends(get_db)):
    cyclic_test : CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="CyclicTest not found")
    return cyclic_test.trials




@app.get("/projects/{project_id}/next-cyclic-test", response_model=CyclicTestSchema)
def get_next_cyclic_test(project_id: int, db: Session = Depends(get_db)):
    project : Project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    all_tests : List[CyclicTest] = project.cyclic_tests
    all_tests.sort(key=lambda v : v.index)
    for t in all_tests:
        if t.finished: continue
        else: return t
    return HTTPException(status_code=404, detail="No Test Available")


@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_index}/start", response_model=CyclicTestSchema)
def start_cyclic_test(project_id: int, cyclic_test_index: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")
    if cyclic_test.finished : raise HTTPException(status_code=400, detail="Already finished")
    cyclic_test.resume = True
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_index}/update_status", response_model=CyclicTestSchema)
def update_cyclic_test_status(project_id: int, cyclic_test_index: int, data: CyclicTestUpdateStatusSchema , db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test:CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")

    cyclic_test.current_cycle = data.current_cycle
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test

@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_index}/reset", response_model=CyclicTestSchema)
def reset_cyclic_test_status(project_id: int, cyclic_test_index: int , db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test:CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")

    cyclic_test.current_cycle = 0
    cyclic_test.resume = False
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test


@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_index}/finish", response_model=CyclicTestSchema)
def finish_cyclic_test(project_id: int, cyclic_test_index: int, db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test : CyclicTest = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")

    cyclic_test.finished = True
    cyclic_test.resume = False
    cyclic_test.current_cycle = 0
    db.commit()
    db.refresh(cyclic_test)
    return cyclic_test


@app.put("/devices/{master_id}/turbo_master", response_model=DeviceSchema)
def enable_turbo(master_id: int, command: DeviceTurboMaster, db: Session = Depends(get_db)):
    db_master:Device = db.query(Device).filter(Device.id == master_id).first()
    db_slave:Device = db.query(Device).filter(Device.id == command.slave_id).first()
    if not db_master or not db_slave:
        raise HTTPException(status_code=404, detail="Device not found")
    # Update cyclic tests
    if not db_slave.turbo_slave:
        raise HTTPException(status_code=400, detail="Device is not slave")
    db_master.turbo_mode = command.turbo_mode
    if command.turbo_mode:
        db_master.turbo_charger = db_slave.id
    else:
        db_master.turbo_charger = None
        db_slave.turbo_slave = False
    db.commit()
    db.refresh(db_master)
    db.refresh(db_slave)
    return db_master



@app.put("/devices/{slave_id}/turbo_slave", response_model=DeviceSchema)
def set_turbo_slave(slave_id: int,command: DeviceTurboSlave, db: Session = Depends(get_db)):
    db_slave:Device = db.query(Device).filter(Device.id == slave_id).first()
    if  not db_slave:
        raise HTTPException(status_code=404, detail="Device not found")
    # Update cyclic tests
    if command.slave_mode:
        if  db_slave.turbo_slave:
            raise HTTPException(status_code=400, detail="Device is already slave")
        db_slave.turbo_slave = True
    else:
        db_master:Device = db.query(Device).filter(Device.turbo_charger == slave_id).first()
        if db_master:
            raise HTTPException(status_code=400, detail=f"This device is already attached to {db_master.name}")
        else:
            db_slave.turbo_slave = False
    db.commit()
    db.refresh(db_slave)
    db.refresh(db_slave)
    return db_slave


@app.get("/test-results/{test_result_id}", response_model=TestResultResponseSchema)
def get_test_result(test_result_id: int, db: Session = Depends(get_db)):
    """
    Get a test result by ID.
    """
    test_result = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not test_result:
        raise HTTPException(status_code=404, detail="Test result not found")
    
    return test_result


@app.put("/test-results/{test_result_id}", response_model=TestResultResponseSchema)
async def update_test_result(
    test_result_id: int,
    note: str = Form(None),
    image: UploadFile = File(None),
    db: Session = Depends(get_db)
):
    """
    Update a test result with note and/or image.
    The image will be saved to a local directory and the path will be stored in the database.
    """
    # Find the test result
    test_result = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not test_result:
        raise HTTPException(status_code=404, detail="Test result not found")
    
    # Create uploads directory if it doesn't exist
    upload_dir = Path("uploads")
    upload_dir.mkdir(exist_ok=True)
    
    # Handle image upload
    image_path = None
    if image:
        # Generate unique filename
        file_extension = Path(image.filename).suffix if image.filename else ".jpg"
        unique_filename = f"{uuid.uuid4()}{file_extension}"
        file_path = upload_dir / unique_filename
        
        # Save the file
        try:
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(image.file, buffer)
            image_path = str(file_path)
        except Exception as e:
            logger.error(f"Error saving image: {e}")
            raise HTTPException(status_code=500, detail="Error saving image")
    
    # Update the test result
    if note is not None:
        test_result.note = note
    if image_path:
        test_result.image_path = image_path
    
    db.commit()
    db.refresh(test_result)
    
    return test_result




@app.get("/project-parents", response_model=List[ProjectParentSchema])
def get_project_parents(db: Session = Depends(get_db)):
    all_parents = db.query(ProjectParent).all()
    # Sort by id descending to get most recent first
    sorted_by_id = sorted(all_parents, key=lambda x: x.id, reverse=True)
    # Get the two most recent
    most_recent = sorted_by_id[:2]
    # Get the rest and sort alphabetically by name
    rest = sorted_by_id[2:]
    rest_sorted = sorted(rest, key=lambda x: x.name)
    # Combine: most recent two first, then rest alphabetically
    return most_recent + rest_sorted

@app.post("/project-parents", response_model=ProjectParentSchema)
def create_project_parent(project_parent: ProjectParentCreateSchema, db: Session = Depends(get_db)):
    try:
        db_project_parent = ProjectParent(name=project_parent.name)
        db.add(db_project_parent)
        db.commit()
        db.refresh(db_project_parent)
        return db_project_parent
    except IntegrityError as e:
        db.rollback()
        if "UNIQUE constraint failed" in str(e) or "duplicate key value" in str(e):
            raise HTTPException(status_code=400, detail=f"Project parent with name '{project_parent.name}' already exists")
        else:
            raise HTTPException(status_code=400, detail="Database constraint violation")






@app.get("/projects/{project_id}/report")
async def download_specimen_report(project_id: int, db: Session = Depends(get_db)):
    """
    Download comprehensive test report for all specimens under the project parent
    Note: When user selects any specimen, report includes ALL specimens in that project parent
    """
    from sqlalchemy.orm import joinedload
    
    # Get the project to find its parent
    db_project = db.query(Project).filter(Project.id == project_id).first()
    
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # If project has no parent, treat it as a standalone project
    if not db_project.parent_id:
        raise HTTPException(status_code=400, detail="Project has no parent. Cannot generate comprehensive report.")
    
    # Get the project parent
    project_parent = db.query(ProjectParent).filter(ProjectParent.id == db_project.parent_id).first()
    
    if not project_parent:
        raise HTTPException(status_code=404, detail="Project parent not found")
    
    # Get ALL specimens for this parent with all their test data eagerly loaded
    specimens = db.query(Project).filter(Project.parent_id == db_project.parent_id)\
        .options(
            joinedload(Project.device),
            joinedload(Project.static_tests).joinedload(StaticTest.trials).joinedload(StaticTestResult.deflections),
            joinedload(Project.cyclic_tests).joinedload(CyclicTest.trials).joinedload(CyclicTestResult.deflections),
            joinedload(Project.infiltration_tests),
            joinedload(Project.missile_impact_tests).joinedload(MissileImpactTest.shots)
        ).all()
    
    if not specimens:
        raise HTTPException(status_code=404, detail="No specimens found for this project parent")
    
    # Build comprehensive report data for ALL specimens
    report_data = {
        'project_parent': {
            'id': project_parent.id,
            'name': project_parent.name
        },
        'specimens': []
    }
    
    # Collect data for each specimen
    for specimen in specimens:
        specimen_data = {
            'id': specimen.id,
            'name': specimen.name,
            'inward_design_pressure': specimen.inward_design_pressure,
            'outward_design_pressure': specimen.outward_design_pressure,
            'device': {
                'id': specimen.device.id,
                'name': specimen.device.name,
                'turbo_mode': specimen.device.turbo_mode,
                'turbo_slave': specimen.device.turbo_slave
            },
            'static_tests': [],
            'cyclic_tests': [],
            'infiltration_tests': [],
            'missile_impact_tests': []
        }
        
        # Collect static test data
        for static_test in specimen.static_tests:
            test_data = {
                'id': static_test.id,
                'index': static_test.index,
                'type': static_test.type,
                'pressure': static_test.pressure,
                'pressure_factor': static_test.pressure_factor,
                'duration': static_test.duration,
                'finished': static_test.finished,
                'trials': []
            }
            
            for trial in static_test.trials:
                trial_data = {
                    'trial_number': trial.trial_number,
                    'result': trial.result,
                    'note': trial.note,
                    'image_path': trial.image_path,
                    'deflections': []
                }
                
                for deflection in trial.deflections:
                    trial_data['deflections'].append({
                        'gauge': deflection.deflection_gauge,
                        'max_deflection': deflection.max_deflection,
                        'permanent_deflection': deflection.permanent_deflection,
                        'recovery': deflection.recovery
                    })
                
                test_data['trials'].append(trial_data)
            
            specimen_data['static_tests'].append(test_data)
        
        # Collect cyclic test data
        for cyclic_test in specimen.cyclic_tests:
            test_data = {
                'id': cyclic_test.id,
                'index': cyclic_test.index,
                'type': cyclic_test.type,
                'cycles': cyclic_test.cycles,
                'low_pressure': cyclic_test.low_pressure,
                'high_pressure': cyclic_test.high_pressure,
                'current_cycle': cyclic_test.current_cycle,
                'finished': cyclic_test.finished,
                'trials': []
            }
            
            for trial in cyclic_test.trials:
                trial_data = {
                    'trial_number': trial.trial_number,
                    'result': trial.result,
                    'note': trial.note,
                    'image_path': trial.image_path,
                    'deflections': []
                }
                
                for deflection in trial.deflections:
                    trial_data['deflections'].append({
                        'gauge': deflection.deflection_gauge,
                        'max_deflection': deflection.max_deflection,
                        'permanent_deflection': deflection.permanent_deflection,
                        'recovery': deflection.recovery
                    })
                
                test_data['trials'].append(trial_data)
            
            specimen_data['cyclic_tests'].append(test_data)
        
        # Collect infiltration test data
        for inf_test in specimen.infiltration_tests:
            specimen_data['infiltration_tests'].append({
                'id': inf_test.id,
                'type': inf_test.type,
                'pressure': inf_test.pressure,
                'duration': inf_test.duration,
                'leakage': inf_test.leakage
            })
        
        # Collect missile impact test data
        for missile_test in specimen.missile_impact_tests:
            test_data = {
                'id': missile_test.id,
                'missile': missile_test.missile,
                'missile_weight': missile_test.missile_weight,
                'shots': []
            }
            
            for shot in missile_test.shots:
                test_data['shots'].append({
                    'area': shot.area,
                    'velocity': shot.velocity,
                    'result': shot.result,
                    'note': shot.note
                })
            
            specimen_data['missile_impact_tests'].append(test_data)
        
        report_data['specimens'].append(specimen_data)
    
    # Create temporary file for the PDF
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        pdf_path = create_test_report_pdf(report_data, temp_file.name)
    
    # Return the PDF as a file response with inline disposition
    return FileResponse(
        path=pdf_path,
        filename=f"project_{project_id}_report.pdf",
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=project_{project_id}_report.pdf"}
    )


@app.get("/project-parents/{parent_id}/report")
async def download_project_parent_report(parent_id: int, db: Session = Depends(get_db)):
    """
    Download comprehensive test report for a project parent including all specimens
    """
    from sqlalchemy.orm import joinedload
    
    # Get the project parent with all related data
    project_parent = db.query(ProjectParent).filter(ProjectParent.id == parent_id).first()
    
    if not project_parent:
        raise HTTPException(status_code=404, detail="Project parent not found")
    
    # Get all projects (specimens) for this parent with all their test data eagerly loaded
    specimens = db.query(Project).filter(Project.parent_id == parent_id)\
        .options(
            joinedload(Project.device),
            joinedload(Project.static_tests).joinedload(StaticTest.trials).joinedload(StaticTestResult.deflections),
            joinedload(Project.cyclic_tests).joinedload(CyclicTest.trials).joinedload(CyclicTestResult.deflections),
            joinedload(Project.infiltration_tests),
            joinedload(Project.missile_impact_tests).joinedload(MissileImpactTest.shots)
        ).all()
    
    if not specimens:
        raise HTTPException(status_code=404, detail="No specimens found for this project parent")
    
    # Prepare comprehensive data structure
    report_data = {
        'project_parent': {
            'id': project_parent.id,
            'name': project_parent.name
        },
        'specimens': []
    }
    
    # Collect data for each specimen
    for specimen in specimens:
        specimen_data = {
            'id': specimen.id,
            'name': specimen.name,
            'inward_design_pressure': specimen.inward_design_pressure,
            'outward_design_pressure': specimen.outward_design_pressure,
            'device': {
                'id': specimen.device.id,
                'name': specimen.device.name,
                'turbo_mode': specimen.device.turbo_mode,
                'turbo_slave': specimen.device.turbo_slave
            },
            'static_tests': [],
            'cyclic_tests': [],
            'infiltration_tests': [],
            'missile_impact_tests': []
        }
        
        # Collect static test data
        for static_test in specimen.static_tests:
            test_data = {
                'id': static_test.id,
                'index': static_test.index,
                'type': static_test.type,
                'pressure': static_test.pressure,
                'pressure_factor': static_test.pressure_factor,
                'duration': static_test.duration,
                'finished': static_test.finished,
                'trials': []
            }
            
            for trial in static_test.trials:
                trial_data = {
                    'trial_number': trial.trial_number,
                    'result': trial.result,
                    'note': trial.note,
                    'image_path': trial.image_path,
                    'deflections': []
                }
                
                for deflection in trial.deflections:
                    trial_data['deflections'].append({
                        'gauge': deflection.deflection_gauge,
                        'max_deflection': deflection.max_deflection,
                        'permanent_deflection': deflection.permanent_deflection,
                        'recovery': deflection.recovery
                    })
                
                test_data['trials'].append(trial_data)
            
            specimen_data['static_tests'].append(test_data)
        
        # Collect cyclic test data
        for cyclic_test in specimen.cyclic_tests:
            test_data = {
                'id': cyclic_test.id,
                'index': cyclic_test.index,
                'type': cyclic_test.type,
                'cycles': cyclic_test.cycles,
                'low_pressure': cyclic_test.low_pressure,
                'high_pressure': cyclic_test.high_pressure,
                'current_cycle': cyclic_test.current_cycle,
                'finished': cyclic_test.finished,
                'trials': []
            }
            
            for trial in cyclic_test.trials:
                trial_data = {
                    'trial_number': trial.trial_number,
                    'result': trial.result,
                    'note': trial.note,
                    'image_path': trial.image_path,
                    'deflections': []
                }
                
                for deflection in trial.deflections:
                    trial_data['deflections'].append({
                        'gauge': deflection.deflection_gauge,
                        'max_deflection': deflection.max_deflection,
                        'permanent_deflection': deflection.permanent_deflection,
                        'recovery': deflection.recovery
                    })
                
                test_data['trials'].append(trial_data)
            
            specimen_data['cyclic_tests'].append(test_data)
        
        # Collect infiltration test data
        for inf_test in specimen.infiltration_tests:
            specimen_data['infiltration_tests'].append({
                'id': inf_test.id,
                'type': inf_test.type,
                'pressure': inf_test.pressure,
                'duration': inf_test.duration,
                'leakage': inf_test.leakage
            })
        
        # Collect missile impact test data
        for missile_test in specimen.missile_impact_tests:
            test_data = {
                'id': missile_test.id,
                'missile': missile_test.missile,
                'missile_weight': missile_test.missile_weight,
                'shots': []
            }
            
            for shot in missile_test.shots:
                test_data['shots'].append({
                    'area': shot.area,
                    'velocity': shot.velocity,
                    'result': shot.result,
                    'note': shot.note
                })
            
            specimen_data['missile_impact_tests'].append(test_data)
        
        report_data['specimens'].append(specimen_data)
    
    # Create temporary file for the PDF
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        pdf_path = create_test_report_pdf(report_data, temp_file.name)
    
    # Return the PDF as a file response with inline disposition
    return FileResponse(
        path=pdf_path,
        filename=f"project_parent_{parent_id}_report.pdf",
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=project_parent_{parent_id}_report.pdf"}
    )

# ============================================================================
# Manual test capture — Impact, Forced Entry, ANSI Z97.1
# ============================================================================
#
# Delivery plan §4.5. None of these three touches the rig: no VFD, no valves,
# no pressure setpoint, no stage trials. `state_machine` accepts exactly one
# command — `start`, mode `manual` or `cyclic` — and none of these is either,
# so the firmware is not involved and needs no release.
#
# Three phases, matching the write contract: create → finish → verdict.
#
#   create   allocates identity, sets Test Result = Pending
#   finish   records the operator's outcome and freezes the evidence
#   verdict  a NAMED REVIEWER records Pass/Fail/Inconclusive, once
#
# The operator's `result` boolean and the reviewer's `test_result` are
# deliberately different columns. They are different people and different
# moments, and collapsing them is what lets an operator certify their own work.
#
# Nothing in this section imports `app.airtable` or `app.sync`. `report-api`
# never calls Airtable; results reach it through the outbox, and
# `tests/test_report_api_isolation.py` enforces that.

MANUAL_TEST_TYPES = ("Forced Entry", "ANSI Z97.1")
_IN_PROGRESS, _COMPLETED, _ABORTED = "In Progress", "Completed", "Aborted"
_PENDING = "Pending"


def _utcnow():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _next_attempt_number(db, model, project_id, **extra):
    """Server-side allocation. A client that chose its own could overwrite one.

    Every attempt is retained — the product owner's requirement is that the same
    test may be attempted more than once and LabOS keeps them all — so this
    counts existing attempts rather than replacing them.
    """
    q = db.query(model).filter(model.project_id == project_id)
    for k, v in extra.items():
        q = q.filter(getattr(model, k) == v)
    return q.count() + 1


def _require_project(db, project_id):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_open(test, what):
    if test.status != _IN_PROGRESS:
        raise HTTPException(
            status_code=400,
            detail=f"{what} is already {test.status}; evidence is frozen. "
                   "A further result requires a new attempt.")


def _apply_verdict(db, test, verdict, what):
    """The first review. Once, with a name and a time on it.

    Refused on an attempt still In Progress: contract §4 freezes evidence on
    termination and reviews it afterwards. Refused twice: the first verdict
    stands, and a change is a correction — a new attempt referring to this one,
    never an edit of it.
    """
    if test.status == _IN_PROGRESS:
        raise HTTPException(
            status_code=400,
            detail=f"{what} has not been finished; a verdict cannot precede termination.")
    if test.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{what} was already reviewed by {test.verdict_by!r} at "
                   f"{test.verdict_at.isoformat()}. The first verdict stands; record a "
                   "correction as a new attempt instead.")
    test.test_result = verdict.test_result
    test.verdict_by = verdict.verdict_by
    test.verdict_at = _utcnow()
    test.retest_required = verdict.retest_required
    if verdict.rationale:
        test.note = f"{test.note}\n\n[review] {verdict.rationale}" if test.note \
            else f"[review] {verdict.rationale}"
    db.commit()
    db.refresh(test)
    return test


def _save_photo(db, upload, note, **owner):
    suffix = Path(upload.filename or "").suffix or ".jpg"
    stored = f"{uuid.uuid4()}{suffix}"
    dest = uploads_dir / stored
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    photo = TestPhoto(filename=upload.filename or stored, path=str(dest),
                      note=note, created_at=_utcnow(), **owner)
    db.add(photo)
    db.commit()
    db.refresh(photo)
    return photo


# ---------------------------------------------- Forced Entry / ANSI Z97.1 ---

@app.post("/projects/{project_id}/manual-tests/", response_model=ManualTestSchema)
def create_manual_test(project_id: int, body: ManualTestCreateSchema,
                       db: Session = Depends(get_db)):
    _require_project(db, project_id)
    test = ManualTest(
        project_id=project_id,
        type=body.type,
        required_option=body.required_option,
        operator_name=body.operator_name,
        airtable_protocol_id=body.airtable_protocol_id,
        airtable_section_id=body.airtable_section_id,
        airtable_section_name=body.airtable_section_name,
        labos_attempt_id=str(uuid.uuid4()),
        attempt_number=_next_attempt_number(db, ManualTest, project_id, type=body.type),
        status=_IN_PROGRESS,
        test_result=_PENDING,
        testing_start_date=_utcnow(),
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    logger.info("manual test %s attempt %s started on project %s",
                test.type, test.attempt_number, project_id)
    return test


@app.get("/projects/{project_id}/manual-tests/", response_model=List[ManualTestSchema])
def list_manual_tests(project_id: int, type: Optional[str] = None,
                      db: Session = Depends(get_db)):
    _require_project(db, project_id)
    q = db.query(ManualTest).filter(ManualTest.project_id == project_id)
    if type:
        q = q.filter(ManualTest.type == type)
    return q.order_by(ManualTest.id).all()


@app.put("/manual-tests/{test_id}/finish", response_model=ManualTestSchema)
def finish_manual_test(test_id: int, body: ManualTestFinishSchema,
                       db: Session = Depends(get_db)):
    test = db.query(ManualTest).filter(ManualTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Manual test not found")
    _require_open(test, "This manual test")

    # Completion is explicit or it is an abort with a reason. Never inferred
    # from a timeout or a disconnect: missing telemetry is not a pass.
    if body.abort_reason:
        test.status = _ABORTED
        test.abort_reason = body.abort_reason
    else:
        if body.result is None:
            raise HTTPException(
                status_code=400,
                detail="A completed Forced Entry or ANSI Z97.1 test records pass or fail. "
                       "Supply `result`, or `abort_reason` to abandon the attempt.")
        test.status = _COMPLETED
        test.result = body.result

    test.note = body.note or test.note
    test.testing_continued = body.testing_continued
    test.testing_end_date = _utcnow()
    # Test Result stays Pending — the verdict is the reviewer's, not this call's.
    db.commit()
    db.refresh(test)
    return test


@app.put("/manual-tests/{test_id}/verdict", response_model=ManualTestSchema)
def review_manual_test(test_id: int, body: VerdictSchema, db: Session = Depends(get_db)):
    test = db.query(ManualTest).filter(ManualTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Manual test not found")
    return _apply_verdict(db, test, body, "This manual test")


@app.post("/manual-tests/{test_id}/photos", response_model=PhotoSchema)
def add_manual_test_photo(test_id: int, file: UploadFile = File(...),
                          note: Optional[str] = Form(None),
                          db: Session = Depends(get_db)):
    test = db.query(ManualTest).filter(ManualTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Manual test not found")
    if test.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This attempt has been reviewed; its evidence is frozen. Adding "
                   "substantive evidence afterwards requires a correction.")
    return _save_photo(db, file, note, manual_test_id=test.id)


# ------------------------------------------------------- Missile Impact ---
#
# `missile_impact_tests` and `shots` already exist and already hold production
# data — 39 tests and 114 shots — but there has never been a create route:
# Impact was write-by-report-generation only. These add the capture path.

@app.post("/projects/{project_id}/impact-tests/", response_model=ImpactTestSchema)
def create_impact_test(project_id: int, body: ImpactTestCreateSchema,
                       db: Session = Depends(get_db)):
    _require_project(db, project_id)
    test = MissileImpactTest(
        project_id=project_id,
        missile=body.missile,
        missile_weight=body.missile_weight,
        operator_name=body.operator_name,
        airtable_protocol_id=body.airtable_protocol_id,
        airtable_section_id=body.airtable_section_id,
        airtable_section_name=body.airtable_section_name,
        labos_attempt_id=str(uuid.uuid4()),
        attempt_number=_next_attempt_number(db, MissileImpactTest, project_id),
        status=_IN_PROGRESS,
        test_result=_PENDING,
        testing_start_date=_utcnow(),
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


@app.get("/projects/{project_id}/impact-tests/", response_model=List[ImpactTestSchema])
def list_impact_tests(project_id: int, db: Session = Depends(get_db)):
    _require_project(db, project_id)
    return (db.query(MissileImpactTest)
            .filter(MissileImpactTest.project_id == project_id)
            .order_by(MissileImpactTest.id).all())


@app.post("/impact-tests/{test_id}/shots", response_model=ShotDetailSchema)
def record_shot(test_id: int, body: ShotRecordSchema, db: Session = Depends(get_db)):
    """Record one impact — numbered, with its outcome.

    An impact test is a sequence: impact 1, impact 2, impact 3. The number is
    allocated here rather than accepted from the client, because a client that
    chose its own could number two impacts the same or renumber a sequence
    someone has already photographed.

    The business shape is "how many impacts and whether each passed". Area and
    velocity are optional because the protocol fixes them; requiring them per
    impact was retyping, not data capture. Photographs attach afterwards, per
    impact, via `POST /shots/{shot_id}/photos`.
    """
    test = db.query(MissileImpactTest).filter(MissileImpactTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Impact test not found")
    _require_open(test, "This impact test")

    next_number = (db.query(Shot)
                   .filter(Shot.missile_impact_test_id == test.id).count()) + 1
    shot = Shot(missile_impact_test_id=test.id, shot_number=next_number,
                result=body.result, area=body.area, velocity=body.velocity,
                note=body.note)
    db.add(shot)
    db.commit()
    db.refresh(shot)
    logger.info("impact test %s: impact %s recorded as %s",
                test.id, shot.shot_number, "pass" if shot.result else "fail")
    return shot


@app.get("/impact-tests/{test_id}/shots", response_model=List[ShotDetailSchema])
def list_shots(test_id: int, db: Session = Depends(get_db)):
    """The impacts in order, each with its value and its photographs."""
    test = db.query(MissileImpactTest).filter(MissileImpactTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Impact test not found")
    return (db.query(Shot).filter(Shot.missile_impact_test_id == test.id)
            .order_by(Shot.shot_number).all())


@app.post("/shots/{shot_id}/photos", response_model=PhotoSchema)
def add_shot_photo(shot_id: int, file: UploadFile = File(...),
                   note: Optional[str] = Form(None),
                   db: Session = Depends(get_db)):
    """Attach a photograph to one specific impact.

    Distinct from the attempt-level route: "impact 3 cracked the corner" needs
    the photograph tied to impact 3, not to the attempt. An attempt-level
    photograph is still available via `/impact-tests/{id}/photos` and is what
    Forced Entry and ANSI Z97.1 use.
    """
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Impact not found")
    test = shot.missile_impact_test
    if test is not None and test.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This attempt has been reviewed; its evidence is frozen. Adding "
                   "substantive evidence afterwards requires a correction.")
    return _save_photo(db, file, note, shot_id=shot.id,
                       missile_impact_test_id=test.id if test else None)


@app.put("/impact-tests/{test_id}/finish", response_model=ImpactTestSchema)
def finish_impact_test(test_id: int, body: ManualTestFinishSchema,
                       db: Session = Depends(get_db)):
    test = db.query(MissileImpactTest).filter(MissileImpactTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Impact test not found")
    _require_open(test, "This impact test")

    if body.abort_reason:
        test.status = _ABORTED
        test.abort_reason = body.abort_reason
    else:
        if not test.shots:
            raise HTTPException(
                status_code=400,
                detail="A completed impact test records at least one impact. Post a shot, "
                       "or supply `abort_reason` to abandon the attempt.")
        # Impact requires photographic evidence, and it is checked HERE rather
        # than in the outbound payload. Attachments are their own delivery
        # channel and may settle after the row is published (write contract §6),
        # so making the upload a precondition for publishing would let a queued
        # file block a measured result. The requirement belongs where the
        # operator is.
        if not test.photos:
            raise HTTPException(
                status_code=400,
                detail="A completed impact test requires at least one photograph. "
                       "Evidence cannot be added once the attempt is reviewed.")
        test.status = _COMPLETED

    test.note = body.note or test.note
    test.testing_continued = body.testing_continued
    test.testing_end_date = _utcnow()
    db.commit()
    db.refresh(test)
    return test


@app.put("/impact-tests/{test_id}/verdict", response_model=ImpactTestSchema)
def review_impact_test(test_id: int, body: VerdictSchema, db: Session = Depends(get_db)):
    test = db.query(MissileImpactTest).filter(MissileImpactTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Impact test not found")
    return _apply_verdict(db, test, body, "This impact test")


@app.post("/impact-tests/{test_id}/photos", response_model=PhotoSchema)
def add_impact_photo(test_id: int, file: UploadFile = File(...),
                     note: Optional[str] = Form(None),
                     db: Session = Depends(get_db)):
    test = db.query(MissileImpactTest).filter(MissileImpactTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Impact test not found")
    if test.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This attempt has been reviewed; its evidence is frozen. Adding "
                   "substantive evidence afterwards requires a correction.")
    return _save_photo(db, file, note, missile_impact_test_id=test.id)
