import os
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from app.data.models import Project, ProjectParent, StaticTest, InfiltrationTest, MissileImpactTest, CyclicTest

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/dbname")
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def find_duplicate_projects(db):
    """Find projects with duplicate names."""
    duplicates = db.query(
        Project.name,
        func.count(Project.id).label('count')
    ).group_by(Project.name).having(func.count(Project.id) > 1).all()
    
    result = []
    for name, count in duplicates:
        projects = db.query(Project).filter(Project.name == name).all()
        result.append({
            'name': name,
            'count': count,
            'projects': projects
        })
    return result


def find_duplicate_project_parents(db):
    """Find project parents with duplicate names."""
    duplicates = db.query(
        ProjectParent.name,
        func.count(ProjectParent.id).label('count')
    ).group_by(ProjectParent.name).having(func.count(ProjectParent.id) > 1).all()
    
    result = []
    for name, count in duplicates:
        parents = db.query(ProjectParent).filter(ProjectParent.name == name).all()
        result.append({
            'name': name,
            'count': count,
            'parents': parents
        })
    return result


def get_project_references_count(db, project_id):
    """Get count of all references to a project."""
    static_tests_count = db.query(StaticTest).filter(StaticTest.project_id == project_id).count()
    infiltration_tests_count = db.query(InfiltrationTest).filter(InfiltrationTest.project_id == project_id).count()
    missile_tests_count = db.query(MissileImpactTest).filter(MissileImpactTest.project_id == project_id).count()
    cyclic_tests_count = db.query(CyclicTest).filter(CyclicTest.project_id == project_id).count()
    
    return {
        'static_tests': static_tests_count,
        'infiltration_tests': infiltration_tests_count,
        'missile_impact_tests': missile_tests_count,
        'cyclic_tests': cyclic_tests_count,
        'total': static_tests_count + infiltration_tests_count + missile_tests_count + cyclic_tests_count
    }


def get_project_parent_references_count(db, parent_id):
    """Get count of all projects referencing a project parent."""
    projects_count = db.query(Project).filter(Project.parent_id == parent_id).count()
    return projects_count


def merge_projects(db, keep_project_id, duplicate_project_ids):
    """Merge duplicate projects into the selected one."""
    try:
        for dup_id in duplicate_project_ids:
            # Update all static tests
            db.query(StaticTest).filter(StaticTest.project_id == dup_id).update(
                {StaticTest.project_id: keep_project_id}
            )
            
            # Update all infiltration tests
            db.query(InfiltrationTest).filter(InfiltrationTest.project_id == dup_id).update(
                {InfiltrationTest.project_id: keep_project_id}
            )
            
            # Update all missile impact tests
            db.query(MissileImpactTest).filter(MissileImpactTest.project_id == dup_id).update(
                {MissileImpactTest.project_id: keep_project_id}
            )
            
            # Update all cyclic tests
            db.query(CyclicTest).filter(CyclicTest.project_id == dup_id).update(
                {CyclicTest.project_id: keep_project_id}
            )
            
            # Delete the duplicate project
            db.query(Project).filter(Project.id == dup_id).delete()
        
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"Error merging projects: {e}")
        return False


def merge_project_parents(db, keep_parent_id, duplicate_parent_ids):
    """Merge duplicate project parents into the selected one."""
    try:
        for dup_id in duplicate_parent_ids:
            # Update all projects referencing this parent
            db.query(Project).filter(Project.parent_id == dup_id).update(
                {Project.parent_id: keep_parent_id}
            )
            
            # Delete the duplicate parent
            db.query(ProjectParent).filter(ProjectParent.id == dup_id).delete()
        
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"Error merging project parents: {e}")
        return False


def display_duplicate_projects(duplicates):
    """Display duplicate projects for user selection."""
    if not duplicates:
        print("\nNo duplicate projects found.")
        return
    
    print("\n" + "="*80)
    print("DUPLICATE PROJECTS FOUND")
    print("="*80)
    
    for idx, dup_group in enumerate(duplicates, 1):
        print(f"\n[{idx}] Project Name: '{dup_group['name']}' ({dup_group['count']} duplicates)")
        print("-" * 80)
        
        for project in dup_group['projects']:
            refs = get_project_references_count(SessionLocal(), project.id)
            print(f"  ID: {project.id}")
            print(f"    Device ID: {project.device_id}")
            print(f"    Parent ID: {project.parent_id}")
            print(f"    Inward Pressure: {project.inward_design_pressure}")
            print(f"    Outward Pressure: {project.outward_design_pressure}")
            print(f"    References: {refs['total']} total "
                  f"(Static: {refs['static_tests']}, "
                  f"Infiltration: {refs['infiltration_tests']}, "
                  f"Missile: {refs['missile_impact_tests']}, "
                  f"Cyclic: {refs['cyclic_tests']})")
            print()


