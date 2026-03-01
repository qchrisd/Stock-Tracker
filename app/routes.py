from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
import requests
import json
from app.models import db, PortfolioMeta, WatchlistMeta
from app.portfolio_models import Stock, Transaction, Account
from app.cache_models import StockCache, CacheScheduler
from app.database import db_manager
from bs4 import BeautifulSoup
import re

main_bp = Blueprint('main', __name__)


# ── Context processor: inject nav data into every template ───────────────────

@main_bp.app_context_processor
def inject_nav_data():
    portfolios = PortfolioMeta.query.filter_by(is_active=True).order_by(PortfolioMeta.sort_order).all()
    watchlists = WatchlistMeta.query.filter_by(is_active=True).order_by(WatchlistMeta.sort_order).all()
    return {'nav_portfolios': portfolios, 'nav_watchlists': watchlists}

_ET_TZ = ZoneInfo('America/New_York')
_UTC_TZ = ZoneInfo('UTC')


def _convert_schedule_tz(day_of_week, hour, minute, from_tz, to_tz):
    today = date.today()
    days_ahead = (day_of_week - today.weekday()) % 7
    ref_date = today + timedelta(days=days_ahead if days_ahead > 0 else 7)
    dt_from = datetime(ref_date.year, ref_date.month, ref_date.day, hour, minute, tzinfo=from_tz)
    dt_to = dt_from.astimezone(to_tz)
    return dt_to.weekday(), dt_to.hour, dt_to.minute


import time
import threading

_graham_last_request_time = 0
_graham_request_lock = None

# ── Background cache scheduler ────────────────────────────────────────────────

_scheduler_thread = None

_cache_job_state = {
    'running': False,
    'status': 'idle',
    'message': '',
    'processed': 0,
    'total': 0,
    'successful': 0,
    'percent': 0,
}
_cache_cancel_requested = False


def _reset_job_state():
    global _cache_job_state, _cache_cancel_requested
    _cache_cancel_requested = False
    _cache_job_state.update({
        'running': True,
        'status': 'fetching_tickers',
        'message': 'Fetching SEC securities list\u2026',
        'processed': 0,
        'total': 0,
        'successful': 0,
        'percent': 0,
    })


def _update_job_state(**kwargs):
    _cache_job_state.update(kwargs)


def _fetch_and_store_all_stocks(app):
    """Core cache job: fetch every SEC-listed stock and write to cache.db."""
    def _fetch_one(symbol, attempt=1):
        max_attempts = 3
        try:
            time.sleep(0.2)
            ticker = yf.Ticker(symbol)
            try:
                hist = ticker.history(period='1y', timeout=10)
                if hist.empty or len(hist) < 200:
                    return None
            except Exception as e:
                if attempt < max_attempts and any(
                    x in str(e).lower() for x in ['unauthorized', 'crumb', 'timeout', '429', '503', 'temporarily']
                ):
                    time.sleep(2)
                    return _fetch_one(symbol, attempt + 1)
                return None
            try:
                info = ticker.info
                if not info or 'symbol' not in info:
                    return None
            except Exception as e:
                if attempt < max_attempts and any(
                    x in str(e).lower() for x in ['unauthorized', 'crumb', 'timeout', '429', '503', 'temporarily']
                ):
                    time.sleep(2)
                    return _fetch_one(symbol, attempt + 1)
                return None

            current_price = float(hist['Close'].iloc[-1]) if not hist.empty else None
            price_52w_low  = float(hist['Low'].tail(252).min())  if len(hist) >= 252 else float(hist['Low'].min())
            price_52w_high = float(hist['High'].tail(252).max()) if len(hist) >= 252 else float(hist['High'].max())

            if not current_price or not price_52w_low or current_price <= 0 or price_52w_low <= 0:
                return None

            distance_from_low = ((current_price - price_52w_low) / price_52w_low) * 100
            market_cap = info.get('marketCap')
            market_cap_billions = (market_cap / 1_000_000_000) if market_cap and market_cap > 0 else None

            graham_metrics = {}
            try:
                graham_metrics = get_graham_metrics_from_grahamvalue(symbol, apply_rate_limit=False) or {}
            except Exception:
                pass

            return {
                'symbol': symbol,
                'name': info.get('longName', symbol),
                'sector': info.get('sector', 'N/A'),
                'market_cap': market_cap,
                'market_cap_billions': market_cap_billions,
                'forward_pe': info.get('forwardPE'),
                'trailing_pe': info.get('trailingPE'),
                'dividend_yield': info.get('dividendYield', 0),
                'current_price': current_price,
                'price_52w_low': price_52w_low,
                'price_52w_high': price_52w_high,
                'distance_from_low': distance_from_low,
                'eps': info.get('trailingEps'),
                'book_value_per_share': info.get('bookValue'),
                **{k: graham_metrics.get(k) for k in (
                    'graham_number', 'rating_score', 'size_in_sales',
                    'current_assets_to_2x_liabilities', 'net_current_assets_to_ltdebt',
                    'earnings_stability', 'dividend_record', 'earnings_growth',
                    'graham_number_percent', 'ncav_or_net_net', 'equity_to_debt', 'size_in_assets'
                )}
            }
        except Exception as e:
            print(f"[cache-job] Error fetching {symbol}: {str(e)[:100]}")
            return None

    _reset_job_state()

    with app.app_context():
        cache_session = db_manager.get_cache_session()
        try:
            print("[cache-job] Starting cache download \u2026")
            _update_job_state(status='fetching_tickers', message='Fetching SEC securities list\u2026')

            symbols = get_sec_stock_symbols()
            if not symbols:
                _update_job_state(running=False, status='error', message='Failed to fetch SEC ticker list')
                return 0

            total = len(symbols)
            _update_job_state(status='fetching_started', total=total,
                              message=f'Fetching data for {total:,} securities\u2026')

            cache_session.query(StockCache).delete()
            cache_session.commit()

            successful = 0
            batch = []
            for i, symbol in enumerate(symbols, 1):
                if _cache_cancel_requested:
                    if batch:
                        for entry in batch:
                            cache_session.add(entry)
                        cache_session.commit()
                        batch = []
                    _update_job_state(running=False, status='cancelled',
                                      message=f'Cancelled after {i-1} stocks. {successful} cached.',
                                      processed=i-1, percent=int(((i-1)/total)*100))
                    return successful

                try:
                    result = _fetch_one(symbol)
                    if result:
                        batch.append(StockCache(
                            symbol=result['symbol'],
                            name=result['name'],
                            sector=result['sector'],
                            market_cap=result['market_cap'],
                            market_cap_billions=result['market_cap_billions'],
                            forward_pe=result['forward_pe'],
                            trailing_pe=result['trailing_pe'],
                            dividend_yield=result['dividend_yield'],
                            current_price=result['current_price'],
                            price_52w_low=result['price_52w_low'],
                            price_52w_high=result['price_52w_high'],
                            distance_from_low=result['distance_from_low'],
                            eps=result['eps'],
                            book_value_per_share=result['book_value_per_share'],
                            graham_number=result.get('graham_number'),
                            rating_score=result.get('rating_score'),
                            size_in_sales=result.get('size_in_sales'),
                            current_assets_to_2x_liabilities=result.get('current_assets_to_2x_liabilities'),
                            net_current_assets_to_ltdebt=result.get('net_current_assets_to_ltdebt'),
                            earnings_stability=result.get('earnings_stability'),
                            dividend_record=result.get('dividend_record'),
                            earnings_growth=result.get('earnings_growth'),
                            graham_number_percent=result.get('graham_number_percent'),
                            ncav_or_net_net=result.get('ncav_or_net_net'),
                            equity_to_debt=result.get('equity_to_debt'),
                            size_in_assets=result.get('size_in_assets'),
                        ))
                        successful += 1
                    if i % 25 == 0:
                        for entry in batch:
                            cache_session.add(entry)
                        cache_session.commit()
                        batch = []
                        _update_job_state(status='progress', processed=i, total=total,
                                          successful=successful,
                                          percent=int((i/total)*100))
                        print(f"[cache-job] {i}/{total} processed, {successful} cached")
                except Exception as e:
                    print(f"[cache-job] Exception for {symbol}: {str(e)[:100]}")

            if batch:
                for entry in batch:
                    cache_session.add(entry)
                cache_session.commit()

            _update_job_state(running=False, status='complete', processed=total,
                              successful=successful, percent=100,
                              message=f'Successfully cached {successful:,} stocks')
            print(f"[cache-job] Completed: {successful}/{total}")
            return successful
        except Exception as e:
            _update_job_state(running=False, status='error', message=str(e))
            print(f"[cache-job] Fatal error: {e}")
            return 0
        finally:
            cache_session.remove()


