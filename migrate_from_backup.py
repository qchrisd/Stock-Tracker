#!/usr/bin/env python3
"""
Migration script to move data from stock_tracker.db.backup to stock_tracker.db
"""

import sqlite3
import os
from datetime import datetime

def migrate_data():
    """Migrate data from backup database to current database"""
    
    backup_db = '/home/chris/Stock-Tracker/stock_tracker.db.backup'
    current_db = '/home/chris/Stock-Tracker/stock_tracker.db'
    
    if not os.path.exists(backup_db):
        print(f"Error: Backup database not found at {backup_db}")
        return False
    
    try:
        # Connect to both databases
        backup_conn = sqlite3.connect(backup_db)
        current_conn = sqlite3.connect(current_db)
        
        backup_cursor = backup_conn.cursor()
        current_cursor = current_conn.cursor()
        
        print("Starting migration from backup database...")
        
        # Get list of tables from backup
        backup_cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = backup_cursor.fetchall()
        
        if not tables:
            print("No tables found in backup database")
            backup_conn.close()
            current_conn.close()
            return False
        
        print(f"Found {len(tables)} table(s) in backup database")
        
        # Migrate each table
        for (table_name,) in tables:
            print(f"\nMigrating table: {table_name}")
            
            # Get the schema
            backup_cursor.execute(f"PRAGMA table_info([{table_name}])")
            columns = backup_cursor.fetchall()
            
            if not columns:
                print(f"  Skipping {table_name} (no columns found)")
                continue
            
            # Get column names
            column_names = [col[1] for col in columns]
            
            # Fetch all data from backup table
            backup_cursor.execute(f"SELECT * FROM [{table_name}]")
            rows = backup_cursor.fetchall()
            
            print(f"  Found {len(rows)} rows in {table_name}")
            
            if rows:
                # Check if the table exists in current db
                current_cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
                table_exists = current_cursor.fetchone() is not None
                
                if not table_exists:
                    print(f"  Table {table_name} does not exist in current database")
                    continue
                
                # Clear existing data in current table
                current_cursor.execute(f"DELETE FROM [{table_name}]")
                print(f"  Cleared existing data from {table_name}")
                
                # Insert data
                placeholders = ','.join(['?' for _ in column_names])
                insert_query = f"INSERT INTO [{table_name}] ({','.join([f'[{col}]' for col in column_names])}) VALUES ({placeholders})"
                
                current_cursor.executemany(insert_query, rows)
                print(f"  Inserted {len(rows)} rows into {table_name}")
        
        # Commit changes
        current_conn.commit()
        
        # Close connections
        backup_conn.close()
        current_conn.close()
        
        print("\nMigration completed successfully!")
        return True
        
    except Exception as e:
        print(f"Error during migration: {str(e)}")
        return False

if __name__ == '__main__':
    success = migrate_data()
    exit(0 if success else 1)
