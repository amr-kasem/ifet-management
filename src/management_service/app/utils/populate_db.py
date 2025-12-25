import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.data.utils import run_migrations
from app.data.models import (
    Device, Project, ProjectParent, CyclicTest, StaticTest, InfiltrationTest, 
    MissileImpactTest, Shot, Deflection, CyclicTestResult, StaticTestResult
)
from app.domain.cyclic_test_pressure_calculator import CyclicTestPressureCalculator
from app.domain.static_test_pressure_calculator import StaticTestPressureCalculator
import random

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")
engine = create_engine(DATABASE_URL)
run_migrations(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def populate_database():
    db = next(get_db())
    try:
        # Purge all existing data
        print("Purging all existing data...")
        
        # Delete in order to respect foreign key constraints
        db.query(Deflection).delete()
        db.query(CyclicTestResult).delete()
        db.query(StaticTestResult).delete()
        db.query(Shot).delete()
        db.query(MissileImpactTest).delete()
        db.query(InfiltrationTest).delete()
        db.query(CyclicTest).delete()
        db.query(StaticTest).delete()
        db.query(Project).delete()
        db.query(ProjectParent).delete()
        db.query(Device).delete()
        
        # Reset sequences to start from 1
        from sqlalchemy import text
        db.execute(text("ALTER SEQUENCE devices_id_seq RESTART WITH 1"))
        db.execute(text("ALTER SEQUENCE project_parents_id_seq RESTART WITH 1"))
        db.execute(text("ALTER SEQUENCE projects_id_seq RESTART WITH 1"))
        
        db.commit()
        print("All data purged successfully!")
        
        # Create 2 devices with explicit IDs 1 and 2
        print("Creating 2 devices...")
        device_names = ["IFET Test Chamber Alpha", "IFET Test Chamber Beta"]
        devices = []
        
        for device_idx, device_name in enumerate(device_names, 1):
            device = Device(
                id=device_idx,
                name=device_name, 
                turbo_mode=False,
                turbo_slave=False,
                turbo_charger=None
            )
            db.add(device)
            db.flush()
            devices.append(device)
            print(f"  Created Device {device.id}: {device.name}")
        
        db.commit()

        # Realistic project parent names - unique for each device
        project_parent_names_device1 = [
            "Hurricane Impact Window System - Alpha Series 2024",
            "Storm Resistant Door Assembly - Alpha Grade",
            "High-Performance Curtain Wall System - Alpha Model",
            "Impact Resistant Skylight Assembly - Alpha Type"
        ]
        
        project_parent_names_device2 = [
            "Hurricane Impact Window System - Beta Series 2024",
            "Storm Resistant Door Assembly - Beta Grade",
            "High-Performance Curtain Wall System - Beta Model",
            "Impact Resistant Skylight Assembly - Beta Type"
        ]
        
        all_project_names = [project_parent_names_device1, project_parent_names_device2]

        # Create 4 project parents for each device, each with 3 specimens
        for device_idx, device in enumerate(devices):
            project_parent_names = all_project_names[device_idx]
            for parent_idx, parent_name in enumerate(project_parent_names, 1):
                project_parent = ProjectParent(name=parent_name)
                db.add(project_parent)
                db.flush()

                # Create 3 specimens for each project parent
                specimen_names = ["Specimen A", "Specimen B", "Specimen C"]
                
                for spec_idx, spec_name in enumerate(specimen_names, 1):
                    # Realistic design pressures
                    inward_pressure = random.uniform(150.0, 400.0)
                    outward_pressure = random.uniform(200.0, 500.0)
                    
                    specimen = Project(
                        name=f"{spec_name}",
                        device_id=device.id,
                        parent_id=project_parent.id,
                        inward_design_pressure=inward_pressure,
                        outward_design_pressure=outward_pressure
                    )
                    db.add(specimen)
                    db.flush()

                    # Create 8 cyclic tests (4 inward, 4 outward)
                    for i in range(8):
                        h, l, c = CyclicTestPressureCalculator.get_cylcic_test_data(
                            inward_pressure if i < 4 else outward_pressure,
                            i,
                        )
                        cyclic_test = CyclicTest(
                            type="inward" if i < 4 else "outward",
                            cycles=c,
                            low_pressure=l,
                            high_pressure=h,
                            index=i,
                            project_id=specimen.id,
                            finished=random.choice([True, True, True, False]),  # Mostly finished
                            resume=False,
                            current_cycle=random.randint(0, c) if random.random() > 0.7 else c,
                            preset=True,
                        )
                        db.add(cyclic_test)
                        db.flush()

                        # Add test results for some cyclic tests
                        if cyclic_test.finished and random.random() > 0.3:
                            num_trials = random.randint(1, 3)
                            for trial_num in range(1, num_trials + 1):
                                cyclic_result = CyclicTestResult(
                                    cyclic_test_id=cyclic_test.id,
                                    trial_number=trial_num,
                                    result=random.choice([True, True, False]),  # Mostly pass
                                    note=f"Cyclic test {i+1} trial {trial_num} completed successfully" if random.random() > 0.2 else "Minor observation noted",
                                    image_path=f"/images/cyclic_{specimen.id}_{i}_{trial_num}.jpg" if random.random() > 0.5 else None
                                )
                                db.add(cyclic_result)
                                db.flush()

                                # Add deflections for some results
                                if random.random() > 0.4:
                                    num_gauges = random.randint(2, 4)
                                    gauge_names = ["Gauge 1", "Gauge 2", "Gauge 3", "Gauge 4"]
                                    for gauge_idx in range(num_gauges):
                                        max_def = random.uniform(0.5, 15.0)
                                        perm_def = random.uniform(0.1, max_def * 0.3)
                                        recovery = max_def - perm_def
                                        
                                        deflection = Deflection(
                                            deflection_gauge=gauge_names[gauge_idx],
                                            max_deflection=round(max_def, 2),
                                            permanent_deflection=round(perm_def, 2),
                                            recovery=round(recovery, 2),
                                            test_id=cyclic_result.id
                                        )
                                        db.add(deflection)

                    # Create 6 static tests (3 inward, 3 outward)
                    for j in range(6):
                        p, d = StaticTestPressureCalculator.get_static_test_data(
                            outward_pressure if j % 2 else inward_pressure, 
                            j
                        )
                        static_test = StaticTest(
                            pressure_factor='Structural Pressure',
                            pressure=p,
                            duration=d,
                            type="outward" if j % 2 else "inward",
                            index=j,
                            project_id=specimen.id,
                            finished=random.choice([True, True, True, False]),  # Mostly finished
                            preset=True,
                        )
                        db.add(static_test)
                        db.flush()

                        # Add test results for some static tests
                        if static_test.finished and random.random() > 0.3:
                            num_trials = random.randint(1, 2)
                            for trial_num in range(1, num_trials + 1):
                                static_result = StaticTestResult(
                                    static_test_id=static_test.id,
                                    trial_number=trial_num,
                                    result=random.choice([True, True, False]),  # Mostly pass
                                    note=f"Static test {j+1} trial {trial_num} completed" if random.random() > 0.2 else "Test observation recorded",
                                    image_path=f"/images/static_{specimen.id}_{j}_{trial_num}.jpg" if random.random() > 0.5 else None
                                )
                                db.add(static_result)
                                db.flush()

                                # Add deflections for some results
                                if random.random() > 0.4:
                                    num_gauges = random.randint(2, 4)
                                    gauge_names = ["Gauge 1", "Gauge 2", "Gauge 3", "Gauge 4"]
                                    for gauge_idx in range(num_gauges):
                                        max_def = random.uniform(0.3, 12.0)
                                        perm_def = random.uniform(0.05, max_def * 0.25)
                                        recovery = max_def - perm_def
                                        
                                        deflection = Deflection(
                                            deflection_gauge=gauge_names[gauge_idx],
                                            max_deflection=round(max_def, 2),
                                            permanent_deflection=round(perm_def, 2),
                                            recovery=round(recovery, 2),
                                            test_id=static_result.id
                                        )
                                        db.add(deflection)

                    # Create infiltration tests (1-2 per specimen)
                    num_infiltration = random.randint(1, 2)
                    infiltration_types = ["Water Infiltration", "Air Infiltration"]
                    for inf_idx in range(num_infiltration):
                        infiltration_test = InfiltrationTest(
                            type=infiltration_types[inf_idx] if inf_idx < len(infiltration_types) else "Water Infiltration",
                            pressure=random.uniform(0.1 * inward_pressure, 0.2 * inward_pressure),
                            duration=random.uniform(300.0, 1800.0),
                            leakage=random.uniform(0.0, 5.0),  # Always provide a float value
                            project_id=specimen.id
                        )
                        db.add(infiltration_test)

                    # Create missile impact tests (1-2 per specimen)
                    num_missile_tests = random.randint(1, 2)
                    missile_types = ["2x4 Lumber", "Steel Ball", "Large Missile"]
                    missile_weights = [9.0, 2.0, 15.0]  # kg
                    
                    for mit_idx in range(num_missile_tests):
                        missile_type = random.choice(missile_types)
                        missile_weight = missile_weights[missile_types.index(missile_type)] if missile_type in missile_types else random.uniform(2.0, 15.0)
                        
                        missile_test = MissileImpactTest(
                            missile=missile_type,
                            missile_weight=missile_weight,
                            project_id=specimen.id
                        )
                        db.add(missile_test)
                        db.flush()

                        # Add shots for each missile impact test (2-4 shots)
                        num_shots = random.randint(2, 4)
                        shot_notes = [
                            "Direct impact on center panel",
                            "Corner impact test",
                            "Edge impact evaluation",
                            "Multiple impact sequence"
                        ]
                        
                        for shot_idx in range(num_shots):
                            shot = Shot(
                                area=random.uniform(0.5, 2.5),  # square meters
                                velocity=random.uniform(15.0, 50.0),  # m/s
                                result=random.choice([True, True, False]),  # Mostly pass
                                note=random.choice(shot_notes) if shot_idx < len(shot_notes) else f"Shot {shot_idx + 1}",
                                missile_impact_test_id=missile_test.id
                            )
                            db.add(shot)

        db.commit()
        print("\n" + "="*60)
        print("Database populated successfully!")
        print("="*60)
        print(f"Created:")
        print(f"  - 2 Devices (ID {devices[0].id} and {devices[1].id})")
        print(f"  - 8 Project Parents (4 per device)")
        print(f"  - 24 Specimens (3 per parent, 12 per device)")
        print(f"  - 192 Cyclic Tests (8 per specimen)")
        print(f"  - 144 Static Tests (6 per specimen)")
        print(f"  - Multiple Infiltration Tests, Missile Impact Tests, Shots, and Test Results with Deflections")
        print("="*60)
    except Exception as e:
        db.rollback()
        print(f"Error populating database: {e}")
        import traceback
        traceback.print_exc()
    finally:
        db.close()

if __name__ == "__main__":
    populate_database()
