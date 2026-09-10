import os
import sys
import re
import random
import string
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ============================================
# CONFIGURATION
# ============================================

class Config:
    APP_NAME = "VyahMandap"
    APP_TAGLINE = "Taiyari Hamari, Celebration Aapka!"
    
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'vyahmandap-fixed-secret-key-2026-do-not-change'
    
    # SQLite Database - Use /tmp on Render, local otherwise
    if os.environ.get('RENDER') or 'RENDER' in os.environ:
        db_path = '/tmp/vyahmandap.db'
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{db_path}'
        UPLOAD_FOLDER = '/tmp/uploads'
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///database/vyahmandap.db'
        UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {
            'check_same_thread': False,
            'timeout': 30
        }
    }
    
    # ===== SESSION FIX (Fix for double-login issue) =====
    SESSION_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    REMEMBER_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    # Business Details
    BUSINESS_NAME = "VyahMandap"
    BUSINESS_PHONE = "8319337063"
    BUSINESS_PHONE_ALT = "9981845362"
    BUSINESS_EMAIL = "info@vyahmandap.com"
    BUSINESS_LOCATION = "Harda, Madhya Pradesh"
    
    # Admin Credentials
    ADMIN_MOBILE = "8319337063"
    ADMIN_PASSWORD = "123456"
    
    # Transport Rates
    TRANSPORT_RATES = {
        'city': {'till_20': 600, 'above_20': 1100},
        'outskirts': {'till_20': 1500, 'above_20': 2000}
    }
    
    COMMISSION_RATE = 0.05
    
    CATEGORIES = [
        ('furniture', 'Sofas & Furniture'),
        ('lighting', 'Truss & Lighting'),
        ('decor', 'Decor & Floral'),
        ('mandap', 'Mandap & Stage')
    ]


# ============================================
# INITIALIZE APP
# ============================================

app = Flask(__name__)
app.config.from_object(Config)

# Ensure directories exist
try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    print(f"✅ Upload folder created: {app.config['UPLOAD_FOLDER']}")
except Exception as e:
    print(f"⚠️ Could not create upload folder: {e}")

if 'sqlite:///database/' in app.config['SQLALCHEMY_DATABASE_URI']:
    os.makedirs('database', exist_ok=True)
    print("✅ Database directory created")

db = SQLAlchemy(app)


# ============================================
# MODELS
# ============================================

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mobile = db.Column(db.String(10), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='customer')  # admin, vendor, customer
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    items = db.relationship('Item', backref='vendor', lazy=True)
    bookings = db.relationship('Booking', backref='customer', lazy=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    @property
    def is_authenticated(self):
        return True
    
    @property
    def is_active(self):
        return True
    
    @property
    def is_anonymous(self):
        return False
    
    def get_id(self):
        return str(self.id)
    
    def __repr__(self):
        return f'<User {self.mobile}>'


class Item(db.Model):
    __tablename__ = 'items'
    
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(50), nullable=False)
    rate_per_day = db.Column(db.Float, nullable=False)
    deposit_amount = db.Column(db.Float, default=0)
    stock = db.Column(db.Integer, default=1)
    image_url = db.Column(db.String(500))
    image_filename = db.Column(db.String(200))
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_available = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    bookings = db.relationship('Booking', backref='item', lazy=True)
    
    @property
    def rate_with_commission(self):
        return round(self.rate_per_day * (1 + Config.COMMISSION_RATE), 2)
    
    def get_image(self):
        if self.image_url:
            return self.image_url
        elif self.image_filename:
            return f'/uploads/{self.image_filename}'
        return '/static/images/default-item.jpg'
    
    def __repr__(self):
        return f'<Item {self.title}>'


class Booking(db.Model):
    __tablename__ = 'bookings'
    
    id = db.Column(db.Integer, primary_key=True)
    booking_reference = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    
    start_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(10), default='10:00')
    end_date = db.Column(db.Date, nullable=False)
    end_time = db.Column(db.String(10), default='20:00')
    quantity = db.Column(db.Integer, default=1)
    
    venue_address = db.Column(db.Text, nullable=False)
    delivery_area = db.Column(db.String(20), default='city')
    weight_category = db.Column(db.String(20), default='till_20')
    
    base_rent = db.Column(db.Float, nullable=False)
    commission = db.Column(db.Float, nullable=False)
    deposit = db.Column(db.Float, nullable=False)
    transport_fee = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    
    utr_number = db.Column(db.String(50), nullable=False)
    payment_status = db.Column(db.String(20), default='pending')
    booking_status = db.Column(db.String(20), default='confirmed')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def get_status_badge(self):
        colors = {
            'confirmed': 'bg-green-100 text-green-800',
            'pending': 'bg-yellow-100 text-yellow-800',
            'cancelled': 'bg-red-100 text-red-800',
            'completed': 'bg-blue-100 text-blue-800'
        }
        return colors.get(self.booking_status, 'bg-gray-100 text-gray-800')
    
    def __repr__(self):
        return f'<Booking {self.booking_reference}>'


