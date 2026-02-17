from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, Response
from datetime import datetime
import yfinance as yf
import requests
import json
from app.models import db, Stock, Transaction, Account, StockCache

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Display portfolio (owned stocks)"""
    return redirect(url_for('main.portfolio'))


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
    Server-Sent Events endpoint that downloads and caches financial data for all SEC stocks.
    Sends progress updates to the client.
    """
    from flask import current_app
    import traceback
    
    # Get the app while we're still in the request context
    app = current_app._get_current_object()
    
    def generate_progress():
        # Create an app context for the generator execution
        with app.app_context():
            try:
                # Get all SEC tickers
                yield f"data: {json.dumps({'status': 'fetching_tickers', 'message': 'Fetching SEC securities list...'})}\n\n"
                
                symbols = get_sec_stock_symbols()
                total = len(symbols)
                
                if total == 0:
                    yield f"data: {json.dumps({'status': 'error', 'message': 'Failed to fetch SEC ticker list'})}\n\n"
                    return
                
                yield f"data: {json.dumps({'status': 'fetching_started', 'total': total, 'message': f'Fetching financial data for {total:,} SEC-listed securities...'})}\n\n"
                
                # Clear old cache
                StockCache.query.delete()
                db.session.commit()
                
                processed = 0
                successful = 0
                batch = []  # Batch of entries to add
                
                for symbol in symbols:
                    processed += 1
                    try:
                        # Fetch data with timeout
                        ticker = yf.Ticker(symbol)
                        
                        # Fetch historical data (1 year)
                        hist = ticker.history(period='1y')
                        if len(hist) < 200:
                            continue
                        
                        # Fetch info
                        info = ticker.info
                        
                        # Calculate metrics
                        current_price = float(hist['Close'].iloc[-1]) if not hist.empty else None
                        price_52w_low = float(hist['Low'].tail(252).min()) if len(hist) >= 252 else float(hist['Low'].min())
                        price_52w_high = float(hist['High'].tail(252).max()) if len(hist) >= 252 else float(hist['High'].max())
                        
                        if not current_price or not price_52w_low:
                            continue
                        
                        distance_from_low = ((current_price - price_52w_low) / price_52w_low) * 100
                        
                        market_cap = info.get('marketCap')
                        market_cap_billions = (market_cap / 1_000_000_000) if market_cap else None
                        
                        forward_pe = info.get('forwardPE')
                        trailing_pe = info.get('trailingPE')
                        dividend_yield = info.get('dividendYield', 0)
                        
                        # Create cache entry
                        cache_entry = StockCache(
                            symbol=symbol,
                            name=info.get('longName', symbol),
                            sector=info.get('sector', 'N/A'),
                            market_cap=market_cap,
                            market_cap_billions=market_cap_billions,
                            forward_pe=forward_pe,
                            trailing_pe=trailing_pe,
                            dividend_yield=dividend_yield,
                            current_price=current_price,
                            price_52w_low=price_52w_low,
                            price_52w_high=price_52w_high,
                            distance_from_low=distance_from_low
                        )
                        
                        batch.append(cache_entry)
                        successful += 1
                        
                        # Commit every 10 stocks
                        if successful % 10 == 0:
                            for entry in batch:
                                db.session.add(entry)
                            db.session.commit()
                            batch = []
                            yield f"data: {json.dumps({'status': 'progress', 'processed': processed, 'total': total, 'successful': successful, 'percent': int((processed/total)*100)})}\n\n"
                    
                    except Exception as e:
                        # Skip stocks with errors - don't print
                        pass
                
                # Final commit for remaining batch
                if batch:
                    for entry in batch:
                        db.session.add(entry)
                    db.session.commit()
                
                yield f"data: {json.dumps({'status': 'complete', 'processed': processed, 'total': total, 'successful': successful, 'message': f'Successfully cached {successful:,} stocks'})}\n\n"
                
            except Exception as e:
                error_msg = f"{str(e)}"
                tb = traceback.format_exc()
                print(f"Error in cache_stocks: {error_msg}")
                print(tb)
                yield f"data: {json.dumps({'status': 'error', 'message': f'Error during caching: {error_msg}', 'traceback': tb})}\n\n"
    
    return Response(generate_progress(), mimetype='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no'
    })



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
    market_cap_min = request.args.get('market_cap_min', 0.1, type=float)  # Default 0.1B (minimum viable cap)
    market_cap_max = request.args.get('market_cap_max', 10000, type=float)  # Default 10000B (essentially unlimited)
    distance_min = request.args.get('distance_min', 0, type=float)  # Default 0%
    distance_max = request.args.get('distance_max', 100, type=float)  # Default 100% (entire 52-week range)
    forward_pe_max = request.args.get('forward_pe_max', 100, type=float)  # Default 100 (generous limit)
    
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
                            'pe_ratio': info.get('trailingPE')
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
            
            # Apply forward P/E filter (handling None values)
            for stock in cached_stocks:
                forward_pe = stock.forward_pe or stock.trailing_pe or 0
                if forward_pe == 0 or forward_pe > forward_pe_max:
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
                    'pe_ratio': stock.trailing_pe
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
                         market_cap_min=market_cap_min,
                         market_cap_max=market_cap_max,
                         distance_min=distance_min,
                         distance_max=distance_max,
                         forward_pe_max=forward_pe_max)
