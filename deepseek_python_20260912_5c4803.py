import os
import sys
import re
import random
import string
import requests
import threading
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
    
    if os.environ.get('RENDER') or 'RENDER' in os.environ:
        db_path = '/tmp/vyahmandap.db'
        SQLALCHEMY_DATABASE_URI = f'sqlite:///{db_path}'
        UPLOAD_FOLDER = '/tmp/uploads'
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///database/vyahmandap.db'
        UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'connect_args': {'check_same_thread': False, 'timeout': 30}
    }
    
    SESSION_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    REMEMBER_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    # Telegram Bot Configuration
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or ''
    
    BUSINESS_NAME = "VyahMandap"
    BUSINESS_PHONE = "8319337063"
    BUSINESS_PHONE_ALT = "9981845362"
    BUSINESS_EMAIL = "info@vyahmandap.com"
    BUSINESS_LOCATION = "Harda, Madhya Pradesh"
    
    ADMIN_MOBILE = "8319337063"
    ADMIN_PASSWORD = "123456"
    
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


app = Flask(__name__)
app.config.from_object(Config)

try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
except Exception as e:
    print(f"⚠️ Could not create upload folder: {e}")

if 'sqlite:///database/' in app.config['SQLALCHEMY_DATABASE_URI']:
    os.makedirs('database', exist_ok=True)

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
    role = db.Column(db.String(20), default='customer')
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)
    verified_until = db.Column(db.DateTime, nullable=True)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
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
    is_verified = db.Column(db.Boolean, default=False)
    verified_until = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    bookings = db.relationship('Booking', backref='item', lazy=True)
    
    @property
    def rate_with_commission(self):
        return round(self.rate_per_day * (1 + Config.COMMISSION_RATE), 2)
    
    @property
    def is_currently_verified(self):
        if not self.is_verified:
            return False
        if self.verified_until and self.verified_until > datetime.utcnow():
            return True
        return False
    
    def get_image(self):
        if self.image_url:
            return self.image_url
        elif self.image_filename:
            return f'/uploads/{self.image_filename}'
        return 'https://via.placeholder.com/400x300/e5e7eb/9ca3af?text=No+Image'


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
    kyc_required = db.Column(db.Boolean, default=False)
    aadhaar_number = db.Column(db.String(12), nullable=True)
    pan_number = db.Column(db.String(10), nullable=True)
    kyc_verified = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(db.Model):
    __tablename__ = 'messages'
    id = db.Column(db.Integer, primary_key=True)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    receiver_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    body = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    sender = db.relationship('User', foreign_keys=[sender_id], backref='sent_messages')
    receiver = db.relationship('User', foreign_keys=[receiver_id], backref='received_messages')


class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, unique=True)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    rating = db.Column(db.Integer, nullable=False)
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    customer = db.relationship('User', foreign_keys=[customer_id])


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
    return {'days': days, 'base_rent': base_rent, 'commission': commission,
            'deposit': deposit, 'transport_fee': transport_fee, 'total': total}

def generate_booking_reference():
    return 'VM' + ''.join(random.choices(string.digits, k=8))

def format_currency(amount):
    return f"₹{amount:,.2f}"

def validate_mobile(mobile):
    return re.match(r'^\d{10}$', mobile) is not None

def mask_phone_numbers(text):
    return re.sub(r'\b(\d{2})\d{6}(\d{2})\b', r'\1XXXXXX\2', text)

def contains_too_many_digits(text, max_consecutive=4):
    matches = re.findall(r'\d{' + str(max_consecutive + 1) + r',}', text)
    return len(matches) > 0

def get_longest_digit_sequence(text):
    sequences = re.findall(r'\d+', text)
    if not sequences:
        return 0
    return max(len(s) for s in sequences)


# ============================================
# TELEGRAM NOTIFICATION HELPERS
# ============================================

