import os
from typing import Optional
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlalchemy.orm import sessionmaker, Session
from app.data.models import *
from app.data.schema import *
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

# Create uploads directory if it doesn't exist
uploads_dir = Path("uploads")
uploads_dir.mkdir(exist_ok=True)

# Mount static files for uploaded images
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

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
        note=None
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
        note=None
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
    return db.query(ProjectParent).all()

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
    Download specimen report for a specific project
    """
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Create temporary file for the PDF
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
        pdf_path = create_test_report_pdf(db_project, temp_file.name)
    
    # Return the PDF as a file response with inline disposition
    return FileResponse(
        path=pdf_path,
        filename=f"project_{project_id}_report.pdf",
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename=project_{project_id}_report.pdf"}
    )