def _compute_next_utc_run(day_of_week_utc, hour_utc, minute_utc):
    now = datetime.now(_UTC_TZ)
    days_ahead = (day_of_week_utc - now.weekday()) % 7
    candidate = now.replace(hour=hour_utc, minute=minute_utc, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(weeks=1)
    return candidate


def _scheduler_loop(app):
    print("[cache-scheduler] Background scheduler thread started")
    while True:
        try:
            with app.app_context():
                cache_session = db_manager.get_cache_session()
                try:
                    scheduler = cache_session.query(CacheScheduler).first()
                    if scheduler and scheduler.enabled:
                        next_run = _compute_next_utc_run(
                            scheduler.day_of_week, scheduler.hour, scheduler.minute
                        )
                        if scheduler.next_run is None or abs(
                            (next_run.replace(tzinfo=None) - scheduler.next_run).total_seconds()
                        ) > 60:
                            scheduler.next_run = next_run.replace(tzinfo=None)
                            cache_session.commit()

                        now_utc = datetime.now(_UTC_TZ).replace(tzinfo=None)
                        if scheduler.next_run and abs((scheduler.next_run - now_utc).total_seconds()) < 60:
                            print(f"[cache-scheduler] Firing at {now_utc} UTC")
                            successful = _fetch_and_store_all_stocks(app)
                            with app.app_context():
                                cs2 = db_manager.get_cache_session()
                                sch = cs2.query(CacheScheduler).first()
                                if sch:
                                    sch.last_run = datetime.utcnow()
                                    sch.next_run = _compute_next_utc_run(
                                        sch.day_of_week, sch.hour, sch.minute
                                    ).replace(tzinfo=None)
                                    cs2.commit()
                            print(f"[cache-scheduler] Done. {successful} cached.")
                finally:
                    cache_session.remove()
        except Exception as e:
            print(f"[cache-scheduler] Error: {e}")
        time.sleep(60)


def start_scheduler_thread(app):
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, args=(app,), name='cache-scheduler', daemon=True
    )
    _scheduler_thread.start()
    print("[cache-scheduler] Scheduler daemon thread launched")


# ── Graham Value scraping ─────────────────────────────────────────────────────

def get_graham_metrics_from_grahamvalue(symbol, apply_rate_limit=True):
    global _graham_last_request_time, _graham_request_lock
    if _graham_request_lock is None:
        _graham_request_lock = threading.Lock()

    empty = {k: None for k in (
        'graham_number', 'defensive_price', 'enterprising_price', 'ncav_price',
        'rating_score', 'size_in_sales', 'current_assets_to_2x_liabilities',
        'net_current_assets_to_ltdebt', 'earnings_stability', 'dividend_record',
        'earnings_growth', 'graham_number_percent', 'ncav_or_net_net',
        'equity_to_debt', 'size_in_assets'
    )}

    try:
        if apply_rate_limit:
            with _graham_request_lock:
                elapsed = time.time() - _graham_last_request_time
                if elapsed < 5:
                    time.sleep(5 - elapsed)
                _graham_last_request_time = time.time()

        url = f"https://www.grahamvalue.com/stock/{symbol.lower()}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        full_text = soup.get_text()

        data = dict(empty)

        m = re.search(r'Rating Score\s*=\s*([\d.]+)', full_text)
        if m: data['rating_score'] = float(m.group(1))
        m = re.search(r'Defensive Price[^:]*?\(Graham[^:]*?\):\s+([\d.]+)', full_text)
        if m:
            data['defensive_price'] = float(m.group(1))
            data['graham_number'] = float(m.group(1))
        m = re.search(r'Enterprising Price[^:]*?\(Serenity[^:]*?\):\s+([\d.]+)', full_text)
        if m: data['enterprising_price'] = float(m.group(1))
        m = re.search(r'NCAV Price[^:]*?\(Net-Net\):\s+([\d.]+)', full_text)
        if m: data['ncav_price'] = float(m.group(1))
        m = re.search(r'Size in Sales.*?:\s+([\d,]+\.[\d]+)%', full_text)
        if m: data['size_in_sales'] = float(m.group(1).replace(',', ''))
        m = re.search(r'Current Assets\s*\xf7\s*\[2\s*x\s*Current Liabilities\]\s*:\s+([\d.]+)%', full_text)
        if m: data['current_assets_to_2x_liabilities'] = float(m.group(1))
        m = re.search(r'Net Current Assets\s*\xf7\s*Long-Term Debt\s*:\s+([\d.]+)%', full_text)
        if m: data['net_current_assets_to_ltdebt'] = float(m.group(1))
        m = re.search(r'Earnings Stability\s*[^:]*?:\s+([\d.]+)%', full_text)
        if m: data['earnings_stability'] = float(m.group(1))
        m = re.search(r'Dividend Record\s*[^:]*?:\s+([\d.]+)%', full_text)
        if m: data['dividend_record'] = float(m.group(1))
        m = re.search(r'Earnings Growth\s*[^:]*?:\s+([\d.]+)%', full_text)
        if m: data['earnings_growth'] = float(m.group(1))
        m = re.search(r'Graham Number\(%\)\s*:\s+([\d.]+)%', full_text)
        if m: data['graham_number_percent'] = float(m.group(1))
        m = re.search(r'NCAV or Net-Net\(%\)\s*:\s+([\d.]+)%', full_text)
        if m: data['ncav_or_net_net'] = float(m.group(1))
        m = re.search(r'\[2\s*x\s*Equity\]\s*\xf7\s*Debt\s*:\s+([\d.]+)%', full_text)
        if m: data['equity_to_debt'] = float(m.group(1))
        m = re.search(r'Size in Assets.*?:\s+([\d,]+\.[\d]+)%', full_text)
        if m: data['size_in_assets'] = float(m.group(1).replace(',', ''))

        return data
    except Exception as e:
        print(f"Error scraping Graham data for {symbol}: {str(e)}")
        return empty