def send_telegram_notification(chat_id, message):
    """Send a message via Telegram bot (runs in background thread)"""
    token = Config.TELEGRAM_BOT_TOKEN
    if not chat_id or not token:
        return
    
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        response.raise_for_status()
        print(f"✅ Telegram notification sent to {chat_id}")
    except requests.RequestException as e:
        print(f"❌ Telegram error: {e}")


def send_telegram_notification_async(chat_id, message):
    """Wrapper to send Telegram notification in a background thread"""
    if chat_id:
        thread = threading.Thread(
            target=send_telegram_notification,
            args=(chat_id, message)
        )
        thread.daemon = True
        thread.start()


# ============================================
# AUTH ROUTES
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
            login_user(user, remember=True)
            flash(f'🎉 Welcome back, {user.name}!', 'success')
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
        if not name: errors.append('Name is required.')
        if not mobile: errors.append('Mobile number is required.')
        elif not validate_mobile(mobile): errors.append('Please enter a valid 10-digit mobile number.')
        if not password: errors.append('Password is required.')
        elif len(password) < 6: errors.append('Password must be at least 6 characters.')
        if password != confirm_password: errors.append('Passwords do not match.')
        if User.query.filter_by(mobile=mobile).first():
            errors.append('A user with this mobile number already exists.')
        
        if errors:
            for error in errors: flash(error, 'danger')
            return render_template('auth/register.html')
        
        role = 'vendor' if account_type == 'vendor' else 'customer'
        user = User(name=name, mobile=mobile, email=email, role=role)
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
# TELEGRAM LINKING ROUTE
# ============================================

@app.route('/link-telegram', methods=['GET', 'POST'])
@login_required
def link_telegram():
    if request.method == 'POST':
        chat_id = request.form.get('chat_id', '').strip()
        if chat_id:
            # Validate it's numeric
            if not re.match(r'^-?\d+$', chat_id):
                flash('⚠️ Chat ID must be a number.', 'danger')
                return render_template('link_telegram.html', current_chat_id=current_user.telegram_chat_id)
            
            current_user.telegram_chat_id = chat_id
            db.session.commit()
            
            # Send a confirmation test message
            test_msg = (
                f"✅ *VyahMandap* — Telegram linked!\n\n"
                f"Hi *{current_user.name}*,\n\n"
                f"You will now receive notifications here for:\n"
                f"• New bookings\n"
                f"• Chat messages\n"
                f"• Payment updates\n"
                f"• Booking status changes\n\n"
                f"_Taiyari Hamari, Celebration Aapka!_ 🎉"
            )
            send_telegram_notification_async(chat_id, test_msg)
            
            flash('✅ Telegram linked! Check your Telegram for confirmation.', 'success')
            return redirect(url_for('link_telegram'))
        else:
            flash('⚠️ Please enter a valid Chat ID.', 'danger')
    
    return render_template('link_telegram.html', current_chat_id=current_user.telegram_chat_id)


@app.route('/unlink-telegram', methods=['POST'])
@login_required
def unlink_telegram():
    current_user.telegram_chat_id = None
    db.session.commit()
    flash('🔓 Telegram unlinked.', 'info')
    return redirect(url_for('link_telegram'))


# ============================================
# MAIN ROUTES
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
    
    result = []
    for item in items:
        avg_rating = 0
        review_count = 0
        try:
            reviews = Review.query.filter_by(item_id=item.id).all()
            if reviews:
                avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1)
                review_count = len(reviews)
        except:
            pass
        
        result.append({
            'id': item.id, 'title': item.title, 'description': item.description,
            'category': item.category, 'rate': item.rate_per_day,
            'rate_with_commission': item.rate_with_commission,
            'deposit': item.deposit_amount, 'stock': item.stock,
            'image': item.get_image(), 'vendor': item.vendor.name,
            'vendor_id': item.vendor_id,
            'vendor_verified': item.vendor.is_verified,
            'item_verified': item.is_currently_verified,
            'avg_rating': avg_rating,
            'review_count': review_count
        })
    return jsonify(result)


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


