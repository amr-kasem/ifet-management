import os
from typing import Optional
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker, Session
from app.data.models import *
from app.data.schema import *  # RunStartSchema included
from app.data import attempts
# The transactional-outbox seam. Persistence and payload only — it cannot
# open a socket, which `tests/test_report_api_isolation.py` enforces.
from app.airtable import importer, mirror, release, requirements
from app.sync import publish
# Local status reads only — `state` and `outbox` are persistence, not transport.
from app.sync import outbox as outbox_mod
from app.sync import state as sync_state
from app.sync.outbox import SyncOutbox
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

    # **Here as well as on `start`, and that is not belt and braces.** The rig
    # posts a finished stage directly to this route; nothing obliges it to have
    # called `start` first, so a gate only on `start` would be a gate a rig
    # walks past. DG14.
    _require_released(db, db_project, static_test)

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
    db.flush()  # the attempt needs its id before its deflections
    
    for d in trial_data.deflections:
        new_deflection = Deflection(
            deflection_gauge=d.deflection_gauge,
            max_deflection=d.max_deflection,
            permanent_deflection=d.permanent_deflection,
            recovery=d.recovery,
            test_id=new_trial.id,
        )
        db.add(new_deflection)

    # Enqueued **after** the deflections, not before. The payload is a snapshot
    # and the worker never re-derives it, so a phase queued mid-build would
    # publish a row that never existed — here, one whose `data_quality` did not
    # yet know a gauge had been read.
    db.flush()
    # Freeze the requirement now, not at publish time: upstream requirements
    # change, and an attempt that read its own later would report having been
    # run against something it was not.
    importer.freeze_requirement(db, new_trial, static_test)
    publish.record_phase(db, new_trial, publish.CREATE)
    # The rig posted a finished stage, so terminate it in the same call when the
    # post carried an operator. Without one the attempt stays In Progress and
    # the refusal is recorded rather than silent — see `complete_rig_trial`.
    if attempts.complete_rig_trial(new_trial, trial_data,
                                   test_operator=static_test.operator_name):
        # Cycles only: snapshot the rig-reported count before `finish` zeroes it.
        attempts.capture_cycles_completed(new_trial, static_test)
        db.flush()
        publish.record_phase(db, new_trial, publish.TERMINAL)
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

    # As on the static trial route: the rig posts a finished stage here
    # without necessarily having called `start`, so the gate has to be on the
    # path that actually records evidence. DG14.
    _require_released(db, db.query(Project).filter(Project.id == project_id).first(),
                      cyclic_test)

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
    db.flush()  # the attempt needs its id before its deflections

    for d in trial_data.deflections:
        new_deflection = Deflection(
            deflection_gauge=d.deflection_gauge,
            max_deflection=d.max_deflection,
            permanent_deflection=d.permanent_deflection,
            recovery=d.recovery,
            test_id=new_trial.id,
        )
        db.add(new_deflection)

    # Enqueued **after** the deflections, not before. The payload is a snapshot
    # and the worker never re-derives it, so a phase queued mid-build would
    # publish a row that never existed — here, one whose `data_quality` did not
    # yet know a gauge had been read.
    db.flush()
    # Freeze the requirement now, not at publish time: upstream requirements
    # change, and an attempt that read its own later would report having been
    # run against something it was not.
    importer.freeze_requirement(db, new_trial, cyclic_test)
    publish.record_phase(db, new_trial, publish.CREATE)
    # The rig posted a finished stage, so terminate it in the same call when the
    # post carried an operator. Without one the attempt stays In Progress and
    # the refusal is recorded rather than silent — see `complete_rig_trial`.
    if attempts.complete_rig_trial(new_trial, trial_data,
                                   test_operator=cyclic_test.operator_name):
        # Cycles only: snapshot the rig-reported count before `finish` zeroes it.
        attempts.capture_cycles_completed(new_trial, cyclic_test)
        db.flush()
        publish.record_phase(db, new_trial, publish.TERMINAL)
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


def _require_released(db, project, test):
    """Refuse to start or record a rig stage whose requirement is not released.

    **DG14 / contract §3.3, and §3.3 says to check it here.** An Airtable-bound
    static or cyclic test derives all fourteen of its stages from the imported
    design-pressure pair, and the upstream extractor is known to shift values
    one column to the left — 60 PSF arrives as 9, plausibly. The pair must
    therefore have been read independently off the trusted proposal by a named
    person and agree with what LabOS mirrored, before any of it can move
    hardware.

    A `409`, not a `400`: nothing about the request is malformed. The job is
    simply not in a state where it may run, and it becomes so when somebody
    verifies it — which is what the message says.

    A LabOS-only test passes straight through. Its pressures are the operator's
    own input and there is no second source to reconcile.
    """
    state = release.evaluate(db, project, test)
    if not state.executable:
        raise HTTPException(status_code=409, detail=state.reason)
    return state


@app.get("/projects/{project_id}/requirement-release",
         response_model=RequirementReleaseSchema)
