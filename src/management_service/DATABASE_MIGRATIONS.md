# Database Migrations Guide

This guide explains how to use the automatic database migration system for the IFET Management Service.

## Overview

The system automatically detects changes to your SQLAlchemy models and generates/applies database migrations using Alembic. This ensures your database schema stays in sync with your model definitions.

## Features

- **Automatic Detection**: Detects when model attributes are added, removed, or modified
- **Auto-Generation**: Automatically creates migration files for detected changes
- **Safe Application**: Applies migrations safely with rollback capability
- **Manual Control**: Provides manual commands for complex scenarios
- **Fallback**: Falls back to basic table creation if migrations fail

## How It Works

1. **On Application Start**: The system checks for model changes
2. **Change Detection**: Compares current models with database schema
3. **Migration Generation**: Creates migration files for detected changes
4. **Application**: Applies all pending migrations to the database

## Usage

### Automatic Mode (Default)

When you start your application, migrations run automatically:

```python
from app.data.utils import run_migrations
from sqlalchemy import create_engine

engine = create_engine(DATABASE_URL)
run_migrations(engine)  # Auto-generates and applies migrations
```

### Manual Commands

Use the `manage_db.py` script for manual control:

#### Check for Changes
```bash
python manage_db.py check
```

#### Run Migrations with Auto-Generation
```bash
python manage_db.py migrate --auto
```

#### Run Migrations Without Auto-Generation
```bash
python manage_db.py apply
```

#### Create Manual Migration
```bash
python manage_db.py create "Add new column to users table"
```

#### Reset Database (Destructive)
```bash
python manage_db.py reset --confirm
```

## Examples

### Adding a New Column

1. Add the column to your model:
```python
class Device(Base):
    __tablename__ = "devices"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # New column added
    description = Column(String, nullable=True)  # NEW
```

2. Start your application or run migrations:
```bash
python manage_db.py migrate
```

The system will:
- Detect the new `description` column
- Generate a migration file automatically
- Apply the migration to add the column to the database

### Modifying an Existing Column

1. Modify the column in your model:
```python
class Project(Base):
    __tablename__ = "projects"
    
    # Changed from nullable=False to nullable=True
    name = Column(String, nullable=True)  # MODIFIED
```

2. The system will automatically generate and apply a migration to update the column constraints.

### Removing a Column

1. Remove the column from your model:
```python
class Device(Base):
    __tablename__ = "devices"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    # removed: description = Column(String, nullable=True)
```

2. The system will generate a migration to drop the column.

## Environment Variables

- `DATABASE_URL`: PostgreSQL connection string (required)
- Example: `postgresql://user:password@localhost/report_db`

## Docker Integration

The system works seamlessly with Docker. In your `compose.yaml`:

```yaml
report-api:
  build: ./src/management_service/
  environment:
    DATABASE_URL: "postgresql://user:password@db:5432/report_db"
  depends_on:
    - db
```

## Migration Files

Migration files are stored in `alembic/versions/` and contain:
- `upgrade()`: Function to apply the change
- `downgrade()`: Function to rollback the change
- Metadata about the migration

## Best Practices

1. **Review Generated Migrations**: Always review auto-generated migrations before deploying
2. **Backup Before Major Changes**: Backup your database before major schema changes
3. **Test Migrations**: Test migrations in a development environment first
4. **Manual Migrations**: Use manual migrations for complex changes that require custom logic

## Troubleshooting

### Migration Fails
If automatic migration fails, the system falls back to basic table creation:
```
Migration failed: [error details]
Falling back to basic table creation...
```

### Reset Database
If you need to start fresh:
```bash
python manage_db.py reset --confirm
```

### Check Migration Status
```bash
python manage_db.py check
```

## Configuration Files

- `alembic.ini`: Alembic configuration
- `alembic/env.py`: Environment setup for migrations
- `alembic/script.py.mako`: Template for migration files

## Safety Features

- **Automatic Backup**: Always backup before destructive operations
- **Rollback Capability**: All migrations can be rolled back
- **Change Detection**: Only creates migrations when actually needed
- **Error Handling**: Graceful fallback if migrations fail
- **Logging**: Detailed logging of all migration operations

## Development Workflow

1. Modify your models in `app/data/models.py`
2. Start your application (migrations run automatically)
3. Or manually run: `python manage_db.py migrate`
4. Review the generated migration file
5. Deploy to production

The system handles the rest automatically! 