@app.route('/api/item/<int:item_id>/availability-calendar')
def item_availability_calendar(item_id):
    item = Item.query.get_or_404(item_id)
    bookings = Booking.query.filter_by(item_id=item_id)\
        .filter(Booking.booking_status.in_(['confirmed', 'pending'])).all()
    
    if not bookings:
        return jsonify([])
    
    date_booked = {}
    for b in bookings:
        current = b.start_date
        while current <= b.end_date:
            date_booked[current] = date_booked.get(current, 0) + b.quantity
            current += timedelta(days=1)
    
    events = []
    for date, booked_qty in date_booked.items():
        available_qty = item.stock - booked_qty
        if available_qty <= 0:
            status = 'booked'; color = '#dc2626'
        elif available_qty < item.stock:
            status = 'partial'; color = '#eab308'
        else:
            status = 'available'; color = '#16a34a'
        
        events.append({
            'title': f"{available_qty}/{item.stock} available",
            'start': date.isoformat(),
            'allDay': True,
            'backgroundColor': color,
            'borderColor': color,
            'extendedProps': {
                'status': status,
                'total_stock': item.stock,
                'booked_qty': booked_qty,
                'available_qty': max(0, available_qty)
            }
        })
    return jsonify(events)


@app.route('/api/item/<int:item_id>/availability')
def item_availability_single(item_id):
    item = Item.query.get_or_404(item_id)
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({'error': 'date required'}), 400
    try:
        check_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except:
        return jsonify({'error': 'invalid date'}), 400
    
    overlapping = Booking.query.filter(
        Booking.item_id == item_id,
        Booking.booking_status.in_(['confirmed', 'pending']),
        Booking.start_date <= check_date,
        Booking.end_date >= check_date
    ).all()
    
    booked_qty = sum(b.quantity for b in overlapping)
    available_qty = max(0, item.stock - booked_qty)
    if available_qty <= 0:
        status = 'booked'
    elif available_qty < item.stock:
        status = 'partial'
    else:
        status = 'available'
    
    return jsonify({
        'date': date_str, 'status': status,
        'total_stock': item.stock, 'booked_qty': booked_qty,
        'available_qty': available_qty
    })


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
            
            kyc_required = calc['total'] >= 30000
            aadhaar = None
            pan = None
            
            if kyc_required:
                aadhaar = request.form.get('aadhaar', '').strip()
                pan = request.form.get('pan', '').strip().upper()
                if not aadhaar or not re.match(r'^\d{12}$', aadhaar):
                    flash('⚠️ Booking ≥ ₹30,000 — Valid 12-digit Aadhaar required.', 'danger')
                    return render_template('booking.html', item=item)
                if not pan or not re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
                    flash('⚠️ Booking ≥ ₹30,000 — Valid PAN required (e.g. ABCDE1234F).', 'danger')
                    return render_template('booking.html', item=item)
            
            booking = Booking(
                booking_reference=generate_booking_reference(),
                customer_id=current_user.id, item_id=item.id,
                start_date=start_date, start_time=request.form.get('start_time', '10:00'),
                end_date=end_date, end_time=request.form.get('end_time', '20:00'),
                quantity=quantity, venue_address=venue_address,
                delivery_area=area, weight_category=weight,
                base_rent=calc['base_rent'], commission=calc['commission'],
                deposit=calc['deposit'], transport_fee=calc['transport_fee'],
                total_amount=calc['total'], utr_number=utr,
                payment_status='verified', booking_status='confirmed',
                kyc_required=kyc_required, aadhaar_number=aadhaar,
                pan_number=pan, kyc_verified=kyc_required
            )
            item.stock -= quantity
            if item.stock <= 0:
                item.is_available = False
            db.session.add(booking)
            db.session.commit()
            
            # ===== Telegram Notifications =====
            # To customer
            if current_user.telegram_chat_id:
                customer_msg = (
                    f"🎉 *Booking Confirmed!*\n\n"
                    f"*Item:* {item.title}\n"
                    f"*Reference:* `{booking.booking_reference}`\n"
                    f"*Dates:* {start_date} → {end_date}\n"
                    f"*Quantity:* {quantity}\n"
                    f"*Total Paid:* ₹{calc['total']}\n\n"
                    f"Thank you for booking with VyahMandap! 🙏"
                )
                send_telegram_notification_async(current_user.telegram_chat_id, customer_msg)
            
            # To vendor
            if item.vendor.telegram_chat_id:
                vendor_msg = (
                    f"📦 *New Booking Received!*\n\n"
                    f"*Item:* {item.title}\n"
                    f"*Customer:* {current_user.name}\n"
                    f"*Mobile:* {current_user.mobile[:2]}XXXX{current_user.mobile[-2:]}\n"
                    f"*Reference:* `{booking.booking_reference}`\n"
                    f"*Dates:* {start_date} → {end_date}\n"
                    f"*Quantity:* {quantity}\n"
                    f"*Your Earning:* ₹{calc['base_rent']}\n\n"
                    f"Please prepare the equipment. 🚚"
                )
                send_telegram_notification_async(item.vendor.telegram_chat_id, vendor_msg)
            
            if kyc_required:
                flash(f'🎉 Booking confirmed! Reference: {booking.booking_reference}. KYC verified.', 'success')
            else:
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
    return render_template('dashboard.html', bookings=bookings,
                         total_spent=total_spent, active_bookings=active_bookings)