def display_duplicate_project_parents(duplicates):
    """Display duplicate project parents for user selection."""
    if not duplicates:
        print("\nNo duplicate project parents found.")
        return
    
    print("\n" + "="*80)
    print("DUPLICATE PROJECT PARENTS FOUND")
    print("="*80)
    
    for idx, dup_group in enumerate(duplicates, 1):
        print(f"\n[{idx}] Parent Name: '{dup_group['name']}' ({dup_group['count']} duplicates)")
        print("-" * 80)
        
        for parent in dup_group['parents']:
            refs_count = get_project_parent_references_count(SessionLocal(), parent.id)
            print(f"  ID: {parent.id}")
            print(f"    Referenced by {refs_count} project(s)")
            print()


def interactive_merge_projects():
    """Interactive mode for merging duplicate projects."""
    db = SessionLocal()
    try:
        duplicates = find_duplicate_projects(db)
        display_duplicate_projects(duplicates)
        
        if not duplicates:
            return
        
        print("\n" + "="*80)
        print("MERGE DUPLICATE PROJECTS")
        print("="*80)
        
        for idx, dup_group in enumerate(duplicates, 1):
            print(f"\nProcessing duplicate group [{idx}]: '{dup_group['name']}'")
            print("Available IDs:", [p.id for p in dup_group['projects']])
            
            while True:
                try:
                    keep_id = int(input("Enter the ID to KEEP (others will be deleted): "))
                    if keep_id in [p.id for p in dup_group['projects']]:
                        break
                    else:
                        print("Invalid ID. Please choose from the available IDs.")
                except ValueError:
                    print("Please enter a valid integer ID.")
            
            duplicate_ids = [p.id for p in dup_group['projects'] if p.id != keep_id]
            
            confirm = input(f"Confirm: Keep ID {keep_id} and delete IDs {duplicate_ids}? (yes/no): ")
            if confirm.lower() == 'yes':
                success = merge_projects(db, keep_id, duplicate_ids)
                if success:
                    print(f"✓ Successfully merged projects. Kept ID {keep_id}, deleted {duplicate_ids}")
                else:
                    print(f"✗ Failed to merge projects.")
            else:
                print("Skipped.")
    finally:
        db.close()


def interactive_merge_project_parents():
    """Interactive mode for merging duplicate project parents."""
    db = SessionLocal()
    try:
        duplicates = find_duplicate_project_parents(db)
        display_duplicate_project_parents(duplicates)
        
        if not duplicates:
            return
        
        print("\n" + "="*80)
        print("MERGE DUPLICATE PROJECT PARENTS")
        print("="*80)
        
        for idx, dup_group in enumerate(duplicates, 1):
            print(f"\nProcessing duplicate group [{idx}]: '{dup_group['name']}'")
            print("Available IDs:", [p.id for p in dup_group['parents']])
            
            while True:
                try:
                    keep_id = int(input("Enter the ID to KEEP (others will be deleted): "))
                    if keep_id in [p.id for p in dup_group['parents']]:
                        break
                    else:
                        print("Invalid ID. Please choose from the available IDs.")
                except ValueError:
                    print("Please enter a valid integer ID.")
            
            duplicate_ids = [p.id for p in dup_group['parents'] if p.id != keep_id]
            
            confirm = input(f"Confirm: Keep ID {keep_id} and delete IDs {duplicate_ids}? (yes/no): ")
            if confirm.lower() == 'yes':
                success = merge_project_parents(db, keep_id, duplicate_ids)
                if success:
                    print(f"✓ Successfully merged project parents. Kept ID {keep_id}, deleted {duplicate_ids}")
                else:
                    print(f"✗ Failed to merge project parents.")
            else:
                print("Skipped.")
    finally:
        db.close()


def merge_projects_by_ids():
    """Merge specific projects by manually provided IDs."""
    db = SessionLocal()
    try:
        print("\n" + "="*80)
        print("MERGE PROJECTS BY MANUAL ID SELECTION")
        print("="*80)
        
        ids_input = input("\nEnter project IDs separated by commas (e.g., 45, 46, 43): ")
        try:
            project_ids = [int(x.strip()) for x in ids_input.split(',')]
        except ValueError:
            print("Invalid input. Please enter valid integer IDs separated by commas.")
            return
        
        if len(project_ids) < 2:
            print("Please provide at least 2 project IDs to merge.")
            return
        
        # Fetch the projects
        projects = db.query(Project).filter(Project.id.in_(project_ids)).all()
        
        if len(projects) != len(project_ids):
            found_ids = [p.id for p in projects]
            missing_ids = [pid for pid in project_ids if pid not in found_ids]
            print(f"\nWarning: Some IDs were not found in database: {missing_ids}")
            if not projects:
                print("No valid projects found.")
                return
        
        # Display the projects
        print(f"\nFound {len(projects)} project(s):")
        print("-" * 80)
        
        for project in projects:
            refs = get_project_references_count(db, project.id)
            print(f"ID: {project.id}")
            print(f"  Name: {project.name}")
            print(f"  Device ID: {project.device_id}")
            print(f"  Parent ID: {project.parent_id}")
            print(f"  Inward Pressure: {project.inward_design_pressure}")
            print(f"  Outward Pressure: {project.outward_design_pressure}")
            print(f"  References: {refs['total']} total "
                  f"(Static: {refs['static_tests']}, "
                  f"Infiltration: {refs['infiltration_tests']}, "
                  f"Missile: {refs['missile_impact_tests']}, "
                  f"Cyclic: {refs['cyclic_tests']})")
            print()
        
        # Ask which to keep
        available_ids = [p.id for p in projects]
        while True:
            try:
                keep_id = int(input(f"Enter the ID to KEEP from {available_ids} (others will be deleted): "))
                if keep_id in available_ids:
                    break
                else:
                    print(f"Invalid ID. Please choose from: {available_ids}")
            except ValueError:
                print("Please enter a valid integer ID.")
        
        duplicate_ids = [pid for pid in available_ids if pid != keep_id]
        
        confirm = input(f"\nConfirm: Keep project ID {keep_id} and delete IDs {duplicate_ids}? (yes/no): ")
        if confirm.lower() == 'yes':
            success = merge_projects(db, keep_id, duplicate_ids)
            if success:
                print(f"✓ Successfully merged projects. Kept ID {keep_id}, deleted {duplicate_ids}")
            else:
                print(f"✗ Failed to merge projects.")
        else:
            print("Operation cancelled.")
    finally:
        db.close()


