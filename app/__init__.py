from flask import Flask
from app.models import db
from app.config import config
import os


def create_app(config_name=None):
    """Application factory function"""
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')

    app = Flask(__name__)
    app.config.from_object(config.get(config_name, config['default']))

    # Initialize extensions
    db.init_app(app)

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    # Create database tables
    with app.app_context():
        db.create_all()

    # Start background cache scheduler (daemon thread, fires once per configured schedule)
    # Guard against double-start in Flask's reloader child process
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        from app.routes import start_scheduler_thread
        start_scheduler_thread(app)

    return app
