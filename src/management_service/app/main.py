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
    
    
    _test_id = attempts.test_id_for_test(attempts.STATIC, static_test.id)

    def _build_trial():
        return StaticTestResult(
            static_test_id=static_test.id,
            # Numbered under UniqueConstraint(labos_test_id, trial_number), and
            # re-read on each try: `len(trials)+1` was unprotected against a
            # concurrent start, and the rig posting a trial while the UI posts
            # one is the case plan §4.3 says actually happens here.
            trial_number=attempts.next_attempt_number(db, _test_id),
            result=None,
            note=None,
            # P1 / Ref 46 — an attempt is born with its Airtable identity and
            # in the In Progress state, not stamped later. The merge key has to
            # exist before anything can reference the attempt, and
            # `labos_test_id` is DERIVED from (kind, test id) so a retest joins
            # its own group without depending on a sibling being present — see
            # `attempts.test_id_for_test`.
            **attempts.begin(
                static_test.trials,
                test_type="Static Load",
                test_name=static_test.airtable_section_name,
                kind=attempts.STATIC, parent_id=static_test.id,
            ),
        )

    new_trial = attempts.insert_attempt(db, _build_trial)
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
    
    _test_id = attempts.test_id_for_test(attempts.CYCLIC, cyclic_test.id)

    def _build_trial():
        return CyclicTestResult(
            cyclic_test_id=cyclic_test.id,
            # Numbered under UniqueConstraint(labos_test_id, trial_number), and
            # re-read on each try: `len(trials)+1` was unprotected against a
            # concurrent start, and the rig posting a trial while the UI posts
            # one is the case plan §4.3 says actually happens here.
            trial_number=attempts.next_attempt_number(db, _test_id),
            result=None,
            note=None,
            # See the static-load endpoint above. "Cycles" is the contract §4.3
            # option name for this test type; the LabOS word is "cyclic".
            **attempts.begin(
                cyclic_test.trials,
                test_type="Cycles",
                test_name=cyclic_test.airtable_section_name,
                kind=attempts.CYCLIC, parent_id=cyclic_test.id,
            ),
        )

    new_trial = attempts.insert_attempt(db, _build_trial)
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
    
    # Use the configured uploads directory, not a second hard-coded one.
    # This was `Path("uploads")` with its own mkdir until 2026-09-08 — a
    # duplicate of the module-level path that would silently diverge from it the
    # moment LABOS_UPLOADS_DIR was set, writing evidence somewhere the static
    # mount does not serve.
    upload_dir = uploads_dir
    
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
# Delivery plan §4.5, following the shape this codebase already uses rather than
# inventing one:
#
#   test row              attempt row                      children
#   ─────────────────     ──────────────────────────────   ────────────
#   static_tests    ──►   static_test_results (TestResult)  deflections
#   cyclic_tests    ──►   cyclic_test_results (TestResult)  deflections
#   manual_tests    ──►   manual_test_results (TestResult)
#   missile_impact_tests ─► impact_test_results (TestResult) shots
#
# So the convention is: **create the test, then start an attempt on it** —
# `POST …/trials` — exactly as static and cyclic record theirs.
#
# Everything that operates on an *attempt* lives on `/test-results/{id}`, which
# is already the polymorphic route for all five types. Terminating, reviewing
# and attaching evidence are the same business whatever the test type, so they
# are one route each, not one per type. That also means static and cyclic
# inherit review and evidence the day they need it, with no new endpoints.
#
# None of these three tests touches the rig: no VFD, no valves, no setpoint, no
# MQTT. `state_machine` accepts one command — `start`, mode `manual` or
# `cyclic` — and none of these is either, so the firmware needs no release.
#
# Nothing here imports `app.airtable` or `app.sync`; results reach Airtable
# through `sync_outbox`, drained by the separate worker.

MANUAL_TEST_TYPES = ("Forced Entry", "ANSI Z97.1")
_IN_PROGRESS, _COMPLETED, _ABORTED = "In Progress", "Completed", "Aborted"
_PENDING = "Pending"


def _utcnow():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


def _require_project(db, project_id):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _require_test(db, model, project_id, test_id, what):
    _require_project(db, project_id)
    test = db.query(model).filter(model.id == test_id,
                                  model.project_id == project_id).first()
    if not test:
        raise HTTPException(status_code=404, detail=f"{what} not found")
    return test


