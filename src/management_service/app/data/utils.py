import os
import logging
from alembic.config import Config
from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from app.data.models import Base

logger = logging.getLogger(__name__)

def get_alembic_config() -> Config:
    """Get Alembic configuration."""
    # Get the directory where this file is located
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the management_service directory
    service_dir = os.path.dirname(os.path.dirname(current_dir))
    alembic_ini_path = os.path.join(service_dir, "alembic.ini")
    
    if not os.path.exists(alembic_ini_path):
        raise FileNotFoundError(f"Alembic configuration file not found at {alembic_ini_path}")
    
    alembic_cfg = Config(alembic_ini_path)
    
    # Set the database URL from environment variable if available
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        alembic_cfg.set_main_option("sqlalchemy.url", database_url)
    
    return alembic_cfg

def init_alembic_if_needed(engine: Engine):
    """Initialize Alembic if it hasn't been initialized yet."""
    try:
        alembic_cfg = get_alembic_config()
        
        # Check if alembic_version table exists
        inspector = inspect(engine)
        if 'alembic_version' not in inspector.get_table_names():
            logger.info("Initializing Alembic for the first time...")
            command.stamp(alembic_cfg, "head")
            logger.info("Alembic initialized successfully")
        
    except Exception as e:
        logger.warning(f"Could not initialize Alembic: {e}")
        logger.info("Falling back to basic table creation...")
        Base.metadata.create_all(bind=engine)

def check_for_model_changes(engine: Engine) -> bool:
    """Check if there are any pending model changes that need migration."""
    try:
        alembic_cfg = get_alembic_config()
        
        # Create a migration context
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            
            # Compare current metadata with database schema
            diff = compare_metadata(context, Base.metadata)
            
            # If there are differences, we need a migration
            return len(diff) > 0
            
    except Exception as e:
        logger.error(f"Error checking for model changes: {e}")
        return False

def generate_migration_if_needed(engine: Engine, message: str = "Auto-generated migration") -> bool:
    """Generate a new migration if model changes are detected."""
    try:
        if not check_for_model_changes(engine):
            logger.info("No model changes detected")
            return False
        
        alembic_cfg = get_alembic_config()
        
        logger.info("Model changes detected, generating migration...")
        command.revision(alembic_cfg, message=message, autogenerate=True)
        logger.info("Migration generated successfully")
        return True
        
    except Exception as e:
        logger.error(f"Error generating migration: {e}")
        return False

def apply_migrations(engine: Engine):
    """Apply all pending migrations."""
    try:
        alembic_cfg = get_alembic_config()
        
        logger.info("Applying pending migrations...")
        command.upgrade(alembic_cfg, "head")
        logger.info("Migrations applied successfully")
        
    except Exception as e:
        logger.error(f"Error applying migrations: {e}")
        raise

def run_migrations(engine: Engine, auto_generate: bool = True):
    """
    Run database migrations with automatic model change detection.
    
    Args:
        engine: SQLAlchemy engine
        auto_generate: Whether to automatically generate migrations for model changes
    """
    try:
        # Initialize Alembic if needed
        init_alembic_if_needed(engine)
        
        if auto_generate:
            # Check for model changes and generate migration if needed
            if generate_migration_if_needed(engine):
                logger.info("New migration generated due to model changes")
        
        # Apply all pending migrations
        apply_migrations(engine)
        
        logger.info("Database migration completed successfully")
        
    except Exception as e:
        logger.error(f"Migration failed: {e}")
        logger.info("Falling back to basic table creation...")
        # Fallback to the old method if migrations fail
        Base.metadata.create_all(bind=engine)

def reset_database(engine: Engine):
    """Reset the database by dropping all tables and recreating them."""
    try:
        logger.warning("Resetting database - dropping all tables...")
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        
        # Reinitialize Alembic
        alembic_cfg = get_alembic_config()
        command.stamp(alembic_cfg, "head")
        
        logger.info("Database reset completed")
        
    except Exception as e:
        logger.error(f"Error resetting database: {e}")
        raise

def create_manual_migration(message: str):
    """Create a manual migration file."""
    try:
        alembic_cfg = get_alembic_config()
        command.revision(alembic_cfg, message=message)
        logger.info(f"Manual migration created: {message}")
        
    except Exception as e:
        logger.error(f"Error creating manual migration: {e}")
        raise

if __name__ == "__main__":
    # For testing purposes
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/report_db")
    engine = create_engine(DATABASE_URL)
    run_migrations(engine)
