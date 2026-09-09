from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from models import db, Item, Booking, User
from utils import save_uploaded_file
from datetime import datetime
from sqlalchemy import func

bp = Blueprint('admin', __name__, url_prefix='/admin')

@bp.before_request
@login_required
def require_admin():
    if current_user.role != 'admin':
        flash('Access denied. Admin privileges required.', 'danger')
        return redirect(url_for('main.index'))

@bp.route('/')
def dashboard():
    total_bookings = Booking.query.count()
    total_commission = db.session.query(func.sum(Booking.commission)).scalar() or 0
    total_revenue = db.session.query(func.sum(Booking.total_amount)).scalar() or 0
    total_items = Item.query.count()
    total_users = User.query.count()
    pending_bookings = Booking.query.filter_by(booking_status='pending').count()
    
    # Monthly stats
    current_month = datetime.now().month
    monthly_bookings = Booking.query.filter(
        func.extract('month', Booking.created_at) == current_month
    ).count()
    monthly_revenue = db.session.query(func.sum(Booking.total_amount)).filter(
        func.extract('month', Booking.created_at) == current_month
    ).scalar() or 0
    
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(20).all()
    
    return render_template('admin/dashboard.html',
                         total_bookings=total_bookings,
                         total_commission=total_commission,
                         total_revenue=total_revenue,
                         total_items=total_items,
                         total_users=total_users,
                         pending_bookings=pending_bookings,
                         monthly_bookings=monthly_bookings,
                         monthly_revenue=monthly_revenue,
                         recent_bookings=recent_bookings)

@bp.route('/items')
def items():
    items = Item.query.order_by(Item.created_at.desc()).all()
    return render_template('admin/items.html', items=items)

@bp.route('/item/add', methods=['GET', 'POST'])
def add_item():
    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', '')
        rate = float(request.form.get('rate', 0))
        deposit = float(request.form.get('deposit', 0))
        stock = int(request.form.get('stock', 1))
        image_url = request.form.get('image_url', '').strip()
        
        image_filename = None
        if 'image_file' in request.files and request.files['image_file'].filename:
            image_filename = save_uploaded_file(request.files['image_file'])
        
        if not title or rate <= 0:
            flash('Title and rate are required.', 'danger')
            return render_template('admin/item_form.html')
        
        item = Item(
            title=title,
            description=description,
            category=category,
            rate_per_day=rate,
            deposit_amount=deposit,
            stock=stock,
            image_url=image_url if image_url else None,
            image_filename=image_filename,
            vendor_id=current_user.id
        )
        
        db.session.add(item)
        db.session.commit()
        
        flash('✅ Item added successfully!', 'success')
        return redirect(url_for('admin.items'))
    
    return render_template('admin/item_form.html')

@bp.route('/item/edit/<int:item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    item = Item.query.get_or_404(item_id)
    
    if request.method == 'POST':
        item.title = request.form.get('title', '').strip()
        item.description = request.form.get('description', '').strip()
        item.category = request.form.get('category', '')
        item.rate_per_day = float(request.form.get('rate', 0))
        item.deposit_amount = float(request.form.get('deposit', 0))
        item.stock = int(request.form.get('stock', 1))
        item.is_available = 'is_available' in request.form
        
        if 'image_file' in request.files and request.files['image_file'].filename:
            image_filename = save_uploaded_file(request.files['image_file'])
            if image_filename:
                item.image_filename = image_filename
                item.image_url = None
        
        image_url = request.form.get('image_url', '').strip()
        if image_url:
            item.image_url = image_url
            item.image_filename = None
        
        db.session.commit()
        flash('✅ Item updated successfully!', 'success')
        return redirect(url_for('admin.items'))
    
    return render_template('admin/item_form.html', item=item)

@bp.route('/item/delete/<int:item_id>', methods=['POST'])
def delete_item(item_id):
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash('Item deleted successfully.', 'success')
    return redirect(url_for('admin.items'))

@bp.route('/bookings')
def bookings():
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    return render_template('admin/bookings.html', bookings=bookings)

@bp.route('/booking/<int:booking_id>/status', methods=['POST'])
def update_booking_status(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get('status', '')
    
    if new_status in ['confirmed', 'cancelled', 'completed', 'pending']:
        booking.booking_status = new_status
        db.session.commit()
        flash('Booking status updated.', 'success')
    
    return redirect(url_for('admin.bookings'))

@bp.route('/users')
def users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)

@bp.route('/user/<int:user_id>/role', methods=['POST'])
def update_user_role(user_id):
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role', '')
    
    if new_role in ['admin', 'customer'] and user.id != current_user.id:
        user.role = new_role
        db.session.commit()
        flash('User role updated.', 'success')
    
    return redirect(url_for('admin.users'))