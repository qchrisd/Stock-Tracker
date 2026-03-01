"""
migrate_to_multi_db.py
======================
Migrates data from the old single-database (stock_tracker.db.backup) to the
new multi-database architecture:

  instance/stock_tracker.db  — registry (PortfolioMeta, WatchlistMeta)
  instance/portfolios/1.db   — portfolio stocks + transactions + account
  instance/watchlists/1.db   — watchlist stocks + transactions

Usage:
    python migrate_to_multi_db.py [--source stock_tracker.db.backup] [--dry-run]

The script is safe to run multiple times — it skips rows that already exist.
"""

import argparse
import os
import sys
from datetime import datetime

import sqlite3

# ── Args ──────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description='Migrate stock tracker data to multi-db layout')
parser.add_argument('--source', default='stock_tracker.db.backup',
                    help='Path to the old single-file database (default: stock_tracker.db.backup)')
parser.add_argument('--dry-run', action='store_true',
                    help='Print what would be migrated without writing anything')
args = parser.parse_args()

SOURCE_DB = args.source
DRY_RUN = args.dry_run

# ── Bootstrap Flask app so DatabaseManager is available ──────────────────────

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault('FLASK_ENV', 'development')

from app import create_app
from app.models import db, PortfolioMeta, WatchlistMeta
from app.database import db_manager
from app.portfolio_models import Stock, Transaction, Account

app = create_app()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(val):
    """Accept DATE strings in several formats and return a date object."""
    if val is None:
        return None
    if hasattr(val, 'date'):
        return val.date()
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(val)[:19], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date: {val!r}")


def _parse_dt(val):
    """Return a datetime from a stored string, or now()."""
    if val is None:
        return datetime.utcnow()
    if hasattr(val, 'timetuple'):
        return val
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            return datetime.strptime(str(val)[:26], fmt)
        except ValueError:
            continue
    return datetime.utcnow()

# ── Main migration ─────────────────────────────────────────────────────────────

