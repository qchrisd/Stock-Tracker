from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import yfinance as yf
import requests
import json
from app.models import db, Stock, Transaction, Account, StockCache, CacheScheduler
from bs4 import BeautifulSoup
import re

main_bp = Blueprint('main', __name__)

_ET_TZ = ZoneInfo('America/New_York')
_UTC_TZ = ZoneInfo('UTC')

def _convert_schedule_tz(day_of_week, hour, minute, from_tz, to_tz):
    """Convert a (day_of_week 0=Mon, hour, minute) schedule from one timezone to another.
    Uses the next upcoming occurrence of that weekday as the DST reference date.
    """
    today = date.today()
    days_ahead = (day_of_week - today.weekday()) % 7
    ref_date = today + timedelta(days=days_ahead if days_ahead > 0 else 7)
    dt_from = datetime(ref_date.year, ref_date.month, ref_date.day, hour, minute, tzinfo=from_tz)
    dt_to = dt_from.astimezone(to_tz)
    return dt_to.weekday(), dt_to.hour, dt_to.minute


import time
import threading

# Global rate limiter for Graham metrics scraping (used during bulk download)
_graham_last_request_time = 0
_graham_request_lock = None

# ── Background cache scheduler ──────────────────────────────────────────────

_scheduler_thread = None  # Singleton background thread

# Shared job state – written by both the SSE route and the background job,
# read by /api/cache-status so any page load can pick up an in-progress run.
_cache_job_state = {
    'running': False,
    'status': 'idle',      # idle | fetching_tickers | fetching_started | progress | complete | error | cancelled
    'message': '',
    'processed': 0,
    'total': 0,
    'successful': 0,
    'percent': 0,
}
_cache_cancel_requested = False  # Set True by /api/cache-cancel


def _reset_job_state():
    global _cache_job_state, _cache_cancel_requested
    _cache_cancel_requested = False
    _cache_job_state.update({
        'running': True,
        'status': 'fetching_tickers',
        'message': 'Fetching SEC securities list…',
        'processed': 0,
        'total': 0,
        'successful': 0,
        'percent': 0,
    })


def _update_job_state(**kwargs):
    _cache_job_state.update(kwargs)


def _fetch_and_store_all_stocks(app):
    """
    Core cache job: fetches data for every SEC-listed stock and writes it to the
    database.  Designed to run outside of a request context (background thread).
    Returns the number of stocks successfully cached.
    """
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
        try:
            print("[cache-job] Starting cache download …")
            _update_job_state(status='fetching_tickers', message='Fetching SEC securities list…')

            symbols = get_sec_stock_symbols()
            if not symbols:
                _update_job_state(running=False, status='error', message='Failed to fetch SEC ticker list')
                print("[cache-job] Failed to fetch SEC ticker list")
                return 0

            total = len(symbols)
            _update_job_state(status='fetching_started', total=total,
                              message=f'Fetching data for {total:,} securities…')

            StockCache.query.delete()
            db.session.commit()

            successful = 0
            batch = []
            for i, symbol in enumerate(symbols, 1):
                # Check cancel flag
                if _cache_cancel_requested:
                    # Flush whatever we have so far
                    if batch:
                        for entry in batch:
                            db.session.add(entry)
                        db.session.commit()
                        batch = []
                    _update_job_state(running=False, status='cancelled',
                                      message=f'Cancelled after {i-1} stocks. {successful} cached.',
                                      processed=i-1, percent=int(((i-1)/total)*100))
                    print(f"[cache-job] Cancelled at {i-1}/{total}")
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
                            db.session.add(entry)
                        db.session.commit()
                        batch = []
                        _update_job_state(status='progress', processed=i, total=total,
                                          successful=successful,
                                          percent=int((i/total)*100))
                        print(f"[cache-job] {i}/{total} processed, {successful} cached")
                except Exception as e:
                    print(f"[cache-job] Exception for {symbol}: {str(e)[:100]}")

            if batch:
                for entry in batch:
                    db.session.add(entry)
                db.session.commit()

            _update_job_state(running=False, status='complete', processed=total,
                              successful=successful, percent=100,
                              message=f'Successfully cached {successful:,} stocks')
            print(f"[cache-job] Completed: {successful} stocks cached out of {total}")
            return successful
        except Exception as e:
            _update_job_state(running=False, status='error', message=str(e))
            print(f"[cache-job] Fatal error: {e}")
            return 0


