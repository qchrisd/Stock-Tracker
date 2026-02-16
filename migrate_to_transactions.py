#!/usr/bin/env python3
"""
Migration script to convert old Stock model entries to new Transaction system
"""
from app import create_app, db
from app.models import Stock, Transaction
from datetime import datetime

def migrate_stock_to_transactions():
    """Convert existing Stock records to Transaction records"""
    app = create_app()
    
    with app.app_context():
        # Create all tables first
        db.create_all()
        
        # This script will be used if needed to migrate existing stocks to transactions
        # For now, new stocks will be added directly as transactions
        print("Database tables created successfully!")
        print("New stocks can now be added with transaction tracking")

if __name__ == '__main__':
    migrate_stock_to_transactions()