def run():
    if not os.path.exists(SOURCE_DB):
        print(f"ERROR: Source database not found: {SOURCE_DB}")
        print("  Try: python migrate_to_multi_db.py --source path/to/your.db")
        sys.exit(1)

    print(f"Source : {SOURCE_DB}")
    print(f"Dry run: {DRY_RUN}")
    print()

    src = sqlite3.connect(SOURCE_DB)
    src.row_factory = sqlite3.Row

    with app.app_context():
        # ── Ensure at least one portfolio and watchlist exist ─────────────────
        portfolio_meta = PortfolioMeta.query.first()
        watchlist_meta = WatchlistMeta.query.first()

        if portfolio_meta is None:
            if DRY_RUN:
                print("[dry-run] Would create PortfolioMeta 'My Portfolio'")
                portfolio_id = 1
            else:
                portfolio_meta = PortfolioMeta(name='My Portfolio')
                db.session.add(portfolio_meta)
                db.session.commit()
                portfolio_id = portfolio_meta.id
        else:
            portfolio_id = portfolio_meta.id

        if watchlist_meta is None:
            if DRY_RUN:
                print("[dry-run] Would create WatchlistMeta 'My Watchlist'")
                watchlist_id = 1
            else:
                watchlist_meta = WatchlistMeta(name='My Watchlist')
                db.session.add(watchlist_meta)
                db.session.commit()
                watchlist_id = watchlist_meta.id
        else:
            watchlist_id = watchlist_meta.id

        print(f"Target portfolio : id={portfolio_id}  ({portfolio_meta.name if portfolio_meta else 'My Portfolio'})")
        print(f"Target watchlist : id={watchlist_id}  ({watchlist_meta.name if watchlist_meta else 'My Watchlist'})")
        print()

        # ── Open target sessions ──────────────────────────────────────────────
        if not DRY_RUN:
            p_session = db_manager.get_portfolio_session(portfolio_id)
            w_session = db_manager.get_watchlist_session(watchlist_id)

        # ── Migrate stocks ────────────────────────────────────────────────────
        old_stocks = src.execute('SELECT * FROM "stock"').fetchall()
        portfolio_stocks_count = 0
        watchlist_stocks_count = 0
        skipped_stocks = 0

        for row in old_stocks:
            symbol      = row['symbol'].upper().strip()
            add_date    = _parse_date(row['add_date'])
            shares      = float(row['shares'] or 0)
            init_price  = float(row['initial_price'] or 0)
            date_added  = _parse_dt(row['date_added'])
            is_wl       = bool(row['is_watchlist'])

            if is_wl:
                label = f"[watchlist] {symbol}"
                if DRY_RUN:
                    print(f"  [dry-run] Would migrate watchlist stock: {symbol}")
                    watchlist_stocks_count += 1
                    continue
                session = w_session
            else:
                label = f"[portfolio] {symbol}"
                if DRY_RUN:
                    print(f"  [dry-run] Would migrate portfolio stock: {symbol}")
                    portfolio_stocks_count += 1
                    continue
                session = p_session

            existing = session.query(Stock).filter_by(symbol=symbol).first()
            if existing:
                print(f"  SKIP (already exists): {label}")
                skipped_stocks += 1
                continue

            stock = Stock(
                symbol=symbol,
                add_date=add_date,
                shares=shares,
                initial_price=init_price,
                date_added=date_added,
            )
            session.add(stock)

            if is_wl:
                watchlist_stocks_count += 1
            else:
                portfolio_stocks_count += 1
            print(f"  + {label}  shares={shares}  price={init_price}")

        if not DRY_RUN:
            p_session.commit()
            w_session.commit()

        print(f"\nStocks migrated  — portfolio: {portfolio_stocks_count}, watchlist: {watchlist_stocks_count}, skipped: {skipped_stocks}")

        # ── Migrate transactions ──────────────────────────────────────────────
        old_txns = src.execute('SELECT * FROM "transaction" ORDER BY date').fetchall()
        portfolio_txns_count = 0
        watchlist_txns_count = 0
        skipped_txns = 0

        for row in old_txns:
            symbol      = row['symbol'].upper().strip()
            txn_type    = row['type']
            txn_date    = _parse_date(row['date'])
            shares      = float(row['shares']) if row['shares'] is not None else None
            pps         = float(row['price_per_share']) if row['price_per_share'] is not None else None
            amount      = float(row['amount']) if row['amount'] is not None else None
            created_at  = _parse_dt(row['created_at'])
            is_wl       = bool(row['is_watchlist'])

            if DRY_RUN:
                label = "watchlist" if is_wl else "portfolio"
                print(f"  [dry-run] Would migrate {label} txn: {symbol} {txn_type} {txn_date}")
                if is_wl:
                    watchlist_txns_count += 1
                else:
                    portfolio_txns_count += 1
                continue

            session = w_session if is_wl else p_session

            # Deduplicate by symbol + type + date + shares + amount
            dup = session.query(Transaction).filter_by(
                symbol=symbol, type=txn_type, date=txn_date,
            ).first()
            if dup and abs((dup.shares or 0) - (shares or 0)) < 0.0001:
                skipped_txns += 1
                continue

            txn = Transaction(
                symbol=symbol,
                type=txn_type,
                date=txn_date,
                shares=shares,
                price_per_share=pps,
                amount=amount,
                created_at=created_at,
            )
            session.add(txn)

            if is_wl:
                watchlist_txns_count += 1
            else:
                portfolio_txns_count += 1

        if not DRY_RUN:
            p_session.commit()
            w_session.commit()

        print(f"Transactions migrated — portfolio: {portfolio_txns_count}, watchlist: {watchlist_txns_count}, skipped: {skipped_txns}")

        # ── Migrate account ───────────────────────────────────────────────────
        old_account = src.execute('SELECT * FROM "account" LIMIT 1').fetchone()
        if old_account:
            if DRY_RUN:
                print(f"\n[dry-run] Would migrate account: initial_value={old_account['initial_value']}  start_date={old_account['start_date']}")
            else:
                existing_account = p_session.query(Account).first()
                if existing_account:
                    print(f"\nAccount SKIP (already exists): initial_value={existing_account.initial_value}")
                else:
                    account = Account(
                        initial_value=float(old_account['initial_value'] or 0),
                        start_date=_parse_date(old_account['start_date']),
                        created_at=_parse_dt(old_account['created_at']),
                        updated_at=_parse_dt(old_account['updated_at']),
                    )
                    p_session.add(account)
                    p_session.commit()
                    print(f"\nAccount migrated: initial_value={account.initial_value}  start_date={account.start_date}")
        else:
            print("\nNo account row found in source — skipping")

        src.close()

        print()
        print("Migration complete." if not DRY_RUN else "Dry-run complete — nothing was written.")


if __name__ == '__main__':
    run()
