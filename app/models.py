from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class PortfolioMeta(db.Model):
    """Registry entry for a tracked portfolio (each has its own database file)."""
    __tablename__ = 'portfolio_meta'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<PortfolioMeta {self.id} "{self.name}">'


class WatchlistMeta(db.Model):
    """Registry entry for a watchlist (each has its own database file)."""
    __tablename__ = 'watchlist_meta'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.String(500), default='')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    sort_order = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<WatchlistMeta {self.id} "{self.name}">'