def _start_attempt(db, cls, test, test_type, operator_name, **link):
    """Begin an attempt. `trial_number` already means Attempt Number (§4.1).

    Allocated server-side — a client that chose its own could number two
    attempts the same — and now allocated **under a uniqueness constraint**, so
    two simultaneous starts produce attempts 2 and 3 rather than two attempt 2s.
    Every attempt is retained rather than overwritten.
    """
    if test.finished:
        raise HTTPException(
            status_code=400,
            detail="This test is finished. Reopen it or create a new one to test again.")
    test_id = attempts.test_id_for_test(_kind_of(test), test.id)

    def build():
        return cls(
            trial_number=attempts.next_attempt_number(db, test_id),
            test_type=test_type,
            test_name=test.airtable_section_name,
            status=_IN_PROGRESS,
            test_result=_PENDING,
            operator_name=operator_name,
            testing_start_date=_utcnow(),
            labos_test_id=test_id,
            schema_version=None,
            **link,
        )

    attempt = attempts.insert_attempt(db, build)
    db.commit()
    db.refresh(attempt)
    return attempt


def _kind_of(test):
    """Which `attempts` kind token this parent test row is.

    Keyed on the table rather than on `test_type`, because `manual_tests` holds
    both Forced Entry and ANSI Z97.1 — two test types, one parent table, and it
    is the table that `labos_test_id` must be derived from.
    """
    return (attempts.IMPACT if test.__tablename__ == "missile_impact_tests"
            else attempts.MANUAL)


def _impact_attempt(db, attempt_id):
    """The `ImpactTestResult` for this attempt id, or None.

    Queried explicitly rather than by `isinstance`: `TestResult` has no
    polymorphic discriminator — neither `StaticTestResult` nor
    `CyclicTestResult` declares one — so a query against the base returns a base
    instance whatever subclass row exists beside it.
    """
    return (db.query(ImpactTestResult)
            .filter(ImpactTestResult.id == attempt_id).first())