def merge_project_parents_by_ids():
    """Merge specific project parents by manually provided IDs."""
    db = SessionLocal()
    try:
        print("\n" + "="*80)
        print("MERGE PROJECT PARENTS BY MANUAL ID SELECTION")
        print("="*80)
        
        ids_input = input("\nEnter project parent IDs separated by commas (e.g., 5, 6, 7): ")
        try:
            parent_ids = [int(x.strip()) for x in ids_input.split(',')]
        except ValueError:
            print("Invalid input. Please enter valid integer IDs separated by commas.")
            return
        
        if len(parent_ids) < 2:
            print("Please provide at least 2 project parent IDs to merge.")
            return
        
        # Fetch the project parents
        parents = db.query(ProjectParent).filter(ProjectParent.id.in_(parent_ids)).all()
        
        if len(parents) != len(parent_ids):
            found_ids = [p.id for p in parents]
            missing_ids = [pid for pid in parent_ids if pid not in found_ids]
            print(f"\nWarning: Some IDs were not found in database: {missing_ids}")
            if not parents:
                print("No valid project parents found.")
                return
        
        # Display the project parents
        print(f"\nFound {len(parents)} project parent(s):")
        print("-" * 80)
        
        for parent in parents:
            refs_count = get_project_parent_references_count(db, parent.id)
            print(f"ID: {parent.id}")
            print(f"  Name: {parent.name}")
            print(f"  Referenced by {refs_count} project(s)")
            print()
        
        # Ask which to keep
        available_ids = [p.id for p in parents]
        while True:
            try:
                keep_id = int(input(f"Enter the ID to KEEP from {available_ids} (others will be deleted): "))
                if keep_id in available_ids:
                    break
                else:
                    print(f"Invalid ID. Please choose from: {available_ids}")
            except ValueError:
                print("Please enter a valid integer ID.")
        
        duplicate_ids = [pid for pid in available_ids if pid != keep_id]
        
        confirm = input(f"\nConfirm: Keep project parent ID {keep_id} and delete IDs {duplicate_ids}? (yes/no): ")
        if confirm.lower() == 'yes':
            success = merge_project_parents(db, keep_id, duplicate_ids)
            if success:
                print(f"✓ Successfully merged project parents. Kept ID {keep_id}, deleted {duplicate_ids}")
            else:
                print(f"✗ Failed to merge project parents.")
        else:
            print("Operation cancelled.")
    finally:
        db.close()


def main():
    """Main menu for duplicate management."""
    while True:
        print("\n" + "="*80)
        print("DUPLICATE MANAGEMENT TOOL")
        print("="*80)
        print("1. Analyze and merge duplicate projects (by name)")
        print("2. Analyze and merge duplicate project parents (by name)")
        print("3. Merge projects by manual ID selection")
        print("4. Merge project parents by manual ID selection")
        print("5. View duplicate projects (analysis only)")
        print("6. View duplicate project parents (analysis only)")
        print("7. Exit")
        
        choice = input("\nEnter your choice (1-7): ")
        
        if choice == '1':
            interactive_merge_projects()
        elif choice == '2':
            interactive_merge_project_parents()
        elif choice == '3':
            merge_projects_by_ids()
        elif choice == '4':
            merge_project_parents_by_ids()
        elif choice == '5':
            db = SessionLocal()
            try:
                duplicates = find_duplicate_projects(db)
                display_duplicate_projects(duplicates)
            finally:
                db.close()
        elif choice == '6':
            db = SessionLocal()
            try:
                duplicates = find_duplicate_project_parents(db)
                display_duplicate_project_parents(duplicates)
            finally:
                db.close()
        elif choice == '7':
            print("Exiting...")
            break
        else:
            print("Invalid choice. Please try again.")


if __name__ == "__main__":
    main()

