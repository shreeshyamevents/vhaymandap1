from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from models import db, Item, Booking
from utils import calculate_booking_total, generate_booking_reference
from datetime import datetime

bp = Blueprint('main', __name__)

@bp.route('/')
def index():
    return render_template('index.html')

@bp.route('/api/items')
def get_items():
    category = request.args.get('category', 'all')
    query = Item.query.filter_by(is_available=True)
    
    if category != 'all':
        query = query.filter_by(category=category)
    
    items = query.order_by(Item.created_at.desc()).all()
    
    return jsonify([{
        'id': item.id,
        'title': item.title,
        'description': item.description,
        'category': item.category,
        'rate': item.rate_per_day,
        'rate_with_commission': item.rate_with_commission,
        'deposit': item.deposit_amount,
        'stock': item.stock,
        'image': item.get_image(),
        'vendor': item.vendor.name,
        'vendor_id': item.vendor_id
    } for item in items])

@bp.route('/api/calculate', methods=['POST'])
def calculate():
    data = request.get_json()
    item_id = data.get('item_id')
    start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
    end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    quantity = int(data.get('quantity', 1))
    area = data.get('area', 'city')
    weight = data.get('weight', 'till_20')
    
    item = Item.query.get_or_404(item_id)
    result = calculate_booking_total(item, start_date, end_date, quantity, area, weight)
    
    return jsonify(result)

@bp.route('/book/<int:item_id>', methods=['GET', 'POST'])
@login_required
def book_item(item_id):
    item = Item.query.get_or_404(item_id)
    
    if not item.is_available or item.stock <= 0:
        flash('This item is currently not available for booking.', 'danger')
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        try:
            start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
            end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()
            quantity = int(request.form.get('quantity', 1))
            area = request.form.get('area', 'city')
            weight = request.form.get('weight', 'till_20')
            venue_address = request.form.get('address', '').strip()
            utr = request.form.get('utr', '').strip()
            
            if not venue_address:
                flash('Venue address is required.', 'danger')
                return render_template('booking.html', item=item)
            
            if not utr:
                flash('UTR number is required.', 'danger')
                return render_template('booking.html', item=item)
            
            if quantity > item.stock:
                flash(f'Only {item.stock} items available.', 'danger')
                return render_template('booking.html', item=item)
            
            calc = calculate_booking_total(item, start_date, end_date, quantity, area, weight)
            
            booking = Booking(
                booking_reference=generate_booking_reference(),
                customer_id=current_user.id,
                item_id=item.id,
                start_date=start_date,
                start_time=request.form.get('start_time', '10:00'),
                end_date=end_date,
                end_time=request.form.get('end_time', '20:00'),
                quantity=quantity,
                venue_address=venue_address,
                delivery_area=area,
                weight_category=weight,
                base_rent=calc['base_rent'],
                commission=calc['commission'],
                deposit=calc['deposit'],
                transport_fee=calc['transport_fee'],
                total_amount=calc['total'],
                utr_number=utr,
                payment_status='verified',
                booking_status='confirmed'
            )
            
            item.stock -= quantity
            if item.stock <= 0:
                item.is_available = False
            
            db.session.add(booking)
            db.session.commit()
            
            flash(f'🎉 Booking confirmed! Reference: {booking.booking_reference}', 'success')
            return redirect(url_for('main.dashboard'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating booking: {str(e)}', 'danger')
            return render_template('booking.html', item=item)
    
    return render_template('booking.html', item=item)

@bp.route('/dashboard')
@login_required
def dashboard():
    bookings = Booking.query.filter_by(customer_id=current_user.id)\
        .order_by(Booking.created_at.desc()).all()
    
    total_spent = sum(b.total_amount for b in bookings)
    active_bookings = sum(1 for b in bookings if b.booking_status == 'confirmed')
    
    return render_template('dashboard.html',
                         bookings=bookings,
                         total_spent=total_spent,
                         active_bookings=active_bookings)