def get_requirement_release(project_id: int, static_test_index: int = 0,
                            kind: str = "static",
                            db: Session = Depends(get_db)):
    """May this job's rig tests run, and if not, what has to happen first?

    Served from the **same** `release.evaluate` the start path enforces, so a
    screen cannot show a green light the backend will refuse. `code` is a
    stable token to branch on; `reason` is the sentence to show the operator.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    model = CyclicTest if kind == "cyclic" else StaticTest
    test = db.query(model).filter(model.index == static_test_index,
                                  model.project_id == project_id).first()
    if not test:
        raise HTTPException(status_code=404, detail=f"{kind} test not found")
    return release.evaluate(db, project, test).as_dict()


@app.post("/projects/{project_id}/requirement-verification",
          response_model=RequirementReleaseSchema)
def verify_requirement(project_id: int, body: RequirementVerificationSchema,
                       db: Session = Depends(get_db)):
    """Record the design-pressure pair as read off the trusted proposal.

    **The control DG14 needs, and the only one that can work.** A shifted value
    is individually plausible, so nothing can detect it by looking at it. What
    catches it is a second independent reading of the same fact: this route
    takes the operator's, compares it with what LabOS mirrored from Airtable,
    and **refuses the two to disagree**.

    A disagreement is a `409` and **nothing is stored**. LabOS does not pick a
    winner between two contradicting sources — publishing a result against a
    requirement we chose for ourselves is the failure mode, not the fix — and
    storing a pair nobody believes would leave the job permanently stuck with a
    wrong number in it.

    Immutable once a rig attempt exists. Before that it may be re-recorded: a
    typo caught immediately should not need a new job. §3.3: changes after
    start require a new run, not a mutated active snapshot.
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    section_ids = {t.airtable_section_id for t in
                   list(project.static_tests) + list(project.cyclic_tests)
                   if getattr(t, "airtable_section_id", None)}
    if not section_ids:
        raise HTTPException(
            status_code=400,
            detail="This job has no Airtable-bound static or cyclic test, so "
                   "there is no imported requirement to verify. Its design "
                   "pressures are the operator's own input already.")

    if release.is_verified(project) and _has_rig_attempt(db, project):
        raise HTTPException(
            status_code=409,
            detail="This requirement is already verified and a rig attempt has "
                   "been recorded against it. A requirement snapshot is frozen "
                   "onto every attempt at start, so changing it now would "
                   "leave finished tests claiming a requirement they were not "
                   "run against. Contract §3.3: record a new run instead.")

    mirrored = None
    for section_id in sorted(section_ids):
        try:
            pair, _ = release.typed_pair(db, section_id)
        except (LookupError, requirements.RequirementError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"the Airtable requirement is not readable "
                       f"unambiguously, so there is nothing well-formed to "
                       f"verify against: {exc}")
        if pair is not None:
            mirrored = pair
            break

    refusal = release.record(
        project, inward=body.inward_psf, outward=body.outward_psf,
        unit=body.unit, reference=body.reference, verified_by=body.verified_by,
        mirrored_pair=mirrored)
    if refusal:
        raise HTTPException(status_code=409, detail=refusal)
    db.commit()
    db.refresh(project)

    test = next((t for t in list(project.static_tests) + list(project.cyclic_tests)
                 if getattr(t, "airtable_section_id", None)), None)
    return release.evaluate(db, project, test).as_dict()


def _has_rig_attempt(db, project):
    """Any static or cyclic attempt at all, in any state."""
    for test in list(project.static_tests) + list(project.cyclic_tests):
        if test.trials:
            return True
    return False


@app.put("/projects/{project_id}/static_tests/{static_test_index}/start",
         response_model=StaticTestSchema)