# ============================================
# VENDOR ROUTES
# ============================================

@app.route('/vendor')
@login_required
def vendor_dashboard():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied. Vendor account required.', 'danger')
        return redirect(url_for('index'))
    
    items = Item.query.filter_by(vendor_id=current_user.id)\
        .order_by(Item.created_at.desc()).all()
    item_ids = [i.id for i in items]
    
    if item_ids:
        bookings = Booking.query.filter(Booking.item_id.in_(item_ids))\
            .order_by(Booking.created_at.desc()).all()
    else:
        bookings = []
    
    total_earnings = sum(b.base_rent for b in bookings)
    total_bookings = len(bookings)
    active_rentals = sum(1 for b in bookings if b.booking_status == 'confirmed')
    total_items = len(items)
    
    today = datetime.utcnow().date()
    thirty_days_later = today + timedelta(days=30)
    upcoming_bookings = [
        b for b in bookings 
        if b.start_date >= today and b.start_date <= thirty_days_later
        and b.booking_status == 'confirmed'
    ]
    
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_bookings=total_bookings,
                         active_rentals=active_rentals,
                         total_items=total_items,
                         upcoming_bookings=upcoming_bookings)


@app.route('/vendor/calendar')
@login_required
def vendor_calendar():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied. Vendor account required.', 'danger')
        return redirect(url_for('index'))
    items = Item.query.filter_by(vendor_id=current_user.id).all()
    return render_template('vendor/calendar.html', items=items)


