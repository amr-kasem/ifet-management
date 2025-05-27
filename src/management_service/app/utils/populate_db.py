import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.data.utils import run_migrations
from app.data.models import Device, Project, CyclicTest, StaticTest, InfiltrationTest, MissileImpactTest, Shot, Deflection
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
        # Create 2 devices
        for device_id in range(1, 3):
            device = Device(
                name=f"Device {device_id}", 
                turbo_mode=False,
                turbo_slave=False,
                turbo_charger=None
            )
            db.add(device)
            db.flush()

            # Create 2 projects for each device
            for project_id in range(1, 3):
                project = Project(
                    name=f"Project {device_id}-{project_id}", 
                    device_id=device.id,
                    inward_design_pressure=random.uniform(100.0, 500.0),
                    outward_design_pressure=random.uniform(100.0, 500.0)
                )
                db.add(project)
                db.flush()

              
               
        db.commit()
        print("Database populated successfully!")
    except Exception as e:
        db.rollback()
        print(f"Error populating database: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    populate_database()
