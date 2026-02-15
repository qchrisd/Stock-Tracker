from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from app.models import db, Stock

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Display all tracked stocks and their value changes"""
    stocks = Stock.query.all()
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

    return render_template('index.html', portfolio=portfolio_data, summary=portfolio_summary)


@main_bp.route('/add', methods=['GET', 'POST'])
def add_stock():
    """Add a new stock to track"""
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').upper().strip()
        date_str = request.form.get('date')
        shares_str = request.form.get('shares', '').strip()
        
        if not symbol or not date_str or not shares_str:
            flash('Please provide symbol, date, and number of shares', 'error')
            return redirect(url_for('main.add_stock'))

        # Check if stock already exists
        if Stock.query.filter_by(symbol=symbol).first():
            flash(f'Stock {symbol} is already being tracked', 'error')
            return redirect(url_for('main.add_stock'))

        try:
            # Validate and parse shares
            try:
                shares = float(shares_str)
                if shares <= 0:
                    flash('Number of shares must be greater than 0', 'error')
                    return redirect(url_for('main.add_stock'))
            except ValueError:
                flash('Invalid number of shares. Please enter a valid number', 'error')
                return redirect(url_for('main.add_stock'))

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
                    return redirect(url_for('main.add_stock'))

            initial_price = float(hist['Close'].iloc[0])

            # Create new stock record
            new_stock = Stock(
                symbol=symbol,
                add_date=add_date,
                shares=shares,
                initial_price=initial_price
            )
            
            db.session.add(new_stock)
            db.session.commit()

            total_value = shares * initial_price
            flash(f'Successfully added {shares} shares of {symbol} at ${initial_price:.2f} (Total: ${total_value:,.2f}) on {add_date}', 'success')
            return redirect(url_for('main.index'))

        except ValueError:
            flash('Invalid date format. Please use YYYY-MM-DD', 'error')
            return redirect(url_for('main.add_stock'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding stock: {str(e)}', 'error')
            return redirect(url_for('main.add_stock'))

    return render_template('add_stock.html')


@main_bp.route('/stock/<int:stock_id>/delete', methods=['POST'])
def delete_stock(stock_id):
    """Delete a tracked stock"""
    stock = Stock.query.get(stock_id)
    
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.index'))

    try:
        symbol = stock.symbol
        db.session.delete(stock)
        db.session.commit()
        flash(f'Successfully removed {symbol} from tracking', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting stock: {str(e)}', 'error')

    return redirect(url_for('main.index'))


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
    stocks = Stock.query.all()
    
    if not stocks:
        return jsonify({'error': 'No stocks tracked yet'}), 404
    
    chart_data = {'stocks': {}, 'sp500': None}
    
    try:
        # Get earliest add_date to use for S&P 500
        earliest_date = min(stock.add_date for stock in stocks)
        
        # Fetch S&P 500 data
        sp500_data = Stock.get_sp500_historical_data(earliest_date)
        if sp500_data:
            chart_data['sp500'] = sp500_data
        
        # Fetch historical data for each stock
        for stock in stocks:
            hist_data = stock.get_historical_data()
            if hist_data:
                chart_data['stocks'][stock.symbol] = {
                    'name': stock.symbol,
                    'data': hist_data
                }
        
        return jsonify(chart_data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