def start_static_test(project_id: int, static_test_index: int,
                      body: RunStartSchema = None,
                      db: Session = Depends(get_db)):
    """Declare who is running this static test, before any hardware moves.

    Static had no start route at all, so there was nowhere to capture the
    operator — and the rig callback carries only `deflections`. That absence,
    not the firmware, is why rig attempts could not be completed. See
    `a3d8e5c71f04`.

    Optional body, so an existing caller that starts a run without declaring an
    operator still works; the attempt is then completable only if the trial
    callback carries one.
    """
    test = db.query(StaticTest).filter(
        StaticTest.index == static_test_index,
        StaticTest.project_id == project_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Static test not found")
    project = db.query(Project).filter(Project.id == project_id).first()
    _require_released(db, project, test)
    if body and body.operator_name:
        test.operator_name = body.operator_name
    db.commit()
    db.refresh(test)
    return test


@app.put("/projects/{project_id}/cyclic_tests/{cyclic_test_index}/start", response_model=CyclicTestSchema)
def start_cyclic_test(project_id: int, cyclic_test_index: int,
                      body: RunStartSchema = None,
                      db: Session = Depends(get_db)):
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    cyclic_test = db.query(CyclicTest).filter(CyclicTest.index == cyclic_test_index, CyclicTest.project_id == project_id).first()
    if not cyclic_test:
        raise HTTPException(status_code=404, detail="Cyclic test not found")
    _require_released(db, db_project, cyclic_test)

    # Check if previous tests are finished
    previous_tests = db.query(CyclicTest).filter(CyclicTest.project_id == project_id, CyclicTest.index < cyclic_test.index).all()
    if any(not test.finished for test in previous_tests):
        raise HTTPException(status_code=400, detail="Previous cyclic tests are not finished")
    if cyclic_test.finished : raise HTTPException(status_code=400, detail="Already finished")
    # Declared at run start, before hardware moves — see `a3d8e5c71f04`.
    if body and body.operator_name:
        cyclic_test.operator_name = body.operator_name
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

    # **A duplicate Start is not a second physical attempt.** One test runs once
    # at a time, so if this test already has an open attempt, that attempt IS
    # this request's answer — returning it makes Start idempotent. Allocating
    # the next number instead would turn an operator's double-click into two
    # certification records for one physical test, and Airtable would show them
    # as attempts 1 and 2.
    #
    # An intentional retest still gets the next number: it requires the previous
    # attempt to be terminal, which is what `PUT /finish` stamps.
    existing = attempts.open_attempt_for(db, cls, list(link)[0], test.id)
    if existing is not None:
        return existing

    # One active physical run per rig — a hardware fact, not a policy. Refused
    # rather than serialised, because a second concurrent run cannot correspond
    # to anything that is actually happening.
    device_id = getattr(getattr(test, "project", None), "device_id", None)
    if device_id is not None:
        busy = attempts.rig_is_busy(db, device_id,
                                    exclude_test=(test.__tablename__, test.id))
        if busy is not None:
            raise HTTPException(
                status_code=409,
                detail=f"This rig is already running attempt "
                       f"{busy.trial_number} of another test "
                       f"({busy.test_type}). One rig runs one test at a time; "
                       "finish or abort that attempt first.")

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
            # Contract §4.1 puts both of these in the always-required set, so
            # the create payload is refused without them. `attempts.begin()`
            # stamps them on the static and cyclic paths; this path builds its
            # own object and did not, which the acceptance suite caught the
            # first time a manual attempt was actually queued.
            labos_created_at=_utcnow(),
            labos_updated_at=_utcnow(),
            schema_version=None,
            **link,
        )

    attempt = attempts.insert_attempt(db, build)
    db.flush()   # the attempt needs its identity before it can be queued
    # Frozen at start, for the same reason as the rig paths above.
    importer.freeze_requirement(db, attempt, test)
    publish.record_phase(db, attempt, publish.CREATE)
    db.commit()  # attempt + queue entry, one transaction (contract §4)
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
    them, so requiring them per test was retyping rather than data capture.

    The classification and target velocity are optional here too, and for a
    different reason: neither is known when the test object is made. They are
    required by the time an attempt completes, and `PATCH` is how they arrive.
    """
    _require_project(db, project_id)

    # **The binding is this test's own `airtable_section_id`, not its
    # project's.** A project can be Airtable-bound while a test added to it is
    # not, so the project-level identity is not authority over this row.
    #
    # A bound test created here must not end up with a NULL family. This is a
    # real product path, not a vestigial one: MANUAL_TESTS_API.md section 7
    # tells the UI to send `airtable_*` "from the mirror" for an
    # Airtable-linked job, so the route has to derive the family the same way
    # `importer.bind` does — from the section's requirement code, through the
    # one shared mapping.
    impact_family = _family_for(db, body.airtable_section_id,
                                body.impact_family)
    _refuse_level_without_lmi(impact_family, body.impact_level)

    test = MissileImpactTest(
        project_id=project_id, missile=body.missile,
        missile_weight=body.missile_weight,
        airtable_protocol_id=body.airtable_protocol_id,
        airtable_section_id=body.airtable_section_id,
        airtable_section_name=body.airtable_section_name,
        impact_family=impact_family,
        impact_level=body.impact_level,
        target_velocity=body.target_velocity,
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


def _family_for(db, section_id, supplied):
    """The impact family for a test being created.

    Unbound: whatever the operator supplied, including nothing — a LabOS-only
    test has no requirement code to own it, and may acquire one later through
    `PATCH`.

    Bound: resolved from the section, never from the client. The mapping is
    `importer.IMPACT_FAMILY_BY_CODE`, the same object `bind` uses, so the two
    creation paths cannot drift.
    """
    if not section_id:
        return supplied

    if supplied is not None:
        raise HTTPException(
            status_code=400,
            detail=(f"impact_family is owned by the bound Airtable requirement "
                    f"code for section {section_id} and cannot be supplied or "
                    "overridden here. Omit it — it is resolved from the "
                    "section."))

    section = (db.query(mirror.AtMirrorSection)
               .filter(mirror.AtMirrorSection.record_id == section_id).first())
    if section is None:
        raise HTTPException(
            status_code=400,
            detail=(f"Protocol Section {section_id} is not in the mirror, so "
                    "the impact family it owns cannot be resolved. Refresh the "
                    "mirror, or create the test without a section id."))

    family = importer.IMPACT_FAMILY_BY_CODE.get(section.requirement_code)
    if family is None:
        raise HTTPException(
            status_code=400,
            detail=(f"Protocol Section {section_id} carries requirement code "
                    f"{section.requirement_code!r}, which is not an impact "
                    "requirement. An impact test binds to IMPACT_SMI or "
                    "IMPACT_LMI only."))
    return family


def _refuse_level_without_lmi(family, level):
    """A level is meaningful only for LMI. The database refuses SMI + level;
    this refuses it with a sentence rather than an IntegrityError, and also
    catches the family-not-yet-known case the CHECK deliberately permits."""
    if level is None:
        return
    if family == "SMI":
        raise HTTPException(
            status_code=400,
            detail="impact_level applies to LMI only; this test is SMI.")
    if family is None:
        raise HTTPException(
            status_code=400,
            detail=("impact_level cannot be set before the impact family is "
                    "known. For an Airtable-bound test the importer sets it "
                    "from the requirement code; for a LabOS-only test supply "
                    "impact_family first."))


def _any_attempts(test):
    """Every attempt of this test, **including aborted ones**.

    A different bar from `_completed_attempts`, deliberately. The impact
    family is the context an attempt was run in — an abort still happened
    against a particular missile, and re-labelling the test afterwards would
    rewrite what that attempt meant. So execution beginning at all fixes the
    family, while the level and the target velocity stay editable until an
    attempt actually completes.
    """
    return list(test.trials or [])


def _completed_attempts(db, test):
    """Attempts of this test that actually completed.

    **Aborted attempts do not count.** An abort is a run that produced no
    result, so a test whose only history is an abort has recorded nothing and
    must stay editable — otherwise one abandoned attempt would freeze the
    classification of a test that has never produced a result.
    """
    return [a for a in (test.trials or []) if a.status == _COMPLETED]


@app.patch("/projects/{project_id}/impact-tests/{test_id}",
           response_model=ImpactTestSchema)
def update_impact_test(project_id: int, test_id: int,
                       body: ImpactTestUpdateSchema,
                       db: Session = Depends(get_db)):
    """Set the impact level and target velocity before an attempt completes.

    None of them is required when the test is created, so this is the
    supported route that supplies them afterwards.

    Two different lifetimes, deliberately. `impact_family` is write-once and
    LabOS-only: a bound test's family belongs to its requirement code, and any
    attempt at all — aborted included — fixes it, because the family is the
    context that attempt ran in. `impact_level` and `target_velocity` stay
    editable until an attempt *completes*, because until then nothing has been
    claimed.
    """
    test = _require_test(db, MissileImpactTest, project_id, test_id,
                         "Impact test")
    fields = body.model_dump(exclude_unset=True)

    # -- the family: write-once, and only ever for a LabOS-only test --------
    if "impact_family" in fields:
        if test.airtable_section_id:
            raise HTTPException(
                status_code=400,
                detail=("impact_family is owned by the bound Airtable "
                        "requirement code for section "
                        f"{test.airtable_section_id} and is not editable."))
        started = _any_attempts(test)
        if started:
            raise HTTPException(
                status_code=409,
                detail=(f"this test has {len(started)} attempt(s); execution "
                        "has begun, so the impact family is fixed. Changing it "
                        "now would change what those attempts were run "
                        "against."))
        # The level **after** this request, not the one already stored:
        # `fields.get` cannot tell "not sent" from "explicitly cleared", so
        # clearing the level and switching to SMI in one call has to be
        # allowed rather than refused by its own leftover value.
        resulting_level = (fields["impact_level"] if "impact_level" in fields
                           else test.impact_level)
        if fields["impact_family"] == "SMI" and resulting_level:
            raise HTTPException(
                status_code=400,
                detail=("this test carries a level, which applies to LMI only. "
                        "Clear impact_level in the same request to make it "
                        "SMI."))
        test.impact_family = fields["impact_family"]

    # -- the level and the velocity: editable until a result exists ---------
    if "impact_level" in fields or "target_velocity" in fields:
        done = _completed_attempts(db, test)
        if done:
            raise HTTPException(
                status_code=409,
                detail=(f"this test has {len(done)} completed attempt(s); the "
                        "level and target velocity it ran under cannot be "
                        "changed afterwards. Record a correction instead."))
    if "impact_level" in fields:
        _refuse_level_without_lmi(test.impact_family, fields["impact_level"])
        test.impact_level = fields["impact_level"]
    if "target_velocity" in fields:
        test.target_velocity = fields["target_velocity"]

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
            # **Exactly one, not at least one** (§4.5a). One attempt is one
            # impact, so an attempt with none has nothing to report and an
            # attempt with several is a shape the constraint should already have
            # refused — checked here too, because a database error at terminal
            # time would be reported as a 500 rather than as the operator's
            # problem.
            if len(impact.shots) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=("A completed impact attempt records exactly one impact; "
                            f"this one has {len(impact.shots)}. "
                            + ("Post the impact, or supply `abort_reason` to abandon "
                               "the attempt." if not impact.shots else
                               "One attempt is one impact — record the others as "
                               "their own attempts."))) 
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
            # **The classification and the target velocity, checked here and
            # not at creation.** Neither is known when the test object is
            # made; both are part of what the attempt is a result *of*, so
            # the last honest moment to require them is the moment a result
            # becomes a claim. An abort never reaches this branch.
            # `ImpactTestResult.missile_impact_test` is the existing
            # relationship; no new lookup is needed.
            parent = impact.missile_impact_test
            if parent is not None:
                if parent.impact_classification is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "A completed impact attempt records which missile "
                            "classification it ran under. This test is "
                            f"{parent.impact_family or 'unclassified'}"
                            + (" and needs its level (D or E)."
                               if parent.impact_family == "LMI" else
                               " — set the impact family first.")
                            + " Set it on the test, or supply `abort_reason` "
                              "to abandon the attempt."))
                if parent.target_velocity is None:
                    raise HTTPException(
                        status_code=400,
                        detail=("A completed impact attempt records the target "
                                "velocity it ran against. Set target_velocity "
                                "on the test, or supply `abort_reason` to "
                                "abandon the attempt."))
        elif body.result is None:
            raise HTTPException(
                status_code=400,
                detail="A completed manual attempt records pass or fail. Supply "
                       "`result`, or `abort_reason` to abandon the attempt.")
        attempt.status = _COMPLETED
        if body.result is not None:
            attempt.result = body.result
        elif impact is not None and impact.shots:
            # **The attempt's outcome is its impact's outcome.** With one impact
            # per attempt the two cannot differ, and the `elif body.result is
            # None` branch above never reaches an impact attempt — its outcome
            # comes from the shot, not from a body field. Left unset, an impact
            # attempt terminated without an explicit `result` had `None` here
            # while its impact recorded pass or fail, so the attempt row
            # disagreed with the impact it contained.
            attempt.result = impact.shots[0].result

    attempt.note = body.note or attempt.note
    attempt.testing_continued = body.testing_continued
    attempt.testing_end_date = _utcnow()
    attempt.terminal_at = _utcnow()
    # The revision time this phase's payload is stamped with, so the queue entry
    # and the record it describes agree about when it changed.
    attempt.labos_updated_at = _utcnow()
    db.flush()
    publish.record_phase(db, attempt, publish.TERMINAL)
    db.commit()
    db.refresh(attempt)
    return attempt


# Each attempt subclass, the column naming its parent test, and that parent.
# Ordered rather than keyed on `test_type`, for the same reason `_kind_of` is
# keyed on the table: `manual_tests` holds two test types.
_ATTEMPT_PARENT = (
    (StaticTestResult, "static_test_id", StaticTest),
    (CyclicTestResult, "cyclic_test_id", CyclicTest),
    (ManualTestResult, "manual_test_id", ManualTest),
    (ImpactTestResult, "missile_impact_test_id", MissileImpactTest),
)


def _typed_attempt(db, attempt_id):
    """The attempt as its own subclass, with its parent test resolved.

    **`db.query(TestResult)` cannot do this.** The subclasses are joined-table
    inheritance with no polymorphic discriminator, so a query against the base
    returns base `TestResult` rows whose `__tablename__` is always
    `test_results` — the subclass, and therefore the parent test, is not
    recoverable from the object. `_impact_attempt` already queries its subclass
    directly for exactly this reason; this is that idiom for all four.

    Returns `(attempt, fk_name, test)` or `(None, None, None)` when the id
    belongs to no per-type attempt table — which is the pre-integration rows.
    """
    for cls, fk_name, parent_cls in _ATTEMPT_PARENT:
        row = db.query(cls).filter(cls.id == attempt_id).first()
        if row is None:
            continue
        parent_id = getattr(row, fk_name, None)
        test = (db.query(parent_cls).filter(parent_cls.id == parent_id).first()
                if parent_id is not None else None)
        return row, fk_name, test
    return None, None, None


@app.post("/test-results/{test_result_id}/correct", response_model=AttemptSchema)
def correct_attempt(test_result_id: int, body: AttemptCorrectSchema,
                    db: Session = Depends(get_db)):
    """Supersede a recorded result with a new attempt that names it.

    **The gap this closes.** `corrects_attempt_id` and `correction_reason` have
    had columns, a property and an envelope mapping since P1, and nothing set
    them — so every attempt was a retest, and a wrongly recorded result could
    only be superseded by claiming a physical test that never happened. There is
    also no edit or delete route for a shot or an attempt, by design, so until
    this existed a mis-recorded result had no route at all.

    Why it matters beyond our own records: the change document's §0.3 argument to
    the Airtable team is that a retest and a correction are indistinguishable
    without this field, and that a roll-up counting attempts would then be wrong
    *and look right*. This is the half of that promise that lives on our side.

    The correction starts **open**, and is recorded and finished through the
    ordinary paths. So there is one lifecycle rather than two, and a correction
    is an ordinary attempt that happens to name its predecessor.

    Three refusals, each for a different reason:

    * **The original must be terminal.** An open attempt is finished correctly,
      not corrected — its evidence is not frozen yet, so there is nothing to
      supersede.
    * **The test must have no open attempt.** A correction creates one, and two
      open attempts for one test make "the current attempt" ambiguous.
    * **A reason is required** — enforced in `attempts.as_correction`, because
      contract §4.1 puts it in the always-required set beside the reference.

    Deliberately *not* refused when the parent test is `finished`. Correcting a
    record is not running the rig again, and a finished test is exactly where a
    correction is most likely to be needed.
    """
    original = db.query(TestResult).filter(TestResult.id == test_result_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Attempt not found")

    if original.status not in (_COMPLETED, _ABORTED):
        raise HTTPException(
            status_code=400,
            detail=f"This attempt is {original.status!r}, not finished. An open "
                   "attempt is completed correctly rather than corrected — "
                   "there is no recorded result to supersede yet.")

    typed, fk_name, test = _typed_attempt(db, test_result_id)
    if typed is None:
        raise HTTPException(
            status_code=400,
            detail="This attempt predates the per-type attempt tables, so the "
                   "test it belongs to cannot be resolved. Corrections are "
                   "available on attempts recorded through the current routes.")
    if test is None:
        raise HTTPException(
            status_code=400,
            detail="The test this attempt belongs to no longer exists, so a "
                   "correction has nothing to attach to.")
    original = typed

    open_now = attempts.open_attempt_for(db, type(original), fk_name, test.id)
    if open_now is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Attempt {open_now.trial_number} of this test is still open. "
                   "Finish or abort it before recording a correction, so that "
                   "one test has one open attempt.")

    # **The original's `labos_test_id`, not a freshly derived one.** Deriving it
    # would be right for a test created through the current routes and wrong for
    # a legacy one, whose id was a random uuid4 — and a correction that lands in
    # a different group is exactly the failure `labos_test_id` exists to prevent.
    test_id = original.labos_test_id or attempts.test_id_for_test(
        _kind_of(test), test.id)

    def build():
        return type(original)(
            trial_number=attempts.next_attempt_number(db, test_id),
            test_type=original.test_type,
            test_name=original.test_name,
            status=_IN_PROGRESS,
            test_result=_PENDING,
            operator_name=body.operator_name or original.operator_name,
            testing_start_date=_utcnow(),
            labos_test_id=test_id,
            labos_created_at=_utcnow(),
            labos_updated_at=_utcnow(),
            schema_version=None,
            **{fk_name: test.id},
        )

    correction = attempts.insert_attempt(db, build)
    # Raises ValueError on an empty reason or a self-reference; both are client
    # errors, so they are reported as such rather than as a 500.
    try:
        attempts.as_correction(correction, original.labos_attempt_id, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    db.flush()
    importer.freeze_requirement(db, correction, test)
    publish.record_phase(db, correction, publish.CREATE)
    db.commit()
    db.refresh(correction)
    return correction


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
    attempt.labos_updated_at = _utcnow()
    db.flush()
    publish.record_phase(db, attempt, publish.VERDICT)
    db.commit()
    db.refresh(attempt)
    return attempt


# ---------------------------------------------------------------------------
# Sync status — delivery plan §4.2 and §4.7.
#
# Three routes over functions that already existed and were unrouted:
# `sync.state.status()` was documented as "the payload behind GET /sync/status"
# and `outbox.resume()` as "the manual half of POST /sync/queue/{id}/retry",
# with nothing serving either. That mattered beyond tidiness — DG6 justified
# giving the worker container no health check on the grounds that liveness is
# the heartbeat row `report-api` serves, so until these existed the worker had
# no liveness surface at all.
#
# **All three are local reads.** They compute from `sync_outbox` and
# `sync_state` at request time and never call Airtable, which is what makes
# them safe to poll from an operator's screen during an outage: the status of a
# stalled queue must be visible precisely when Airtable is unreachable.
# `retry` re-enables eligibility and sends nothing itself.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The inbound half — hierarchy selection, import and pre-fill.
#
# **Every read here serves from the local mirror, never from Airtable.** An
# operator picking a job at a rig cannot have the picker depend on someone
# else's API being up, and an empty mirror is an empty picker rather than a
# blocked operator (plan §4.6). `POST /airtable/refresh` is the only thing that
# talks to Airtable, and it is a deliberate action rather than a side effect of
# reading.
#
# `report-api` imports `app.airtable.mirror`, `importer` and `requirements` —
# all three are pure persistence and interpretation, and
# `tests/test_report_api_isolation.py` holds them to that.
# ---------------------------------------------------------------------------

@app.get("/airtable/projects")
def airtable_projects(db: Session = Depends(get_db)):
    """Jobs available to import, from the mirror.

    `mirrored_at` is returned per row because a stale mirror is a fact the
    operator may need: importing from a two-week-old mirror is legitimate, and
    knowing that it is two weeks old is what makes it a decision.
    """
    rows = (db.query(mirror.AtMirrorProject)
            .order_by(mirror.AtMirrorProject.job_number).all())
    return {"count": len(rows),
            "projects": [{"record_id": r.record_id,
                          "job_number": r.job_number,
                          "project_name": r.project_name,
                          "mirrored_at": r.mirrored_at.isoformat()
                                         if r.mirrored_at else None}
                         for r in rows]}


@app.get("/airtable/projects/{record_id}/specimens")
def airtable_specimens(record_id: str, db: Session = Depends(get_db)):
    rows = (db.query(mirror.AtMirrorSpecimen)
            .filter(mirror.AtMirrorSpecimen.project_record_id == record_id)
            .order_by(mirror.AtMirrorSpecimen.specimen_name).all())
    return {"count": len(rows),
            "specimens": [{"record_id": r.record_id,
                           "name": r.specimen_name,
                           # Already imported? The picker needs to say so, or an
                           # operator re-imports and wonders why nothing changed.
                           "imported_project_id": getattr(
                               importer.existing_project(db, r.record_id),
                               "id", None)}
                          for r in rows]}


@app.get("/airtable/specimens/{record_id}/protocols")
def airtable_protocols(record_id: str, db: Session = Depends(get_db)):
    rows = (db.query(mirror.AtMirrorProtocol)
            .filter(mirror.AtMirrorProtocol.specimen_record_id == record_id)
            .order_by(mirror.AtMirrorProtocol.protocol_name).all())
    return {"count": len(rows),
            "protocols": [{"record_id": r.record_id, "name": r.protocol_name}
                          for r in rows]}


@app.get("/airtable/protocols/{record_id}/sections")
def airtable_sections(record_id: str, db: Session = Depends(get_db)):
    """The sections of one protocol, each with whether LabOS can execute it.

    `refused` carries the reason, because a section LabOS will not run is the
    one thing the operator can actually fix — and a picker that simply omitted
    it would look identical to a protocol where every section was fine.
    """
    rows = (db.query(mirror.AtMirrorSection)
            .filter(mirror.AtMirrorSection.protocol_record_id == record_id)
            .order_by(mirror.AtMirrorSection.record_id).all())
    out = []
    for r in rows:
        entry = {"record_id": r.record_id, "section_name": r.section_name,
                 "requirement_code": r.requirement_code,
                 "applicability": requirements.applicability_of(r),
                 "executable": False, "refused": None}
        try:
            requirements.validate(r)
            entry["executable"] = (
                r.requirement_code in requirements.EXECUTABLE_CODES
                and entry["applicability"] == requirements.REQUIRED)
        except requirements.RequirementError as exc:
            entry["refused"] = str(exc)
        out.append(entry)
    return {"count": len(out), "sections": out}


@app.post("/airtable/refresh")
def airtable_refresh(db: Session = Depends(get_db)):
    """Re-read the hierarchy into the mirror. **The only route that calls Airtable.**

    Deliberately separate from every read, so no request an operator makes can
    block on Airtable. Idempotent: it updates rows in place and creates none on
    a re-run, which is what makes a repeated import safe.

    This is the one place `report-api` builds an Airtable client, and it is
    imported inside the function rather than at module scope — the isolation
    test forbids the transport in the request path, and a refresh is the
    explicit exception rather than a hole in the rule.
    """
    from app.airtable.client import AirtableClient   # noqa: PLC0415
    from app.config import airtable_settings         # noqa: PLC0415
    if not airtable_settings.token:
        raise HTTPException(
            status_code=503,
            detail="No Airtable token is configured, so the mirror cannot be "
                   "refreshed. Existing mirrored data is still readable and "
                   "still importable.")
    counts = mirror.refresh(db, AirtableClient(settings=airtable_settings))
    db.commit()
    return {"refreshed": counts}


@app.post("/airtable/import/plan")
def airtable_import_plan(body: ImportRequestSchema,
                         db: Session = Depends(get_db)):
    """What an import would do, without doing any of it.

    Separate from the import so an operator sees the consequences first: which
    sections are executable, which are unconfirmed, and which are refused and
    why. An import that silently skipped a malformed section would look
    identical to one where every section was fine.
    """
    try:
        return importer.plan(db, body.project_record_id,
                             body.specimen_record_id,
                             body.protocol_record_id).as_dict()
    except importer.ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/airtable/import", response_model=ProjectSchema)
def airtable_import(body: ImportRequestSchema, db: Session = Depends(get_db)):
    """Import a hierarchy as a LabOS project, once.

    **Goes through `create_project_for_device`** — the same function a typed
    project goes through, called with pre-filled values instead of typed ones.
    Not a parallel create path: the six static and eight cyclic tests derived
    from the design pressures are exactly what would drift between two paths,
    and §4.6 says standalone and synced are one form with the boxes empty or
    filled.

    **Duplicate-safe on the mock-up record.** A repeated import returns the
    project it already made. Re-importing after a refresh is a normal action,
    and answering it with a second set of tests against the same specimen would
    turn a refresh into duplicated certification work.
    """
    device = db.query(Device).filter(Device.id == body.device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    already = importer.existing_project(db, body.specimen_record_id)
    if already is not None:
        return already

    try:
        import_plan = importer.plan(db, body.project_record_id,
                                    body.specimen_record_id,
                                    body.protocol_record_id)
        values = importer.prefill_values(import_plan, name=body.name)
    except importer.ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    parent = (db.query(ProjectParent)
              .filter(ProjectParent.name == import_plan.project.job_number)
              .first())
    if parent is None and import_plan.project.job_number:
        parent = ProjectParent(name=import_plan.project.job_number)
        db.add(parent)
        db.flush()

    project = create_project_for_device(
        body.device_id,
        ProjectCreateSchema(parent_id=parent.id if parent else None, **values),
        db=db)

    importer.bind(db, project, import_plan)
    db.commit()
    db.refresh(project)
    return project


@app.get("/sync/status")
def sync_status(db: Session = Depends(get_db)):
    """Queue health in the four contractual words, plus the worker heartbeat.

    `status` is one of `Synced` · `Pending` · `Sync Failed` · `Retry Required`
    — the Airtable team's own four words, which is why they are contractual
    values and not our own vocabulary. `attachment_backlog` is counted
    separately because evidence delivers on its own channel and a photograph
    still in flight is not a result that failed.
    """
    return sync_state.status(db)


@app.get("/sync/queue")
def sync_queue(state: Optional[str] = None, limit: int = 100,
               db: Session = Depends(get_db)):
    """The queue itself — one row per pending write, oldest first.

    Ordered by `(attempt_id, attempt_seq)` rather than by time, because the
    ordering that matters is *within* an attempt: its phases merge onto one
    Airtable record and must arrive in sequence. `?state=parked` is the useful
    filter — those are the entries that will never clear on their own.
    """
    q = db.query(SyncOutbox)
    if state:
        q = q.filter(SyncOutbox.state == state)
    entries = (q.order_by(SyncOutbox.attempt_id, SyncOutbox.attempt_seq)
               .limit(min(limit, 500)).all())
    return {
        "count": len(entries),
        "entries": [{
            "id": e.id,
            "attempt_id": e.attempt_id,
            "attempt_seq": e.attempt_seq,
            "phase": e.phase,
            # Which of the attempt's two queues this is in. An attachment
            # cannot block a record phase and vice versa.
            "channel": "attachment" if e.phase == "attachment" else "record",
            "state": e.state,
            "attempts": e.attempts,
            "next_attempt_at": e.next_attempt_at.isoformat() if e.next_attempt_at else None,
            "last_error": e.last_error,
        } for e in entries],
    }


@app.get("/sync/failures")
def sync_failures(db: Session = Depends(get_db)):
    """Payloads LabOS refused to queue — the failures with no queue entry.

    These are invisible in `/sync/queue` by construction: they never got an
    entry. Until they were persisted, the headline status read `Synced` for an
    attempt that had never been published, and there was nothing to retry.

    `recoverable` records why it was refused *at the time*, and is advisory: a
    payload our own envelope rejected needs the defect fixed; an incomplete
    Airtable binding needs someone to bind the protocol and section. Neither is
    permanent, and repair re-checks rather than trusting the flag — so a result
    whose job was bound later is still publishable, with its measurements and
    evidence intact.
    """
    rows = outbox_mod.open_publication_failures(db)
    return {
        "count": len(rows),
        "failures": [{
            "id": r.id,
            "attempt_id": r.attempt_id,
            "phase": r.phase,
            "error": r.error,
            "recoverable": r.recoverable,
            "payload_updated_at": r.payload_updated_at.isoformat()
                                  if r.payload_updated_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows],
    }


@app.post("/sync/failures/{failure_id}/repair")
def sync_repair(failure_id: int, db: Session = Depends(get_db)):
    """Rebuild and re-queue one refused phase. Sends nothing itself.

    Repair goes back through `publish.record_phase`, so the payload is built by
    the same code the route would have used and is subject to the same envelope
    rules — a repair that bypassed them could queue exactly the payload that was
    refused. If it is refused again, the failure row is updated with the new
    reason rather than resolved, and this returns 409: the defect is still there.

    Refused for a non-recoverable failure, because re-running it cannot help.
    """
    row = (db.query(outbox_mod.SyncPublicationFailure)
           .filter(outbox_mod.SyncPublicationFailure.id == failure_id).first())
    if not row:
        raise HTTPException(status_code=404, detail="Failure record not found")
    if row.resolved_at is not None:
        raise HTTPException(status_code=400,
                            detail="This failure is already resolved.")
    # **`recoverable` is advisory, not a verdict.** It says why the payload was
    # refused when it was refused; it must not decide whether repair may be
    # attempted now. An incomplete Airtable binding is the case that matters: it
    # is genuinely unfixable by retry *at the time*, and becomes fixable the
    # moment someone binds the protocol and section. Refusing on the stored flag
    # would strand that result permanently — the attempt would keep its
    # measurements and its evidence and never be publishable.
    #
    # So repair always re-derives the payload against the data as it stands now.
    # If the binding has since been completed it succeeds; if not, it 409s with
    # the current reason, and `_refuse` has updated the row with it.
    attempt = (db.query(TestResult)
               .filter(TestResult.labos_attempt_id == row.attempt_id).first())
    if not attempt:
        raise HTTPException(status_code=404,
                            detail="The attempt this failure refers to is gone.")

    entry = publish.record_phase(db, attempt, row.phase)
    db.commit()
    if entry is None:
        db.refresh(row)
        hint = ("" if row.recoverable else
                " Bind this job's Airtable protocol and section records, then "
                "repair again — the result and its evidence are kept meanwhile.")
        raise HTTPException(
            status_code=409,
            detail=f"Still refused: {row.error} The payload has not been "
                   f"repaired, so nothing was queued.{hint}")
    return {"failure_id": failure_id, "queued_phase": row.phase,
            "attempt_id": row.attempt_id, "resolved": True}


@app.post("/sync/queue/{entry_id}/retry")
def sync_retry(entry_id: int, db: Session = Depends(get_db)):
    """Un-park one entry. Re-enables eligibility; sends nothing.

    Deliberately not a send: the same single worker still owns delivery and
    still applies the lease and the fencing token. A retry that pushed inline
    from the request would be a second sender, which is the one thing §7.1's
    mechanisms exist to prevent.
    """
    entry = db.query(SyncOutbox).filter(SyncOutbox.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Queue entry not found")
    if entry.state != "parked":
        raise HTTPException(
            status_code=400,
            detail=f"This entry is {entry.state!r}, not parked. Only a parked "
                   "entry needs a retry; the worker handles the rest.")
    outbox_mod.resume(db, entry_id)
    db.commit()
    db.refresh(entry)
    return {"id": entry.id, "state": entry.state,
            "next_attempt_at": entry.next_attempt_at.isoformat()
                               if entry.next_attempt_at else None}


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
    """Record this attempt's impact — one attempt, one impact.

    **One attempt per impact** (delivery plan §4.5a, product owner 2026-09-08).
    An impact test is still a sequence — impact 1, 2, 3 — but the sequence is
    made of attempts, not of shots inside one attempt. So this route records the
    single impact belonging to this attempt, and the next impact is the next
    attempt.

    `shot_number` mirrors `attempt.trial_number` rather than counting within the
    attempt. Counting would give every impact `shot_number = 1`, and the number
    is not internal: the published JSON emits it as which impact this is. The
    mirror also makes the invariant free — two impacts on one attempt collide on
    `uq_shots_attempt_number`, so "exactly one impact per attempt" is enforced
    by a constraint that already existed rather than by a rule in this function.
    The 409 below is that collision reported in words; the constraint is what
    guarantees it.
    """
    attempt = _require_open_attempt(db, test_result_id)
    impact = _impact_attempt(db, attempt.id)
    if impact is None:
        raise HTTPException(status_code=400,
                            detail="Only an impact attempt records impacts.")
    existing = db.query(Shot).filter(Shot.test_result_id == impact.id).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Attempt {impact.trial_number} already records impact "
                   f"{existing.shot_number}. One attempt is one impact — start "
                   "a new attempt on this test to record the next one.")
    shot = Shot(test_result_id=impact.id,
                missile_impact_test_id=impact.missile_impact_test_id,
                shot_number=impact.trial_number, result=body.result,
                area=body.area, velocity=body.velocity, note=body.note)
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
    db.flush()   # the photo needs its id before it can be queued
    if attempt is not None:
        # One entry per photograph, queued when the photograph is added —
        # including one added legally between termination and review, which the
        # 409 above permits until the verdict. See `sync.publish`.
        publish.record_attachment(db, attempt, photo)
    db.commit()
    db.refresh(photo)
    return photo