# ── Management routes ─────────────────────────────────────────────────────────

@main_bp.route('/')
def index():
    return redirect(url_for('main.dashboard'))


@main_bp.route('/manage')
def manage():
    portfolios = PortfolioMeta.query.filter_by(is_active=True).order_by(PortfolioMeta.sort_order).all()
    watchlists = WatchlistMeta.query.filter_by(is_active=True).order_by(WatchlistMeta.sort_order).all()

    portfolio_summaries = []
    for p in portfolios:
        try:
            session = db_manager.get_portfolio_session(p.id)
            stock_count = session.query(Stock).count()
            txn_count = session.query(Transaction).count()
        except Exception:
            stock_count = txn_count = 0
        portfolio_summaries.append({'meta': p, 'stock_count': stock_count, 'txn_count': txn_count})

    watchlist_summaries = []
    for w in watchlists:
        try:
            session = db_manager.get_watchlist_session(w.id)
            stock_count = session.query(Stock).count()
        except Exception:
            stock_count = 0
        watchlist_summaries.append({'meta': w, 'stock_count': stock_count})

    return render_template('manage.html',
                           portfolio_summaries=portfolio_summaries,
                           watchlist_summaries=watchlist_summaries)


@main_bp.route('/portfolios/new', methods=['POST'])
def create_portfolio():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    if not name:
        flash('Portfolio name is required', 'error')
        return redirect(url_for('main.manage'))
    p = PortfolioMeta(name=name, description=description)
    db.session.add(p)
    db.session.commit()
    db_manager.ensure_portfolio_db(p.id)
    flash(f'Portfolio "{name}" created', 'success')
    return redirect(url_for('main.portfolio_view', portfolio_id=p.id))