@app.route('/api/vendor/calendar')
@login_required
def api_vendor_calendar():
    if current_user.role not in ['admin', 'vendor']:
        return jsonify([])
    
    items = Item.query.filter_by(vendor_id=current_user.id).all()
    item_ids = [i.id for i in items]
    
    if not item_ids:
        return jsonify([])
    
    item_filter = request.args.get('item_id', 'all')
    query = Booking.query.filter(Booking.item_id.in_(item_ids))
    if item_filter != 'all':
        query = query.filter_by(item_id=int(item_filter))
    bookings = query.all()
    
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
            'end': (b.end_date + timedelta(days=1)).isoformat(),
            'backgroundColor': item_color_map.get(b.item_id, '#2d5a3d'),
            'borderColor': item_color_map.get(b.item_id, '#2d5a3d'),
            'extendedProps': {
                'customer': b.customer.name,
                'customer_mobile': b.customer.mobile,
                'customer_id': b.customer_id,
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


@app.route('/vendor/item/add', methods=['GET', 'POST'])
@login_required
def vendor_add_item():
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
        item = Item(title=title, description=description, category=category,
                    rate_per_day=rate, deposit_amount=deposit, stock=stock,
                    image_url=image_url if image_url else None,
                    image_filename=image_filename, vendor_id=current_user.id)
        db.session.add(item)
        db.session.commit()
        flash('✅ Item added successfully!', 'success')
        return redirect(url_for('vendor_dashboard'))
    return render_template('vendor/item_form.html')


@app.route('/vendor/item/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def vendor_edit_item(item_id):
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
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
# VERIFICATION ROUTES
# ============================================

VERIFICATION_PRICE = 999
VERIFICATION_DAYS = 90


@app.route('/vendor/item/<int:item_id>/verify', methods=['GET', 'POST'])
@login_required
def vendor_verify_item(item_id):
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    item = Item.query.get_or_404(item_id)
    
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied. This item does not belong to you.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    
    if item.is_currently_verified:
        flash('This item is already verified.', 'info')
        return redirect(url_for('vendor_dashboard'))
    
    if request.method == 'POST':
        utr = request.form.get('utr', '').strip()
        if not utr:
            flash('UTR number is required.', 'danger')
            return render_template('vendor/verify_item.html', item=item, price=VERIFICATION_PRICE)
        
        item.is_verified = True
        if item.verified_until and item.verified_until > datetime.utcnow():
            item.verified_until = item.verified_until + timedelta(days=VERIFICATION_DAYS)
        else:
            item.verified_until = datetime.utcnow() + timedelta(days=VERIFICATION_DAYS)
        
        db.session.commit()
        
        # Telegram notification
        if current_user.telegram_chat_id:
            msg = (
                f"✅ *Item Verified!*\n\n"
                f"*Item:* {item.title}\n"
                f"*Valid Until:* {item.verified_until.strftime('%d %b, %Y')}\n"
                f"*Fee Paid:* ₹{VERIFICATION_PRICE}\n\n"
                f"Your item now shows a blue verified badge! ✓"
            )
            send_telegram_notification_async(current_user.telegram_chat_id, msg)
        
        flash(f'✅ Item verified successfully! Valid until {item.verified_until.strftime("%d %b, %Y")}.', 'success')
        return redirect(url_for('vendor_dashboard'))
    
    return render_template('vendor/verify_item.html', item=item, price=VERIFICATION_PRICE)


# ============================================
# REVIEW ROUTES
# ============================================

@app.route('/item/<int:item_id>/reviews')
def item_reviews(item_id):
    item = Item.query.get_or_404(item_id)
    reviews = Review.query.filter_by(item_id=item_id).order_by(Review.created_at.desc()).all()
    avg_rating = 0
    if reviews:
        avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1)
    return render_template('item_reviews.html', item=item, reviews=reviews, avg_rating=avg_rating)


@app.route('/booking/<int:booking_id>/review', methods=['GET', 'POST'])
@login_required
def submit_review(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.customer_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    if booking.booking_status not in ['completed', 'confirmed']:
        flash('You can only review completed or confirmed bookings.', 'warning')
        return redirect(url_for('dashboard'))
    existing = Review.query.filter_by(booking_id=booking_id).first()
    if existing:
        flash('You have already reviewed this booking.', 'info')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        rating = int(request.form.get('rating', 0))
        comment = request.form.get('comment', '').strip()
        if rating < 1 or rating > 5:
            flash('Please select a rating between 1 and 5.', 'danger')
            return render_template('review_form.html', booking=booking)
        review = Review(booking_id=booking.id, item_id=booking.item_id,
                       customer_id=current_user.id, rating=rating, comment=comment)
        db.session.add(review)
        db.session.commit()
        
        # Telegram notification to vendor
        if booking.item.vendor.telegram_chat_id:
            stars = '⭐' * rating
            msg = (
                f"⭐ *New Review Received!*\n\n"
                f"*Item:* {booking.item.title}\n"
                f"*Customer:* {current_user.name}\n"
                f"*Rating:* {stars} ({rating}/5)\n"
                f"*Comment:* {comment[:150] if comment else '_No comment_'}\n\n"
                f"Keep up the good work! 🙌"
            )
            send_telegram_notification_async(booking.item.vendor.telegram_chat_id, msg)
        
        flash('⭐ Thank you for your review!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('review_form.html', booking=booking)


# ============================================
# CHAT ROUTES
# ============================================

@app.route('/chat')
@login_required
def chat_list():
    messages = Message.query.filter(
        (Message.sender_id == current_user.id) | (Message.receiver_id == current_user.id)
    ).order_by(Message.created_at.desc()).all()
    
    conversations = {}
    for msg in messages:
        other_id = msg.receiver_id if msg.sender_id == current_user.id else msg.sender_id
        if other_id not in conversations:
            other_user = User.query.get(other_id)
            if other_user:
                conversations[other_id] = {
                    'user': other_user,
                    'last_message': msg,
                    'unread': 0
                }
        if not msg.is_read and msg.receiver_id == current_user.id:
            conversations[other_id]['unread'] += 1
    
    return render_template('chat/list.html', conversations=conversations.values())


@app.route('/chat/<int:user_id>', methods=['GET', 'POST'])
@login_required
def chat_with(user_id):
    other_user = User.query.get_or_404(user_id)
    
    if other_user.id == current_user.id:
        flash('You cannot chat with yourself.', 'danger')
        return redirect(url_for('chat_list'))
    
    item_id = request.args.get('item_id', type=int)
    
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        if not body:
            flash('Message cannot be empty.', 'danger')
        elif contains_too_many_digits(body, max_consecutive=4):
            longest = get_longest_digit_sequence(body)
            flash(f'⚠️ Message blocked! Cannot share more than 4 consecutive digits '
                  f'(found {longest}). This prevents phone number sharing.', 'danger')
        else:
            masked_body = mask_phone_numbers(body)
            msg = Message(sender_id=current_user.id, receiver_id=other_user.id,
                         item_id=item_id, body=masked_body)
            db.session.add(msg)
            db.session.commit()
            
            # Telegram notification to receiver
            if other_user.telegram_chat_id:
                preview = masked_body[:100] + ('...' if len(masked_body) > 100 else '')
                tg_msg = (
                    f"💬 *New Message from {current_user.name}*\n\n"
                    f"{preview}\n\n"
                    f"[Open Chat](https://vhaymandap1.onrender.com/chat/{current_user.id})"
                )
                send_telegram_notification_async(other_user.telegram_chat_id, tg_msg)
            
            if masked_body != body:
                flash('ℹ️ Some numbers were masked for privacy.', 'info')
            return redirect(url_for('chat_with', user_id=other_user.id))
    
    messages = Message.query.filter(
        ((Message.sender_id == current_user.id) & (Message.receiver_id == other_user.id)) |
        ((Message.sender_id == other_user.id) & (Message.receiver_id == current_user.id))
    ).order_by(Message.created_at.asc()).all()
    
    for msg in messages:
        if msg.receiver_id == current_user.id and not msg.is_read:
            msg.is_read = True
    db.session.commit()
    
    item = Item.query.get(item_id) if item_id else None
    
    return render_template('chat/conversation.html',
                         other_user=other_user, messages=messages, item=item)


@app.route('/api/chat/send', methods=['POST'])
@login_required
def api_chat_send():
    data = request.get_json()
    receiver_id = data.get('receiver_id')
    body = data.get('body', '').strip()
    item_id = data.get('item_id')
    
    if not receiver_id or not body:
        return jsonify({'error': 'Missing fields'}), 400
    
    other_user = User.query.get(receiver_id)
    if not other_user:
        return jsonify({'error': 'User not found'}), 404
    
    if contains_too_many_digits(body, max_consecutive=4):
        longest = get_longest_digit_sequence(body)
        return jsonify({'error': f'Number masking: Cannot share more than 4 consecutive digits. Found {longest}.'}), 400
    
    masked_body = mask_phone_numbers(body)
    msg = Message(sender_id=current_user.id, receiver_id=other_user.id,
                 item_id=item_id, body=masked_body)
    db.session.add(msg)
    db.session.commit()
    
    # Telegram notification
    if other_user.telegram_chat_id:
        preview = masked_body[:100] + ('...' if len(masked_body) > 100 else '')
        tg_msg = (
            f"💬 *New Message from {current_user.name}*\n\n"
            f"{preview}\n\n"
            f"[Open Chat](https://vhaymandap1.onrender.com/chat/{current_user.id})"
        )
        send_telegram_notification_async(other_user.telegram_chat_id, tg_msg)
    
    return jsonify({
        'success': True,
        'message': {
            'id': msg.id, 'body': msg.body,
            'masked': masked_body != body,
            'created_at': msg.created_at.strftime('%H:%M')
        }
    })


@app.route('/api/chat/messages/<int:user_id>')
@login_required
def api_chat_messages(user_id):
    messages = Message.query.filter(
        ((Message.sender_id == current_user.id) & (Message.receiver_id == user_id)) |
        ((Message.sender_id == user_id) & (Message.receiver_id == current_user.id))
    ).order_by(Message.created_at.asc()).all()
    
    for msg in messages:
        if msg.receiver_id == current_user.id and not msg.is_read:
            msg.is_read = True
    db.session.commit()
    
    return jsonify([{
        'id': m.id, 'sender_id': m.sender_id, 'body': m.body,
        'is_mine': m.sender_id == current_user.id,
        'created_at': m.created_at.strftime('%d %b, %H:%M')
    } for m in messages])


# ============================================
# ADMIN ROUTES
# ============================================

@app.route('/admin')
@login_required
def admin_dashboard():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
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
                         total_bookings=total_bookings, total_commission=total_commission,
                         total_revenue=total_revenue, total_items=total_items,
                         total_users=total_users, total_vendors=total_vendors,
                         pending_bookings=pending_bookings, recent_bookings=recent_bookings)


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
        item = Item(title=title, description=description, category=category,
                    rate_per_day=rate, deposit_amount=deposit, stock=stock,
                    image_url=image_url if image_url else None,
                    image_filename=image_filename, vendor_id=current_user.id)
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
        old_status = booking.booking_status
        booking.booking_status = new_status
        db.session.commit()
        
        # Telegram notification to customer
        if booking.customer.telegram_chat_id:
            status_emoji = {
                'confirmed': '✅',
                'cancelled': '❌',
                'completed': '🎉',
                'pending': '⏳'
            }.get(new_status, '🔄')
            
            msg = (
                f"{status_emoji} *Booking Status Updated*\n\n"
                f"*Reference:* `{booking.booking_reference}`\n"
                f"*Item:* {booking.item.title}\n"
                f"*Old Status:* {old_status.title()}\n"
                f"*New Status:* {new_status.title()}\n\n"
                f"Thank you for using VyahMandap!"
            )
            send_telegram_notification_async(booking.customer.telegram_chat_id, msg)
        
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
    def unread_count():
        if not current_user.is_authenticated:
            return 0
        return Message.query.filter_by(receiver_id=current_user.id, is_read=False).count()
    
    return dict(
        app_name=Config.APP_NAME, app_tagline=Config.APP_TAGLINE,
        business_phone=Config.BUSINESS_PHONE,
        business_phone_alt=Config.BUSINESS_PHONE_ALT,
        business_location=Config.BUSINESS_LOCATION,
        categories=Config.CATEGORIES, format_currency=format_currency,
        unread_message_count=unread_count(),
        get_category_icon=lambda c: {
            'furniture': 'fa-couch', 'lighting': 'fa-lightbulb',
            'decor': 'fa-palette', 'mandap': 'fa-archway'
        }.get(c, 'fa-box')
    )


# ============================================
# INIT DATABASE
# ============================================

def init_database():
    try:
        db.create_all()
        print("✅ Database tables created!")
        
        admin = User.query.filter_by(mobile=Config.ADMIN_MOBILE).first()
        if not admin:
            admin = User(name='VyahMandap Admin', mobile=Config.ADMIN_MOBILE,
                        email='admin@vyahmandap.com', role='admin')
            admin.set_password(Config.ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.commit()
            print('✅ Admin created!')
        
        demo = User.query.filter_by(mobile='9876543210').first()
        if not demo:
            demo = User(name='Demo Customer', mobile='9876543210',
                       email='demo@vyahmandap.com', role='customer')
            demo.set_password('123456')
            db.session.add(demo)
            db.session.commit()
            print('✅ Demo customer created!')
        
        demo_vendor = User.query.filter_by(mobile='9999888877').first()
        if not demo_vendor:
            demo_vendor = User(name='Demo Vendor', mobile='9999888877',
                              email='vendor@vyahmandap.com', role='vendor')
            demo_vendor.set_password('123456')
            db.session.add(demo_vendor)
            db.session.commit()
            print('✅ Demo vendor created!')
        
        if Item.query.count() == 0:
            default_items = [
                {'title': 'Maharaja Gold Carved Wedding Sofa',
                 'description': 'Elegant gold carved sofa for royal wedding setups.',
                 'category': 'furniture', 'rate_per_day': 4500, 'deposit_amount': 3000,
                 'stock': 5, 'image_url': 'https://images.unsplash.com/photo-1586023492125-27b2c045efd7?auto=format&fit=crop&w=600&q=80',
                 'vendor_id': admin.id},
                {'title': 'Heavy Truss & LED Setup (Per Box)',
                 'description': 'Professional truss lighting system for events.',
                 'category': 'lighting', 'rate_per_day': 2500, 'deposit_amount': 1500,
                 'stock': 12, 'image_url': 'https://images.unsplash.com/photo-1516450360452-9312f5e86fc7?auto=format&fit=crop&w=600&q=80',
                 'vendor_id': admin.id},
                {'title': 'Royal Floral Mandap Setup',
                 'description': 'Beautiful floral mandap for wedding ceremonies.',
                 'category': 'mandap', 'rate_per_day': 12000, 'deposit_amount': 5000,
                 'stock': 3, 'image_url': 'https://images.unsplash.com/photo-1519741497674-611481863552?auto=format&fit=crop&w=600&q=80',
                 'vendor_id': admin.id},
                {'title': 'Wedding Arch Decor',
                 'description': 'Elegant wedding arch with floral arrangements.',
                 'category': 'decor', 'rate_per_day': 7500, 'deposit_amount': 2500,
                 'stock': 4, 'image_url': 'https://images.unsplash.com/photo-1519225421980-715cb0215aed?auto=format&fit=crop&w=600&q=80',
                 'vendor_id': admin.id}
            ]
            for item_data in default_items:
                db.session.add(Item(**item_data))
            db.session.commit()
            print('✅ Default items created!')
        return True
    except Exception as e:
        print(f"❌ DB init error: {e}")
        return False


with app.app_context():
    print("=" * 50)
    print(f"🚀 Starting {Config.APP_NAME}...")
    print(f"📱 Telegram Bot: {'Enabled' if Config.TELEGRAM_BOT_TOKEN else 'Disabled (set TELEGRAM_BOT_TOKEN)'}")
    print("=" * 50)
    init_database()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)