# ============================================
# LOGIN MANAGER
# ============================================

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.login_message = 'Please login to access this page.'
login_manager.login_message_category = 'warning'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ============================================
# UTILITY FUNCTIONS
# ============================================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS

def save_uploaded_file(file):
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
        
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            file.save(filepath)
            return unique_filename
        except Exception as e:
            print(f"Error saving file: {e}")
            return None
    return None

def calculate_booking_total(item, start_date, end_date, quantity, area, weight_category):
    days = (end_date - start_date).days + 1
    if days < 1:
        days = 1
    
    base_rent = item.rate_per_day * days * quantity
    commission = round(base_rent * Config.COMMISSION_RATE, 2)
    deposit = item.deposit_amount * quantity
    transport_fee = Config.TRANSPORT_RATES.get(area, {}).get(weight_category, 600)
    total = base_rent + commission + deposit + transport_fee
    
    return {
        'days': days,
        'base_rent': base_rent,
        'commission': commission,
        'deposit': deposit,
        'transport_fee': transport_fee,
        'total': total
    }

def generate_booking_reference():
    return 'VM' + ''.join(random.choices(string.digits, k=8))

def format_currency(amount):
    return f"₹{amount:,.2f}"

def validate_mobile(mobile):
    return re.match(r'^\d{10}$', mobile) is not None


