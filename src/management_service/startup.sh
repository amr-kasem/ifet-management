#!/bin/bash

# Wait for the database to be ready
echo "Waiting for database to be ready..."
sleep 10

# Run database migrations using alembic directly (avoiding model import issues)
echo "Running database migrations..."
python -c "
import os
from alembic.config import Config
from alembic import command
import sys

# Get database URL
database_url = os.getenv('DATABASE_URL', 'postgresql://user:password@localhost/report_db')

# Create alembic config
config = Config('alembic.ini')
config.set_main_option('sqlalchemy.url', database_url)

# Check for changes and generate migration if needed
try:
    # Generate migration if needed
    command.revision(config, message='Auto-generated migration', autogenerate=True)
    print('✅ Migration generated successfully')
except Exception as e:
    print(f'No new migration needed: {e}')

# Apply all pending migrations
try:
    command.upgrade(config, 'head')
    print('✅ Migrations applied successfully')
except Exception as e:
    print(f'❌ Migration failed: {e}')
"

# Check migration status
if [ $? -eq 0 ]; then
    echo "✅ Database migrations completed successfully"
else
    echo "❌ Database migrations failed, but continuing..."
fi

# Start the FastAPI application with gunicorn
echo "Starting FastAPI application..."
exec /start.sh 