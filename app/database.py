"""
Multi-database manager for Stock Tracker.

Three categories of databases:
  1. Registry    – instance/stock_tracker.db   (Flask-SQLAlchemy, see models.py)
  2. Cache        – instance/cache.db           (plain SQLAlchemy, see cache_models.py)
  3. Per-portfolio – instance/portfolios/<id>.db
  4. Per-watchlist  – instance/watchlists/<id>.db  (reuse PortfolioBase schema)
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session


class DatabaseManager:
    """
    Manages SQLAlchemy engines and scoped sessions for all dynamic databases.

    Usage:
        session = db_manager.get_portfolio_session(portfolio_id)
        stocks = session.query(Stock).all()
        session.commit()
        # Sessions are cleaned up automatically at app-context teardown.
    """

    def __init__(self):
        self.instance_path: str | None = None
        self._engines: dict = {}           # key → Engine
        self._factories: dict = {}         # key → scoped_session

    # ── App initialisation ────────────────────────────────────────────────────

    def init_app(self, app):
        """Bind to a Flask app; creates directories and opens the cache database."""
        self.instance_path = app.instance_path
        os.makedirs(os.path.join(self.instance_path, 'portfolios'), exist_ok=True)
        os.makedirs(os.path.join(self.instance_path, 'watchlists'), exist_ok=True)

        # Open the shared cache database and create its tables
        cache_path = os.path.join(self.instance_path, 'cache.db')
        self._open(cache_path, 'cache')
        from app.cache_models import CacheBase
        CacheBase.metadata.create_all(self._engines['cache'])
        self._migrate_cache_db(cache_path)

        # Tear down all scoped sessions at the end of every app-context
        @app.teardown_appcontext
        def _cleanup(_exc):
            for factory in self._factories.values():
                factory.remove()

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def _migrate_cache_db(self, cache_path: str):
        """Add any columns present in the model but missing from the live DB.

        SQLAlchemy's create_all() won't ALTER existing tables, so we do it
        manually here on every startup (safe to run repeatedly — skips cols
        that already exist).
        """
        import sqlite3 as _sqlite3
        from app.cache_models import StockCache
        con = _sqlite3.connect(cache_path)
        try:
            cur = con.cursor()
            cur.execute("PRAGMA table_info(stock_cache)")
            existing = {r[1] for r in cur.fetchall()}
            type_map = {
                'INTEGER': 'INTEGER', 'FLOAT': 'REAL', 'BOOLEAN': 'INTEGER',
                'VARCHAR': 'TEXT', 'DATETIME': 'TEXT', 'STRING': 'TEXT',
            }
            for col in StockCache.__table__.columns:
                if col.name not in existing:
                    sql_type = type_map.get(col.type.__class__.__name__.upper(), 'TEXT')
                    cur.execute(f"ALTER TABLE stock_cache ADD COLUMN {col.name} {sql_type}")
                    print(f"[db-migrate] Added column stock_cache.{col.name} ({sql_type})")
            con.commit()
        finally:
            con.close()

    def _open(self, db_path: str, key: str):
        """Open an engine + scoped session for *db_path* and store under *key*."""
        if key not in self._engines:
            engine = create_engine(
                f'sqlite:///{db_path}',
                connect_args={'check_same_thread': False},
            )
            self._engines[key] = engine
            self._factories[key] = scoped_session(sessionmaker(bind=engine))

    def _session(self, key: str):
        return self._factories[key]

    # ── Cache database ────────────────────────────────────────────────────────

    def get_cache_session(self):
        """Return the scoped session for the shared cache database."""
        return self._session('cache')

    def get_cache_engine(self):
        return self._engines['cache']

    # ── Portfolio databases ───────────────────────────────────────────────────

    def _portfolio_key(self, portfolio_id: int) -> str:
        return f'portfolio_{portfolio_id}'

    def _portfolio_path(self, portfolio_id: int) -> str:
        return os.path.join(self.instance_path, 'portfolios', f'{portfolio_id}.db')

    def ensure_portfolio_db(self, portfolio_id: int) -> str:
        """Create the portfolio database file and tables if they don't exist."""
        key = self._portfolio_key(portfolio_id)
        db_path = self._portfolio_path(portfolio_id)
        self._open(db_path, key)
        from app.portfolio_models import PortfolioBase
        PortfolioBase.metadata.create_all(self._engines[key])
        return db_path

    def get_portfolio_session(self, portfolio_id: int):
        """Return the scoped session for the given portfolio database."""
        key = self._portfolio_key(portfolio_id)
        if key not in self._engines:
            self.ensure_portfolio_db(portfolio_id)
        return self._factories[key]

    # ── Watchlist databases ───────────────────────────────────────────────────

    def _watchlist_key(self, watchlist_id: int) -> str:
        return f'watchlist_{watchlist_id}'

    def _watchlist_path(self, watchlist_id: int) -> str:
        return os.path.join(self.instance_path, 'watchlists', f'{watchlist_id}.db')

    def ensure_watchlist_db(self, watchlist_id: int) -> str:
        """Create the watchlist database file and tables if they don't exist."""
        key = self._watchlist_key(watchlist_id)
        db_path = self._watchlist_path(watchlist_id)
        self._open(db_path, key)
        from app.portfolio_models import PortfolioBase   # watchlists share the same schema
        PortfolioBase.metadata.create_all(self._engines[key])
        return db_path

    def get_watchlist_session(self, watchlist_id: int):
        """Return the scoped session for the given watchlist database."""
        key = self._watchlist_key(watchlist_id)
        if key not in self._engines:
            self.ensure_watchlist_db(watchlist_id)
        return self._factories[key]


# Module-level singleton – initialised by create_app() via db_manager.init_app(app)
db_manager = DatabaseManager()