def _compute_next_utc_run(day_of_week_utc, hour_utc, minute_utc):
    """Return the next datetime (UTC, timezone-aware) matching the given UTC weekday/hour/minute."""
    now = datetime.now(_UTC_TZ)
    days_ahead = (day_of_week_utc - now.weekday()) % 7
    candidate = now.replace(hour=hour_utc, minute=minute_utc, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(weeks=1)
    return candidate


def _scheduler_loop(app):
    """Background daemon thread: wakes up every minute, fires the cache job when due."""
    print("[cache-scheduler] Background scheduler thread started")
    while True:
        try:
            with app.app_context():
                scheduler = CacheScheduler.query.first()
                if scheduler and scheduler.enabled:
                    next_run = _compute_next_utc_run(
                        scheduler.day_of_week, scheduler.hour, scheduler.minute
                    )
                    # Persist next_run if it changed
                    if scheduler.next_run is None or abs(
                        (next_run.replace(tzinfo=None) - scheduler.next_run).total_seconds()
                    ) > 60:
                        scheduler.next_run = next_run.replace(tzinfo=None)
                        db.session.commit()

                    now_utc = datetime.now(_UTC_TZ).replace(tzinfo=None)
                    # Fire within a 60-second window of the scheduled time
                    if scheduler.next_run and abs((scheduler.next_run - now_utc).total_seconds()) < 60:
                        print(f"[cache-scheduler] Firing scheduled cache job at {now_utc} UTC")
                        successful = _fetch_and_store_all_stocks(app)
                        with app.app_context():
                            sch = CacheScheduler.query.first()
                            if sch:
                                sch.last_run = datetime.utcnow()
                                sch.next_run = _compute_next_utc_run(
                                    sch.day_of_week, sch.hour, sch.minute
                                ).replace(tzinfo=None)
                                db.session.commit()
                        print(f"[cache-scheduler] Job done. {successful} stocks cached. "
                              f"Next run: {sch.next_run} UTC")
        except Exception as e:
            print(f"[cache-scheduler] Error in scheduler loop: {e}")

        time.sleep(60)  # check once per minute


def start_scheduler_thread(app):
    """Start the background scheduler daemon thread (idempotent — only starts once)."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(app,),
        name='cache-scheduler',
        daemon=True,
    )
    _scheduler_thread.start()
    print("[cache-scheduler] Scheduler daemon thread launched")

def get_graham_metrics_from_grahamvalue(symbol, apply_rate_limit=True):
    """
    Scrape comprehensive Graham metrics from grahamvalue.com
    Returns dict with all Graham rating metrics, or empty dict if scraping fails
    
    Args:
        symbol: Stock ticker symbol
        apply_rate_limit: If True, enforces rate limiting. Set to False when calling from cache_stocks
                         which handles its own sequential rate limiting.
    """
    global _graham_last_request_time, _graham_request_lock
    
    if _graham_request_lock is None:
        import threading
        _graham_request_lock = threading.Lock()
    
    try:
        # Rate limiting: only apply when not in bulk cache mode
        # Bulk cache handles its own sequential rate limiting
        if apply_rate_limit:
            with _graham_request_lock:
                elapsed = time.time() - _graham_last_request_time
                if elapsed < 5:
                    time.sleep(5 - elapsed)
                _graham_last_request_time = time.time()
        
        url = f"https://www.grahamvalue.com/stock/{symbol.lower()}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # Timeout to 30 seconds
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Get all text from the page
        full_text = soup.get_text()
        
        graham_data = {
            'graham_number': None,
            'defensive_price': None,
            'enterprising_price': None,
            'ncav_price': None,
            'rating_score': None,
            'size_in_sales': None,
            'current_assets_to_2x_liabilities': None,
            'net_current_assets_to_ltdebt': None,
            'earnings_stability': None,
            'dividend_record': None,
            'earnings_growth': None,
            'graham_number_percent': None,
            'ncav_or_net_net': None,
            'equity_to_debt': None,
            'size_in_assets': None
        }
        
        # Extract Rating Score - look for "Rating Score = X.X" pattern
        rating_match = re.search(r'Rating Score\s*=\s*([\d.]+)', full_text)
        if rating_match:
            graham_data['rating_score'] = float(rating_match.group(1))
        
        # Extract Defensive Price (Graham №)
        defensive_match = re.search(r'Defensive Price[^:]*?\(Graham[^:]*?\):\s+([\d.]+)', full_text)
        if defensive_match:
            graham_data['defensive_price'] = float(defensive_match.group(1))
            graham_data['graham_number'] = float(defensive_match.group(1))
        
        # Extract Enterprising Price (Serenity №)
        enterprising_match = re.search(r'Enterprising Price[^:]*?\(Serenity[^:]*?\):\s+([\d.]+)', full_text)
        if enterprising_match:
            graham_data['enterprising_price'] = float(enterprising_match.group(1))
        
        # Extract NCAV Price (Net-Net)
        ncav_match = re.search(r'NCAV Price[^:]*?\(Net-Net\):\s+([\d.]+)', full_text)
        if ncav_match:
            graham_data['ncav_price'] = float(ncav_match.group(1))
        
        # Extract Graham Ratings - look for the pattern with percentage
        size_sales_match = re.search(r'Size in Sales.*?:\s+([\d,]+\.[\d]+)%', full_text)
        if size_sales_match:
            # Remove commas and convert to float
            graham_data['size_in_sales'] = float(size_sales_match.group(1).replace(',', ''))
        
        current_assets_match = re.search(r'Current Assets\s*÷\s*\[2\s*x\s*Current Liabilities\]\s*:\s+([\d.]+)%', full_text)
        if current_assets_match:
            graham_data['current_assets_to_2x_liabilities'] = float(current_assets_match.group(1))
        
        net_current_match = re.search(r'Net Current Assets\s*÷\s*Long-Term Debt\s*:\s+([\d.]+)%', full_text)
        if net_current_match:
            graham_data['net_current_assets_to_ltdebt'] = float(net_current_match.group(1))
        
        earnings_stability_match = re.search(r'Earnings Stability\s*[^:]*?:\s+([\d.]+)%', full_text)
        if earnings_stability_match:
            graham_data['earnings_stability'] = float(earnings_stability_match.group(1))
        
        dividend_record_match = re.search(r'Dividend Record\s*[^:]*?:\s+([\d.]+)%', full_text)
        if dividend_record_match:
            graham_data['dividend_record'] = float(dividend_record_match.group(1))
        
        earnings_growth_match = re.search(r'Earnings Growth\s*[^:]*?:\s+([\d.]+)%', full_text)
        if earnings_growth_match:
            graham_data['earnings_growth'] = float(earnings_growth_match.group(1))
        
        graham_number_pct_match = re.search(r'Graham Number\(%\)\s*:\s+([\d.]+)%', full_text)
        if graham_number_pct_match:
            graham_data['graham_number_percent'] = float(graham_number_pct_match.group(1))
        
        ncav_pct_match = re.search(r'NCAV or Net-Net\(%\)\s*:\s+([\d.]+)%', full_text)
        if ncav_pct_match:
            graham_data['ncav_or_net_net'] = float(ncav_pct_match.group(1))
        
        equity_debt_match = re.search(r'\[2\s*x\s*Equity\]\s*÷\s*Debt\s*:\s+([\d.]+)%', full_text)
        if equity_debt_match:
            graham_data['equity_to_debt'] = float(equity_debt_match.group(1))
        
        size_assets_match = re.search(r'Size in Assets.*?:\s+([\d,]+\.[\d]+)%', full_text)
        if size_assets_match:
            # Remove commas and convert to float
            graham_data['size_in_assets'] = float(size_assets_match.group(1).replace(',', ''))
        
        return graham_data
        
    except Exception as e:
        print(f"Error scraping Graham data for {symbol}: {str(e)}")
        # Return empty dict on error instead of None
        return {
            'graham_number': None,
            'defensive_price': None,
            'enterprising_price': None,
            'ncav_price': None,
            'rating_score': None,
            'size_in_sales': None,
            'current_assets_to_2x_liabilities': None,
            'net_current_assets_to_ltdebt': None,
            'earnings_stability': None,
            'dividend_record': None,
            'earnings_growth': None,
            'graham_number_percent': None,
            'ncav_or_net_net': None,
            'equity_to_debt': None,
            'size_in_assets': None
        }


@main_bp.route('/')
def index():
    """Redirect to dashboard"""
    return redirect(url_for('main.dashboard'))


@main_bp.route('/portfolio')
def portfolio():
    """Display all portfolio stocks and their value changes"""
    stocks = Stock.query.filter_by(is_watchlist=False).all()
    portfolio_data = []
    sold_stocks_data = []
    total_initial_value = 0
    total_current_value = 0

    for stock in stocks:
        try:
            current_price = stock.get_current_price()
            current_value = stock.get_current_value()
            value_change = stock.get_value_change()
            percent_change = stock.get_value_change_percent()
            
            # Get transactions for this stock
            transactions = Transaction.query.filter_by(symbol=stock.symbol, is_watchlist=False).order_by(Transaction.date).all()
            
            current_shares = stock.get_current_shares_from_transactions()
            
            stock_info = {
                'stock': stock,
                'current_price': current_price,
                'current_value': current_value,
                'value_change': value_change,
                'percent_change': percent_change,
                'transactions': transactions
            }
            
            # Separate active stocks from sold stocks
            if current_shares > 0:
                portfolio_data.append(stock_info)
                initial_value = stock.get_initial_value()
                total_initial_value += initial_value
                if current_value is not None:
                    total_current_value += current_value
            else:
                # Stock has been completely sold, add to sold stocks list
                cost_basis = stock.get_cost_basis_from_transactions()
                sale_proceeds = stock.get_proceeds_from_sales()
                realized_gains = stock.get_realized_gains_from_transactions()
                dividends = stock.get_unreinvested_dividends()
                
                sold_stocks_data.append({
                    'stock': stock,
                    'cost_basis': cost_basis,
                    'sale_proceeds': sale_proceeds,
                    'realized_gains': realized_gains,
                    'dividends': dividends,
                    'transactions': transactions
                })
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    # Get account information
    account = Account.query.first()
    
    # Calculate account gains/losses from realized gains and sales
    total_realized_gains = 0
    total_dividends = 0
    total_sale_proceeds = 0
    total_current_cost_basis = 0
    
    # Calculate realized gains from ALL stocks (active and sold)
    for stock in stocks:
        total_realized_gains += stock.get_realized_gains_from_transactions()
    
    # Calculate other metrics from active stocks only
    for stock in stocks:
        current_shares = stock.get_current_shares_from_transactions()
        if current_shares > 0:
            total_dividends += stock.get_unreinvested_dividends()
            total_sale_proceeds += stock.get_proceeds_from_sales()
            total_current_cost_basis += stock.get_current_cost_basis_from_transactions()

    # Unrealized gains = current value of holdings - cost basis of currently held shares
    unrealized_gains = total_current_value - total_current_cost_basis if total_current_value is not None else None

    portfolio_summary = {
        'total_initial_value': total_initial_value,
        'total_current_value': total_current_value if total_current_value > 0 else None,
        'total_value_change': total_current_value - total_initial_value if total_current_value > 0 else None,
        'total_percent_change': ((total_current_value - total_initial_value) / total_initial_value * 100) if total_initial_value > 0 and total_current_value > 0 else None,
        'account_initial_value': account.initial_value if account else None,
        'total_realized_gains': total_realized_gains,
        'total_dividends': total_dividends,
        'total_sale_proceeds': total_sale_proceeds,
        'total_invested': total_initial_value,
        'unrealized_gains': unrealized_gains
    }

    return render_template('portfolio.html', portfolio=portfolio_data, sold_stocks=sold_stocks_data, summary=portfolio_summary, account=account)


@main_bp.route('/account-settings', methods=['GET', 'POST'])
def account_settings():
    """Manage account settings and starting value"""
    account = Account.query.first()
    
    if request.method == 'POST':
        initial_value_str = request.form.get('initial_value', '').strip()
        start_date_str = request.form.get('start_date', '').strip()
        
        if not initial_value_str:
            flash('Please provide an initial account value', 'error')
            return redirect(url_for('main.account_settings'))
        
        try:
            initial_value = float(initial_value_str)
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else datetime.utcnow().date()
            
            if initial_value < 0:
                flash('Initial account value must be non-negative', 'error')
                return redirect(url_for('main.account_settings'))
            
            if account:
                # Update existing account
                account.initial_value = initial_value
                account.start_date = start_date
                db.session.commit()
                flash(f'Account settings updated: ${initial_value:,.2f} starting on {start_date}', 'success')
            else:
                # Create new account
                account = Account(initial_value=initial_value, start_date=start_date)
                db.session.add(account)
                db.session.commit()
                flash(f'Account created with initial value: ${initial_value:,.2f}', 'success')
            
            return redirect(url_for('main.portfolio'))
        except ValueError as e:
            flash(f'Invalid input: {str(e)}', 'error')
            return redirect(url_for('main.account_settings'))
    
    start_date_str = account.start_date.strftime('%Y-%m-%d') if account else datetime.utcnow().date().strftime('%Y-%m-%d')
    return render_template('account_settings.html', account=account, start_date_str=start_date_str)


@main_bp.route('/watchlist')
def watchlist():
    """Display all watchlist stocks"""
    stocks = Stock.query.filter_by(is_watchlist=True).all()
    watchlist_data = []

    for stock in stocks:
        try:
            current_price = stock.get_current_price()
            
            watchlist_data.append({
                'stock': stock,
                'current_price': current_price,
            })
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    return render_template('watchlist.html', watchlist=watchlist_data)


@main_bp.route('/dashboard')
def dashboard():
    """Display combined portfolio and watchlist with average gains/losses"""
    all_stocks = Stock.query.all()
    dashboard_data = []

    for stock in all_stocks:
        try:
            current_price = stock.get_current_price()
            current_value = stock.get_current_value()
            value_change = stock.get_value_change()
            percent_change = stock.get_value_change_percent()
            
            list_type = 'Watchlist' if stock.is_watchlist else 'Portfolio'
            
            dashboard_data.append({
                'stock': stock,
                'current_price': current_price,
                'current_value': current_value,
                'value_change': value_change,
                'percent_change': percent_change,
                'list_type': list_type
            })
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    # Calculate summary statistics
    total_initial_value = 0
    total_current_value = 0
    
    portfolio_items = [item for item in dashboard_data if not item['stock'].is_watchlist]
    watchlist_items = [item for item in dashboard_data if item['stock'].is_watchlist]
    
    for item in dashboard_data:
        initial_value = item['stock'].get_initial_value()
        total_initial_value += initial_value
        if item['current_value'] is not None:
            total_current_value += item['current_value']

    # Get account information
    account = Account.query.first()
    
    # Calculate portfolio metrics
    portfolio_stocks = Stock.query.filter_by(is_watchlist=False).all()
    portfolio_unrealized_gains = 0
    portfolio_realized_gains = 0
    portfolio_total_current_cost_basis = 0
    portfolio_current_value = 0
    
    # Calculate realized gains from ALL portfolio stocks (including sold stocks)
    for stock in portfolio_stocks:
        portfolio_realized_gains += stock.get_realized_gains_from_transactions()
    
    # Calculate unrealized gains and current value from active stocks only
    for stock in portfolio_stocks:
        current_shares = stock.get_current_shares_from_transactions()
        if current_shares > 0:
            portfolio_total_current_cost_basis += stock.get_current_cost_basis_from_transactions()
            current_value = stock.get_current_value()
            if current_value is not None:
                portfolio_current_value += current_value
    
    portfolio_unrealized_gains = portfolio_current_value - portfolio_total_current_cost_basis if portfolio_current_value > 0 else None
    portfolio_total_return = portfolio_realized_gains + (portfolio_unrealized_gains if portfolio_unrealized_gains else 0)
    portfolio_percent_change = None
    if account and account.initial_value and account.initial_value > 0:
        portfolio_percent_change = (portfolio_total_return / account.initial_value) * 100

    # Calculate average performance for Portfolio
    portfolio_avg_price = None
    portfolio_avg_current_price = None
    portfolio_avg_shares = None
    portfolio_avg_percent = None
    
    if portfolio_items:
        total_shares = sum(item['stock'].shares for item in portfolio_items)
        total_initial_prices = sum(item['stock'].initial_price * item['stock'].shares for item in portfolio_items)
        total_current_prices = sum(item['current_price'] * item['stock'].shares for item in portfolio_items if item['current_price'])
        valid_percent_items = [item for item in portfolio_items if item['percent_change'] is not None]
        
        portfolio_avg_shares = total_shares / len(portfolio_items) if portfolio_items else 0
        portfolio_avg_price = total_initial_prices / total_shares if total_shares > 0 else 0
        portfolio_avg_current_price = total_current_prices / total_shares if total_shares > 0 and total_current_prices > 0 else None
        portfolio_avg_percent = sum(item['percent_change'] for item in valid_percent_items) / len(valid_percent_items) if valid_percent_items else None
    
    # Calculate average performance for Watchlist
    watchlist_avg_price = None
    watchlist_avg_current_price = None
    watchlist_avg_shares = None
    watchlist_avg_percent = None
    
    if watchlist_items:
        total_shares = sum(item['stock'].shares for item in watchlist_items)
        total_initial_prices = sum(item['stock'].initial_price * item['stock'].shares for item in watchlist_items)
        total_current_prices = sum(item['current_price'] * item['stock'].shares for item in watchlist_items if item['current_price'])
        valid_percent_items = [item for item in watchlist_items if item['percent_change'] is not None]
        
        watchlist_avg_shares = total_shares / len(watchlist_items) if watchlist_items else 0
        watchlist_avg_price = total_initial_prices / total_shares if total_shares > 0 else 0
        watchlist_avg_current_price = total_current_prices / total_shares if total_shares > 0 and total_current_prices > 0 else None
        watchlist_avg_percent = sum(item['percent_change'] for item in valid_percent_items) / len(valid_percent_items) if valid_percent_items else None

    dashboard_summary = {
        'total_initial_value': total_initial_value,
        'total_current_value': total_current_value if total_current_value > 0 else None,
        'total_value_change': total_current_value - total_initial_value if total_current_value > 0 else None,
        'total_percent_change': ((total_current_value - total_initial_value) / total_initial_value * 100) if total_initial_value > 0 and total_current_value > 0 else None,
        'account_initial_value': account.initial_value if account else None,
        'portfolio_current_value': portfolio_current_value,
        'portfolio_unrealized_gains': portfolio_unrealized_gains,
        'portfolio_realized_gains': portfolio_realized_gains,
        'portfolio_percent_change': portfolio_percent_change,
        'portfolio_avg_shares': portfolio_avg_shares,
        'portfolio_avg_price': portfolio_avg_price,
        'portfolio_avg_current_price': portfolio_avg_current_price,
        'portfolio_avg_percent': portfolio_avg_percent,
        'watchlist_avg_shares': watchlist_avg_shares,
        'watchlist_avg_price': watchlist_avg_price,
        'watchlist_avg_current_price': watchlist_avg_current_price,
        'watchlist_avg_percent': watchlist_avg_percent
    }

    return render_template('dashboard.html', dashboard=dashboard_data, summary=dashboard_summary)


@main_bp.route('/add', methods=['GET', 'POST'])
def add_stock():
    """Add a new stock to track"""
    list_type = request.args.get('type', 'portfolio')  # 'portfolio' or 'watchlist'
    
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').upper().strip()
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        is_watchlist = request.form.get('list_type') == 'watchlist'
        
        if not symbol or not date_str or not shares_str:
            flash('Please provide symbol, date, and number of shares', 'error')
            return redirect(url_for('main.add_stock', type=list_type))

        # Check if stock already exists in the same list
        if Stock.query.filter_by(symbol=symbol, is_watchlist=is_watchlist).first():
            list_name = 'watchlist' if is_watchlist else 'portfolio'
            flash(f'Stock {symbol} is already in your {list_name}', 'error')
            return redirect(url_for('main.add_stock', type=list_type))

        try:
            # Validate and parse shares
            try:
                shares = float(shares_str)
                if shares <= 0:
                    flash('Number of shares must be greater than 0', 'error')
                    return redirect(url_for('main.add_stock', type=list_type))
            except ValueError:
                flash('Invalid number of shares. Please enter a valid number', 'error')
                return redirect(url_for('main.add_stock', type=list_type))

            # Parse the date
            add_date = datetime.strptime(date_str, '%Y-%m-%d').date()

            # Fetch the stock price on the specified date
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            
            # Get historical data starting from the specified date
            hist = ticker.history(start=add_date, end=add_date)
            
            if hist.empty:
                # Try to get the price from the next available trading day
                hist = ticker.history(start=add_date, period='5d')
                if hist.empty:
                    flash(f'Could not find price data for {symbol} on or after {add_date}', 'error')
                    return redirect(url_for('main.add_stock', type=list_type))

            initial_price = float(hist['Close'].iloc[0])

            # Create new stock record
            new_stock = Stock(
                symbol=symbol,
                add_date=add_date,
                shares=shares,
                initial_price=initial_price,
                is_watchlist=is_watchlist
            )
            
            # Create corresponding transaction record
            purchase_transaction = Transaction(
                symbol=symbol,
                type='purchase',
                date=add_date,
                shares=shares,
                price_per_share=initial_price,
                is_watchlist=is_watchlist
            )
            
            db.session.add(new_stock)
            db.session.add(purchase_transaction)
            db.session.commit()

            total_value = shares * initial_price
            list_name = 'watchlist' if is_watchlist else 'portfolio'
            flash(f'Successfully added {shares} shares of {symbol} to {list_name} at ${initial_price:.2f} (Total: ${total_value:,.2f}) on {add_date}', 'success')
            
            # Redirect based on which list was being edited
            if is_watchlist:
                return redirect(url_for('main.watchlist'))
            else:
                return redirect(url_for('main.portfolio'))

        except ValueError:
            flash('Invalid date format. Please use YYYY-MM-DD', 'error')
            return redirect(url_for('main.add_stock', type=list_type))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding stock: {str(e)}', 'error')
            return redirect(url_for('main.add_stock', type=list_type))

    return render_template('add_stock.html', list_type=list_type)


@main_bp.route('/stock/<symbol>/sale', methods=['GET', 'POST'])
def record_sale(symbol):
    """Record a stock sale transaction"""
    symbol = symbol.upper().strip()
    stock = Stock.query.filter_by(symbol=symbol, is_watchlist=False).first()
    
    if not stock:
        flash(f'Stock {symbol} not found in portfolio', 'error')
        return redirect(url_for('main.portfolio'))
    
    current_shares = stock.get_current_shares_from_transactions()
    sale_price = None
    sale_date_str = None
    
    if request.method == 'POST':
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        
        if not date_str or not shares_str:
            flash('Please provide date and shares', 'error')
            return redirect(url_for('main.record_sale', symbol=symbol))
        
        try:
            shares = float(shares_str)
            sale_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            if shares <= 0:
                flash('Shares must be greater than 0', 'error')
                return redirect(url_for('main.record_sale', symbol=symbol))
            
            if shares > current_shares:
                flash(f'Cannot sell {shares} shares. You only have {current_shares} shares', 'error')
                return redirect(url_for('main.record_sale', symbol=symbol))
            
            # Fetch the stock price on the sale date
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            
            # Get historical data for the sale date
            hist = ticker.history(start=sale_date, end=sale_date)
            
            if hist.empty:
                # Try to get the price from the next available trading day
                hist = ticker.history(start=sale_date, period='5d')
                if hist.empty:
                    flash(f'Could not find price data for {symbol} on or after {sale_date}', 'error')
                    return redirect(url_for('main.record_sale', symbol=symbol))
            
            price_per_share = float(hist['Close'].iloc[0])
            
            # Create sale transaction
            sale_transaction = Transaction(
                symbol=symbol,
                type='sale',
                date=sale_date,
                shares=shares,
                price_per_share=price_per_share,
                is_watchlist=False
            )
            
            db.session.add(sale_transaction)
            db.session.commit()
            
            total_proceeds = shares * price_per_share
            flash(f'Successfully recorded sale of {shares} shares of {symbol} at ${price_per_share:.2f} (Total: ${total_proceeds:,.2f})', 'success')
            return redirect(url_for('main.portfolio'))
            
        except ValueError as e:
            flash(f'Invalid input: {str(e)}', 'error')
            return redirect(url_for('main.record_sale', symbol=symbol))
    
    return render_template('record_sale.html', symbol=symbol, current_shares=current_shares, sale_price=sale_price, sale_date_str=sale_date_str)


@main_bp.route('/stock/<symbol>/buy', methods=['GET', 'POST'])
def record_purchase(symbol):
    """Record a stock purchase transaction for an existing stock"""
    symbol = symbol.upper().strip()
    
    # Check if this is in portfolio or watchlist
    portfolio_stock = Stock.query.filter_by(symbol=symbol, is_watchlist=False).first()
    watchlist_stock = Stock.query.filter_by(symbol=symbol, is_watchlist=True).first()
    
    stock = portfolio_stock or watchlist_stock
    
    if not stock:
        flash(f'Stock {symbol} not found', 'error')
        return redirect(url_for('main.portfolio'))
    
    list_type = 'watchlist' if stock.is_watchlist else 'portfolio'
    current_shares = stock.get_current_shares_from_transactions()
    purchase_price = None
    purchase_date_str = None
    
    if request.method == 'POST':
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        
        if not date_str or not shares_str:
            flash('Please provide date and shares', 'error')
            return redirect(url_for('main.record_purchase', symbol=symbol))
        
        try:
            shares = float(shares_str)
            purchase_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            if shares <= 0:
                flash('Shares must be greater than 0', 'error')
                return redirect(url_for('main.record_purchase', symbol=symbol))
            
            # Fetch the stock price on the purchase date
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            
            # Get historical data for the purchase date
            hist = ticker.history(start=purchase_date, end=purchase_date)
            
            if hist.empty:
                # Try to get the price from the next available trading day
                hist = ticker.history(start=purchase_date, period='5d')
                if hist.empty:
                    flash(f'Could not find price data for {symbol} on or after {purchase_date}', 'error')
                    return redirect(url_for('main.record_purchase', symbol=symbol))
            
            price_per_share = float(hist['Close'].iloc[0])
            
            # Create purchase transaction
            purchase_transaction = Transaction(
                symbol=symbol,
                type='purchase',
                date=purchase_date,
                shares=shares,
                price_per_share=price_per_share,
                is_watchlist=stock.is_watchlist
            )
            
            db.session.add(purchase_transaction)
            db.session.commit()
            
            total_cost = shares * price_per_share
            flash(f'Successfully recorded purchase of {shares} shares of {symbol} at ${price_per_share:.2f} (Total: ${total_cost:,.2f})', 'success')
            
            if stock.is_watchlist:
                return redirect(url_for('main.watchlist'))
            else:
                return redirect(url_for('main.portfolio'))
            
        except ValueError as e:
            flash(f'Invalid input: {str(e)}', 'error')
            return redirect(url_for('main.record_purchase', symbol=symbol))
    
    return render_template('record_purchase.html', symbol=symbol, current_shares=current_shares, list_type=list_type, purchase_price=purchase_price, purchase_date_str=purchase_date_str)


@main_bp.route('/stock/<symbol>/dividend', methods=['GET', 'POST'])
def record_dividend(symbol):
    """Record a dividend payment"""
    symbol = symbol.upper().strip()
    stock = Stock.query.filter_by(symbol=symbol, is_watchlist=False).first()
    
    if not stock:
        flash(f'Stock {symbol} not found in portfolio', 'error')
        return redirect(url_for('main.portfolio'))
    
    if request.method == 'POST':
        date_str = request.form.get('date')
        amount_str = request.form.get('amount', '').strip()
        reinvest = request.form.get('reinvest') == 'on'
        
        if not date_str or not amount_str:
            flash('Please provide date and dividend amount', 'error')
            return redirect(url_for('main.record_dividend', symbol=symbol))
        
        try:
            amount = float(amount_str)
            dividend_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            if amount <= 0:
                flash('Dividend amount must be greater than 0', 'error')
                return redirect(url_for('main.record_dividend', symbol=symbol))
            
            # Create dividend transaction
            dividend_transaction = Transaction(
                symbol=symbol,
                type='dividend',
                date=dividend_date,
                amount=amount,
                is_watchlist=False
            )
            
            db.session.add(dividend_transaction)
            
            # If reinvesting, create reinvestment transaction
            if reinvest:
                price_str = request.form.get('reinvest_price', '').strip()
                if not price_str:
                    flash('Please provide price per share for reinvestment', 'error')
                    db.session.rollback()
                    return redirect(url_for('main.record_dividend', symbol=symbol))
                
                try:
                    price_per_share = float(price_str)
                except ValueError:
                    flash('Invalid price format', 'error')
                    db.session.rollback()
                    return redirect(url_for('main.record_dividend', symbol=symbol))
                
                if price_per_share <= 0:
                    flash('Reinvestment price must be greater than 0', 'error')
                    db.session.rollback()
                    return redirect(url_for('main.record_dividend', symbol=symbol))
                
                reinvestment_shares = amount / price_per_share
                
                reinvestment_transaction = Transaction(
                    symbol=symbol,
                    type='reinvestment',
                    date=dividend_date,
                    shares=reinvestment_shares,
                    price_per_share=price_per_share,
                    is_watchlist=False
                )
                
                db.session.add(reinvestment_transaction)
                db.session.commit()
                flash(f'Recorded dividend of ${amount:.2f} and reinvested {reinvestment_shares:.4f} shares at ${price_per_share:.2f}', 'success')
            else:
                db.session.commit()
                flash(f'Recorded dividend of ${amount:.2f}', 'success')
            
            return redirect(url_for('main.portfolio'))
            
        except ValueError as e:
            db.session.rollback()
            flash(f'Invalid input: {str(e)}', 'error')
            return redirect(url_for('main.record_dividend', symbol=symbol))
    
    return render_template('record_dividend.html', symbol=symbol)


@main_bp.route('/stock/<int:stock_id>/delete', methods=['POST'])
def delete_stock(stock_id):
    """Delete a tracked stock"""
    stock = Stock.query.get(stock_id)
    
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.portfolio'))

    try:
        symbol = stock.symbol
        is_watchlist = stock.is_watchlist
        db.session.delete(stock)
        db.session.commit()
        flash(f'Successfully removed {symbol} from tracking', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting stock: {str(e)}', 'error')

    # Redirect to the appropriate list
    if is_watchlist:
        return redirect(url_for('main.watchlist'))
    else:
        return redirect(url_for('main.portfolio'))


@main_bp.route('/stock/<int:stock_id>/edit', methods=['GET', 'POST'])
def edit_stock(stock_id):
    """Edit a tracked stock (shares and date acquired)"""
    stock = Stock.query.get(stock_id)
    
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.portfolio'))
    
    if request.method == 'POST':
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        
        if not date_str or not shares_str:
            flash('Please provide date and number of shares', 'error')
            return redirect(url_for('main.edit_stock', stock_id=stock_id))
        
        try:
            # Validate and parse shares
            try:
                shares = float(shares_str)
                if shares <= 0:
                    flash('Number of shares must be greater than 0', 'error')
                    return redirect(url_for('main.edit_stock', stock_id=stock_id))
            except ValueError:
                flash('Invalid number of shares. Please enter a valid number', 'error')
                return redirect(url_for('main.edit_stock', stock_id=stock_id))
            
            # Parse the date
            add_date = datetime.strptime(date_str, '%Y-%m-%d').date()
            
            # If date changed, fetch the new price for that date
            initial_price = stock.initial_price
            if add_date != stock.add_date:
                try:
                    import yfinance as yf
                    ticker = yf.Ticker(stock.symbol)
                    
                    # Get historical data for the new date
                    hist = ticker.history(start=add_date, end=add_date)
                    
                    if hist.empty:
                        # Try to get the price from the next available trading day
                        hist = ticker.history(start=add_date, period='5d')
                        if hist.empty:
                            flash(f'Could not find price data for {stock.symbol} on or after {add_date}', 'error')
                            return redirect(url_for('main.edit_stock', stock_id=stock_id))
                    
                    initial_price = float(hist['Close'].iloc[0])
                except Exception as e:
                    flash(f'Error fetching price data: {str(e)}', 'error')
                    return redirect(url_for('main.edit_stock', stock_id=stock_id))
            
            # Update the stock
            stock.shares = shares
            stock.add_date = add_date
            stock.initial_price = initial_price
            db.session.commit()
            
            flash(f'Successfully updated {stock.symbol} ({shares} shares, acquired {add_date})', 'success')
            
            # Redirect to appropriate list
            if stock.is_watchlist:
                return redirect(url_for('main.watchlist'))
            else:
                return redirect(url_for('main.portfolio'))
            
        except ValueError:
            flash('Invalid date format. Please use YYYY-MM-DD', 'error')
            return redirect(url_for('main.edit_stock', stock_id=stock_id))
        except Exception as e:
            db.session.rollback()
            flash(f'Error updating stock: {str(e)}', 'error')
            return redirect(url_for('main.edit_stock', stock_id=stock_id))
    
    return render_template('edit_stock.html', stock=stock)


@main_bp.route('/api/stock/<symbol>')
def get_stock_data(symbol):
    """API endpoint to get real-time data for a stock"""
    stock = Stock.query.filter_by(symbol=symbol.upper()).first()
    
    if not stock:
        return jsonify({'error': 'Stock not found'}), 404

    try:
        return jsonify(stock.to_dict())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/chart-data')
def get_chart_data():
    """API endpoint to get historical data for charting"""
    from datetime import datetime, timedelta, date
    
    stocks = Stock.query.filter_by(is_watchlist=False).all()
    
    if not stocks:
        return jsonify({'error': 'No stocks tracked yet'}), 404
    
    # Get time frame parameter
    time_frame = request.args.get('time_frame', 'all', type=str)
    
    # Calculate date range based on time frame
    today = date.today()
    earliest_date = min(stock.add_date for stock in stocks)
    
    if time_frame == '1d':
        start_date = today - timedelta(days=1)
    elif time_frame == '5d':
        start_date = today - timedelta(days=5)
    elif time_frame == '30d':
        start_date = today - timedelta(days=30)
    elif time_frame == '6m':
        start_date = today - timedelta(days=180)
    elif time_frame == '1y':
        start_date = today - timedelta(days=365)
    elif time_frame == 'ytd':
        # Year to date: January 1 of current year
        start_date = date(today.year, 1, 1)
    else:  # 'all' or default
        start_date = earliest_date
    
    # Don't go before earliest stock addition date
    if start_date < earliest_date:
        start_date = earliest_date
    
    chart_data = {'stocks': {}, 'sp500': None}
    
    try:
        # Fetch S&P 500 data
        sp500_data = Stock.get_sp500_historical_data(start_date)
        if sp500_data:
            chart_data['sp500'] = sp500_data
        
        # Fetch historical data for each stock
        for stock in stocks:
            hist_data = stock.get_historical_data()
            if hist_data:
                # Filter data based on start_date
                filtered_data = [d for d in hist_data if d['date'] >= start_date.isoformat()]
                if filtered_data:
                    chart_data['stocks'][stock.symbol] = {
                        'name': stock.symbol,
                        'data': filtered_data
                    }
        
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/portfolio-average-chart-data')
def get_portfolio_average_chart_data():
    """API endpoint to get average portfolio performance data for charting"""
    from datetime import datetime, timedelta, date
    
    stocks = Stock.query.filter_by(is_watchlist=False).all()
    
    if not stocks:
        return jsonify({'error': 'No portfolio stocks tracked yet'}), 404
    
    # Get time frame parameter
    time_frame = request.args.get('time_frame', 'all', type=str)
    
    # Calculate date range based on time frame
    today = date.today()
    earliest_date = min(stock.add_date for stock in stocks)
    
    if time_frame == '1d':
        start_date = today - timedelta(days=1)
    elif time_frame == '5d':
        start_date = today - timedelta(days=5)
    elif time_frame == '30d':
        start_date = today - timedelta(days=30)
    elif time_frame == '6m':
        start_date = today - timedelta(days=180)
    elif time_frame == '1y':
        start_date = today - timedelta(days=365)
    elif time_frame == 'ytd':
        # Year to date: January 1 of current year
        start_date = date(today.year, 1, 1)
    else:  # 'all' or default
        start_date = earliest_date
    
    # Don't go before earliest stock addition date
    if start_date < earliest_date:
        start_date = earliest_date
    
    try:
        # Collect all historical data points with their share weights
        all_dates = {}
        total_initial_value_at_date = {}
        
        for stock in stocks:
            hist_data = stock.get_historical_data()
            if hist_data:
                for data_point in hist_data:
                    if data_point['date'] >= start_date.isoformat():
                        date_key = data_point['date']
                        if date_key not in all_dates:
                            all_dates[date_key] = []
                            total_initial_value_at_date[date_key] = 0
                        
                        # Store: initial return % and shares for weighted average
                        all_dates[date_key].append({
                            'percent_change': data_point['percent_change'],
                            'initial_value': stock.get_initial_value()
                        })
                        total_initial_value_at_date[date_key] += stock.get_initial_value()
        
        # Calculate weighted average return for each date
        avg_data = []
        for date_key in sorted(all_dates.keys()):
            data_points = all_dates[date_key]
            total_initial = total_initial_value_at_date[date_key]
            
            if total_initial > 0:
                weighted_avg = sum(d['percent_change'] * d['initial_value'] / total_initial for d in data_points)
                avg_data.append({
                    'date': date_key,
                    'return_pct': weighted_avg
                })
        
        # Fetch S&P 500 data for comparison
        sp500_data = Stock.get_sp500_historical_data(start_date)
        
        # Convert sp500 data from percent_change to return_pct format
        sp500_formatted = []
        if sp500_data:
            for point in sp500_data:
                sp500_formatted.append({
                    'date': point['date'],
                    'return_pct': point['percent_change']
                })
        
        chart_data = {
            'portfolio_avg': avg_data,
            'sp500': sp500_formatted
        }
        
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/transactions')
def transactions():
    """Display all transactions"""
    all_transactions = Transaction.query.order_by(Transaction.date.desc()).all()
    
    # Group transactions by symbol for easy viewing
    transactions_data = []
    for txn in all_transactions:
        transactions_data.append({
            'transaction': txn,
            'stock_symbol': txn.symbol,
            'type_display': txn.type.capitalize(),
            'list_type': 'Watchlist' if txn.is_watchlist else 'Portfolio'
        })
    
    return render_template('transactions.html', transactions=transactions_data)


@main_bp.route('/transaction/<int:txn_id>/delete', methods=['POST'])
def delete_transaction(txn_id):
    """Delete a transaction"""
    transaction = Transaction.query.get(txn_id)
    
    if not transaction:
        flash('Transaction not found', 'error')
        return redirect(url_for('main.transactions'))
    
    symbol = transaction.symbol
    txn_type = transaction.type
    
    try:
        db.session.delete(transaction)
        db.session.commit()
        flash(f'Successfully deleted {txn_type} transaction for {symbol}', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting transaction: {str(e)}', 'error')
    
    return redirect(url_for('main.transactions'))


@main_bp.route('/transaction/<int:txn_id>/edit', methods=['GET', 'POST'])
def edit_transaction(txn_id):
    """Edit a transaction"""
    transaction = Transaction.query.get(txn_id)
    
    if not transaction:
        flash('Transaction not found', 'error')
        return redirect(url_for('main.transactions'))
    
    if request.method == 'POST':
        date_str = request.form.get('date')
        
        # For purchases, sales, and reinvestments: edit shares and price
        if transaction.type in ('purchase', 'sale', 'reinvestment'):
            shares_str = request.form.get('shares', '').strip()
            price_str = request.form.get('price', '').strip()
            
            if not date_str or not shares_str or not price_str:
                flash('Please provide date, shares, and price', 'error')
                return redirect(url_for('main.edit_transaction', txn_id=txn_id))
            
            try:
                shares = float(shares_str)
                price = float(price_str)
                txn_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                
                if shares <= 0 or price <= 0:
                    flash('Shares and price must be greater than 0', 'error')
                    return redirect(url_for('main.edit_transaction', txn_id=txn_id))
                
                transaction.date = txn_date
                transaction.shares = shares
                transaction.price_per_share = price
                db.session.commit()
                
                flash(f'Successfully updated {transaction.type} transaction for {transaction.symbol}', 'success')
                return redirect(url_for('main.transactions'))
                
            except ValueError as e:
                flash(f'Invalid input: {str(e)}', 'error')
                return redirect(url_for('main.edit_transaction', txn_id=txn_id))
        
        # For dividends: edit amount
        elif transaction.type == 'dividend':
            amount_str = request.form.get('amount', '').strip()
            
            if not date_str or not amount_str:
                flash('Please provide date and amount', 'error')
                return redirect(url_for('main.edit_transaction', txn_id=txn_id))
            
            try:
                amount = float(amount_str)
                txn_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                
                if amount <= 0:
                    flash('Amount must be greater than 0', 'error')
                    return redirect(url_for('main.edit_transaction', txn_id=txn_id))
                
                transaction.date = txn_date
                transaction.amount = amount
                db.session.commit()
                
                flash(f'Successfully updated dividend transaction for {transaction.symbol}', 'success')
                return redirect(url_for('main.transactions'))
                
            except ValueError as e:
                flash(f'Invalid input: {str(e)}', 'error')
                return redirect(url_for('main.edit_transaction', txn_id=txn_id))
    
    return render_template('edit_transaction.html', transaction=transaction)


@main_bp.route('/api/stock-price/<symbol>')
def get_stock_price(symbol):
    """API endpoint to fetch stock price for a given date"""
    symbol = symbol.upper().strip()
    date_str = request.args.get('date')
    
    if not date_str:
        return jsonify({'error': 'Date parameter required'}), 400
    
    try:
        import yfinance as yf
        price_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=price_date, end=price_date)
        
        if hist.empty:
            # Try to get the price from the next available trading day
            hist = ticker.history(start=price_date, period='5d')
            if hist.empty:
                return jsonify({'error': f'No price data available for {symbol} on or after {price_date}'}), 404
        
        price = float(hist['Close'].iloc[0])
        return jsonify({'price': price})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-stocks')
def cache_stocks():
    """
    Server-Sent Events endpoint – streams progress while the cache job runs.
    Delegates all actual work to _fetch_and_store_all_stocks so the shared
    _cache_job_state stays accurate whether the job was triggered manually or
    by the background scheduler.
    """
    from flask import current_app
    import traceback

    # Reject if already running
    if _cache_job_state.get('running'):
        def _already_running():
            yield f"data: {json.dumps({'status': 'error', 'message': 'A cache job is already in progress.'})}\n\n"
        return Response(_already_running(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    app = current_app._get_current_object()

    def generate_progress():
        # Kick off in the same thread so SSE can stream live updates
        # by reading _cache_job_state every 2 seconds.
        import threading as _threading
        job_thread = _threading.Thread(target=_fetch_and_store_all_stocks, args=(app,), daemon=True)
        job_thread.start()

        last_percent = -1
        while job_thread.is_alive() or _cache_job_state.get('running'):
            state = dict(_cache_job_state)
            status = state.get('status', 'idle')

            if status == 'fetching_tickers':
                yield f"data: {json.dumps({'status': 'fetching_tickers', 'message': state['message']})}\n\n"

            elif status == 'fetching_started':
                yield f"data: {json.dumps({'status': 'fetching_started', 'total': state['total'], 'message': state['message']})}\n\n"

            elif status == 'progress':
                pct = state.get('percent', 0)
                if pct != last_percent:
                    last_percent = pct
                    yield f"data: {json.dumps({'status': 'progress', 'processed': state['processed'], 'total': state['total'], 'successful': state['successful'], 'percent': pct})}\n\n"

            elif status in ('complete', 'error', 'cancelled'):
                yield f"data: {json.dumps(state)}\n\n"
                break

            time.sleep(2)

        # Final flush in case we exited the loop before emitting terminal state
        state = dict(_cache_job_state)
        if state.get('status') in ('complete', 'error', 'cancelled'):
            yield f"data: {json.dumps(state)}\n\n"

    return Response(generate_progress(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@main_bp.route('/api/cache-status')
def cache_status_api():
    """Returns the current state of the background/manual cache job as JSON."""
    return jsonify(dict(_cache_job_state))


@main_bp.route('/api/cache-cancel', methods=['POST'])
def cache_cancel_api():
    """Request cancellation of any running cache job."""
    global _cache_cancel_requested
    if _cache_job_state.get('running'):
        _cache_cancel_requested = True
        return jsonify({'success': True, 'message': 'Cancel requested'})
    return jsonify({'success': False, 'message': 'No cache job is currently running'})


@main_bp.route('/api/graham-metrics/<symbol>')
def get_graham_metrics_api(symbol):
    """API endpoint to fetch Graham metrics for a single stock on-demand"""
    try:
        # Try to get from cache first
        cached = StockCache.query.filter_by(symbol=symbol).first()
        if cached and cached.rating_score is not None:
            # Already cached with metrics
            return jsonify({
                'graham_number': cached.graham_number,
                'rating_score': cached.rating_score,
                'size_in_sales': cached.size_in_sales,
                'current_assets_to_2x_liabilities': cached.current_assets_to_2x_liabilities,
                'net_current_assets_to_ltdebt': cached.net_current_assets_to_ltdebt,
                'earnings_stability': cached.earnings_stability,
                'dividend_record': cached.dividend_record,
                'earnings_growth': cached.earnings_growth,
                'graham_number_percent': cached.graham_number_percent,
                'ncav_or_net_net': cached.ncav_or_net_net,
                'equity_to_debt': cached.equity_to_debt,
                'size_in_assets': cached.size_in_assets
            })
        
        # Fetch from GrahamValue
        metrics = get_graham_metrics_from_grahamvalue(symbol) or {}
        
        # Update cache if it exists
        if cached:
            cached.graham_number = metrics.get('graham_number')
            cached.rating_score = metrics.get('rating_score')
            cached.size_in_sales = metrics.get('size_in_sales')
            cached.current_assets_to_2x_liabilities = metrics.get('current_assets_to_2x_liabilities')
            cached.net_current_assets_to_ltdebt = metrics.get('net_current_assets_to_ltdebt')
            cached.earnings_stability = metrics.get('earnings_stability')
            cached.dividend_record = metrics.get('dividend_record')
            cached.earnings_growth = metrics.get('earnings_growth')
            cached.graham_number_percent = metrics.get('graham_number_percent')
            cached.ncav_or_net_net = metrics.get('ncav_or_net_net')
            cached.equity_to_debt = metrics.get('equity_to_debt')
            cached.size_in_assets = metrics.get('size_in_assets')
            db.session.commit()
        
        return jsonify(metrics)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-scheduler/config', methods=['GET'])
def get_cache_scheduler_config():
    """Get current cache scheduler configuration (times returned in Eastern Time)"""
    try:
        scheduler = CacheScheduler.get_or_create()
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        # Convert stored UTC schedule to Eastern Time for display
        et_day, et_hour, et_min = _convert_schedule_tz(
            scheduler.day_of_week, scheduler.hour, scheduler.minute, _UTC_TZ, _ET_TZ
        )

        return jsonify({
            'enabled': scheduler.enabled,
            'day_of_week': et_day,
            'day_name': day_names[et_day],
            'hour': et_hour,
            'minute': et_min,
            'last_run': (scheduler.last_run.isoformat() + 'Z') if scheduler.last_run else None,
            'next_run': (scheduler.next_run.isoformat() + 'Z') if scheduler.next_run else None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/cache-scheduler/config', methods=['POST'])
def update_cache_scheduler_config():
    """Update cache scheduler configuration (accepts times in Eastern Time, stores as UTC)"""
    try:
        data = request.get_json()
        scheduler = CacheScheduler.get_or_create()

        if 'enabled' in data:
            scheduler.enabled = bool(data['enabled'])

        # Start from the current stored UTC values, converted to ET as defaults
        et_day, et_hour, et_min = _convert_schedule_tz(
            scheduler.day_of_week, scheduler.hour, scheduler.minute, _UTC_TZ, _ET_TZ
        )

        if 'day_of_week' in data:
            et_day = int(data['day_of_week'])
            if not (0 <= et_day <= 6):
                return jsonify({'error': 'day_of_week must be 0-6'}), 400
        if 'hour' in data:
            et_hour = int(data['hour'])
            if not (0 <= et_hour <= 23):
                return jsonify({'error': 'hour must be 0-23'}), 400
        if 'minute' in data:
            et_min = int(data['minute'])
            if not (0 <= et_min <= 59):
                return jsonify({'error': 'minute must be 0-59'}), 400

        # Convert ET → UTC before storing
        utc_day, utc_hour, utc_min = _convert_schedule_tz(et_day, et_hour, et_min, _ET_TZ, _UTC_TZ)
        scheduler.day_of_week = utc_day
        scheduler.hour = utc_hour
        scheduler.minute = utc_min

        # Immediately recompute next_run so the UI reflects the new day/time right away
        if scheduler.enabled:
            scheduler.next_run = _compute_next_utc_run(utc_day, utc_hour, utc_min).replace(tzinfo=None)
        else:
            scheduler.next_run = None

        scheduler.updated_at = datetime.utcnow()
        db.session.commit()

        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        return jsonify({
            'success': True,
            'message': f'Scheduler {"enabled" if scheduler.enabled else "disabled"}',
            'enabled': scheduler.enabled,
            'day_of_week': et_day,
            'day_name': day_names[et_day],
            'hour': et_hour,
            'minute': et_min,
            'last_run': (scheduler.last_run.isoformat() + 'Z') if scheduler.last_run else None,
            'next_run': (scheduler.next_run.isoformat() + 'Z') if scheduler.next_run else None
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500



def get_sec_stock_symbols():
    """
    Fetch all valid tradeable stock symbols from the SEC's official company_tickers.json file.
    This is the most comprehensive and authoritative source of all US traded securities.
    Contains 10,000+ stocks across all US exchanges.
    """
    import time
    
    url = 'https://www.sec.gov/files/company_tickers.json'
    max_retries = 3
    
    # Use proper User-Agent to avoid rate limiting
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            
            # Extract all ticker symbols from SEC data
            tickers = []
            for entry in data.values():
                ticker = entry.get('ticker', '').strip().upper()
                if ticker:
                    tickers.append(ticker)
            
            unique_tickers = sorted(list(set(tickers)))
            print(f"Successfully fetched {len(unique_tickers)} securities from SEC")
            return unique_tickers
            
        except requests.exceptions.RequestException as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed to fetch SEC tickers: {e}")
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                time.sleep(wait_time)
            else:
                print("Warning: Failed to fetch SEC stock symbols after retries")
                return []
        except Exception as e:
            print(f"Error parsing SEC data: {e}")
            return []


def get_all_stock_symbols():
    """
    Get a comprehensive list of US stock symbols dynamically from the SEC.
    This includes all valid tradeable stocks across all exchanges.
    """
    return get_sec_stock_symbols()


def get_fallback_stock_symbols():
    """
    Fallback hardcoded list of US stocks for when dynamic fetch fails.
    """
    stocks = [
        # Mega cap tech
        'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'AMZN', 'NVDA', 'META', 'TSLA',
        # Large cap financials & diversified
        'BRK.B', 'BRK.A', 'JPM', 'BAC', 'GS', 'MS', 'C', 'WFC', 'USB', 'PNC', 'TD',
        # Healthcare & Pharma
        'JNJ', 'UNH', 'PFE', 'ABBV', 'MRK', 'TMO', 'ABT', 'CVS', 'LLY', 'AZN', 'AMGN', 'BNTX', 'MRNA', 'SYK',
        # Consumer discretionary
        'WMT', 'HD', 'NKE', 'MCD', 'SBUX', 'TJX', 'LOW', 'COST', 'MAR', 'LVS', 'ULTA', 'GWW',
        # Consumer staples
        'PG', 'PEP', 'KO', 'MO', 'PM', 'CL', 'KMB', 'GIS', 'EL', 'CAG', 'MDLZ', 'MNST',
        # Industrials
        'BA', 'CAT', 'MMM', 'HON', 'ITW', 'GE', 'EMR', 'ETN', 'ROK', 'PCAR', 'NSC', 'UNP', 'CSX',
        # Tech/Software
        'V', 'MA', 'ADBE', 'CRM', 'INTC', 'AMD', 'CSCO', 'PYPL', 'INTU', 'ANET', 'NOW', 'SNOW', 'AVLR', 'FTNT',
        # Semiconductors
        'QCOM', 'AVGO', 'MU', 'LRCX', 'ASML', 'TXN', 'MCHP', 'ON', 'KLAC', 'MRVL', 'SLAB', 'AMAT',
        # Communication services
        'DIS', 'NFLX', 'CMCSA', 'T', 'VZ', 'CHTR', 'FOX', 'FOXE', 'PARA', 'ROKU', 'SNAP', 'PINS', 'MTCH',
        # Energy
        'COP', 'CVX', 'XOM', 'SLB', 'EOG', 'FANG', 'KMI', 'OKE', 'MPC', 'PSX', 'PXD', 'WMB',
        # Utilities
        'SO', 'EXC', 'DUK', 'AEP', 'AWK', 'NEE', 'CMS', 'SRE', 'PEG', 'ED', 'XEL', 'PPL', 'AEE',
        # Real estate & Infrastructure
        'PLD', 'EQIX', 'DLR', 'WELL', 'IRM', 'AVB', 'EQR', 'AMT', 'CCI', 'O', 'PSA', 'SPG', 'WY',
        # Materials
        'MLM', 'HUN', 'APD', 'DOW', 'DD', 'ECL', 'LIN', 'PPG', 'ALB', 'NEM', 'GLD', 'SLV', 'FCX', 'RIO', 'VALE',
    ]
    
    return sorted(list(set(stocks)))


@main_bp.route('/research')
def research():
    """Display research page with stock recommendations"""
    suggestions = []
    error_message = None
    cache_status = None
    
    # Get filter parameters from query string with defaults
    symbols_input = request.args.get('symbols', '').strip()
    # Market cap is provided in MILLIONS from the form, so convert to BILLIONS for comparison
    market_cap_min_millions = request.args.get('market_cap_min', 0, type=float)  # Default 0M (no minimum)
    market_cap_max_millions = request.args.get('market_cap_max', 10000000, type=float)  # Default 10000000M (essentially unlimited)
    market_cap_min = market_cap_min_millions / 1000  # Convert to billions
    market_cap_max = market_cap_max_millions / 1000  # Convert to billions
    
    distance_min = request.args.get('distance_min', 0, type=float)  # Default 0%
    distance_max = request.args.get('distance_max', 100, type=float)  # Default 100% (entire 52-week range)
    forward_pe_max = request.args.get('forward_pe_max', 100, type=float)  # Default 100 (generous limit)
    rating_score_min = request.args.get('rating_score_min', 0, type=float)  # Default 0 (no minimum)
    rating_score_max = request.args.get('rating_score_max', 10, type=float)  # Default 100 (essentially unlimited)
    
    try:
        # Check if we have cached data
        cache_count = StockCache.query.count()
        
        if symbols_input:
            # If user provides custom symbols, fetch live data for those specific stocks
            symbols_to_search = [s.strip().upper() for s in symbols_input.split(',') if s.strip()]
            cache_status = f'Searching {len(symbols_to_search)} custom symbols (live data)'
            
            for symbol in symbols_to_search:
                try:
                    ticker = yf.Ticker(symbol)
                    hist = ticker.history(period='1y')
                    if len(hist) < 200:
                        continue
                    current_price = hist['Close'].iloc[-1]
                    info = ticker.info
                    week_52_high = hist['High'].tail(252).max()
                    week_52_low = hist['Low'].tail(252).min()
                    if not current_price or not week_52_low or not week_52_high:
                        continue
                    distance_from_low = ((current_price - week_52_low) / week_52_low) * 100
                    forward_pe = info.get('forwardPE') or info.get('trailingPE')
                    if not forward_pe:
                        continue
                    market_cap = info.get('marketCap')
                    if not market_cap:
                        continue
                    market_cap_billions = market_cap / 1_000_000_000
                    
                    if (
                        distance_min <= distance_from_low <= distance_max and
                        forward_pe <= forward_pe_max and
                        market_cap_min <= market_cap_billions <= market_cap_max
                    ):
                        # Calculate Graham Number
                        eps = info.get('trailingEps')
                        book_value_per_share = info.get('bookValue')
                        graham_number = None
                        if eps and book_value_per_share and eps > 0 and book_value_per_share > 0:
                            import math
                            graham_number = math.sqrt(22.5 * eps * book_value_per_share)
                        
                        # Fetch Graham metrics from GrahamValue
                        graham_metrics = get_graham_metrics_from_grahamvalue(symbol) or {}
                        
                        suggestions.append({
                            'symbol': symbol,
                            'name': info.get('longName', symbol),
                            'current_price': current_price,
                            'week_52_low': week_52_low,
                            'week_52_high': week_52_high,
                            'distance_from_low': distance_from_low,
                            'forward_pe': forward_pe,
                            'market_cap': market_cap,
                            'market_cap_billions': market_cap_billions,
                            'sector': info.get('sector', 'N/A'),
                            'dividend_yield': info.get('dividendYield', 0),
                            'pe_ratio': info.get('trailingPE'),
                            'eps': eps,
                            'book_value_per_share': book_value_per_share,
                            'graham_number': graham_number,
                            'rating_score': graham_metrics.get('rating_score'),
                            'size_in_sales': graham_metrics.get('size_in_sales'),
                            'current_assets_to_2x_liabilities': graham_metrics.get('current_assets_to_2x_liabilities'),
                            'net_current_assets_to_ltdebt': graham_metrics.get('net_current_assets_to_ltdebt'),
                            'earnings_stability': graham_metrics.get('earnings_stability'),
                            'dividend_record': graham_metrics.get('dividend_record'),
                            'earnings_growth': graham_metrics.get('earnings_growth'),
                            'graham_number_percent': graham_metrics.get('graham_number_percent'),
                            'ncav_or_net_net': graham_metrics.get('ncav_or_net_net'),
                            'equity_to_debt': graham_metrics.get('equity_to_debt'),
                            'size_in_assets': graham_metrics.get('size_in_assets')
                        })
                except Exception as e:
                    continue
        
        elif cache_count > 0:
            # Use cached data if available
            cache_status = f'✓ {cache_count:,} stocks cached and ready to search'
            
            # Query cached stocks with filters
            cached_stocks = StockCache.query.filter(
                StockCache.market_cap_billions >= market_cap_min,
                StockCache.market_cap_billions <= market_cap_max,
                StockCache.distance_from_low >= distance_min,
                StockCache.distance_from_low <= distance_max
            ).all()
            
            # Apply forward P/E and rating score filters (handling None values)
            for stock in cached_stocks:
                forward_pe = stock.forward_pe or stock.trailing_pe or 0
                if forward_pe == 0 or forward_pe > forward_pe_max:
                    continue
                
                # Filter by rating score if it exists
                if stock.rating_score is not None:
                    if not (rating_score_min <= stock.rating_score <= rating_score_max):
                        continue
                else:
                    # Skip stocks with no rating score if a filter is applied
                    if rating_score_min > 0 or rating_score_max < 100:
                        continue
                
                suggestions.append({
                    'symbol': stock.symbol,
                    'name': stock.name,
                    'current_price': stock.current_price,
                    'week_52_low': stock.price_52w_low,
                    'week_52_high': stock.price_52w_high,
                    'distance_from_low': stock.distance_from_low,
                    'forward_pe': forward_pe,
                    'market_cap': stock.market_cap,
                    'market_cap_billions': stock.market_cap_billions,
                    'sector': stock.sector,
                    'dividend_yield': stock.dividend_yield,
                    'pe_ratio': stock.trailing_pe,
                    'eps': stock.eps,
                    'book_value_per_share': stock.book_value_per_share,
                    'graham_number': stock.graham_number,
                    'rating_score': stock.rating_score,
                    'size_in_sales': stock.size_in_sales,
                    'current_assets_to_2x_liabilities': stock.current_assets_to_2x_liabilities,
                    'net_current_assets_to_ltdebt': stock.net_current_assets_to_ltdebt,
                    'earnings_stability': stock.earnings_stability,
                    'dividend_record': stock.dividend_record,
                    'earnings_growth': stock.earnings_growth,
                    'graham_number_percent': stock.graham_number_percent,
                    'ncav_or_net_net': stock.ncav_or_net_net,
                    'equity_to_debt': stock.equity_to_debt,
                    'size_in_assets': stock.size_in_assets
                })
        
        else:
            # If no cache and no custom symbols, show informational message
            cache_status = '⏳ Ready to cache 10,397+ SEC securities. Click "Download Stock Data" to begin.'
            error_message = 'Cache is empty. Click the "Download Stock Data" button to populate the cache with all 10,397+ SEC-listed securities. This one-time download enables fast filtering across your entire investment universe.'
    
    except Exception as e:
        error_message = f"Error fetching research data: {str(e)}"
    
    return render_template('research.html', 
                         suggestions=suggestions, 
                         error_message=error_message,
                         cache_status=cache_status,
                         symbols=symbols_input,
                         market_cap_min=market_cap_min_millions,
                         market_cap_max=market_cap_max_millions,
                         distance_min=distance_min,
                         distance_max=distance_max,
                         forward_pe_max=forward_pe_max,
                         rating_score_min=rating_score_min,
                         rating_score_max=rating_score_max)