@main_bp.route('/portfolios/<int:portfolio_id>/rename', methods=['POST'])
def rename_portfolio(portfolio_id):
    p = PortfolioMeta.query.get_or_404(portfolio_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Portfolio name is required', 'error')
        return redirect(url_for('main.manage'))
    p.name = name
    p.description = request.form.get('description', p.description).strip()
    db.session.commit()
    flash(f'Portfolio renamed to "{name}"', 'success')
    return redirect(url_for('main.manage'))


@main_bp.route('/portfolios/<int:portfolio_id>/delete', methods=['POST'])
def delete_portfolio_entry(portfolio_id):
    p = PortfolioMeta.query.get_or_404(portfolio_id)
    if PortfolioMeta.query.filter_by(is_active=True).count() <= 1:
        flash('Cannot delete the last portfolio', 'error')
        return redirect(url_for('main.manage'))
    p.is_active = False
    db.session.commit()
    flash(f'Portfolio "{p.name}" removed', 'success')
    return redirect(url_for('main.manage'))


@main_bp.route('/watchlists/new', methods=['POST'])
def create_watchlist_entry():
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    if not name:
        flash('Watchlist name is required', 'error')
        return redirect(url_for('main.manage'))
    w = WatchlistMeta(name=name, description=description)
    db.session.add(w)
    db.session.commit()
    db_manager.ensure_watchlist_db(w.id)
    flash(f'Watchlist "{name}" created', 'success')
    return redirect(url_for('main.watchlist_view', watchlist_id=w.id))


@main_bp.route('/watchlists/<int:watchlist_id>/rename', methods=['POST'])
def rename_watchlist(watchlist_id):
    w = WatchlistMeta.query.get_or_404(watchlist_id)
    name = request.form.get('name', '').strip()
    if not name:
        flash('Watchlist name is required', 'error')
        return redirect(url_for('main.manage'))
    w.name = name
    w.description = request.form.get('description', w.description).strip()
    db.session.commit()
    flash(f'Watchlist renamed to "{name}"', 'success')
    return redirect(url_for('main.manage'))


@main_bp.route('/watchlists/<int:watchlist_id>/delete', methods=['POST'])
def delete_watchlist_entry(watchlist_id):
    w = WatchlistMeta.query.get_or_404(watchlist_id)
    if WatchlistMeta.query.filter_by(is_active=True).count() <= 1:
        flash('Cannot delete the last watchlist', 'error')
        return redirect(url_for('main.manage'))
    w.is_active = False
    db.session.commit()
    flash(f'Watchlist "{w.name}" removed', 'success')
    return redirect(url_for('main.manage'))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@main_bp.route('/dashboard')
def dashboard():
    portfolios = PortfolioMeta.query.filter_by(is_active=True).order_by(PortfolioMeta.sort_order).all()
    watchlists = WatchlistMeta.query.filter_by(is_active=True).order_by(WatchlistMeta.sort_order).all()

    portfolio_cards = []
    for p in portfolios:
        session = db_manager.get_portfolio_session(p.id)
        stocks = session.query(Stock).all()
        total_current_value = 0
        total_current_cost = 0
        total_realized = 0
        active_count = 0

        for stock in stocks:
            current_shares = stock.get_current_shares_from_transactions(session)
            total_realized += stock.get_realized_gains_from_transactions(session)
            if current_shares > 0:
                active_count += 1
                total_current_cost += stock.get_current_cost_basis_from_transactions(session)
                cv = stock.get_current_value(session)
                if cv is not None:
                    total_current_value += cv

        unrealized = (total_current_value - total_current_cost) if total_current_value > 0 else None
        account = session.query(Account).first()

        portfolio_cards.append({
            'meta': p,
            'active_stocks': active_count,
            'total_stocks': len(stocks),
            'total_current_value': total_current_value if total_current_value > 0 else None,
            'unrealized_gains': unrealized,
            'realized_gains': total_realized,
            'account_balance': account.initial_value if account else None,
        })

    watchlist_cards = []
    for w in watchlists:
        session = db_manager.get_watchlist_session(w.id)
        watchlist_cards.append({
            'meta': w,
            'stock_count': session.query(Stock).count(),
        })

    return render_template('dashboard.html',
                           portfolio_cards=portfolio_cards,
                           watchlist_cards=watchlist_cards)


# ── Portfolio routes ──────────────────────────────────────────────────────────

@main_bp.route('/portfolio/<int:portfolio_id>')
def portfolio_view(portfolio_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    stocks = session.query(Stock).all()

    portfolio_data = []
    sold_stocks_data = []
    total_initial_value = 0
    total_current_value = 0

    for stock in stocks:
        try:
            current_price = stock.get_current_price()
            current_value = stock.get_current_value(session)
            value_change = stock.get_value_change(session)
            percent_change = stock.get_value_change_percent(session)
            current_shares = stock.get_current_shares_from_transactions(session)
            transactions = stock.get_transactions(session)
            initial_value = stock.get_initial_value(session)

            stock_info = {
                'stock': stock,
                'current_price': current_price,
                'current_value': current_value,
                'value_change': value_change,
                'percent_change': percent_change,
                'transactions': transactions,
                'current_shares': current_shares,
                'initial_value': initial_value,
            }

            if current_shares > 0:
                portfolio_data.append(stock_info)
                total_initial_value += initial_value
                if current_value is not None:
                    total_current_value += current_value
            else:
                sold_stocks_data.append({
                    'stock': stock,
                    'cost_basis': stock.get_cost_basis_from_transactions(session),
                    'sale_proceeds': stock.get_proceeds_from_sales(session),
                    'realized_gains': stock.get_realized_gains_from_transactions(session),
                    'dividends': stock.get_unreinvested_dividends(session),
                    'transactions': transactions,
                })
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    account = session.query(Account).first()

    total_realized_gains = sum(s.get_realized_gains_from_transactions(session) for s in stocks)
    total_dividends = 0
    total_sale_proceeds = 0
    total_current_cost_basis = 0
    for stock in stocks:
        if stock.get_current_shares_from_transactions(session) > 0:
            total_dividends += stock.get_unreinvested_dividends(session)
            total_sale_proceeds += stock.get_proceeds_from_sales(session)
            total_current_cost_basis += stock.get_current_cost_basis_from_transactions(session)

    unrealized_gains = (total_current_value - total_current_cost_basis
                        if total_current_value > 0 else None)

    portfolio_summary = {
        'total_initial_value': total_initial_value,
        'total_current_value': total_current_value if total_current_value > 0 else None,
        'total_value_change': (total_current_value - total_initial_value
                               if total_current_value > 0 else None),
        'total_percent_change': (
            (total_current_value - total_initial_value) / total_initial_value * 100
            if total_initial_value > 0 and total_current_value > 0 else None
        ),
        'account_initial_value': account.initial_value if account else None,
        'total_realized_gains': total_realized_gains,
        'total_dividends': total_dividends,
        'total_sale_proceeds': total_sale_proceeds,
        'total_invested': total_initial_value,
        'unrealized_gains': unrealized_gains,
    }

    return render_template('portfolio.html',
                           meta=meta,
                           portfolio=portfolio_data,
                           sold_stocks=sold_stocks_data,
                           summary=portfolio_summary,
                           account=account,
                           portfolio_id=portfolio_id)


@main_bp.route('/portfolio/<int:portfolio_id>/account-settings', methods=['GET', 'POST'])
def account_settings(portfolio_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    account = session.query(Account).first()

    if request.method == 'POST':
        initial_value_str = request.form.get('initial_value', '').strip()
        start_date_str = request.form.get('start_date', '').strip()
        if not initial_value_str:
            flash('Please provide an initial account value', 'error')
            return redirect(url_for('main.account_settings', portfolio_id=portfolio_id))
        try:
            initial_value = float(initial_value_str)
            start_date = (datetime.strptime(start_date_str, '%Y-%m-%d').date()
                          if start_date_str else datetime.utcnow().date())
            if initial_value < 0:
                flash('Value must be non-negative', 'error')
                return redirect(url_for('main.account_settings', portfolio_id=portfolio_id))
            if account:
                account.initial_value = initial_value
                account.start_date = start_date
                account.updated_at = datetime.utcnow()
            else:
                account = Account(initial_value=initial_value, start_date=start_date)
                session.add(account)
            session.commit()
            flash(f'Account settings saved: ${initial_value:,.2f} from {start_date}', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except ValueError as e:
            flash(f'Invalid input: {str(e)}', 'error')
            return redirect(url_for('main.account_settings', portfolio_id=portfolio_id))

    start_date_str = (account.start_date.strftime('%Y-%m-%d')
                      if account else datetime.utcnow().date().strftime('%Y-%m-%d'))
    return render_template('account_settings.html', meta=meta, account=account,
                           start_date_str=start_date_str, portfolio_id=portfolio_id)


@main_bp.route('/portfolio/<int:portfolio_id>/add', methods=['GET', 'POST'])
def add_to_portfolio(portfolio_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)

    if request.method == 'POST':
        symbol = request.form.get('symbol', '').upper().strip()
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        if not symbol or not date_str or not shares_str:
            flash('Please provide symbol, date, and number of shares', 'error')
            return redirect(url_for('main.add_to_portfolio', portfolio_id=portfolio_id))
        if session.query(Stock).filter_by(symbol=symbol).first():
            flash(f'{symbol} is already in this portfolio', 'error')
            return redirect(url_for('main.add_to_portfolio', portfolio_id=portfolio_id))
        try:
            shares = float(shares_str)
            if shares <= 0:
                flash('Shares must be > 0', 'error')
                return redirect(url_for('main.add_to_portfolio', portfolio_id=portfolio_id))
            add_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=add_date, end=add_date)
            if hist.empty:
                hist = ticker.history(start=add_date, period='5d')
            if hist.empty:
                flash(f'No price data for {symbol}', 'error')
                return redirect(url_for('main.add_to_portfolio', portfolio_id=portfolio_id))
            initial_price = float(hist['Close'].iloc[0])
            session.add(Stock(symbol=symbol, add_date=add_date, shares=shares, initial_price=initial_price))
            session.add(Transaction(symbol=symbol, type='purchase', date=add_date,
                                    shares=shares, price_per_share=initial_price))
            session.commit()
            flash(f'Added {shares} shares of {symbol} at ${initial_price:.2f}', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.add_to_portfolio', portfolio_id=portfolio_id))

    return render_template('add_stock.html', meta=meta, portfolio_id=portfolio_id,
                           context_type='portfolio')


@main_bp.route('/portfolio/<int:portfolio_id>/stock/<symbol>/buy', methods=['GET', 'POST'])
def record_purchase(portfolio_id, symbol):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    symbol = symbol.upper().strip()
    stock = session.query(Stock).filter_by(symbol=symbol).first()
    if not stock:
        flash(f'{symbol} not found in portfolio', 'error')
        return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
    current_shares = stock.get_current_shares_from_transactions(session)

    if request.method == 'POST':
        try:
            shares = float(request.form.get('shares', '').strip())
            purchase_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            if shares <= 0:
                raise ValueError('Shares must be > 0')
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=purchase_date, end=purchase_date)
            if hist.empty:
                hist = ticker.history(start=purchase_date, period='5d')
            if hist.empty:
                flash(f'No price data for {symbol}', 'error')
                return redirect(url_for('main.record_purchase', portfolio_id=portfolio_id, symbol=symbol))
            price = float(hist['Close'].iloc[0])
            session.add(Transaction(symbol=symbol, type='purchase', date=purchase_date,
                                    shares=shares, price_per_share=price))
            session.commit()
            flash(f'Recorded purchase of {shares} shares at ${price:.2f}', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.record_purchase', portfolio_id=portfolio_id, symbol=symbol))

    return render_template('record_purchase.html', meta=meta, symbol=symbol,
                           current_shares=current_shares, portfolio_id=portfolio_id,
                           purchase_price=None, purchase_date_str=None, list_type='portfolio')


@main_bp.route('/portfolio/<int:portfolio_id>/stock/<symbol>/sell', methods=['GET', 'POST'])
def record_sale(portfolio_id, symbol):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    symbol = symbol.upper().strip()
    stock = session.query(Stock).filter_by(symbol=symbol).first()
    if not stock:
        flash(f'{symbol} not found', 'error')
        return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
    current_shares = stock.get_current_shares_from_transactions(session)

    if request.method == 'POST':
        try:
            shares = float(request.form.get('shares', '').strip())
            sale_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            if shares <= 0:
                raise ValueError('Shares must be > 0')
            if shares > current_shares:
                flash(f'Cannot sell {shares} \u2014 only {current_shares} held', 'error')
                return redirect(url_for('main.record_sale', portfolio_id=portfolio_id, symbol=symbol))
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=sale_date, end=sale_date)
            if hist.empty:
                hist = ticker.history(start=sale_date, period='5d')
            if hist.empty:
                flash(f'No price data for {symbol}', 'error')
                return redirect(url_for('main.record_sale', portfolio_id=portfolio_id, symbol=symbol))
            price = float(hist['Close'].iloc[0])
            session.add(Transaction(symbol=symbol, type='sale', date=sale_date,
                                    shares=shares, price_per_share=price))
            session.commit()
            flash(f'Recorded sale of {shares} shares at ${price:.2f}', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.record_sale', portfolio_id=portfolio_id, symbol=symbol))

    return render_template('record_sale.html', meta=meta, symbol=symbol,
                           current_shares=current_shares, portfolio_id=portfolio_id,
                           sale_price=None, sale_date_str=None)


@main_bp.route('/portfolio/<int:portfolio_id>/stock/<symbol>/dividend', methods=['GET', 'POST'])
def record_dividend(portfolio_id, symbol):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    symbol = symbol.upper().strip()
    stock = session.query(Stock).filter_by(symbol=symbol).first()
    if not stock:
        flash(f'{symbol} not found', 'error')
        return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))

    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', '').strip())
            dividend_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            reinvest = request.form.get('reinvest') == 'on'
            if amount <= 0:
                raise ValueError('Amount must be > 0')
            session.add(Transaction(symbol=symbol, type='dividend', date=dividend_date, amount=amount))
            if reinvest:
                price = float(request.form.get('reinvest_price', '').strip())
                if price <= 0:
                    raise ValueError('Reinvestment price must be > 0')
                reinv_shares = amount / price
                session.add(Transaction(symbol=symbol, type='reinvestment', date=dividend_date,
                                        shares=reinv_shares, price_per_share=price))
                session.commit()
                flash(f'Dividend ${amount:.2f} recorded and reinvested {reinv_shares:.4f} shares', 'success')
            else:
                session.commit()
                flash(f'Dividend ${amount:.2f} recorded', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.record_dividend', portfolio_id=portfolio_id, symbol=symbol))

    return render_template('record_dividend.html', meta=meta, symbol=symbol, portfolio_id=portfolio_id)


@main_bp.route('/portfolio/<int:portfolio_id>/stock/<int:stock_id>/delete', methods=['POST'])
def delete_stock(portfolio_id, stock_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    stock = session.query(Stock).get(stock_id)
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
    symbol = stock.symbol
    try:
        session.query(Transaction).filter_by(symbol=symbol).delete()
        session.delete(stock)
        session.commit()
        flash(f'Removed {symbol}', 'success')
    except Exception as e:
        session.rollback()
        flash(f'Error: {str(e)}', 'error')
    return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))


@main_bp.route('/portfolio/<int:portfolio_id>/stock/<int:stock_id>/edit', methods=['GET', 'POST'])
def edit_stock(portfolio_id, stock_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    stock = session.query(Stock).get(stock_id)
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))

    if request.method == 'POST':
        try:
            shares = float(request.form.get('shares', '').strip())
            add_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            if shares <= 0:
                raise ValueError('Shares must be > 0')
            initial_price = stock.initial_price
            if add_date != stock.add_date:
                ticker = yf.Ticker(stock.symbol)
                hist = ticker.history(start=add_date, end=add_date)
                if hist.empty:
                    hist = ticker.history(start=add_date, period='5d')
                if not hist.empty:
                    initial_price = float(hist['Close'].iloc[0])
            stock.shares = shares
            stock.add_date = add_date
            stock.initial_price = initial_price
            session.commit()
            flash(f'Updated {stock.symbol}', 'success')
            return redirect(url_for('main.portfolio_view', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.edit_stock', portfolio_id=portfolio_id, stock_id=stock_id))

    return render_template('edit_stock.html', meta=meta, stock=stock, portfolio_id=portfolio_id)


@main_bp.route('/portfolio/<int:portfolio_id>/transactions')
def portfolio_transactions(portfolio_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    all_txns = session.query(Transaction).order_by(Transaction.date.desc()).all()
    transactions_data = [
        {'transaction': t, 'stock_symbol': t.symbol, 'type_display': t.type.capitalize()}
        for t in all_txns
    ]
    return render_template('transactions.html', meta=meta, transactions=transactions_data,
                           portfolio_id=portfolio_id)


@main_bp.route('/portfolio/<int:portfolio_id>/transaction/<int:txn_id>/delete', methods=['POST'])
def delete_transaction(portfolio_id, txn_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    txn = session.query(Transaction).get(txn_id)
    if not txn:
        flash('Transaction not found', 'error')
        return redirect(url_for('main.portfolio_transactions', portfolio_id=portfolio_id))
    symbol, ttype = txn.symbol, txn.type
    try:
        session.delete(txn)
        session.commit()
        flash(f'Deleted {ttype} transaction for {symbol}', 'success')
    except Exception as e:
        session.rollback()
        flash(f'Error: {str(e)}', 'error')
    return redirect(url_for('main.portfolio_transactions', portfolio_id=portfolio_id))


@main_bp.route('/portfolio/<int:portfolio_id>/transaction/<int:txn_id>/edit', methods=['GET', 'POST'])
def edit_transaction(portfolio_id, txn_id):
    meta = PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    txn = session.query(Transaction).get(txn_id)
    if not txn:
        flash('Transaction not found', 'error')
        return redirect(url_for('main.portfolio_transactions', portfolio_id=portfolio_id))

    if request.method == 'POST':
        try:
            txn_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            if txn.type in ('purchase', 'sale', 'reinvestment'):
                shares = float(request.form.get('shares', '').strip())
                price = float(request.form.get('price', '').strip())
                if shares <= 0 or price <= 0:
                    raise ValueError('Shares and price must be > 0')
                txn.date = txn_date
                txn.shares = shares
                txn.price_per_share = price
            elif txn.type == 'dividend':
                amount = float(request.form.get('amount', '').strip())
                if amount <= 0:
                    raise ValueError('Amount must be > 0')
                txn.date = txn_date
                txn.amount = amount
            session.commit()
            flash(f'Updated {txn.type} transaction for {txn.symbol}', 'success')
            return redirect(url_for('main.portfolio_transactions', portfolio_id=portfolio_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.edit_transaction', portfolio_id=portfolio_id, txn_id=txn_id))

    return render_template('edit_transaction.html', meta=meta, transaction=txn, portfolio_id=portfolio_id)


# ── Watchlist routes ──────────────────────────────────────────────────────────

@main_bp.route('/watchlist/<int:watchlist_id>')
def watchlist_view(watchlist_id):
    meta = WatchlistMeta.query.get_or_404(watchlist_id)
    session = db_manager.get_watchlist_session(watchlist_id)
    stocks = session.query(Stock).all()

    watchlist_data = []
    for stock in stocks:
        try:
            current_price = stock.get_current_price()
            current_shares = stock.get_current_shares_from_transactions(session)
            initial_value = stock.initial_price * current_shares
            current_value = (current_price * current_shares) if current_price else None
            percent_diff = (
                ((current_price - stock.initial_price) / stock.initial_price * 100)
                if current_price else None
            )
            watchlist_data.append({
                'stock': stock,
                'current_price': current_price,
                'current_shares': current_shares,
                'initial_value': initial_value,
                'current_value': current_value,
                'percent_diff': percent_diff,
            })
        except Exception as e:
            flash(f"Error fetching {stock.symbol}: {str(e)}", 'error')

    return render_template('watchlist.html', meta=meta, watchlist=watchlist_data, watchlist_id=watchlist_id)


@main_bp.route('/watchlist/<int:watchlist_id>/add', methods=['GET', 'POST'])
def add_to_watchlist(watchlist_id):
    meta = WatchlistMeta.query.get_or_404(watchlist_id)
    session = db_manager.get_watchlist_session(watchlist_id)

    if request.method == 'POST':
        symbol = request.form.get('symbol', '').upper().strip()
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        if not symbol or not date_str or not shares_str:
            flash('Please provide symbol, date, and shares', 'error')
            return redirect(url_for('main.add_to_watchlist', watchlist_id=watchlist_id))
        if session.query(Stock).filter_by(symbol=symbol).first():
            flash(f'{symbol} is already in this watchlist', 'error')
            return redirect(url_for('main.add_to_watchlist', watchlist_id=watchlist_id))
        try:
            shares = float(shares_str)
            add_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            ticker = yf.Ticker(symbol)
            hist = ticker.history(start=add_date, end=add_date)
            if hist.empty:
                hist = ticker.history(start=add_date, period='5d')
            if hist.empty:
                flash(f'No price data for {symbol}', 'error')
                return redirect(url_for('main.add_to_watchlist', watchlist_id=watchlist_id))
            initial_price = float(hist['Close'].iloc[0])
            session.add(Stock(symbol=symbol, add_date=add_date, shares=shares, initial_price=initial_price))
            session.add(Transaction(symbol=symbol, type='purchase', date=add_date,
                                    shares=shares, price_per_share=initial_price))
            session.commit()
            flash(f'Added {symbol} to {meta.name}', 'success')
            return redirect(url_for('main.watchlist_view', watchlist_id=watchlist_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.add_to_watchlist', watchlist_id=watchlist_id))

    return render_template('add_stock.html', meta=meta, watchlist_id=watchlist_id, context_type='watchlist')


@main_bp.route('/watchlist/<int:watchlist_id>/stock/<int:stock_id>/delete', methods=['POST'])
def delete_watchlist_stock(watchlist_id, stock_id):
    meta = WatchlistMeta.query.get_or_404(watchlist_id)
    session = db_manager.get_watchlist_session(watchlist_id)
    stock = session.query(Stock).get(stock_id)
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.watchlist_view', watchlist_id=watchlist_id))
    symbol = stock.symbol
    try:
        session.query(Transaction).filter_by(symbol=symbol).delete()
        session.delete(stock)
        session.commit()
        flash(f'Removed {symbol}', 'success')
    except Exception as e:
        session.rollback()
        flash(f'Error: {str(e)}', 'error')
    return redirect(url_for('main.watchlist_view', watchlist_id=watchlist_id))


@main_bp.route('/watchlist/<int:watchlist_id>/stock/<int:stock_id>/edit', methods=['GET', 'POST'])
def edit_watchlist_stock(watchlist_id, stock_id):
    meta = WatchlistMeta.query.get_or_404(watchlist_id)
    session = db_manager.get_watchlist_session(watchlist_id)
    stock = session.query(Stock).get(stock_id)
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.watchlist_view', watchlist_id=watchlist_id))

    if request.method == 'POST':
        try:
            shares = float(request.form.get('shares', '').strip())
            add_date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
            initial_price = stock.initial_price
            if add_date != stock.add_date:
                ticker = yf.Ticker(stock.symbol)
                hist = ticker.history(start=add_date, end=add_date)
                if hist.empty:
                    hist = ticker.history(start=add_date, period='5d')
                if not hist.empty:
                    initial_price = float(hist['Close'].iloc[0])
            stock.shares = shares
            stock.add_date = add_date
            stock.initial_price = initial_price
            session.commit()
            flash(f'Updated {stock.symbol}', 'success')
            return redirect(url_for('main.watchlist_view', watchlist_id=watchlist_id))
        except Exception as e:
            session.rollback()
            flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('main.edit_watchlist_stock', watchlist_id=watchlist_id, stock_id=stock_id))

    return render_template('edit_stock.html', meta=meta, stock=stock, watchlist_id=watchlist_id)


@main_bp.route('/watchlist/<int:watchlist_id>/transactions')
def watchlist_transactions(watchlist_id):
    meta = WatchlistMeta.query.get_or_404(watchlist_id)
    session = db_manager.get_watchlist_session(watchlist_id)
    all_txns = session.query(Transaction).order_by(Transaction.date.desc()).all()
    transactions_data = [
        {'transaction': t, 'stock_symbol': t.symbol, 'type_display': t.type.capitalize()}
        for t in all_txns
    ]
    return render_template('transactions.html', meta=meta, transactions=transactions_data, watchlist_id=watchlist_id)


# ── API endpoints ─────────────────────────────────────────────────────────────

@main_bp.route('/api/stock-price/<symbol>')
def get_stock_price(symbol):
    symbol = symbol.upper().strip()
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'Date parameter required'}), 400
    try:
        price_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=price_date, end=price_date)
        if hist.empty:
            hist = ticker.history(start=price_date, period='5d')
        if hist.empty:
            return jsonify({'error': f'No price data for {symbol}'}), 404
        return jsonify({'price': float(hist['Close'].iloc[0])})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/portfolio/<int:portfolio_id>/chart-data')
def get_chart_data(portfolio_id):
    PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    stocks = session.query(Stock).all()
    if not stocks:
        return jsonify({'error': 'No stocks tracked yet'}), 404

    time_frame = request.args.get('time_frame', 'all')
    today = date.today()
    earliest = min(s.add_date for s in stocks)
    start_date = {'1d': today-timedelta(days=1), '5d': today-timedelta(days=5),
                  '30d': today-timedelta(days=30), '6m': today-timedelta(days=180),
                  '1y': today-timedelta(days=365), 'ytd': date(today.year,1,1)}.get(time_frame, earliest)
    if start_date < earliest:
        start_date = earliest

    chart_data = {'stocks': {}, 'sp500': None}
    try:
        sp500 = Stock.get_sp500_historical_data(start_date)
        if sp500:
            chart_data['sp500'] = sp500
        for stock in stocks:
            hist = stock.get_historical_data()
            if hist:
                filtered = [d for d in hist if d['date'] >= start_date.isoformat()]
                if filtered:
                    chart_data['stocks'][stock.symbol] = {'name': stock.symbol, 'data': filtered}
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/portfolio/<int:portfolio_id>/average-chart-data')
def get_portfolio_average_chart_data(portfolio_id):
    PortfolioMeta.query.get_or_404(portfolio_id)
    session = db_manager.get_portfolio_session(portfolio_id)
    stocks = session.query(Stock).all()
    if not stocks:
        return jsonify({'error': 'No stocks yet'}), 404

    time_frame = request.args.get('time_frame', 'all')
    today = date.today()
    earliest = min(s.add_date for s in stocks)
    start_date = {'1d': today-timedelta(days=1), '5d': today-timedelta(days=5),
                  '30d': today-timedelta(days=30), '6m': today-timedelta(days=180),
                  '1y': today-timedelta(days=365), 'ytd': date(today.year,1,1)}.get(time_frame, earliest)
    if start_date < earliest:
        start_date = earliest

    try:
        all_dates = {}
        total_iv_at_date = {}
        for stock in stocks:
            hist = stock.get_historical_data()
            if hist:
                iv = stock.get_initial_value(session)
                for dp in hist:
                    if dp['date'] >= start_date.isoformat():
                        dk = dp['date']
                        all_dates.setdefault(dk, []).append({'percent_change': dp['percent_change'], 'iv': iv})
                        total_iv_at_date[dk] = total_iv_at_date.get(dk, 0) + iv

        avg_data = []
        for dk in sorted(all_dates):
            tiv = total_iv_at_date[dk]
            if tiv > 0:
                avg_data.append({'date': dk,
                                  'return_pct': sum(d['percent_change']*d['iv']/tiv for d in all_dates[dk])})

        sp500_raw = Stock.get_sp500_historical_data(start_date)
        sp500_fmt = [{'date': p['date'], 'return_pct': p['percent_change']} for p in sp500_raw] if sp500_raw else []
        return jsonify({'portfolio_avg': avg_data, 'sp500': sp500_fmt})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-stocks')
def cache_stocks():
    from flask import current_app
    if _cache_job_state.get('running'):
        def _already():
            yield f"data: {json.dumps({'status': 'error', 'message': 'A cache job is already running.'})}\n\n"
        return Response(_already(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    app = current_app._get_current_object()

    def generate():
        job_thread = threading.Thread(target=_fetch_and_store_all_stocks, args=(app,), daemon=True)
        job_thread.start()
        last_pct = -1
        while job_thread.is_alive() or _cache_job_state.get('running'):
            state = dict(_cache_job_state)
            status = state.get('status', 'idle')
            if status == 'fetching_tickers':
                yield f"data: {json.dumps({'status': 'fetching_tickers', 'message': state['message']})}\n\n"
            elif status == 'fetching_started':
                yield f"data: {json.dumps({'status': 'fetching_started', 'total': state['total'], 'message': state['message']})}\n\n"
            elif status == 'progress':
                pct = state.get('percent', 0)
                if pct != last_pct:
                    last_pct = pct
                    yield f"data: {json.dumps({'status': 'progress', 'processed': state['processed'], 'total': state['total'], 'successful': state['successful'], 'percent': pct})}\n\n"
            elif status in ('complete', 'error', 'cancelled'):
                yield f"data: {json.dumps(state)}\n\n"
                break
            time.sleep(2)
        state = dict(_cache_job_state)
        if state.get('status') in ('complete', 'error', 'cancelled'):
            yield f"data: {json.dumps(state)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@main_bp.route('/api/cache-status')
def cache_status_api():
    return jsonify(dict(_cache_job_state))


@main_bp.route('/api/cache-cancel', methods=['POST'])
def cache_cancel_api():
    global _cache_cancel_requested
    if _cache_job_state.get('running'):
        _cache_cancel_requested = True
        return jsonify({'success': True, 'message': 'Cancel requested'})
    return jsonify({'success': False, 'message': 'No cache job running'})


@main_bp.route('/api/graham-metrics/<symbol>')
def get_graham_metrics_api(symbol):
    try:
        cache_session = db_manager.get_cache_session()
        cached = cache_session.query(StockCache).filter_by(symbol=symbol).first()
        if cached and cached.rating_score is not None:
            return jsonify({k: getattr(cached, k) for k in (
                'graham_number', 'rating_score', 'size_in_sales',
                'current_assets_to_2x_liabilities', 'net_current_assets_to_ltdebt',
                'earnings_stability', 'dividend_record', 'earnings_growth',
                'graham_number_percent', 'ncav_or_net_net', 'equity_to_debt', 'size_in_assets'
            )})
        metrics = get_graham_metrics_from_grahamvalue(symbol) or {}
        if cached:
            for k, v in metrics.items():
                if hasattr(cached, k):
                    setattr(cached, k, v)
            cache_session.commit()
        return jsonify(metrics)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-scheduler/config', methods=['GET'])
def get_cache_scheduler_config():
    try:
        cs = db_manager.get_cache_session()
        scheduler = cs.query(CacheScheduler).first()
        if not scheduler:
            scheduler = CacheScheduler(enabled=False, day_of_week=0, hour=2, minute=0)
            cs.add(scheduler)
            cs.commit()
        day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        et_day, et_hour, et_min = _convert_schedule_tz(
            scheduler.day_of_week, scheduler.hour, scheduler.minute, _UTC_TZ, _ET_TZ)
        return jsonify({'enabled': scheduler.enabled, 'day_of_week': et_day,
                        'day_name': day_names[et_day], 'hour': et_hour, 'minute': et_min,
                        'last_run': (scheduler.last_run.isoformat()+'Z') if scheduler.last_run else None,
                        'next_run': (scheduler.next_run.isoformat()+'Z') if scheduler.next_run else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-scheduler/config', methods=['POST'])
def update_cache_scheduler_config():
    try:
        data = request.get_json()
        cs = db_manager.get_cache_session()
        scheduler = cs.query(CacheScheduler).first()
        if not scheduler:
            scheduler = CacheScheduler(enabled=False, day_of_week=0, hour=2, minute=0)
            cs.add(scheduler)
            cs.commit()
        if 'enabled' in data:
            scheduler.enabled = bool(data['enabled'])
        et_day, et_hour, et_min = _convert_schedule_tz(
            scheduler.day_of_week, scheduler.hour, scheduler.minute, _UTC_TZ, _ET_TZ)
        if 'day_of_week' in data: et_day = int(data['day_of_week'])
        if 'hour' in data: et_hour = int(data['hour'])
        if 'minute' in data: et_min = int(data['minute'])
        utc_day, utc_hour, utc_min = _convert_schedule_tz(et_day, et_hour, et_min, _ET_TZ, _UTC_TZ)
        scheduler.day_of_week = utc_day
        scheduler.hour = utc_hour
        scheduler.minute = utc_min
        scheduler.next_run = (_compute_next_utc_run(utc_day, utc_hour, utc_min).replace(tzinfo=None)
                               if scheduler.enabled else None)
        scheduler.updated_at = datetime.utcnow()
        cs.commit()
        day_names = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        return jsonify({'success': True, 'message': f'Scheduler {"enabled" if scheduler.enabled else "disabled"}',
                        'enabled': scheduler.enabled, 'day_of_week': et_day, 'day_name': day_names[et_day],
                        'hour': et_hour, 'minute': et_min,
                        'last_run': (scheduler.last_run.isoformat()+'Z') if scheduler.last_run else None,
                        'next_run': (scheduler.next_run.isoformat()+'Z') if scheduler.next_run else None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ── Research ──────────────────────────────────────────────────────────────────

@main_bp.route('/research')
def research():
    suggestions = []
    error_message = None
    cache_status = None

    symbols_input = request.args.get('symbols', '').strip()
    market_cap_min_m = request.args.get('market_cap_min', 0, type=float)
    market_cap_max_m = request.args.get('market_cap_max', 10000000, type=float)
    market_cap_min = market_cap_min_m / 1000
    market_cap_max = market_cap_max_m / 1000
    distance_min = request.args.get('distance_min', 0, type=float)
    distance_max = request.args.get('distance_max', 100, type=float)
    forward_pe_max = request.args.get('forward_pe_max', 100, type=float)
    rating_score_min = request.args.get('rating_score_min', 0, type=float)
    rating_score_max = request.args.get('rating_score_max', 10, type=float)

    cs = db_manager.get_cache_session()
    try:
        cache_count = cs.query(StockCache).count()
        if symbols_input:
            symbols_to_search = [s.strip().upper() for s in symbols_input.split(',') if s.strip()]
            cache_status = f'Searching {len(symbols_to_search)} custom symbols (live data)'
            for symbol in symbols_to_search:
                try:
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period='1y')
                    if len(hist) < 200: continue
                    current_price = hist['Close'].iloc[-1]
                    info = ticker.info
                    w52h = hist['High'].tail(252).max()
                    w52l = hist['Low'].tail(252).min()
                    if not current_price or not w52l or not w52h: continue
                    dist = ((current_price - w52l) / w52l) * 100
                    fpe = info.get('forwardPE') or info.get('trailingPE')
                    if not fpe: continue
                    mc = info.get('marketCap')
                    if not mc: continue
                    mc_b = mc / 1e9
                    if not (distance_min <= dist <= distance_max and fpe <= forward_pe_max
                            and market_cap_min <= mc_b <= market_cap_max): continue
                    eps = info.get('trailingEps')
                    bvps = info.get('bookValue')
                    gn = None
                    if eps and bvps and eps > 0 and bvps > 0:
                        import math
                        gn = math.sqrt(22.5 * eps * bvps)
                    gm = get_graham_metrics_from_grahamvalue(symbol) or {}
                    suggestions.append({
                        'symbol': symbol, 'name': info.get('longName', symbol),
                        'current_price': current_price, 'week_52_low': w52l, 'week_52_high': w52h,
                        'distance_from_low': dist, 'forward_pe': fpe, 'market_cap': mc,
                        'market_cap_billions': mc_b, 'sector': info.get('sector', 'N/A'),
                        'dividend_yield': info.get('dividendYield', 0),
                        'pe_ratio': info.get('trailingPE'), 'eps': eps,
                        'book_value_per_share': bvps, 'graham_number': gn,
                        **{k: gm.get(k) for k in ('rating_score','size_in_sales',
                            'current_assets_to_2x_liabilities','net_current_assets_to_ltdebt',
                            'earnings_stability','dividend_record','earnings_growth',
                            'graham_number_percent','ncav_or_net_net','equity_to_debt','size_in_assets')},
                    })
                except Exception: continue
        elif cache_count > 0:
            cache_status = f'\u2713 {cache_count:,} stocks cached and ready to search'
            cached_stocks = cs.query(StockCache).filter(
                StockCache.market_cap_billions >= market_cap_min,
                StockCache.market_cap_billions <= market_cap_max,
                StockCache.distance_from_low >= distance_min,
                StockCache.distance_from_low <= distance_max,
            ).all()
            for stock in cached_stocks:
                fpe = stock.forward_pe or stock.trailing_pe or 0
                if fpe == 0 or fpe > forward_pe_max: continue
                if stock.rating_score is not None:
                    if not (rating_score_min <= stock.rating_score <= rating_score_max): continue
                suggestions.append({
                    'symbol': stock.symbol, 'name': stock.name,
                    'current_price': stock.current_price, 'week_52_low': stock.price_52w_low,
                    'week_52_high': stock.price_52w_high, 'distance_from_low': stock.distance_from_low,
                    'forward_pe': fpe, 'market_cap': stock.market_cap,
                    'market_cap_billions': stock.market_cap_billions, 'sector': stock.sector,
                    'dividend_yield': stock.dividend_yield, 'pe_ratio': stock.trailing_pe,
                    'eps': stock.eps, 'book_value_per_share': stock.book_value_per_share,
                    'graham_number': stock.graham_number, 'rating_score': stock.rating_score,
                    'size_in_sales': stock.size_in_sales,
                    'current_assets_to_2x_liabilities': stock.current_assets_to_2x_liabilities,
                    'net_current_assets_to_ltdebt': stock.net_current_assets_to_ltdebt,
                    'earnings_stability': stock.earnings_stability,
                    'dividend_record': stock.dividend_record, 'earnings_growth': stock.earnings_growth,
                    'graham_number_percent': stock.graham_number_percent,
                    'ncav_or_net_net': stock.ncav_or_net_net, 'equity_to_debt': stock.equity_to_debt,
                    'size_in_assets': stock.size_in_assets,
                })
        else:
            cache_status = '\u23f3 Ready to cache 10,000+ SEC securities. Click "Download Stock Data" to begin.'
            error_message = ('Cache is empty. Click "Download Stock Data" to populate with all '
                             'SEC-listed securities.')
    except Exception as e:
        error_message = f"Error: {str(e)}"

    return render_template('research.html',
                           suggestions=suggestions, error_message=error_message,
                           cache_status=cache_status, symbols=symbols_input,
                           market_cap_min=market_cap_min_m, market_cap_max=market_cap_max_m,
                           distance_min=distance_min, distance_max=distance_max,
                           forward_pe_max=forward_pe_max, rating_score_min=rating_score_min,
                           rating_score_max=rating_score_max)


# ── SEC helpers ───────────────────────────────────────────────────────────────

def get_sec_stock_symbols():
    import time as _t
    url = 'https://www.sec.gov/files/company_tickers.json'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            tickers = sorted({e.get('ticker','').strip().upper()
                               for e in r.json().values() if e.get('ticker','').strip()})
            print(f"Fetched {len(tickers)} SEC tickers")
            return tickers
        except Exception as e:
            print(f"SEC fetch attempt {attempt+1}/3 failed: {e}")
            if attempt < 2: _t.sleep(2**attempt)
    return []