def _require_open_attempt(db, attempt_id):
    attempt = db.query(TestResult).filter(TestResult.id == attempt_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status in (_COMPLETED, _ABORTED):
        raise HTTPException(
            status_code=400,
            detail=f"This attempt is already {attempt.status}; its evidence is frozen. "
                   "A further result requires a new attempt.")
    return attempt


# ------------------------------------------------- Forced Entry / ANSI ---

@app.post("/projects/{project_id}/manual-tests/", response_model=ManualTestSchema)
def create_manual_test(project_id: int, body: ManualTestCreateSchema,
                       db: Session = Depends(get_db)):
    """Create the test. Attempts are started separately, via `…/trials`."""
    _require_project(db, project_id)
    test = ManualTest(
        project_id=project_id, type=body.type,
        required_option=body.required_option,
        airtable_protocol_id=body.airtable_protocol_id,
        airtable_section_id=body.airtable_section_id,
        airtable_section_name=body.airtable_section_name,
        finished=False,
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


@app.get("/projects/{project_id}/manual-tests/", response_model=List[ManualTestSchema])
def list_manual_tests(project_id: int, type: Optional[str] = None,
                      db: Session = Depends(get_db)):
    _require_project(db, project_id)
    q = db.query(ManualTest).filter(ManualTest.project_id == project_id)
    if type:
        q = q.filter(ManualTest.type == type)
    return q.order_by(ManualTest.id).all()


@app.post("/projects/{project_id}/manual-tests/{test_id}/trials",
          response_model=AttemptSchema)
def start_manual_test_attempt(project_id: int, test_id: int,
                              body: AttemptStartSchema,
                              db: Session = Depends(get_db)):
    """Start an attempt — the operator pressing the button."""
    test = _require_test(db, ManualTest, project_id, test_id, "Manual test")
    return _start_attempt(db, ManualTestResult, test, test.type,
                          body.operator_name, manual_test_id=test.id)


@app.get("/projects/{project_id}/manual-tests/{test_id}/trials",
         response_model=List[AttemptSchema])
def list_manual_test_attempts(project_id: int, test_id: int,
                              db: Session = Depends(get_db)):
    test = _require_test(db, ManualTest, project_id, test_id, "Manual test")
    return sorted(test.trials, key=lambda a: a.trial_number)


@app.put("/projects/{project_id}/manual-tests/{test_id}/finish",
         response_model=ManualTestSchema)
def finish_manual_test(project_id: int, test_id: int, db: Session = Depends(get_db)):
    """Mark the test itself complete, as `…/static_tests/{idx}/finish` does."""
    test = _require_test(db, ManualTest, project_id, test_id, "Manual test")
    test.finished = True
    db.commit()
    db.refresh(test)
    return test


# ------------------------------------------------------- Missile Impact ---

@app.post("/projects/{project_id}/impact-tests/", response_model=ImpactTestSchema)
def create_impact_test(project_id: int, body: ImpactTestCreateSchema,
                       db: Session = Depends(get_db)):
    """Create the test. Missile and weight are optional — the protocol fixes
    them, so requiring them per test was retyping rather than data capture."""
    _require_project(db, project_id)
    test = MissileImpactTest(
        project_id=project_id, missile=body.missile,
        missile_weight=body.missile_weight,
        airtable_protocol_id=body.airtable_protocol_id,
        airtable_section_id=body.airtable_section_id,
        airtable_section_name=body.airtable_section_name,
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


@app.post("/projects/{project_id}/impact-tests/{test_id}/trials",
          response_model=AttemptSchema)
def start_impact_test_attempt(project_id: int, test_id: int,
                              body: AttemptStartSchema,
                              db: Session = Depends(get_db)):
    test = _require_test(db, MissileImpactTest, project_id, test_id, "Impact test")
    return _start_attempt(db, ImpactTestResult, test, "Impact",
                          body.operator_name, missile_impact_test_id=test.id)


@app.get("/projects/{project_id}/impact-tests/{test_id}/trials",
         response_model=List[AttemptSchema])
def list_impact_test_attempts(project_id: int, test_id: int,
                              db: Session = Depends(get_db)):
    test = _require_test(db, MissileImpactTest, project_id, test_id, "Impact test")
    return sorted(test.trials, key=lambda a: a.trial_number)


@app.put("/projects/{project_id}/impact-tests/{test_id}/finish",
         response_model=ImpactTestSchema)
def finish_impact_test(project_id: int, test_id: int, db: Session = Depends(get_db)):
    """Mark the test itself complete, as the rig tests' `/finish` does."""
    test = _require_test(db, MissileImpactTest, project_id, test_id, "Impact test")
    test.finished = True
    db.commit()
    db.refresh(test)
    return test


# ---------------------------------------------- the attempt, any test type ---
#
# One route each. Terminating, reviewing and attaching evidence are the same
# business for all five types, so a per-type copy would be three duplicates of
# each. `/test-results/{id}` is already the polymorphic attempt route.

@app.put("/test-results/{test_result_id}/finish", response_model=AttemptSchema)
def finish_attempt(test_result_id: int, body: AttemptFinishSchema,
                   db: Session = Depends(get_db)):
    """Terminate an attempt. Explicit completion, or an abort with a reason.

    Never inferred from a timeout or a disconnect: missing telemetry is not a
    pass. `test_result` stays `Pending` — the verdict is a separate act.
    """
    attempt = _require_open_attempt(db, test_result_id)

    if body.abort_reason:
        attempt.status = _ABORTED
        attempt.abort_reason = body.abort_reason
    else:
        impact = _impact_attempt(db, attempt.id)
        if impact is not None:
            if not impact.shots:
                raise HTTPException(
                    status_code=400,
                    detail="A completed impact attempt records at least one impact. "
                           "Post a shot, or supply `abort_reason` to abandon it.")
            # Impact requires photographic evidence, and it is checked HERE
            # rather than in the outbound payload: attachments deliver on their
            # own channel and may settle after the row is published (write
            # contract §6), so making an upload a precondition for publishing
            # would let a queued file block a measured result.
            if not attempt.photos:
                raise HTTPException(
                    status_code=400,
                    detail="A completed impact attempt requires at least one "
                           "photograph. Evidence cannot be added after review.")
        elif body.result is None:
            raise HTTPException(
                status_code=400,
                detail="A completed manual attempt records pass or fail. Supply "
                       "`result`, or `abort_reason` to abandon the attempt.")
        attempt.status = _COMPLETED
        if body.result is not None:
            attempt.result = body.result

    attempt.note = body.note or attempt.note
    attempt.testing_continued = body.testing_continued
    attempt.testing_end_date = _utcnow()
    attempt.terminal_at = _utcnow()
    db.commit()
    db.refresh(attempt)
    return attempt


@app.put("/test-results/{test_result_id}/verdict", response_model=AttemptSchema)
def review_attempt(test_result_id: int, body: VerdictSchema,
                   db: Session = Depends(get_db)):
    """The first review. Once, by someone with a name.

    Refused before termination: §4 freezes evidence on termination and reviews
    it afterwards. Refused twice: the first verdict stands, and a change is a
    correction — a new attempt naming this one — never an edit of it.
    """
    attempt = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    if attempt.status not in (_COMPLETED, _ABORTED):
        raise HTTPException(
            status_code=400,
            detail="This attempt has not been finished; a verdict cannot precede "
                   "termination.")
    if attempt.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Already reviewed by {attempt.verdict_by!r} at "
                   f"{attempt.verdict_at.isoformat()}. The first verdict stands; "
                   "record a correction as a new attempt instead.")

    attempt.test_result = body.test_result
    attempt.verdict_by = body.verdict_by
    attempt.verdict_at = _utcnow()
    attempt.retest_required = body.retest_required
    if body.rationale:
        attempt.result_rationale = body.rationale
    db.commit()
    db.refresh(attempt)
    return attempt


@app.post("/test-results/{test_result_id}/photos", response_model=PhotoSchema)
def add_attempt_photo(test_result_id: int, file: UploadFile = File(...),
                      note: Optional[str] = Form(None),
                      db: Session = Depends(get_db)):
    """Attempt-level evidence — the specimen before testing, the setup."""
    attempt = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return _save_photo(db, file, note, test_result_id=attempt.id)


# ------------------------------------------------------ impacts in an attempt ---

@app.post("/test-results/{test_result_id}/shots", response_model=ShotDetailSchema)
def record_shot(test_result_id: int, body: ShotRecordSchema,
                db: Session = Depends(get_db)):
    """Record one impact — numbered, with its outcome.

    An impact test is a sequence: impact 1, 2, 3. `shot_number` is allocated
    here rather than accepted from the client, because a client that chose its
    own could number two impacts the same or renumber a sequence someone has
    already photographed.
    """
    attempt = _require_open_attempt(db, test_result_id)
    impact = _impact_attempt(db, attempt.id)
    if impact is None:
        raise HTTPException(status_code=400,
                            detail="Only an impact attempt records impacts.")
    n = db.query(Shot).filter(Shot.test_result_id == impact.id).count()
    shot = Shot(test_result_id=impact.id,
                missile_impact_test_id=impact.missile_impact_test_id,
                shot_number=n + 1, result=body.result, area=body.area,
                velocity=body.velocity, note=body.note)
    db.add(shot)
    db.commit()
    db.refresh(shot)
    return shot


@app.get("/test-results/{test_result_id}/shots", response_model=List[ShotDetailSchema])
def list_shots(test_result_id: int, db: Session = Depends(get_db)):
    """The impacts in order, each with its value and its photographs."""
    attempt = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt not found")
    return (db.query(Shot).filter(Shot.test_result_id == attempt.id)
            .order_by(Shot.shot_number).all())


@app.post("/shots/{shot_id}/photos", response_model=PhotoSchema)
def add_shot_photo(shot_id: int, file: UploadFile = File(...),
                   note: Optional[str] = Form(None),
                   db: Session = Depends(get_db)):
    """Attach a photograph to one specific impact.

    "Impact 3 cracked the corner" needs the photograph tied to impact 3. A
    per-impact photograph also counts as attempt evidence, so photographing each
    impact satisfies the finish requirement without a separate upload.
    """
    shot = db.query(Shot).filter(Shot.id == shot_id).first()
    if not shot:
        raise HTTPException(status_code=404, detail="Impact not found")
    if shot.test_result_id is None:
        raise HTTPException(status_code=400,
                            detail="This impact predates the attempt level and "
                                   "cannot take new evidence.")
    return _save_photo(db, file, note, test_result_id=shot.test_result_id,
                       shot_id=shot.id)


def _save_photo(db, upload, note, **owner):
    attempt = db.query(TestResult).filter(
        TestResult.id == owner["test_result_id"]).first()
    if attempt is not None and attempt.verdict_at is not None:
        raise HTTPException(
            status_code=409,
            detail="This attempt has been reviewed; its evidence is frozen. Adding "
                   "substantive evidence afterwards requires a correction.")
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
