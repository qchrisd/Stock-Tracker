#!/usr/bin/env python
"""Entry point for the Stock Tracker web application"""
import os
from app import create_app

if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, port=port, host='0.0.0.0')
