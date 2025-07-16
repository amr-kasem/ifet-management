#!/usr/bin/env python3
"""
Database management script for IFET Management Service.

This script provides commands to manage database migrations manually.
"""

import argparse
import os
import sys
import logging
from sqlalchemy import create_engine

# Add the app directory to the path
sys.path.append(os.path.join(os.path.dirname(__file__), 'app'))

from app.data.utils import (
    run_migrations, 
    reset_database, 
    create_manual_migration,
    check_for_model_changes,
    apply_migrations
)

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def get_database_engine():
    """Get database engine from environment."""
    database_url = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/report_db")
    return create_engine(database_url)

def main():
    parser = argparse.ArgumentParser(description="Database management for IFET")
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Run migrations')
    migrate_parser.add_argument('--auto', action='store_true', 
                               help='Automatically generate migrations for model changes')

    # Check command
    subparsers.add_parser('check', help='Check for pending model changes')

    # Create migration command
    create_parser = subparsers.add_parser('create', help='Create a new migration')
    create_parser.add_argument('message', help='Migration message')

    # Reset command
    reset_parser = subparsers.add_parser('reset', help='Reset database (WARNING: destructive)')
    reset_parser.add_argument('--confirm', action='store_true', 
                            help='Confirm database reset')

    # Apply command
    subparsers.add_parser('apply', help='Apply pending migrations without auto-generation')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    engine = get_database_engine()

    try:
        if args.command == 'migrate':
            auto_generate = args.auto if hasattr(args, 'auto') else True
            run_migrations(engine, auto_generate=auto_generate)
            print("✅ Migration completed successfully")

        elif args.command == 'check':
            has_changes = check_for_model_changes(engine)
            if has_changes:
                print("📋 Model changes detected - migration needed")
                sys.exit(1)
            else:
                print("✅ No model changes detected")

        elif args.command == 'create':
            create_manual_migration(args.message)
            print(f"✅ Migration created: {args.message}")

        elif args.command == 'reset':
            if not args.confirm:
                print("❌ Database reset requires --confirm flag")
                print("⚠️  WARNING: This will delete all data!")
                sys.exit(1)
            
            reset_database(engine)
            print("✅ Database reset completed")

        elif args.command == 'apply':
            apply_migrations(engine)
            print("✅ Migrations applied successfully")

    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 