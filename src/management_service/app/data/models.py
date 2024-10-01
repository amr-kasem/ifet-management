from sqlalchemy import Column, Integer, String, Float, ForeignKey, Boolean
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
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
    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="static_tests")

    deflections = relationship("Deflection", back_populates="static_test", cascade="all, delete-orphan")

class Deflection(Base):
    __tablename__ = "deflections"

    id = Column(Integer, primary_key=True, index=True)
    deflection_gauge = Column(Integer, nullable=False)
    max_deflection = Column(Float, nullable=False)
    permanent_deflection = Column(Float, nullable=False)
    recovery = Column(Float, nullable=False)

    static_test_id = Column(Integer, ForeignKey('static_tests.id'))
    static_test = relationship("StaticTest", back_populates="deflections")

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
    deflection = Column(Float, nullable=True)
    permanent_set = Column(Float, nullable=True)
    result = Column(Boolean, nullable=True)
    note = Column(String, nullable=True)

    project_id = Column(Integer, ForeignKey('projects.id'))
    project = relationship("Project", back_populates="cyclic_tests")
