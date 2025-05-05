from sqlalchemy import Column, Integer, String, Float, ForeignKey, Boolean
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    
    turbo_mode = Column(Boolean, nullable=False)
    turbo_slave = Column(Boolean, nullable=False)
    turbo_charger = Column(Integer, ForeignKey('devices.id'), nullable=True)
    
    projects = relationship("Project", back_populates="device", cascade="all, delete-orphan")

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    device_id = Column(Integer, ForeignKey('devices.id'), nullable=False)
    inward_design_pressure = Column(Float, nullable=False)
    outward_design_pressure = Column(Float, nullable=False)
    device = relationship("Device", back_populates="projects")
    static_tests = relationship("StaticTest", back_populates="project", cascade="all, delete-orphan")
    infiltration_tests = relationship("InfiltrationTest", back_populates="project", cascade="all, delete-orphan")
    missile_impact_tests = relationship("MissileImpactTest", back_populates="project", cascade="all, delete-orphan")
    cyclic_tests = relationship("CyclicTest", back_populates="project", cascade="all, delete-orphan")

class StaticTest(Base):
    __tablename__ = "static_tests"
    finished = Column(Boolean, nullable=False)
    id = Column(Integer, primary_key=True, index=True)
    index = Column(Integer, nullable=False)
    pressure_factor = Column(String, nullable=False)
    pressure = Column(Float, nullable=False)
    duration = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    preset = Column(Boolean, nullable=False, default=False)
    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="static_tests")
    trials = relationship("StaticTestResult", back_populates="static_test", cascade="all, delete-orphan")

class Deflection(Base):
    __tablename__ = "deflections"

    id = Column(Integer, primary_key=True, index=True)
    deflection_gauge = Column(Integer, nullable=False)
    max_deflection = Column(Float, nullable=False)
    permanent_deflection = Column(Float, nullable=False)
    recovery = Column(Float, nullable=False)

    test_id = Column(Integer, ForeignKey('test_results.id'))
    test = relationship("TestResult", back_populates="deflections")

class InfiltrationTest(Base):
    __tablename__ = "infiltration_tests"

    id = Column(Integer, primary_key=True, index=True)
    type = Column(String, nullable=False)
    pressure = Column(Float, nullable=False)
    duration = Column(Float, nullable=True)
    leakage = Column(Float, nullable=True)
    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="infiltration_tests")

class MissileImpactTest(Base):
    __tablename__ = "missile_impact_tests"

    id = Column(Integer, primary_key=True, index=True)
    missile = Column(String, nullable=False)
    missile_weight = Column(Float, nullable=False)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="missile_impact_tests")

    shots = relationship("Shot", back_populates="missile_impact_test", cascade="all, delete-orphan")

class Shot(Base):
    __tablename__ = "shots"

    id = Column(Integer, primary_key=True, index=True)
    area = Column(Float, nullable=False)
    velocity = Column(Float, nullable=False)
    result = Column(Boolean, nullable=False)
    note = Column(String, nullable=False)

    missile_impact_test_id = Column(Integer, ForeignKey('missile_impact_tests.id'))
    missile_impact_test = relationship("MissileImpactTest", back_populates="shots")

class CyclicTest(Base):
    __tablename__ = "cyclic_tests"

    finished = Column(Boolean, nullable=False)
    id = Column(Integer, primary_key=True, index=True)
    index = Column(Integer, nullable=False)
    type = Column(String, nullable=False)
    cycles = Column(Integer, nullable=False)
    low_pressure = Column(Float, nullable=False)
    high_pressure = Column(Float, nullable=False)
    resume = Column(Boolean, nullable=False)
    current_cycle = Column(Integer, nullable=False)
    preset = Column(Boolean, nullable=False, default=False)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="cyclic_tests")
    
    trials = relationship("CyclicTestResult", back_populates="cyclic_test", cascade="all, delete-orphan")

class TestResult(Base):
    __tablename__ = "test_results"

    id = Column(Integer, primary_key=True, index=True)
    trial_number = Column(Integer, nullable=False)
    result = Column(Boolean, nullable=True)
    note = Column(String, nullable=True)
    deflections = relationship("Deflection", back_populates="test", cascade="all, delete-orphan")

class CyclicTestResult(TestResult):
    __tablename__ = "cyclic_test_results"
    id = Column(Integer, ForeignKey('test_results.id'), primary_key=True, index=True)
    cyclic_test_id = Column(Integer, ForeignKey('cyclic_tests.id'))
    cyclic_test = relationship("CyclicTest", back_populates="trials")

class StaticTestResult(TestResult):
    __tablename__ = "static_test_results"
    id = Column(Integer, ForeignKey('test_results.id'), primary_key=True, index=True)
    static_test_id = Column(Integer, ForeignKey('static_tests.id'))
    static_test = relationship("StaticTest", back_populates="trials")