import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.utils import run_migrations
from app.models import Device, Project, CyclicTest, StaticTest, InfiltrationTest, MissileImpactTest, Shot, Deflection
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
        # Create 2 devices
        for device_id in range(1, 3):
            device = Device(name=f"Device {device_id}")
            db.add(device)
            db.flush()

            # Create 5 projects for each device
            for project_id in range(1, 6):
                project = Project(name=f"Project {device_id}-{project_id}", device_id=device.id)
                db.add(project)
                db.flush()

                # Create 3 static tests for each project
                for _ in range(3):
                    static_test = StaticTest(
                        project_id=project.id,
                        pressure_factor=random.uniform(0.5, 2.0),
                        pressure=random.uniform(100.0, 1000.0)
                    )
                    db.add(static_test)
                    db.flush()

                    # Create 2 deflections for each static test
                    for _ in range(2):
                        deflection = Deflection(
                            static_test_id=static_test.id,
                            deflection_gauge=random.randint(1, 10),
                            max_deflection=random.uniform(0.1, 5.0),
                            permanent_deflection=random.uniform(0.1, 5.0),
                            recovery=random.uniform(0.1, 5.0)
                        )
                        db.add(deflection)

                # Create 2 infiltration tests for each project
                for _ in range(2):
                    infiltration_test = InfiltrationTest(
                        project_id=project.id,
                        type=random.choice(["Type A", "Type B", "Type C"]),
                        pressure=random.uniform(0.1, 5.0),
                        duration=random.uniform(1.0, 48.0),
                        leakage=random.uniform(0.1, 10.0)
                    )
                    db.add(infiltration_test)

                # Create 2 missile impact tests for each project
                for _ in range(2):
                    missile_impact_test = MissileImpactTest(
                        project_id=project.id,
                        missile=random.choice(["Missile X", "Missile Y", "Missile Z"]),
                        missile_weight=random.uniform(1.0, 100.0)
                    )
                    db.add(missile_impact_test)
                    db.flush()

                    # Create 3 shots for each missile impact test
                    for _ in range(3):
                        shot = Shot(
                            missile_impact_test_id=missile_impact_test.id,
                            area=random.uniform(0.1, 10.0),
                            velocity=random.uniform(50.0, 500.0),
                            result=random.choice([True, False]),
                            note=f"Shot note for test {missile_impact_test.id}"
                        )
                        db.add(shot)

                # Create 3 cyclic tests for each project
                for _ in range(3):
                    cyclic_test = CyclicTest(
                        project_id=project.id,
                        type=random.choice(["Type 1", "Type 2", "Type 3"]),
                        cycles=random.randint(100, 10000),
                        low_pressure=random.uniform(10.0, 50.0),
                        high_pressure=random.uniform(51.0, 200.0),
                        deflection=random.uniform(0.1, 5.0),
                        permanent_set=random.uniform(0.01, 1.0),
                        result=random.choice([True, False]),
                        note=f"Cyclic test note for project {project.id}"
                    )
                    db.add(cyclic_test)

        db.commit()
        print("Database populated successfully!")
    except Exception as e:
        db.rollback()
        print(f"Error populating database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    populate_database()