# ============================================
# ROUTES - AUTHENTICATION
# ============================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        mobile = request.form.get('mobile', '').strip()
        password = request.form.get('password', '').strip()
        
        user = User.query.filter_by(mobile=mobile).first()
        
        if user and user.check_password(password):
            # LOGIN FIX: Use remember=True and permanent session
            login_user(user, remember=True)
            
            flash(f'🎉 Welcome back, {user.name}!', 'success')
            
            # Redirect based on role
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif user.role == 'vendor':
                return redirect(url_for('vendor_dashboard'))
            else:
                return redirect(url_for('dashboard'))
        else:
            flash('❌ Invalid mobile number or password.', 'danger')
    
    return render_template('auth/login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        mobile = request.form.get('mobile', '').strip()
        email = request.form.get('email', '').strip() or None
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        account_type = request.form.get('account_type', 'customer')
        
        errors = []
        if not name:
            errors.append('Name is required.')
        if not mobile:
            errors.append('Mobile number is required.')
        elif not validate_mobile(mobile):
            errors.append('Please enter a valid 10-digit mobile number.')
        if not password:
            errors.append('Password is required.')
        elif len(password) < 6:
            errors.append('Password must be at least 6 characters.')
        if password != confirm_password:
            errors.append('Passwords do not match.')
        
        if User.query.filter_by(mobile=mobile).first():
            errors.append('A user with this mobile number already exists.')
        
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('auth/register.html')
        
        # Only allow customer or vendor on signup (not admin)
        role = 'vendor' if account_type == 'vendor' else 'customer'
        
        user = User(
            name=name,
            mobile=mobile,
            email=email,
            role=role
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('✅ Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
    
    return render_template('auth/register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('🔒 You have been logged out.', 'info')
    return redirect(url_for('index'))


# ============================================
# ROUTES - MAIN
# ============================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/items')
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

@app.route('/api/calculate', methods=['POST'])
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

@app.route('/book/<int:item_id>', methods=['GET', 'POST'])
@login_required
def book_item(item_id):
    item = Item.query.get_or_404(item_id)
    
    if not item.is_available or item.stock <= 0:
        flash('This item is currently not available for booking.', 'danger')
        return redirect(url_for('index'))
    
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
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating booking: {str(e)}', 'danger')
            return render_template('booking.html', item=item)
    
    return render_template('booking.html', item=item)

@app.route('/dashboard')
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


# ============================================
# ROUTES - VENDOR DASHBOARD (NEW)
# ============================================

@app.route('/vendor')
@login_required
def vendor_dashboard():
    """Vendor dashboard - shows their items, bookings, and stats"""
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied. Vendor account required.', 'danger')
        return redirect(url_for('index'))
    
    # Get vendor's items
    items = Item.query.filter_by(vendor_id=current_user.id)\
        .order_by(Item.created_at.desc()).all()
    
    item_ids = [i.id for i in items]
    
    # Get bookings for vendor's items
    if item_ids:
        bookings = Booking.query.filter(Booking.item_id.in_(item_ids))\
            .order_by(Booking.created_at.desc()).all()
    else:
        bookings = []
    
    # Calculate stats
    total_earnings = sum(b.base_rent for b in bookings)
    total_bookings = len(bookings)
    active_rentals = sum(1 for b in bookings if b.booking_status == 'confirmed')
    total_items = len(items)
    
    # Upcoming bookings (next 30 days)
    today = datetime.utcnow().date()
    thirty_days_later = today + timedelta(days=30)
    upcoming_bookings = [
        b for b in bookings 
        if b.start_date >= today and b.start_date <= thirty_days_later
        and b.booking_status == 'confirmed'
    ]
    
    return render_template('vendor/dashboard.html',
                         items=items,
                         bookings=bookings,
                         total_earnings=total_earnings,
                         total_bookings=total_bookings,
                         active_rentals=active_rentals,
                         total_items=total_items,
                         upcoming_bookings=upcoming_bookings)


@app.route('/vendor/calendar')
@login_required
def vendor_calendar():
    """Vendor calendar view - shows all bookings in calendar format"""
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied. Vendor account required.', 'danger')
        return redirect(url_for('index'))
    
    items = Item.query.filter_by(vendor_id=current_user.id).all()
    return render_template('vendor/calendar.html', items=items)


@app.route('/api/vendor/calendar')
@login_required
def api_vendor_calendar():
    """API endpoint for calendar data"""
    if current_user.role not in ['admin', 'vendor']:
        return jsonify([])
    
    items = Item.query.filter_by(vendor_id=current_user.id).all()
    item_ids = [i.id for i in items]
    
    if not item_ids:
        return jsonify([])
    
    # Optional filter by item
    item_filter = request.args.get('item_id', 'all')
    query = Booking.query.filter(Booking.item_id.in_(item_ids))
    
    if item_filter != 'all':
        query = query.filter_by(item_id=int(item_filter))
    
    bookings = query.all()
    
    # Color palette for different items
    colors = ['#2d5a3d', '#b45309', '#1e40af', '#7c2d12', '#166534', '#9a3412', '#a16207']
    item_color_map = {}
    for idx, item in enumerate(items):
        item_color_map[item.id] = colors[idx % len(colors)]
    
    events = []
    for b in bookings:
        events.append({
            'id': b.id,
            'title': f"{b.item.title} ({b.quantity})",
            'start': b.start_date.isoformat(),
            'end': (b.end_date + timedelta(days=1)).isoformat(),  # FullCalendar end is exclusive
            'backgroundColor': item_color_map.get(b.item_id, '#2d5a3d'),
            'borderColor': item_color_map.get(b.item_id, '#2d5a3d'),
            'extendedProps': {
                'customer': b.customer.name,
                'customer_mobile': b.customer.mobile,
                'item': b.item.title,
                'quantity': b.quantity,
                'base_rent': b.base_rent,
                'total': b.total_amount,
                'status': b.booking_status,
                'venue': b.venue_address,
                'utr': b.utr_number,
                'reference': b.booking_reference,
                'start_time': b.start_time,
                'end_time': b.end_time
            }
        })
    
    return jsonify(events)


@app.route('/api/vendor/availability')
@login_required
def api_vendor_availability():
    """Check item availability for a date range"""
    if current_user.role not in ['admin', 'vendor']:
        return jsonify({'error': 'Access denied'}), 403
    
    item_id = request.args.get('item_id')
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    if not all([item_id, start_date_str, end_date_str]):
        return jsonify({'error': 'Missing parameters'}), 400
    
    item = Item.query.get_or_404(int(item_id))
    
    # Check ownership
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        return jsonify({'error': 'Access denied'}), 403
    
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    
    # Get overlapping bookings
    overlapping = Booking.query.filter(
        Booking.item_id == item.id,
        Booking.booking_status == 'confirmed',
        Booking.start_date <= end_date,
        Booking.end_date >= start_date
    ).all()
    
    booked_qty = sum(b.quantity for b in overlapping)
    available_qty = max(0, item.stock - booked_qty)
    
    return jsonify({
        'item_id': item.id,
        'item_title': item.title,
        'total_stock': item.stock,
        'booked_quantity': booked_qty,
        'available_quantity': available_qty,
        'is_available': available_qty > 0,
        'bookings': [{
            'reference': b.booking_reference,
            'customer': b.customer.name,
            'start': b.start_date.isoformat(),
            'end': b.end_date.isoformat(),
            'quantity': b.quantity
        } for b in overlapping]
    })


@app.route('/vendor/item/add', methods=['GET', 'POST'])
@login_required
def vendor_add_item():
    """Vendor can add their own items"""
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
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
            return render_template('vendor/item_form.html')
        
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
        return redirect(url_for('vendor_dashboard'))
    
    return render_template('vendor/item_form.html')


@app.route('/vendor/item/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def vendor_edit_item(item_id):
    """Vendor can edit their own items"""
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    item = Item.query.get_or_404(item_id)
    
    # Check ownership
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied. This item does not belong to you.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    
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
        return redirect(url_for('vendor_dashboard'))
    
    return render_template('vendor/item_form.html', item=item)


@app.route('/vendor/item/delete/<int:item_id>', methods=['POST'])
@login_required
def vendor_delete_item(item_id):
    """Vendor can delete their own items"""
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    item = Item.query.get_or_404(item_id)
    
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    
    db.session.delete(item)
    db.session.commit()
    flash('Item deleted successfully.', 'success')
    return redirect(url_for('vendor_dashboard'))


# ============================================
# ROUTES - ADMIN
# ============================================

@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Access denied. Admin privileges required.', 'danger')
        return redirect(url_for('index'))
    
    total_bookings = Booking.query.count()
    total_commission = db.session.query(db.func.sum(Booking.commission)).scalar() or 0
    total_revenue = db.session.query(db.func.sum(Booking.total_amount)).scalar() or 0
    total_items = Item.query.count()
    total_users = User.query.count()
    total_vendors = User.query.filter_by(role='vendor').count()
    pending_bookings = Booking.query.filter_by(booking_status='pending').count()
    
    recent_bookings = Booking.query.order_by(Booking.created_at.desc()).limit(20).all()
    
    return render_template('admin/dashboard.html',
                         total_bookings=total_bookings,
                         total_commission=total_commission,
                         total_revenue=total_revenue,
                         total_items=total_items,
                         total_users=total_users,
                         total_vendors=total_vendors,
                         pending_bookings=pending_bookings,
                         recent_bookings=recent_bookings)

@app.route('/admin/items')
@login_required
def admin_items():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    items = Item.query.order_by(Item.created_at.desc()).all()
    return render_template('admin/items.html', items=items)

@app.route('/admin/item/add', methods=['GET', 'POST'])
@login_required
def admin_add_item():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
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
        return redirect(url_for('admin_items'))
    
    return render_template('admin/item_form.html')

@app.route('/admin/item/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
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
        return redirect(url_for('admin_items'))
    
    return render_template('admin/item_form.html', item=item)

@app.route('/admin/item/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_delete_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    item = Item.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    flash('Item deleted successfully.', 'success')
    return redirect(url_for('admin_items'))

@app.route('/admin/bookings')
@login_required
def admin_bookings():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    return render_template('admin/bookings.html', bookings=bookings)

@app.route('/admin/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def admin_update_booking_status(booking_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get('status', '')
    
    if new_status in ['confirmed', 'cancelled', 'completed', 'pending']:
        booking.booking_status = new_status
        db.session.commit()
        flash('Booking status updated.', 'success')
    
    return redirect(url_for('admin_bookings'))

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)

@app.route('/admin/user/<int:user_id>/role', methods=['POST'])
@login_required
def admin_update_user_role(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role', '')
    
    if new_role in ['admin', 'customer', 'vendor'] and user.id != current_user.id:
        user.role = new_role
        db.session.commit()
        flash('User role updated.', 'success')
    
    return redirect(url_for('admin_users'))


# ============================================
# CONTEXT PROCESSORS
# ============================================

@app.context_processor
def utility_processor():
    return dict(
        app_name=Config.APP_NAME,
        app_tagline=Config.APP_TAGLINE,
        business_phone=Config.BUSINESS_PHONE,
        business_phone_alt=Config.BUSINESS_PHONE_ALT,
        business_location=Config.BUSINESS_LOCATION,
        categories=Config.CATEGORIES,
        format_currency=format_currency,
        get_category_icon=lambda c: {
            'furniture': 'fa-couch',
            'lighting': 'fa-lightbulb',
            'decor': 'fa-palette',
            'mandap': 'fa-archway'
        }.get(c, 'fa-box')
    )


# ============================================
# CREATE TABLES & DEFAULT DATA
# ============================================

def init_database():
    try:
        db.create_all()
        print("✅ Database tables created successfully!")
        
        admin = User.query.filter_by(mobile=Config.ADMIN_MOBILE).first()
        if not admin:
            admin = User(
                name='VyahMandap Admin',
                mobile=Config.ADMIN_MOBILE,
                email='admin@vyahmandap.com',
                role='admin'
            )
            admin.set_password(Config.ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.commit()
            print('✅ Admin user created!')
        
        demo = User.query.filter_by(mobile='9876543210').first()
        if not demo:
            demo = User(
                name='Demo Customer',
                mobile='9876543210',
                email='demo@vyahmandap.com',
                role='customer'
            )
            demo.set_password('123456')
            db.session.add(demo)
            db.session.commit()
            print('✅ Demo customer created!')
        
        # Create demo vendor
        demo_vendor = User.query.filter_by(mobile='9999888877').first()
        if not demo_vendor:
            demo_vendor = User(
                name='Demo Vendor',
                mobile='9999888877',
                email='vendor@vyahmandap.com',
                role='vendor'
            )
            demo_vendor.set_password('123456')
            db.session.add(demo_vendor)
            db.session.commit()
            print('✅ Demo vendor created!')
        
        if Item.query.count() == 0:
            default_items = [
                {
                    'title': 'Maharaja Gold Carved Wedding Sofa',
                    'description': 'Elegant gold carved sofa for royal wedding setups.',
                    'category': 'furniture',
                    'rate_per_day': 4500,
                    'deposit_amount': 3000,
                    'stock': 5,
                    'image_url': 'https://images.unsplash.com/photo-1586023492125-27b2c045efd7?auto=format&fit=crop&w=600&q=80',
                    'vendor_id': admin.id
                },
                {
                    'title': 'Heavy Truss & LED Setup (Per Box)',
                    'description': 'Professional truss lighting system for events.',
                    'category': 'lighting',
                    'rate_per_day': 2500,
                    'deposit_amount': 1500,
                    'stock': 12,
                    'image_url': 'https://images.unsplash.com/photo-1516450360452-9312f5e86fc7?auto=format&fit=crop&w=600&q=80',
                    'vendor_id': admin.id
                },
                {
                    'title': 'Royal Floral Mandap Setup',
                    'description': 'Beautiful floral mandap for wedding ceremonies.',
                    'category': 'mandap',
                    'rate_per_day': 12000,
                    'deposit_amount': 5000,
                    'stock': 3,
                    'image_url': 'https://images.unsplash.com/photo-1519741497674-611481863552?auto=format&fit=crop&w=600&q=80',
                    'vendor_id': admin.id
                },
                {
                    'title': 'Wedding Arch Decor',
                    'description': 'Elegant wedding arch with floral arrangements.',
                    'category': 'decor',
                    'rate_per_day': 7500,
                    'deposit_amount': 2500,
                    'stock': 4,
                    'image_url': 'https://images.unsplash.com/photo-1519225421980-715cb0215aed?auto=format&fit=crop&w=600&q=80',
                    'vendor_id': admin.id
                }
            ]
            
            for item_data in default_items:
                item = Item(**item_data)
                db.session.add(item)
            
            db.session.commit()
            print('✅ Default items created!')
        
        return True
    except Exception as e:
        print(f"❌ Error initializing database: {e}")
        return False


# ============================================
# RUN APP
# ============================================

with app.app_context():
    print("=" * 50)
    print(f"🚀 Starting {Config.APP_NAME}...")
    print(f"📁 Database: {app.config['SQLALCHEMY_DATABASE_URI']}")
    print(f"📁 Upload folder: {app.config['UPLOAD_FOLDER']}")
    print("=" * 50)
    
    db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    if db_path:
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, exist_ok=True)
                print(f"✅ Created database directory: {db_dir}")
            except Exception as e:
                print(f"⚠️ Could not create database directory: {e}")
    
    init_database()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
