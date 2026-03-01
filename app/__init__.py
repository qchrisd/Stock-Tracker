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

    # Ensure instance/ directory exists and point the registry DB there
    os.makedirs(app.instance_path, exist_ok=True)
    app.config['SQLALCHEMY_DATABASE_URI'] = (
        os.environ.get('DATABASE_URL') or
        'sqlite:///' + os.path.join(app.instance_path, 'stock_tracker.db')
    )

    # Initialize Flask-SQLAlchemy (registry database: portfolio_meta, watchlist_meta)
    db.init_app(app)

    # Initialize multi-database manager (cache.db + per-portfolio/watchlist dbs)
    from app.database import db_manager
    db_manager.init_app(app)

    # Register blueprints
    from app.routes import main_bp
    app.register_blueprint(main_bp)

    # Create registry tables and seed default portfolio/watchlist if needed
    with app.app_context():
        db.create_all()
        _seed_defaults(db_manager)

    # Start background cache scheduler (daemon thread)
    # Guard against double-start in Flask's reloader child process
    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        from app.routes import start_scheduler_thread
        start_scheduler_thread(app)

    return app


def _seed_defaults(db_manager):
    """Ensure at least one portfolio and one watchlist exist in the registry."""
    from app.models import db, PortfolioMeta, WatchlistMeta

    if PortfolioMeta.query.count() == 0:
        p = PortfolioMeta(name='My Portfolio', description='Default portfolio', sort_order=0)
        db.session.add(p)
        db.session.commit()
        db_manager.ensure_portfolio_db(p.id)

    if WatchlistMeta.query.count() == 0:
        w = WatchlistMeta(name='My Watchlist', description='Default watchlist', sort_order=0)
        db.session.add(w)
        db.session.commit()
        db_manager.ensure_watchlist_db(w.id)

    # Make sure every existing portfolio/watchlist has its DB file + engine loaded
    for p in PortfolioMeta.query.all():
        db_manager.ensure_portfolio_db(p.id)
    for w in WatchlistMeta.query.all():
        db_manager.ensure_watchlist_db(w.id)
