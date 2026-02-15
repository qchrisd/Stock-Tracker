from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from app.models import db, Stock

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
    total_initial_value = 0
    total_current_value = 0

    for stock in stocks:
        try:
            current_price = stock.get_current_price()
            current_value = stock.get_current_value()
            value_change = stock.get_value_change()
            percent_change = stock.get_value_change_percent()
            
            portfolio_data.append({
                'stock': stock,
                'current_price': current_price,
                'current_value': current_value,
                'value_change': value_change,
                'percent_change': percent_change
            })
            
            initial_value = stock.get_initial_value()
            total_initial_value += initial_value
            if current_value is not None:
                total_current_value += current_value
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    portfolio_summary = {
        'total_initial_value': total_initial_value,
        'total_current_value': total_current_value if total_current_value > 0 else None,
        'total_value_change': total_current_value - total_initial_value if total_current_value > 0 else None,
        'total_percent_change': ((total_current_value - total_initial_value) / total_initial_value * 100) if total_initial_value > 0 and total_current_value > 0 else None
    }

    return render_template('portfolio.html', portfolio=portfolio_data, summary=portfolio_summary)


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
            
            db.session.add(new_stock)
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
