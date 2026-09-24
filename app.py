import os
import sys
import re
import random
import string
import requests
import threading
import base64
import json as json_lib
import tempfile
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Payment QR imports
import qrcode
from io import BytesIO
from urllib.parse import quote

# ============================================
# SAFETY: Clear invalid CLOUDINARY_URL
# ============================================
_env_cl_url = os.environ.get('CLOUDINARY_URL', '')
if _env_cl_url and not _env_cl_url.startswith('cloudinary://'):
    print(f"⚠️ Invalid CLOUDINARY_URL detected and removed: {_env_cl_url[:40]}...")
    os.environ.pop('CLOUDINARY_URL', None)

import cloudinary
import cloudinary.uploader


# ============================================
# CONFIGURATION
# ============================================
class Config:
    APP_NAME = "VyahMandap"
    APP_TAGLINE = "Taiyari Hamari, Celebration Aapka!"
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'vyahmandap-fixed-secret-key-2026-do-not-change'
    
    database_url = os.environ.get('DATABASE_URL')
    if database_url:
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        if 'sslmode' not in database_url:
            if '?' in database_url:
                database_url += '&sslmode=require'
            else:
                database_url += '?sslmode=require'
        SQLALCHEMY_DATABASE_URI = database_url
    else:
        SQLALCHEMY_DATABASE_URI = 'sqlite:///database/vyahmandap.db'
    
    if os.environ.get('RENDER') or 'RENDER' in os.environ:
        UPLOAD_FOLDER = '/tmp/uploads'
    else:
        UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    if not database_url:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'connect_args': {'check_same_thread': False, 'timeout': 30}
        }
    else:
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_pre_ping': True,
            'pool_recycle': 300,
        }
    
    SESSION_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    REMEMBER_COOKIE_SECURE = True if os.environ.get('RENDER') else False
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_DURATION = timedelta(days=7)
    
    MAX_CONTENT_LENGTH = 100 * 1024 * 1024
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or ''
    
    CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME') or ''
    CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY') or ''
    CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET') or ''
    
    REPORT_RETENTION_DAYS = 30
    CRON_SECRET = os.environ.get('CRON_SECRET') or 'vyahmandap-cron-secret-2026'
    
    BUSINESS_NAME = "VyahMandap"
    BUSINESS_PHONE = "8319337063"
    BUSINESS_PHONE_ALT = "9981845362"
    BUSINESS_EMAIL = "info@vyahmandap.com"
    BUSINESS_LOCATION = "Harda, Madhya Pradesh"
    
    ADMIN_MOBILE = "8319337063"
    ADMIN_PASSWORD = "123456"
    
    # DPDP compliance
    DPO_MOBILE = "8319337063"
    DPO_EMAIL = "privacy@vyahmandap.com"
    DELETION_GRACE_DAYS = 30
    
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

if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
    os.makedirs('database', exist_ok=True)

db = SQLAlchemy(app)

cloudinary.config(
    cloud_name=Config.CLOUDINARY_CLOUD_NAME,
    api_key=Config.CLOUDINARY_API_KEY,
    api_secret=Config.CLOUDINARY_API_SECRET,
    secure=True
)
print(f"📷 Cloudinary: {'Configured' if Config.CLOUDINARY_CLOUD_NAME else 'Not configured'}")


# ============================================
# MODELS
# ============================================
class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mobile = db.Column(db.String(10), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=True)
    company_name = db.Column(db.String(150), nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='customer')
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)
    verified_until = db.Column(db.DateTime, nullable=True)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # DPDP fields
    deletion_requested_at = db.Column(db.DateTime, nullable=True)
    deletion_scheduled_at = db.Column(db.DateTime, nullable=True)
    is_deleted            = db.Column(db.Boolean, default=False)
    anonymized_at         = db.Column(db.DateTime, nullable=True)
    
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
    def is_anonymous(self):
        return False
    
    def get_id(self):
        return str(self.id)
    
    @property
    def days_until_deletion(self):
        if not self.deletion_scheduled_at:
            return 0
        delta = self.deletion_scheduled_at - datetime.utcnow()
        return max(0, delta.days)


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
    deposit = db.Column(db.Float, default=0)
    transport_fee = db.Column(db.Float, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    utr_number = db.Column(db.String(50), nullable=False)
    payment_status = db.Column(db.String(20), default='pending')
    booking_status = db.Column(db.String(20), default='confirmed')
    kyc_required = db.Column(db.Boolean, default=False)
    aadhaar_number = db.Column(db.String(12), nullable=True)
    pan_number = db.Column(db.String(10), nullable=True)
    kyc_verified = db.Column(db.Boolean, default=False)
    dispatch_report_done = db.Column(db.Boolean, default=False)
    return_report_done = db.Column(db.Boolean, default=False)
    damage_flagged = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EquipmentReport(db.Model):
    __tablename__ = 'equipment_reports'
    id = db.Column(db.Integer, primary_key=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False)
    reporter_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    report_type = db.Column(db.String(20), nullable=False)
    photos_json = db.Column(db.Text, nullable=False)
    video_url = db.Column(db.String(500), nullable=True)
    video_public_id = db.Column(db.String(200), nullable=True)
    condition_rating = db.Column(db.String(20), default='good')
    damage_flagged = db.Column(db.Boolean, default=False)
    damage_notes = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text)
    gps_latitude = db.Column(db.String(30))
    gps_longitude = db.Column(db.String(30))
    device_info = db.Column(db.String(200))
    ip_address = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expired = db.Column(db.Boolean, default=False)
    expired_at = db.Column(db.DateTime, nullable=True)
    keep_forever = db.Column(db.Boolean, default=False)
    
    reporter = db.relationship('User', foreign_keys=[reporter_id])
    booking = db.relationship('Booking', backref='equipment_reports')
    
    @property
    def age_in_days(self):
        if not self.created_at:
            return 0
        return (datetime.utcnow() - self.created_at).days
    
    @property
    def is_expired(self):
        if self.keep_forever:
            return False
        if self.expired:
            return True
        return self.age_in_days >= Config.REPORT_RETENTION_DAYS
    
    @property
    def days_until_expiry(self):
        if self.keep_forever:
            return -1
        remaining = Config.REPORT_RETENTION_DAYS - self.age_in_days
        return max(0, remaining)


class AdminPaymentConfig(db.Model):
    __tablename__ = 'admin_payment_config'
    id = db.Column(db.Integer, primary_key=True)
    upi_id          = db.Column(db.String(120))
    upi_name        = db.Column(db.String(120), default='VyahMandap')
    account_name    = db.Column(db.String(120))
    account_number  = db.Column(db.String(40))
    ifsc_code       = db.Column(db.String(20))
    bank_name       = db.Column(db.String(120))
    branch          = db.Column(db.String(120))
    custom_qr_url   = db.Column(db.String(500))
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    @staticmethod
    def get():
        cfg = AdminPaymentConfig.query.first()
        if not cfg:
            cfg = AdminPaymentConfig()
            db.session.add(cfg)
            db.session.commit()
        return cfg


class PaymentProof(db.Model):
    __tablename__ = 'payment_proof'
    id              = db.Column(db.Integer, primary_key=True)
    booking_id      = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False)
    uploaded_by     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    method          = db.Column(db.String(20), default='upi')
    amount_paid     = db.Column(db.Float, default=0)
    transaction_id  = db.Column(db.String(100))
    payment_date    = db.Column(db.Date)
    payer_name      = db.Column(db.String(120))
    payer_bank      = db.Column(db.String(120))
    notes           = db.Column(db.Text)

    screenshot_url       = db.Column(db.String(500), nullable=False)
    screenshot_public_id = db.Column(db.String(200))

    status           = db.Column(db.String(20), default='pending')
    verified_by      = db.Column(db.Integer, db.ForeignKey('users.id'))
    verified_at      = db.Column(db.DateTime)
    rejection_reason = db.Column(db.Text)

    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    booking   = db.relationship('Booking', backref='payment_proofs')
    uploader  = db.relationship('User', foreign_keys=[uploaded_by])
    verifier  = db.relationship('User', foreign_keys=[verified_by])


class ConsentLog(db.Model):
    """DPDP — audit trail of user consents."""
    __tablename__ = 'consent_log'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    purpose      = db.Column(db.String(100), nullable=False)
    granted      = db.Column(db.Boolean, default=True)
    ip_address   = db.Column(db.String(45))
    user_agent   = db.Column(db.String(300))
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref='consent_logs')


class DataRequest(db.Model):
    """DPDP — audit log of data principal rights requests."""
    __tablename__ = 'data_request'
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    request_type = db.Column(db.String(30), nullable=False)
    status       = db.Column(db.String(20), default='pending')
    details      = db.Column(db.Text)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime)
    
    user = db.relationship('User', backref='data_requests')


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
    vendor_response = db.Column(db.Text, nullable=True)
    vendor_responded_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    customer = db.relationship('User', foreign_keys=[customer_id])


class Ticket(db.Model):
    __tablename__ = 'tickets'
    id = db.Column(db.Integer, primary_key=True)
    ticket_number = db.Column(db.String(20), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(30), default='other')
    priority = db.Column(db.String(20), default='medium')
    status = db.Column(db.String(20), default='open')
    assigned_to = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_reply_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', foreign_keys=[user_id], backref='tickets')
    assignee = db.relationship('User', foreign_keys=[assigned_to])
    replies = db.relationship('TicketReply', backref='ticket', lazy=True, cascade='all, delete-orphan')


class TicketReply(db.Model):
    __tablename__ = 'ticket_replies'
    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('tickets.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    is_admin_reply = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', foreign_keys=[user_id])


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
# DPDP — PENDING DELETION ENFORCEMENT
# ============================================
@app.before_request
def enforce_deletion_pending():
    """If user has pending deletion, restrict them to /my-data & logout."""
    if not current_user.is_authenticated:
        return
    if not current_user.deletion_requested_at:
        return
    # Allow these endpoints only
    allowed = {'my_data', 'my_data_cancel_deletion', 'logout', 'static',
               'my_data_export'}
    if request.endpoint in allowed:
        return
    flash('Your account is scheduled for deletion. Cancel to restore access.', 'warning')
    return redirect(url_for('my_data'))


# ============================================
# AUTO-DELETE OLD REPORTS
# ============================================
def cleanup_old_reports(dry_run=False):
    cutoff_date = datetime.utcnow() - timedelta(days=Config.REPORT_RETENTION_DAYS)
    
    eligible_reports = EquipmentReport.query.filter(
        EquipmentReport.created_at < cutoff_date,
        EquipmentReport.expired == False,
        EquipmentReport.keep_forever == False,
        EquipmentReport.damage_flagged == False
    ).all()
    
    stats = {
        'total_checked': EquipmentReport.query.count(),
        'eligible': len(eligible_reports),
        'deleted_photos': 0,
        'deleted_videos': 0,
        'failed': 0,
        'dry_run': dry_run
    }
    
    if dry_run:
        print(f"🔍 DRY RUN: {len(eligible_reports)} reports would be deleted")
        return stats
    
    for report in eligible_reports:
        try:
            photos = json_lib.loads(report.photos_json) if report.photos_json else []
            for photo in photos:
                try:
                    if photo.get('url'):
                        url = photo['url']
                        if 'cloudinary.com' in url and '/upload/' in url:
                            after_upload = url.split('/upload/')[-1]
                            if after_upload.startswith('v'):
                                parts = after_upload.split('/', 1)
                                if len(parts) > 1:
                                    after_upload = parts[1]
                            public_id = after_upload.rsplit('.', 1)[0]
                            cloudinary.uploader.destroy(public_id)
                            stats['deleted_photos'] += 1
                except Exception as e:
                    print(f"⚠️ Photo delete failed: {e}")
                    stats['failed'] += 1
            
            if report.video_public_id:
                try:
                    cloudinary.uploader.destroy(report.video_public_id, resource_type='video')
                    stats['deleted_videos'] += 1
                except Exception as e:
                    print(f"⚠️ Video delete failed: {e}")
                    stats['failed'] += 1
            
            report.expired = True
            report.expired_at = datetime.utcnow()
            report.video_url = None
            report.video_public_id = None
            
        except Exception as e:
            print(f"❌ Report {report.id} cleanup failed: {e}")
            stats['failed'] += 1
    
    db.session.commit()
    print(f"✅ Cleanup done: {stats}")
    return stats


# ============================================
# AVAILABILITY HELPERS
# ============================================
def get_next_available_date(item_id):
    latest_booking = Booking.query.filter(
        Booking.item_id == item_id,
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated'])
    ).order_by(Booking.end_date.desc()).first()
    if not latest_booking:
        return None
    return latest_booking.end_date + timedelta(days=1)


def check_availability_conflict(item_id, start_date, requested_quantity=1):
    item = Item.query.get(item_id)
    if not item:
        return (True, None, 0, 'fully_booked')
    
    overlapping = Booking.query.filter(
        Booking.item_id == item_id,
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']),
        Booking.start_date <= start_date,
        Booking.end_date >= start_date
    ).all()
    
    booked_qty = sum(b.quantity for b in overlapping)
    available_qty = max(0, item.stock - booked_qty)
    
    returning_booking = None
    for b in overlapping:
        if b.end_date == start_date:
            returning_booking = b
            break
    
    has_conflict = requested_quantity > available_qty
    conflict_type = None
    if has_conflict:
        if available_qty == 0:
            conflict_type = 'return_day_full' if returning_booking else 'fully_booked'
        else:
            conflict_type = 'return_day_partial' if returning_booking else 'insufficient_stock'
    
    return (has_conflict, returning_booking, available_qty, conflict_type)


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


def generate_ticket_number():
    return 'TK' + ''.join(random.choices(string.digits, k=8))


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


def extract_cloudinary_public_id(url):
    """Extract public_id from a Cloudinary URL."""
    try:
        if not url or 'cloudinary.com' not in url or '/upload/' not in url:
            return None
        after = url.split('/upload/')[-1]
        if after.startswith('v'):
            parts = after.split('/', 1)
            if len(parts) > 1:
                after = parts[1]
        return after.rsplit('.', 1)[0]
    except Exception:
        return None


# ============================================
# CONTACT INFO FILTERING
# ============================================
EMAIL_PATTERN = re.compile(
    r'\b[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9][A-Za-z0-9.-]*(?:\.[A-Za-z]{2,})?\b'
)
MAX_CHAT_CHARS = 2000


def contains_email(text):
    return EMAIL_PATTERN.search(text) is not None


def get_first_email(text):
    match = EMAIL_PATTERN.search(text)
    return match.group(0) if match else None


def ist_today():
    """Return today's date in India Standard Time (UTC+5:30)."""
    return (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()


def should_filter_contact_info(user_a, user_b):
    roles = {user_a.role, user_b.role}
    if 'admin' in roles:
        return False
    return roles == {'customer', 'vendor'}


def can_chat(user_a, user_b):
    """Check if two users are allowed to chat."""
    # Admin can chat with anyone
    if user_a.role == 'admin' or user_b.role == 'admin':
        return True

    # Customer <-> vendor allowed
    roles = {user_a.role, user_b.role}
    if roles == {'customer', 'vendor'}:
        return True

    # Allow continuation of existing conversations
    existing = Message.query.filter(
        ((Message.sender_id == user_a.id) & (Message.receiver_id == user_b.id)) |
        ((Message.sender_id == user_b.id) & (Message.receiver_id == user_a.id))
    ).first()
    return existing is not None


# ============================================
# CLOUDINARY UPLOAD HELPERS
# ============================================
def upload_base64_to_cloudinary(base64_string, folder='vyahmandap/reports'):
    if not Config.CLOUDINARY_CLOUD_NAME:
        print("⚠️ Cloudinary not configured")
        return None
    try:
        if ',' in base64_string:
            base64_string = base64_string.split(',')[1]
        result = cloudinary.uploader.upload(
            f"data:image/jpeg;base64,{base64_string}",
            folder=folder,
            resource_type='image',
            transformation=[
                {'width': 1200, 'height': 1200, 'crop': 'limit'},
                {'quality': 'auto:good'},
                {'fetch_format': 'auto'}
            ]
        )
        return result.get('secure_url')
    except Exception as e:
        print(f"❌ Cloudinary image upload failed: {e}")
        return None


def upload_file_to_cloudinary(file_obj, folder='vyahmandap/payments'):
    if not Config.CLOUDINARY_CLOUD_NAME or not file_obj:
        return None
    temp_path = None
    try:
        ext = ''
        if file_obj.filename and '.' in file_obj.filename:
            ext = '.' + file_obj.filename.rsplit('.', 1)[1].lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file_obj.save(tmp.name)
            temp_path = tmp.name
        result = cloudinary.uploader.upload(
            temp_path,
            folder=folder,
            resource_type='image',
            transformation=[
                {'width': 1600, 'height': 1600, 'crop': 'limit'},
                {'quality': 'auto:good'},
                {'fetch_format': 'auto'}
            ]
        )
        return {'url': result.get('secure_url'), 'public_id': result.get('public_id')}
    except Exception as e:
        print(f"❌ Cloudinary file upload failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except:
                pass


def upload_video_to_cloudinary(file_obj, folder='vyahmandap/reports/videos'):
    if not Config.CLOUDINARY_CLOUD_NAME:
        print("⚠️ Cloudinary not configured")
        return None, None
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as tmp:
            file_obj.save(tmp.name)
            temp_path = tmp.name
        file_size = os.path.getsize(temp_path)
        print(f"📹 Video file size: {file_size / (1024*1024):.2f} MB")
        result = cloudinary.uploader.upload(
            temp_path,
            folder=folder,
            resource_type='video',
            timeout=120,
            chunk_size=6000000,
            transformation=[
                {'width': 480, 'height': 360, 'crop': 'limit'},
                {'quality': 60},
                {'fetch_format': 'mp4'}
            ]
        )
        video_url = result.get('secure_url')
        public_id = result.get('public_id')
        print(f"✅ Video uploaded: {video_url}")
        return video_url, public_id
    except Exception as e:
        print(f"❌ Cloudinary video upload failed: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None, None
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except:
                pass


# ============================================
# DPDP — PURGE + ANONYMIZE
# ============================================
def purge_user_cloudinary_assets(user_id):
    """Delete all Cloudinary assets belonging to user."""
    deleted = {'payment_proofs': 0, 'equipment_reports': 0, 'item_images': 0}
    
    # Payment proofs
    proofs = PaymentProof.query.filter_by(uploaded_by=user_id).all()
    for p in proofs:
        pid = p.screenshot_public_id or extract_cloudinary_public_id(p.screenshot_url)
        if pid:
            try:
                cloudinary.uploader.destroy(pid)
                deleted['payment_proofs'] += 1
            except Exception as e:
                app.logger.warning(f"Cloudinary delete failed: {pid}: {e}")
        p.screenshot_url = '[deleted]'
        p.screenshot_public_id = None
    
    # Equipment reports (photos_json has {url, caption, item_title} — no public_id)
    reports = EquipmentReport.query.filter_by(reporter_id=user_id).all()
    for r in reports:
        if r.video_public_id:
            try:
                cloudinary.uploader.destroy(r.video_public_id, resource_type='video')
                deleted['equipment_reports'] += 1
            except Exception as e:
                app.logger.warning(f"Video delete failed: {e}")
        if r.photos_json:
            try:
                photos = json_lib.loads(r.photos_json)
                for photo in photos:
                    pid = photo.get('public_id') or extract_cloudinary_public_id(photo.get('url', ''))
                    if pid:
                        try:
                            cloudinary.uploader.destroy(pid)
                            deleted['equipment_reports'] += 1
                        except Exception as e:
                            app.logger.warning(f"Photo delete failed: {pid}: {e}")
            except Exception as e:
                app.logger.warning(f"photos_json parse failed: {e}")
        r.photos_json = None
        r.video_url = None
        r.video_public_id = None
    
    # Item images (if vendor)
    items = Item.query.filter_by(vendor_id=user_id).all()
    for it in items:
        if it.image_filename and 'cloudinary' in (it.image_filename or ''):
            try:
                cloudinary.uploader.destroy(it.image_filename)
                deleted['item_images'] += 1
            except Exception:
                pass
    
    db.session.commit()
    return deleted


def anonymize_user(user):
    """
    Strip PII from user + linked records. Keeps financial records
    for tax compliance (~7-8 years).
    NOTE: User model has NO aadhaar_number/pan_number fields —
    those live on Booking, handled below.
    """
    anon_id = f"DELETED_{user.id}"
    
    # User row — strip PII
    user.name = anon_id
    user.email = None
    user.mobile = f"000000{user.id:04d}"[:10]
    user.password_hash = 'DELETED'
    user.telegram_chat_id = None
    
    # Chat messages — delete entirely (communication, not financial)
    Message.query.filter(
        (Message.sender_id == user.id) | (Message.receiver_id == user.id)
    ).delete(synchronize_session=False)
    
    # Bookings as customer — keep financials, strip PII
    for b in Booking.query.filter_by(customer_id=user.id).all():
        b.venue_address = '[deleted]'
        b.aadhaar_number = None
        b.pan_number = None
    
    # Bookings as vendor — same
    for b in Booking.query.join(Item, Booking.item_id == Item.id).filter(
        Item.vendor_id == user.id
    ).all():
        b.venue_address = '[deleted]'
        b.aadhaar_number = None
        b.pan_number = None
    
    # Reviews — keep rating, blank comment
    for rv in Review.query.filter_by(customer_id=user.id).all():
        rv.comment = '[removed]'
    
    # Tickets — delete entirely (support convos)
    TicketReply.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    Ticket.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    
    # Items (vendor) — deactivate, keep for record
    for it in Item.query.filter_by(vendor_id=user.id).all():
        it.is_available = False
    
    # Consent logs & data requests — keep (audit trail)
    
    db.session.commit()


# ============================================
# UPI QR GENERATOR
# ============================================
def generate_upi_qr_base64(upi_id, name='VyahMandap', amount=None, box_size=8):
    if not upi_id:
        return None
    params = f"pa={quote(upi_id)}&pn={quote(name or 'VyahMandap')}"
    if amount:
        params += f"&am={amount}&cu=INR"
    upi_url = f"upi://pay?{params}"
    qr = qrcode.QRCode(version=1, box_size=box_size, border=2)
    qr.add_data(upi_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')
    buf = BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()


def get_client_info(request_obj):
    device = request_obj.headers.get('User-Agent', '')[:200]
    ip = request_obj.headers.get('X-Forwarded-For', request_obj.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    return device, ip


# ============================================
# TELEGRAM HELPERS
# ============================================
def send_telegram_notification(chat_id, message):
    token = Config.TELEGRAM_BOT_TOKEN
    if not chat_id or not token:
        return
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": message, "disable_web_page_preview": True}
    try:
        response = requests.post(api_url, json=payload, timeout=10)
        if response.status_code == 200:
            print(f"✅ Telegram sent to {chat_id}")
        else:
            print(f"❌ Telegram error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"❌ Telegram failed: {e}")


def send_telegram_notification_async(chat_id, message):
    if chat_id:
        thread = threading.Thread(target=send_telegram_notification, args=(chat_id, message))
        thread.daemon = True
        thread.start()


def notify_all_admins(message):
    admins = User.query.filter_by(role='admin').all()
    for admin_user in admins:
        if admin_user.telegram_chat_id:
            send_telegram_notification_async(admin_user.telegram_chat_id, message)


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


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        mobile = request.form.get('mobile', '').strip()
        email = request.form.get('email', '').strip()
        company_name = request.form.get('company_name', '').strip() or None
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        account_type = request.form.get('account_type', 'customer')
        
        errors = []
        if not name: errors.append('Name is required.')
        if not mobile: errors.append('Mobile number is required.')
        elif not validate_mobile(mobile): errors.append('Please enter a valid 10-digit mobile number.')
        if not email: errors.append('Email is required.')
        elif not re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email):
            errors.append('Please enter a valid email address.')
        elif User.query.filter_by(email=email).first():
            errors.append('A user with this email already exists.')
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
        if role == 'vendor' and company_name:
            user.company_name = company_name
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        # DPDP: log consent
        try:
            consent = ConsentLog(
                user_id=user.id,
                purpose='terms',
                granted=True,
                ip_address=request.headers.get('X-Forwarded-For', request.remote_addr),
                user_agent=request.headers.get('User-Agent', '')[:300]
            )
            db.session.add(consent)
            db.session.commit()
        except Exception as e:
            print(f"⚠️ ConsentLog create failed: {e}")
        
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
# TELEGRAM LINKING ROUTES
# ============================================
@app.route('/link-telegram', methods=['GET', 'POST'])
@login_required
def link_telegram():
    if request.method == 'POST':
        chat_id = request.form.get('chat_id', '').strip()
        if chat_id:
            if not re.match(r'^-?\d+$', chat_id):
                flash('⚠️ Chat ID must be a number.', 'danger')
                return render_template('link_telegram.html', current_chat_id=current_user.telegram_chat_id)
            current_user.telegram_chat_id = chat_id
            db.session.commit()
            test_msg = (
                f"✅ VyahMandap — Telegram Linked!\n\n"
                f"Hi {current_user.name},\n\n"
                f"You will now receive notifications here.\n\n"
                f"Taiyari Hamari, Celebration Aapka! 🎉"
            )
            send_telegram_notification_async(chat_id, test_msg)
            flash('✅ Telegram linked!', 'success')
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
# DPDP — MY DATA ROUTES
# ============================================
@app.route('/my-data')
@login_required
def my_data():
    user = current_user
    
    bookings_cust = Booking.query.filter_by(customer_id=user.id)\
        .order_by(Booking.created_at.desc()).all()
    
    bookings_vend = []
    items = []
    if user.role in ['vendor', 'admin']:
        items = Item.query.filter_by(vendor_id=user.id).all()
        item_ids = [i.id for i in items]
        if item_ids:
            bookings_vend = Booking.query.filter(Booking.item_id.in_(item_ids))\
                .order_by(Booking.created_at.desc()).all()
    
    msg_count = Message.query.filter(
        (Message.sender_id == user.id) | (Message.receiver_id == user.id)
    ).count()
    
    payments = PaymentProof.query.filter_by(uploaded_by=user.id)\
        .order_by(PaymentProof.created_at.desc()).all()
    
    reviews = Review.query.filter_by(customer_id=user.id)\
        .order_by(Review.created_at.desc()).all()
    
    tickets = Ticket.query.filter_by(user_id=user.id)\
        .order_by(Ticket.created_at.desc()).all()
    
    reports = EquipmentReport.query.filter_by(reporter_id=user.id)\
        .order_by(EquipmentReport.created_at.desc()).all()
    
    consents = ConsentLog.query.filter_by(user_id=user.id)\
        .order_by(ConsentLog.created_at.desc()).all()
    
    return render_template('my_data.html',
                         user=user,
                         bookings_cust=bookings_cust,
                         bookings_vend=bookings_vend,
                         items=items,
                         msg_count=msg_count,
                         payments=payments,
                         reviews=reviews,
                         tickets=tickets,
                         reports=reports,
                         consents=consents,
                         dpo_mobile=Config.DPO_MOBILE,
                         dpo_email=Config.DPO_EMAIL)


@app.route('/my-data/export')
@login_required
def my_data_export():
    user = current_user
    
    data = {
        'exported_at': datetime.utcnow().isoformat(),
        'export_format_version': '1.0',
        'profile': {
            'id': user.id,
            'name': user.name,
            'mobile': user.mobile,
            'email': user.email,
            'role': user.role,
            'joined_at': user.created_at.isoformat() if user.created_at else None,
            'is_verified': user.is_verified,
        },
        'bookings_as_customer': [{
            'reference': b.booking_reference,
            'item': b.item.title if b.item else None,
            'vendor': b.item.vendor.name if b.item and b.item.vendor else None,
            'start_date': b.start_date.isoformat() if b.start_date else None,
            'end_date': b.end_date.isoformat() if b.end_date else None,
            'quantity': b.quantity,
            'total_amount': b.total_amount,
            'status': b.booking_status,
            'payment_status': b.payment_status,
            'venue_address': b.venue_address,
            'created_at': b.created_at.isoformat() if b.created_at else None,
        } for b in Booking.query.filter_by(customer_id=user.id).all()],
        'bookings_as_vendor': [],
        'items_listed': [],
        'payment_proofs': [{
            'booking_ref': p.booking.booking_reference if p.booking else None,
            'amount_paid': p.amount_paid,
            'method': p.method,
            'transaction_id': p.transaction_id,
            'status': p.status,
            'created_at': p.created_at.isoformat() if p.created_at else None,
        } for p in PaymentProof.query.filter_by(uploaded_by=user.id).all()],
        'reviews': [{
            'booking_ref': r.booking_id,
            'rating': r.rating,
            'comment': r.comment,
            'created_at': r.created_at.isoformat() if r.created_at else None,
        } for r in Review.query.filter_by(customer_id=user.id).all()],
        'messages_count': Message.query.filter(
            (Message.sender_id == user.id) | (Message.receiver_id == user.id)
        ).count(),
        'support_tickets': [{
            'ticket_number': t.ticket_number,
            'subject': t.subject,
            'status': t.status,
            'priority': t.priority,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        } for t in Ticket.query.filter_by(user_id=user.id).all()],
        'equipment_reports': [{
            'booking_id': r.booking_id,
            'report_type': r.report_type,
            'condition_rating': r.condition_rating,
            'damage_flagged': r.damage_flagged,
            'created_at': r.created_at.isoformat() if r.created_at else None,
        } for r in EquipmentReport.query.filter_by(reporter_id=user.id).all()],
        'consents': [{
            'purpose': c.purpose,
            'granted': c.granted,
            'created_at': c.created_at.isoformat() if c.created_at else None,
        } for c in ConsentLog.query.filter_by(user_id=user.id).all()],
    }
    
    # Add vendor data
    if user.role in ['vendor', 'admin']:
        items = Item.query.filter_by(vendor_id=user.id).all()
        data['items_listed'] = [{
            'title': it.title,
            'category': it.category,
            'rate_per_day': it.rate_per_day,
            'stock': it.stock,
            'is_available': it.is_available,
            'is_verified': it.is_verified,
        } for it in items]
        
        item_ids = [i.id for i in items]
        if item_ids:
            vendor_bookings = Booking.query.filter(Booking.item_id.in_(item_ids)).all()
            data['bookings_as_vendor'] = [{
                'reference': b.booking_reference,
                'customer': b.customer.name if b.customer else None,
                'item': b.item.title if b.item else None,
                'start_date': b.start_date.isoformat() if b.start_date else None,
                'end_date': b.end_date.isoformat() if b.end_date else None,
                'total_amount': b.total_amount,
                'base_rent': b.base_rent,
                'status': b.booking_status,
            } for b in vendor_bookings]
    
    # Log the export
    try:
        dr = DataRequest(
            user_id=user.id,
            request_type='export',
            status='completed',
            completed_at=datetime.utcnow()
        )
        db.session.add(dr)
        db.session.commit()
    except Exception as e:
        print(f"⚠️ DataRequest export log failed: {e}")
    
    # Return as downloadable JSON
    response = app.response_class(
        response=json_lib.dumps(data, indent=2, default=str),
        status=200,
        mimetype='application/json'
    )
    filename = f"vyahmandap_data_{user.id}_{datetime.utcnow().strftime('%Y%m%d')}.json"
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@app.route('/my-data/delete-request', methods=['POST'])
@login_required
def my_data_delete_request():
    user = current_user
    
    # Block admin self-deletion
    if user.role == 'admin':
        flash('⚠️ Admin accounts cannot be self-deleted.', 'danger')
        return redirect(url_for('my_data'))
    
    # Already requested?
    if user.deletion_requested_at:
        flash('ℹ️ Deletion already requested.', 'info')
        return redirect(url_for('my_data'))
    
    # Check active bookings
    active_statuses = ['pending', 'confirmed', 'dispatched', 'return_initiated']
    active_count = Booking.query.filter(
        Booking.customer_id == user.id,
        Booking.booking_status.in_(active_statuses)
    ).count()
    
    if user.role == 'vendor':
        item_ids = [i.id for i in Item.query.filter_by(vendor_id=user.id).all()]
        if item_ids:
            active_count += Booking.query.filter(
                Booking.item_id.in_(item_ids),
                Booking.booking_status.in_(active_statuses)
            ).count()
    
    if active_count > 0:
        flash('⚠️ You have active bookings. Complete or cancel them first.', 'danger')
        return redirect(url_for('my_data'))
    
    now = datetime.utcnow()
    user.deletion_requested_at = now
    user.deletion_scheduled_at = now + timedelta(days=Config.DELETION_GRACE_DAYS)
    
    try:
        dr = DataRequest(
            user_id=user.id,
            request_type='deletion_request',
            status='pending',
            details=json_lib.dumps({'scheduled_at': user.deletion_scheduled_at.isoformat()})
        )
        db.session.add(dr)
        
        consent = ConsentLog(
            user_id=user.id,
            purpose='deletion',
            granted=True,
            ip_address=request.headers.get('X-Forwarded-For', request.remote_addr),
            user_agent=request.headers.get('User-Agent', '')[:300]
        )
        db.session.add(consent)
    except Exception as e:
        print(f"⚠️ Audit log failed: {e}")
    
    db.session.commit()
    
    notify_all_admins(
        f"🗑️ Deletion requested\n\n"
        f"User: {user.name} ({user.mobile})\n"
        f"Scheduled: {user.deletion_scheduled_at.strftime('%d %b %Y')}"
    )
    
    flash(f'⚠️ Deletion scheduled for {user.deletion_scheduled_at.strftime("%d %b %Y")}. '
          f'You have {Config.DELETION_GRACE_DAYS} days to cancel.', 'warning')
    logout_user()
    return redirect(url_for('login'))


@app.route('/my-data/cancel-deletion', methods=['POST'])
@login_required
def my_data_cancel_deletion():
    user = current_user
    
    if not user.deletion_requested_at:
        flash('ℹ️ No pending deletion to cancel.', 'info')
        return redirect(url_for('my_data'))
    
    if user.deletion_scheduled_at and user.deletion_scheduled_at <= datetime.utcnow():
        flash('⚠️ Deletion grace period has passed.', 'danger')
        return redirect(url_for('my_data'))
    
    user.deletion_requested_at = None
    user.deletion_scheduled_at = None
    
    try:
        dr = DataRequest(
            user_id=user.id,
            request_type='deletion_cancelled',
            status='completed',
            completed_at=datetime.utcnow()
        )
        db.session.add(dr)
    except Exception as e:
        print(f"⚠️ Audit log failed: {e}")
    
    db.session.commit()
    
    flash('✅ Deletion cancelled. Your account is fully restored.', 'success')
    return redirect(url_for('my_data'))


# ============================================
# MAIN ROUTES
# ============================================
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/items')
def get_items():
    category = request.args.get('category', 'all')
    show_unavailable = request.args.get('show_unavailable', 'false').lower() == 'true'
    search = request.args.get('search', '').strip()
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    sort = request.args.get('sort', 'newest')
    verified_only = request.args.get('verified_only', 'false').lower() == 'true'

    query = Item.query
    if not show_unavailable:
        query = query.filter_by(is_available=True)

    if category != 'all':
        query = query.filter_by(category=category)

    if search:
        query = query.filter(db.or_(
            Item.title.ilike(f'%{search}%'),
            Item.description.ilike(f'%{search}%')
        ))

    if min_price is not None:
        query = query.filter(Item.rate_per_day >= min_price)
    if max_price is not None:
        query = query.filter(Item.rate_per_day <= max_price)

    if verified_only:
        query = query.filter_by(is_verified=True)

    # Server-side sort for numeric fields
    if sort == 'price_asc':
        query = query.order_by(Item.rate_per_day.asc())
    elif sort == 'price_desc':
        query = query.order_by(Item.rate_per_day.desc())
    else:  # newest or rating (rating sorted after)
        query = query.order_by(Item.created_at.desc())

    items = query.all()

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
        next_available = get_next_available_date(item.id)
        result.append({
            'id': item.id, 'title': item.title, 'description': item.description,
            'category': item.category, 'rate': item.rate_per_day,
            'rate_with_commission': item.rate_with_commission,
            'deposit': item.deposit_amount, 'stock': item.stock,
            'image': item.get_image(), 'vendor': item.vendor.name,
            'vendor_id': item.vendor_id,
            'vendor_verified': item.vendor.is_verified,
            'item_verified': item.is_currently_verified,
            'avg_rating': avg_rating, 'review_count': review_count,
            'is_available': item.is_available,
            'next_available': next_available.strftime('%Y-%m-%d') if next_available else None
        })

    # Rating sort must run after computing avg_rating
    if sort == 'rating':
        result.sort(key=lambda x: x['avg_rating'], reverse=True)

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

    # Validate inputs
    today_date = ist_today()
    if start_date < today_date:
        return jsonify({'error': True, 'message': 'Start date cannot be in the past.', 'conflict_type': 'validation_error'}), 400
    if end_date < start_date:
        return jsonify({'error': True, 'message': 'End date must be on or after start date.', 'conflict_type': 'validation_error'}), 400
    if quantity <= 0:
        return jsonify({'error': True, 'message': 'Quantity must be at least 1.', 'conflict_type': 'validation_error'}), 400
    if (end_date - start_date).days > 365:
        return jsonify({'error': True, 'message': 'Booking period cannot exceed 365 days.', 'conflict_type': 'validation_error'}), 400
    if area not in ['city', 'outskirts']:
        return jsonify({'error': True, 'message': 'Invalid delivery area.', 'conflict_type': 'validation_error'}), 400
    if weight not in ['till_20', 'above_20']:
        return jsonify({'error': True, 'message': 'Invalid weight category.', 'conflict_type': 'validation_error'}), 400

    item = Item.query.get_or_404(item_id)
    has_conflict, conflict_booking, available_qty, conflict_type = check_availability_conflict(
        item.id, start_date, quantity
    )
    
    if has_conflict:
        next_available = None
        if conflict_type == 'return_day_full':
            next_available = start_date + timedelta(days=1)
            message = f'This item is returning on {start_date.strftime("%d %b %Y")}.'
        elif conflict_type == 'fully_booked':
            latest_end = Booking.query.filter(
                Booking.item_id == item.id,
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']),
                Booking.start_date <= start_date, Booking.end_date >= start_date
            ).order_by(Booking.end_date.desc()).first()
            if latest_end:
                next_available = latest_end.end_date + timedelta(days=1)
            message = f'Item is fully booked on {start_date.strftime("%d %b %Y")}.'
        elif conflict_type == 'return_day_partial':
            next_available = start_date + timedelta(days=1)
            message = f'Only {available_qty} unit(s) available.'
        else:
            latest_end = Booking.query.filter(
                Booking.item_id == item.id,
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']),
                Booking.start_date <= start_date, Booking.end_date >= start_date
            ).order_by(Booking.end_date.desc()).first()
            if latest_end:
                next_available = latest_end.end_date + timedelta(days=1)
            message = f'Only {available_qty} unit(s) available.'
        
        return jsonify({
            'error': True, 'message': message, 'conflict_type': conflict_type,
            'available_qty': available_qty, 'requested_qty': quantity,
            'next_available': next_available.strftime('%Y-%m-%d') if next_available else None
        }), 400
    
    result = calculate_booking_total(item, start_date, end_date, quantity, area, weight)
    return jsonify(result)


@app.route('/api/item/<int:item_id>/availability-calendar')
def item_availability_calendar(item_id):
    item = Item.query.get_or_404(item_id)
    bookings = Booking.query.filter_by(item_id=item_id)\
        .filter(Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated'])).all()
    if not bookings:
        return jsonify([])
    
    date_booked = {}
    end_dates = set()
    for b in bookings:
        end_dates.add(b.end_date)
        current = b.start_date
        while current <= b.end_date:
            date_booked[current] = date_booked.get(current, 0) + b.quantity
            current += timedelta(days=1)
    
    events = []
    for date, booked_qty in date_booked.items():
        available_qty = item.stock - booked_qty
        if available_qty <= 0:
            status, color = 'booked', '#dc2626'
        elif available_qty < item.stock:
            status, color = 'partial', '#eab308'
        else:
            status, color = 'available', '#16a34a'
        events.append({
            'title': f"{available_qty}/{item.stock}",
            'start': date.isoformat(), 'allDay': True,
            'backgroundColor': color, 'borderColor': color,
            'extendedProps': {
                'status': status, 'total_stock': item.stock,
                'booked_qty': booked_qty, 'available_qty': max(0, available_qty),
                'is_end_date': date in end_dates,
                'note': 'Return day' if date in end_dates else ''
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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']),
        Booking.start_date <= check_date, Booking.end_date >= check_date
    ).all()
    booked_qty = sum(b.quantity for b in overlapping)
    available_qty = max(0, item.stock - booked_qty)
    status = 'booked' if available_qty <= 0 else ('partial' if available_qty < item.stock else 'available')
    is_end_date = any(b.end_date == check_date for b in overlapping)
    
    return jsonify({
        'date': date_str, 'status': status, 'total_stock': item.stock,
        'booked_qty': booked_qty, 'available_qty': available_qty, 'is_end_date': is_end_date
    })


@app.route('/book/<int:item_id>', methods=['GET', 'POST'])
@login_required
def book_item(item_id):
    item = Item.query.get_or_404(item_id)
    if not item.is_available:
        flash('⚠️ This item is currently unavailable for booking.', 'warning')
        return redirect(url_for('index'))
    if item.stock <= 0:
        flash('This item is out of stock.', 'danger')
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        try:
            start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
            end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()

            # Validate dates
            today_date = ist_today()
            if start_date < today_date:
                flash('⚠️ Start date cannot be in the past.', 'danger')
                return render_template('booking.html', item=item)
            if end_date < start_date:
                flash('⚠️ End date must be on or after start date.', 'danger')
                return render_template('booking.html', item=item)
            if (end_date - start_date).days > 365:
                flash('⚠️ Booking period cannot exceed 365 days.', 'danger')
                return render_template('booking.html', item=item)

            quantity = int(request.form.get('quantity', 1))
            if quantity <= 0:
                flash('⚠️ Quantity must be at least 1.', 'danger')
                return render_template('booking.html', item=item)
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
            
            has_conflict, conflict_booking, available_qty, conflict_type = check_availability_conflict(
                item.id, start_date, quantity
            )
            if has_conflict:
                flash(f'⚠️ Only {available_qty} unit(s) available on {start_date.strftime("%d %b %Y")}.', 'warning')
                return render_template('booking.html', item=item)
            
            calc = calculate_booking_total(item, start_date, end_date, quantity, area, weight)
            kyc_required = calc['total'] >= 30000
            aadhaar = None
            pan = None
            if kyc_required:
                aadhaar = request.form.get('aadhaar', '').strip()
                pan = request.form.get('pan', '').strip().upper()
                if not aadhaar or not re.match(r'^\d{12}$', aadhaar):
                    flash('⚠️ Valid 12-digit Aadhaar required.', 'danger')
                    return render_template('booking.html', item=item)
                if not pan or not re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
                    flash('⚠️ Valid PAN required.', 'danger')
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
            
            if current_user.telegram_chat_id:
                send_telegram_notification_async(current_user.telegram_chat_id,
                    f"🎉 Booking Confirmed!\n\nItem: {item.title}\nRef: {booking.booking_reference}")
            if item.vendor.telegram_chat_id:
                send_telegram_notification_async(item.vendor.telegram_chat_id,
                    f"📦 New Booking!\n\nRef: {booking.booking_reference}\nFrom: {current_user.name}")
            
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
    active_bookings = sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched'])
    pending_return = [b for b in bookings 
                     if b.dispatch_report_done and not b.return_report_done 
                     and b.booking_status in ['dispatched', 'confirmed']]
    
    return render_template('dashboard.html', 
                         bookings=bookings, total_spent=total_spent, 
                         active_bookings=active_bookings, pending_return=pending_return)


@app.route('/my-booking/<int:booking_id>')
@login_required
def my_booking_detail(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.customer_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    payment_config = AdminPaymentConfig.get()
    qr_base64 = None
    if payment_config.upi_id:
        qr_base64 = generate_upi_qr_base64(
            payment_config.upi_id, payment_config.upi_name, amount=None
        )
    
    proofs = PaymentProof.query.filter_by(booking_id=booking_id)\
        .order_by(PaymentProof.created_at.desc()).all()
    
    return render_template('my_booking.html',
                         booking=booking, payment_config=payment_config,
                         qr_base64=qr_base64, proofs=proofs)


# ============================================
# EQUIPMENT CONDITION REPORT ROUTES
# ============================================
@app.route('/booking/<int:booking_id>/report-dispatch', methods=['GET', 'POST'])
@login_required
def report_dispatch(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    if booking.dispatch_report_done:
        flash('Dispatch report already submitted.', 'info')
        return redirect(url_for('vendor_dashboard'))
    return render_template('reports/dispatch.html', booking=booking)


@app.route('/booking/<int:booking_id>/report-return', methods=['GET', 'POST'])
@login_required
def report_return(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.customer_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    if not booking.dispatch_report_done:
        flash('Vendor has not yet submitted dispatch report.', 'warning')
        return redirect(url_for('dashboard'))
    if booking.return_report_done:
        flash('Return report already submitted.', 'info')
        return redirect(url_for('dashboard'))
    return render_template('reports/return.html', booking=booking)


@app.route('/api/report/upload-video', methods=['POST'])
@login_required
def api_report_upload_video():
    if 'video' not in request.files:
        return jsonify({'error': 'No video file'}), 400
    video = request.files['video']
    if not video.filename:
        return jsonify({'error': 'Empty filename'}), 400
    booking_id = request.form.get('booking_id')
    report_type = request.form.get('report_type')
    if not booking_id or not report_type:
        return jsonify({'error': 'Missing fields'}), 400
    booking = Booking.query.get_or_404(booking_id)
    if report_type == 'dispatch':
        if booking.item.vendor_id != current_user.id and current_user.role != 'admin':
            return jsonify({'error': 'Access denied'}), 403
    elif report_type == 'return':
        if booking.customer_id != current_user.id:
            return jsonify({'error': 'Access denied'}), 403
    else:
        return jsonify({'error': 'Invalid report type'}), 400
    if not Config.CLOUDINARY_CLOUD_NAME:
        return jsonify({'error': 'Cloudinary not configured'}), 500
    folder = f'vyahmandap/reports/videos/booking_{booking_id}_{report_type}'
    url, public_id = upload_video_to_cloudinary(video, folder=folder)
    if not url:
        return jsonify({'error': 'Video upload failed'}), 500
    return jsonify({'success': True, 'video_url': url, 'video_public_id': public_id})


@app.route('/api/report/submit', methods=['POST'])
@login_required
def api_report_submit():
    data = request.get_json()
    booking_id = data.get('booking_id')
    report_type = data.get('report_type')
    photos = data.get('photos', [])
    video_url = data.get('video_url')
    video_public_id = data.get('video_public_id')
    condition_rating = data.get('condition_rating', 'good')
    notes = data.get('notes', '').strip()
    damage_flagged = data.get('damage_flagged', False)
    damage_notes = data.get('damage_notes', '').strip()
    gps_lat = data.get('gps_lat', '')
    gps_lng = data.get('gps_lng', '')
    
    booking = Booking.query.get_or_404(booking_id)
    if report_type == 'dispatch':
        if booking.item.vendor_id != current_user.id and current_user.role != 'admin':
            return jsonify({'error': 'Access denied'}), 403
        if booking.dispatch_report_done:
            return jsonify({'error': 'Already submitted'}), 400
    elif report_type == 'return':
        if booking.customer_id != current_user.id:
            return jsonify({'error': 'Access denied'}), 403
        if booking.return_report_done:
            return jsonify({'error': 'Already submitted'}), 400
        if not booking.dispatch_report_done:
            return jsonify({'error': 'Dispatch report pending'}), 400
    else:
        return jsonify({'error': 'Invalid report type'}), 400
    
    if len(photos) < 1:
        return jsonify({'error': 'At least 1 photo required'}), 400
    
    uploaded = []
    for idx, photo in enumerate(photos):
        url = upload_base64_to_cloudinary(
            photo.get('base64', ''),
            folder=f'vyahmandap/reports/booking_{booking_id}_{report_type}'
        )
        if url:
            uploaded.append({
                'url': url,
                'caption': photo.get('caption', f'Photo {idx+1}'),
                'item_title': photo.get('item_title', '')
            })
    
    if not uploaded:
        return jsonify({'error': 'Photo upload failed.'}), 500
    
    device, ip = get_client_info(request)
    report = EquipmentReport(
        booking_id=booking.id, reporter_id=current_user.id,
        report_type=report_type, photos_json=json_lib.dumps(uploaded),
        video_url=video_url, video_public_id=video_public_id,
        condition_rating=condition_rating, damage_flagged=damage_flagged,
        damage_notes=damage_notes if damage_flagged else None,
        notes=notes, gps_latitude=gps_lat, gps_longitude=gps_lng,
        device_info=device, ip_address=ip
    )
    db.session.add(report)
    
    if report_type == 'dispatch':
        booking.dispatch_report_done = True
        booking.booking_status = 'dispatched'
    else:
        booking.return_report_done = True
        booking.booking_status = 'return_initiated'
        if damage_flagged:
            booking.damage_flagged = True
    
    db.session.commit()
    
    if damage_flagged:
        ticket = Ticket(
            ticket_number=generate_ticket_number(), user_id=current_user.id,
            subject=f"⚠️ Damage Reported — {booking.booking_reference}",
            description=f"Damage on {report_type} report.\n\nBooking: {booking.booking_reference}\nItem: {booking.item.title}",
            category='item', priority='high', status='open'
        )
        db.session.add(ticket)
        db.session.commit()
        notify_all_admins(f"🚨 DAMAGE!\n\nBooking: {booking.booking_reference}\nItem: {booking.item.title}\nTicket: {ticket.ticket_number}")
    
    return jsonify({
        'success': True, 'report_id': report.id,
        'photos_uploaded': len(uploaded), 'video_uploaded': bool(video_url),
        'damage_flagged': damage_flagged, 'message': 'Report submitted!'
    })


@app.route('/booking/<int:booking_id>/report-view')
@login_required
def report_view(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    can_view = (current_user.role == 'admin' or 
                booking.customer_id == current_user.id or 
                booking.item.vendor_id == current_user.id)
    if not can_view:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    dispatch_report = EquipmentReport.query.filter_by(booking_id=booking_id, report_type='dispatch').first()
    return_report = EquipmentReport.query.filter_by(booking_id=booking_id, report_type='return').first()
    dispatch_photos = json_lib.loads(dispatch_report.photos_json) if dispatch_report and dispatch_report.photos_json else []
    return_photos = json_lib.loads(return_report.photos_json) if return_report and return_report.photos_json else []
    
    return render_template('reports/view.html',
                         booking=booking, dispatch_report=dispatch_report,
                         return_report=return_report,
                         dispatch_photos=dispatch_photos, return_photos=return_photos)


# ============================================
# PAYMENT PROOF ROUTES
# ============================================
@app.route('/admin/payments')
@login_required
def admin_payments():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    total_utr_count = len(bookings)
    total_collected = sum(b.total_amount for b in bookings)
    total_commission = sum(b.commission for b in bookings)
    total_deposits = sum(b.deposit for b in bookings)
    total_transport = sum(b.transport_fee for b in bookings)
    
    return render_template('admin/payments.html',
                         bookings=bookings,
                         total_utr_count=total_utr_count,
                         total_collected=total_collected,
                         total_commission=total_commission,
                         total_deposits=total_deposits,
                         total_transport=total_transport)


@app.route('/admin/payment-config', methods=['GET', 'POST'])
@login_required
def admin_payment_config():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    cfg = AdminPaymentConfig.get()
    
    if request.method == 'POST':
        cfg.upi_id         = request.form.get('upi_id', '').strip() or None
        cfg.upi_name       = request.form.get('upi_name', '').strip() or 'VyahMandap'
        cfg.account_name   = request.form.get('account_name', '').strip() or None
        cfg.account_number = request.form.get('account_number', '').strip() or None
        cfg.ifsc_code      = request.form.get('ifsc_code', '').strip().upper() or None
        cfg.bank_name      = request.form.get('bank_name', '').strip() or None
        cfg.branch         = request.form.get('branch', '').strip() or None
        
        if 'custom_qr' in request.files and request.files['custom_qr'].filename:
            qr_file = request.files['custom_qr']
            upload_result = upload_file_to_cloudinary(qr_file, folder='vyahmandap/payments/qr')
            if upload_result and upload_result.get('url'):
                cfg.custom_qr_url = upload_result['url']
        
        db.session.commit()
        flash('✅ Payment configuration saved.', 'success')
        return redirect(url_for('admin_payment_config'))
    
    qr_base64 = None
    if cfg.upi_id:
        qr_base64 = generate_upi_qr_base64(cfg.upi_id, cfg.upi_name)
    
    return render_template('admin/payment_config.html', cfg=cfg, qr_base64=qr_base64)


@app.route('/admin/payment-proofs')
@login_required
def admin_payment_queue():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    status_filter = request.args.get('status', 'pending')
    query = PaymentProof.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    proofs = query.order_by(PaymentProof.created_at.desc()).all()
    
    pending_count = PaymentProof.query.filter_by(status='pending').count()
    verified_count = PaymentProof.query.filter_by(status='verified').count()
    rejected_count = PaymentProof.query.filter_by(status='rejected').count()
    
    return render_template('admin/payment_queue.html',
                         proofs=proofs, status_filter=status_filter,
                         pending_count=pending_count,
                         verified_count=verified_count,
                         rejected_count=rejected_count)


@app.route('/admin/payment-proof/<int:proof_id>/verify', methods=['POST'])
@login_required
def admin_verify_payment_proof(proof_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    proof = PaymentProof.query.get_or_404(proof_id)
    if proof.status != 'pending':
        flash('⚠️ Already reviewed.', 'info')
        return redirect(request.referrer or url_for('admin_payment_queue'))
    
    proof.status = 'verified'
    proof.verified_by = current_user.id
    proof.verified_at = datetime.utcnow()
    
    booking = proof.booking
    total_verified = sum(p.amount_paid for p in booking.payment_proofs 
                         if p.status == 'verified' and p.id != proof.id)
    total_verified += proof.amount_paid
    booking.payment_status = 'paid' if total_verified >= booking.total_amount else 'partial'
    
    db.session.commit()
    
    if booking.customer.telegram_chat_id:
        send_telegram_notification_async(booking.customer.telegram_chat_id,
            f"✅ Payment Verified!\n\nBooking: {booking.booking_reference}\n"
            f"Amount: ₹{proof.amount_paid:,.2f}\nMethod: {proof.method.upper()}")
    
    flash('✅ Payment proof verified.', 'success')
    return redirect(request.referrer or url_for('admin_payment_queue'))


@app.route('/admin/payment-proof/<int:proof_id>/reject', methods=['POST'])
@login_required
def admin_reject_payment_proof(proof_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    
    proof = PaymentProof.query.get_or_404(proof_id)
    if proof.status != 'pending':
        flash('⚠️ Already reviewed.', 'info')
        return redirect(request.referrer or url_for('admin_payment_queue'))
    
    reason = request.form.get('rejection_reason', '').strip()
    if not reason:
        flash('⚠️ Rejection reason is required.', 'danger')
        return redirect(request.referrer or url_for('admin_payment_queue'))
    
    proof.status = 'rejected'
    proof.rejection_reason = reason
    proof.verified_by = current_user.id
    proof.verified_at = datetime.utcnow()
    db.session.commit()
    
    if proof.booking.customer.telegram_chat_id:
        send_telegram_notification_async(proof.booking.customer.telegram_chat_id,
            f"❌ Payment Proof Rejected\n\nBooking: {proof.booking.booking_reference}\n"
            f"Reason: {reason}")
    
    flash('Payment proof rejected.', 'info')
    return redirect(request.referrer or url_for('admin_payment_queue'))


@app.route('/booking/<int:booking_id>/payment-proof', methods=['POST'])
@login_required
def submit_payment_proof(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.customer_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    if 'screenshot' not in request.files or not request.files['screenshot'].filename:
        flash('⚠️ Screenshot is required.', 'danger')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))
    
    screenshot = request.files['screenshot']
    if not Config.CLOUDINARY_CLOUD_NAME:
        flash('⚠️ Cloudinary not configured.', 'danger')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))
    
    folder = f"vyahmandap/payments/booking_{booking_id}"
    upload_result = upload_file_to_cloudinary(screenshot, folder=folder)
    
    if not upload_result or not upload_result.get('url'):
        flash('⚠️ Upload failed.', 'danger')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))
    
    payment_date = None
    pd_str = request.form.get('payment_date', '').strip()
    if pd_str:
        try:
            payment_date = datetime.strptime(pd_str, '%Y-%m-%d').date()
        except:
            payment_date = None
    
    try:
        amount_paid = float(request.form.get('amount_paid', 0) or 0)
    except:
        amount_paid = 0
    
    proof = PaymentProof(
        booking_id=booking.id, uploaded_by=current_user.id,
        method=request.form.get('method', 'upi').strip() or 'upi',
        amount_paid=amount_paid,
        transaction_id=request.form.get('transaction_id', '').strip() or None,
        payment_date=payment_date,
        payer_name=request.form.get('payer_name', '').strip() or None,
        payer_bank=request.form.get('payer_bank', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None,
        screenshot_url=upload_result['url'],
        screenshot_public_id=upload_result.get('public_id'),
        status='pending'
    )
    db.session.add(proof)
    db.session.commit()
    
    notify_all_admins(
        f"💰 New Payment Proof\n\nBooking: {booking.booking_reference}\n"
        f"Amount: ₹{proof.amount_paid:,.2f}\nMethod: {proof.method.upper()}"
    )
    
    flash('✅ Payment proof submitted! Admin will verify shortly.', 'success')
    return redirect(url_for('my_booking_detail', booking_id=booking.id))


# ============================================
# VENDOR DASHBOARDS
# ============================================
@app.route('/vendor/sales')
@login_required
def vendor_sales():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    items = Item.query.filter_by(vendor_id=current_user.id).order_by(Item.created_at.desc()).all()
    item_ids = [i.id for i in items]
    bookings = Booking.query.filter(Booking.item_id.in_(item_ids)).order_by(Booking.created_at.desc()).all() if item_ids else []
    total_earnings = sum(b.base_rent for b in bookings)
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_bookings=len(bookings),
                         active_rentals=sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched']),
                         total_items=len(items),
                         upcoming_bookings=[b for b in bookings if b.start_date >= ist_today() and b.start_date <= ist_today() + timedelta(days=30)],
                         pending_dispatch=[b for b in bookings if not b.dispatch_report_done and b.booking_status == 'confirmed'])


@app.route('/vendor/rentals')
@login_required
def vendor_rentals():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    bookings = Booking.query.filter_by(customer_id=current_user.id).order_by(Booking.created_at.desc()).all()
    total_spent = sum(b.total_amount for b in bookings)
    return render_template('vendor/rentals.html',
                         bookings=bookings, total_spent=total_spent,
                         active_rentals=sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched']),
                         total_rentals=len(bookings),
                         pending_return=[b for b in bookings if b.dispatch_report_done and not b.return_report_done and b.booking_status in ['dispatched', 'confirmed']])


@app.route('/vendor')
@login_required
def vendor_dashboard():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    items = Item.query.filter_by(vendor_id=current_user.id).order_by(Item.created_at.desc()).all()
    item_ids = [i.id for i in items]
    bookings = Booking.query.filter(Booking.item_id.in_(item_ids)).order_by(Booking.created_at.desc()).all() if item_ids else []
    total_earnings = sum(b.base_rent for b in bookings)
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_bookings=len(bookings),
                         active_rentals=sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched']),
                         total_items=len(items),
                         upcoming_bookings=[b for b in bookings if b.start_date >= ist_today() and b.start_date <= ist_today() + timedelta(days=30)],
                         pending_dispatch=[b for b in bookings if not b.dispatch_report_done and b.booking_status == 'confirmed'])


@app.route('/vendor/calendar')
@login_required
def vendor_calendar():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
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
    item_color_map = {item.id: colors[idx % len(colors)] for idx, item in enumerate(items)}
    events = []
    for b in bookings:
        events.append({
            'id': b.id, 'title': f"{b.item.title} ({b.quantity})",
            'start': b.start_date.isoformat(),
            'end': (b.end_date + timedelta(days=1)).isoformat(),
            'backgroundColor': item_color_map.get(b.item_id, '#2d5a3d'),
            'extendedProps': {'customer': b.customer.name, 'reference': b.booking_reference}
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
        flash('✅ Item updated!', 'success')
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
    flash('Item deleted.', 'success')
    return redirect(url_for('vendor_dashboard'))


@app.route('/vendor/item/<int:item_id>/toggle-availability', methods=['POST'])
@login_required
def vendor_toggle_availability(item_id):
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    item.is_available = not item.is_available
    db.session.commit()
    flash(f'✅ "{item.title}" is now {"available" if item.is_available else "unavailable"}.', 'success')
    return redirect(request.referrer or url_for('vendor_dashboard'))


# ============================================
# VERIFICATION
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
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_dashboard'))
    if item.is_currently_verified:
        flash('Already verified.', 'info')
        return redirect(url_for('vendor_dashboard'))
    if request.method == 'POST':
        utr = request.form.get('utr', '').strip()
        if not utr:
            flash('UTR required.', 'danger')
            return render_template('vendor/verify_item.html', item=item, price=VERIFICATION_PRICE)
        item.is_verified = True
        if item.verified_until and item.verified_until > datetime.utcnow():
            item.verified_until += timedelta(days=VERIFICATION_DAYS)
        else:
            item.verified_until = datetime.utcnow() + timedelta(days=VERIFICATION_DAYS)
        db.session.commit()
        flash(f'✅ Verified until {item.verified_until.strftime("%d %b, %Y")}.', 'success')
        return redirect(url_for('vendor_dashboard'))
    return render_template('vendor/verify_item.html', item=item, price=VERIFICATION_PRICE)


# ============================================
# REVIEWS
# ============================================
@app.route('/item/<int:item_id>/reviews')
def item_reviews(item_id):
    item = Item.query.get_or_404(item_id)
    reviews = Review.query.filter_by(item_id=item_id).order_by(Review.created_at.desc()).all()
    avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else 0
    return render_template('item_reviews.html', item=item, reviews=reviews, avg_rating=avg_rating, now=datetime.utcnow())


@app.route('/review/<int:review_id>/respond', methods=['POST'])
@login_required
def vendor_respond_to_review(review_id):
    review = Review.query.get_or_404(review_id)
    item = Item.query.get(review.item_id)
    if not item:
        flash('Item not found.', 'danger')
        return redirect(url_for('dashboard'))

    if item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    response_text = request.form.get('response', '').strip()
    if not response_text:
        flash('Response cannot be empty.', 'danger')
        return redirect(url_for('item_reviews', item_id=item.id))

    if len(response_text) > 1000:
        flash('Response too long (max 1000 chars).', 'danger')
        return redirect(url_for('item_reviews', item_id=item.id))

    # If already responded, enforce 7-day edit window
    if review.vendor_response and review.vendor_responded_at:
        if (datetime.utcnow() - review.vendor_responded_at).days > 7:
            flash('⚠️ Edit window (7 days) has passed.', 'warning')
            return redirect(url_for('item_reviews', item_id=item.id))

    review.vendor_response = response_text
    review.vendor_responded_at = datetime.utcnow()
    db.session.commit()

    # Notify the customer
    if review.customer and review.customer.telegram_chat_id:
        preview = response_text[:150] + ('...' if len(response_text) > 150 else '')
        send_telegram_notification_async(
            review.customer.telegram_chat_id,
            f"💬 Vendor responded to your review\n\n"
            f"Item: {item.title}\n"
            f"Response: {preview}"
        )

    flash('✅ Response posted.', 'success')
    return redirect(url_for('item_reviews', item_id=item.id))


@app.route('/booking/<int:booking_id>/review', methods=['GET', 'POST'])
@login_required
def submit_review(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if booking.customer_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    if booking.booking_status not in ['completed', 'confirmed']:
        flash('Only completed bookings can be reviewed.', 'warning')
        return redirect(url_for('dashboard'))
    if Review.query.filter_by(booking_id=booking_id).first():
        flash('Already reviewed.', 'info')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        rating = int(request.form.get('rating', 0))
        comment = request.form.get('comment', '').strip()
        if rating < 1 or rating > 5:
            flash('Rating must be 1-5.', 'danger')
            return render_template('review_form.html', booking=booking)
        review = Review(booking_id=booking.id, item_id=booking.item_id,
                       customer_id=current_user.id, rating=rating, comment=comment)
        db.session.add(review)
        db.session.commit()
        flash('⭐ Thank you for your review!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('review_form.html', booking=booking)


# ============================================
# CHAT
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
                conversations[other_id] = {'user': other_user, 'last_message': msg, 'unread': 0}
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

    if not can_chat(current_user, other_user):
        flash('⚠️ You can only chat between customers and vendors.', 'warning')
        return redirect(url_for('chat_list'))

    item_id = request.args.get('item_id', type=int)
    
    if request.method == 'POST':
        body = request.form.get('body', '').strip()
        apply_filter = should_filter_contact_info(current_user, other_user)
        if not body:
            flash('Message cannot be empty.', 'danger')
        elif len(body) > MAX_CHAT_CHARS:
            flash(f'⚠️ Message too long! Max {MAX_CHAT_CHARS} chars.', 'danger')
        elif apply_filter and contains_email(body):
            found = get_first_email(body)
            flash(f'⚠️ Sharing email/UPI not allowed (found: {found}).', 'danger')
        elif apply_filter and contains_too_many_digits(body, max_consecutive=4):
            flash(f'⚠️ Cannot share >4 consecutive digits.', 'danger')
        else:
            masked_body = mask_phone_numbers(body)
            msg = Message(sender_id=current_user.id, receiver_id=other_user.id,
                         item_id=item_id, body=masked_body)
            db.session.add(msg)
            db.session.commit()
            if other_user.telegram_chat_id:
                preview = masked_body[:100] + ('...' if len(masked_body) > 100 else '')
                send_telegram_notification_async(other_user.telegram_chat_id,
                    f"💬 New Message from {current_user.name}\n\n{preview}")
            if masked_body != body:
                flash('ℹ️ Some numbers masked for privacy.', 'info')
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
    return render_template('chat/conversation.html', other_user=other_user, messages=messages, item=item)


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

    if not can_chat(current_user, other_user):
        return jsonify({'error': 'Not allowed to chat with this user.'}), 403

    apply_filter = should_filter_contact_info(current_user, other_user)
    if len(body) > MAX_CHAT_CHARS:
        return jsonify({'error': f'Message too long. Max {MAX_CHAT_CHARS}.'}), 400
    if apply_filter and contains_email(body):
        found = get_first_email(body)
        return jsonify({'error': f'Email/UPI not allowed (found: {found}).'}), 400
    if apply_filter and contains_too_many_digits(body, max_consecutive=4):
        return jsonify({'error': 'Cannot share >4 consecutive digits.'}), 400
    masked_body = mask_phone_numbers(body)
    msg = Message(sender_id=current_user.id, receiver_id=other_user.id, item_id=item_id, body=masked_body)
    db.session.add(msg)
    db.session.commit()
    if other_user.telegram_chat_id:
        preview = masked_body[:100] + ('...' if len(masked_body) > 100 else '')
        send_telegram_notification_async(other_user.telegram_chat_id,
            f"💬 New Message from {current_user.name}\n\n{preview}")
    return jsonify({'success': True, 'message': {
        'id': msg.id, 'body': msg.body, 'masked': masked_body != body,
        'created_at': msg.created_at.strftime('%H:%M')
    }})


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
# TICKETS
# ============================================
@app.route('/tickets')
@login_required
def tickets_list():
    tickets = Ticket.query.filter_by(user_id=current_user.id).order_by(Ticket.last_reply_at.desc()).all()
    return render_template('tickets/list.html', tickets=tickets)


@app.route('/tickets/new', methods=['GET', 'POST'])
@login_required
def ticket_new():
    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        description = request.form.get('description', '').strip()
        category = request.form.get('category', 'other')
        priority = request.form.get('priority', 'medium')
        if not subject or not description:
            flash('Subject and description required.', 'danger')
            return render_template('tickets/new.html')
        ticket = Ticket(ticket_number=generate_ticket_number(), user_id=current_user.id,
                       subject=subject, description=description, category=category,
                       priority=priority, status='open')
        db.session.add(ticket)
        db.session.commit()
        notify_all_admins(f"🎫 New Ticket!\n\nFrom: {current_user.name}\n#{ticket.ticket_number}")
        flash(f'✅ Ticket {ticket.ticket_number} created!', 'success')
        return redirect(url_for('ticket_detail', ticket_id=ticket.id))
    return render_template('tickets/new.html')


@app.route('/tickets/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def ticket_detail(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    if ticket.user_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('tickets_list'))
    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        if not message:
            flash('Reply cannot be empty.', 'danger')
        else:
            reply = TicketReply(ticket_id=ticket.id, user_id=current_user.id,
                              message=message, is_admin_reply=(current_user.role == 'admin'))
            db.session.add(reply)
            ticket.last_reply_at = datetime.utcnow()
            if current_user.role == 'admin' and ticket.status == 'open':
                ticket.status = 'in_progress'
            db.session.commit()
            flash('✅ Reply posted.', 'success')
            return redirect(url_for('ticket_detail', ticket_id=ticket.id))
    replies = TicketReply.query.filter_by(ticket_id=ticket.id).order_by(TicketReply.created_at.asc()).all()
    return render_template('tickets/detail.html', ticket=ticket, replies=replies)


@app.route('/tickets/<int:ticket_id>/close', methods=['POST'])
@login_required
def ticket_close(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    if ticket.user_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('tickets_list'))
    ticket.status = 'closed'
    db.session.commit()
    flash('Ticket closed.', 'info')
    return redirect(url_for('admin_tickets') if current_user.role == 'admin' else url_for('tickets_list'))


@app.route('/admin/tickets')
@login_required
def admin_tickets():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    status_filter = request.args.get('status', 'all')
    priority_filter = request.args.get('priority', 'all')
    search = request.args.get('search', '').strip()
    query = Ticket.query
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    if priority_filter != 'all':
        query = query.filter_by(priority=priority_filter)
    if search:
        query = query.join(User, Ticket.user_id == User.id).filter(db.or_(
            Ticket.ticket_number.ilike(f'%{search}%'),
            Ticket.subject.ilike(f'%{search}%'),
            User.name.ilike(f'%{search}%')
        ))
    tickets = query.order_by(Ticket.last_reply_at.desc()).all()
    stats = {
        'total': Ticket.query.count(),
        'open': Ticket.query.filter_by(status='open').count(),
        'in_progress': Ticket.query.filter_by(status='in_progress').count(),
        'resolved': Ticket.query.filter_by(status='resolved').count(),
        'closed': Ticket.query.filter_by(status='closed').count(),
    }
    return render_template('admin/tickets.html', tickets=tickets, stats=stats,
                         status_filter=status_filter, priority_filter=priority_filter, search=search)


@app.route('/admin/ticket/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def admin_ticket_detail(ticket_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    ticket = Ticket.query.get_or_404(ticket_id)
    if request.method == 'POST':
        action = request.form.get('action', 'reply')
        if action == 'status':
            new_status = request.form.get('status', '')
            if new_status in ['open', 'in_progress', 'resolved', 'closed']:
                ticket.status = new_status
                db.session.commit()
                flash('Status updated.', 'success')
            return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
        elif action == 'priority':
            new_priority = request.form.get('priority', '')
            if new_priority in ['low', 'medium', 'high', 'urgent']:
                ticket.priority = new_priority
                db.session.commit()
                flash('Priority updated.', 'success')
            return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
        else:
            message = request.form.get('message', '').strip()
            if not message:
                flash('Reply cannot be empty.', 'danger')
            else:
                reply = TicketReply(ticket_id=ticket.id, user_id=current_user.id,
                                  message=message, is_admin_reply=True)
                db.session.add(reply)
                ticket.last_reply_at = datetime.utcnow()
                if ticket.status == 'open':
                    ticket.status = 'in_progress'
                db.session.commit()
                flash('✅ Reply sent.', 'success')
                return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
    replies = TicketReply.query.filter_by(ticket_id=ticket.id).order_by(TicketReply.created_at.asc()).all()
    return render_template('admin/ticket_detail.html', ticket=ticket, replies=replies)


# ============================================
# LEADERBOARD
# ============================================
def calculate_leaderboard(period='all_time', category='all'):
    cutoff_date = datetime.utcnow() - timedelta(days=30) if period == 'monthly' else None
    vendors = User.query.filter_by(role='vendor').all()
    rankings = []
    for vendor in vendors:
        vendor_items = Item.query.filter_by(vendor_id=vendor.id).all()
        if not vendor_items:
            continue
        if category != 'all':
            vendor_items = [i for i in vendor_items if i.category == category]
            if not vendor_items:
                continue
        vendor_item_ids = [i.id for i in vendor_items]
        booking_query = Booking.query.filter(
            Booking.item_id.in_(vendor_item_ids),
            Booking.booking_status.in_(['completed', 'return_initiated'])
        )
        if cutoff_date:
            booking_query = booking_query.filter(Booking.created_at >= cutoff_date)
        vendor_bookings = booking_query.all()
        total_bookings = len(vendor_bookings)
        if total_bookings < 5:
            continue
        total_earnings = sum(b.base_rent for b in vendor_bookings)
        booking_ids = [b.id for b in vendor_bookings]
        reviews = Review.query.filter(Review.booking_id.in_(booking_ids)).all() if booking_ids else []
        avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 1) if reviews else 0
        has_verified_item = any(i.is_currently_verified for i in vendor_items)
        rating_score = (avg_rating / 5) * 100 if avg_rating > 0 else 0
        bookings_score = min(100, total_bookings * 2)
        earnings_score = min(100, total_earnings / 1000)
        verification_score = 100 if has_verified_item else 0
        trust_score = round((rating_score * 0.4) + (bookings_score * 0.3) + 
                           (earnings_score * 0.2) + (verification_score * 0.1), 1)
        rankings.append({
            'vendor': vendor, 'trust_score': trust_score,
            'total_bookings': total_bookings, 'total_earnings': total_earnings,
            'avg_rating': avg_rating, 'review_count': len(reviews),
            'has_verified_item': has_verified_item, 'total_items': len(vendor_items)
        })
    rankings.sort(key=lambda x: x['trust_score'], reverse=True)
    for idx, r in enumerate(rankings):
        r['rank'] = idx + 1
    return rankings


@app.route('/leaderboard')
def leaderboard():
    period = request.args.get('period', 'all_time')
    category = request.args.get('category', 'all')
    rankings = calculate_leaderboard(period=period, category=category)
    return render_template('leaderboard.html',
                         top_3=rankings[:3] if len(rankings) >= 3 else rankings,
                         rest=rankings[3:] if len(rankings) > 3 else [],
                         total_vendors=len(rankings), period=period, category_filter=category)


@app.route('/api/leaderboard/top3')
def api_leaderboard_top3():
    rankings = calculate_leaderboard(period='all_time', category='all')[:3]
    return jsonify({'vendors': [{
        'name': r['vendor'].name, 'score': r['trust_score'],
        'bookings': r['total_bookings'],
        'rating': r['avg_rating'] if r['review_count'] > 0 else 'New',
        'verified': r['has_verified_item']
    } for r in rankings]})


# ============================================
# ADMIN — STORAGE
# ============================================
@app.route('/admin/storage')
@login_required
def admin_storage():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    all_reports = EquipmentReport.query.all()
    expiring_soon = [r for r in all_reports if not r.expired and not r.keep_forever 
                    and not r.damage_flagged and 0 <= r.days_until_expiry <= 7]
    return render_template('admin/storage.html',
                         total_reports=len(all_reports),
                         active_reports=sum(1 for r in all_reports if not r.expired and not r.keep_forever),
                         expired_reports=sum(1 for r in all_reports if r.expired),
                         protected_reports=sum(1 for r in all_reports if r.keep_forever),
                         disputed_reports=sum(1 for r in all_reports if r.damage_flagged),
                         expiring_soon=expiring_soon,
                         eligible_now=[r for r in all_reports if not r.expired and not r.keep_forever 
                                      and not r.damage_flagged and r.is_expired],
                         recent_expired=EquipmentReport.query.filter_by(expired=True)\
                             .order_by(EquipmentReport.expired_at.desc()).limit(20).all())


@app.route('/admin/storage/cleanup', methods=['POST'])
@login_required
def admin_storage_cleanup():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    dry_run = request.form.get('dry_run') == 'true'
    stats = cleanup_old_reports(dry_run=dry_run)
    flash(f"Cleanup: {stats['deleted_photos']} photos, {stats['deleted_videos']} videos.", 'success')
    return redirect(url_for('admin_storage'))


@app.route('/admin/report/<int:report_id>/toggle-protect', methods=['POST'])
@login_required
def admin_toggle_report_protection(report_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    report = EquipmentReport.query.get_or_404(report_id)
    report.keep_forever = not report.keep_forever
    db.session.commit()
    flash('Protection toggled.', 'success')
    return redirect(request.referrer or url_for('admin_storage'))


# ============================================
# CRON — EXTENDED WITH DPDP DELETION PROCESSING
# ============================================
@app.route('/api/cron/cleanup', methods=['GET', 'POST'])
def cron_cleanup():
    token = request.args.get('token') or request.form.get('token')
    if token != Config.CRON_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    
    # 1. Existing equipment report cleanup
    stats = cleanup_old_reports(dry_run=False)
    
    # 2. NEW: Process pending account deletions
    NOW = datetime.utcnow()
    pending_deletions = User.query.filter(
        User.deletion_scheduled_at != None,
        User.deletion_scheduled_at <= NOW,
        User.is_deleted == False
    ).all()
    
    deletion_stats = {'processed': 0, 'failed': 0, 'cloudinary_purged': {}}
    
    for u in pending_deletions:
        try:
            cloudinary_deleted = purge_user_cloudinary_assets(u.id)
            anonymize_user(u)
            u.is_deleted = True
            u.anonymized_at = NOW
            
            dr = DataRequest(
                user_id=u.id,
                request_type='deletion_completed',
                status='completed',
                completed_at=NOW,
                details=json_lib.dumps(cloudinary_deleted)
            )
            db.session.add(dr)
            db.session.commit()
            
            deletion_stats['processed'] += 1
            deletion_stats['cloudinary_purged'][u.id] = cloudinary_deleted
            print(f"✅ Deleted user {u.id}")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Deletion failed for user {u.id}: {e}")
            deletion_stats['failed'] += 1
    
    # 3. DPDP: Send deletion reminders (5 days before scheduled deletion)
    reminder_stats = {'sent': 0, 'skipped': 0}
    reminder_window_end = NOW + timedelta(days=5)
    users_needing_reminder = User.query.filter(
        User.deletion_scheduled_at != None,
        User.deletion_scheduled_at > NOW,
        User.deletion_scheduled_at <= reminder_window_end,
        User.is_deleted == False
    ).all()

    for u in users_needing_reminder:
        # Skip if a reminder was already sent AFTER the latest deletion request
        already_sent = DataRequest.query.filter_by(
            user_id=u.id, request_type='deletion_reminder'
        ).filter(DataRequest.created_at >= u.deletion_requested_at).first()

        if already_sent:
            reminder_stats['skipped'] += 1
            continue

        days_left = max(0, (u.deletion_scheduled_at - NOW).days)

        if u.telegram_chat_id:
            send_telegram_notification_async(
                u.telegram_chat_id,
                f"⚠️ Account Deletion Reminder\n\n"
                f"Hi {u.name},\n\n"
                f"Your VyahMandap account is scheduled for deletion in "
                f"{days_left} day(s) — on "
                f"{u.deletion_scheduled_at.strftime('%d %b %Y')}.\n\n"
                f"If you changed your mind, log in and click "
                f"'Cancel Deletion' on your My Data page."
            )
            reminder_stats['sent'] += 1
        else:
            reminder_stats['skipped'] += 1

        dr = DataRequest(
            user_id=u.id,
            request_type='deletion_reminder',
            status='completed',
            completed_at=NOW
        )
        db.session.add(dr)
        db.session.commit()

    return jsonify({
        'success': True,
        'report_cleanup': stats,
        'account_deletions': deletion_stats,
        'deletion_reminders': reminder_stats,
        'timestamp': NOW.isoformat()
    })


# ============================================
# ADMIN MAIN
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
    total_customers = User.query.filter_by(role='customer').count()
    total_verified_items = Item.query.filter_by(is_verified=True).count()
    pending_bookings = Booking.query.filter_by(booking_status='pending').count()
    confirmed_bookings = Booking.query.filter_by(booking_status='confirmed').count()
    dispatched_bookings = Booking.query.filter_by(booking_status='dispatched').count()
    completed_bookings = Booking.query.filter_by(booking_status='completed').count()
    cancelled_bookings = Booking.query.filter_by(booking_status='cancelled').count()
    total_transport = db.session.query(db.func.sum(Booking.transport_fee)).scalar() or 0
    total_deposits = db.session.query(db.func.sum(Booking.deposit)).scalar() or 0
    total_base_rent = db.session.query(db.func.sum(Booking.base_rent)).scalar() or 0
    kyc_pending = Booking.query.filter_by(kyc_required=True, kyc_verified=False).count()
    kyc_completed = Booking.query.filter_by(kyc_required=True, kyc_verified=True).count()
    tickets_open = Ticket.query.filter_by(status='open').count()
    tickets_in_progress = Ticket.query.filter_by(status='in_progress').count()
    reports_pending_dispatch = Booking.query.filter_by(dispatch_report_done=False)\
        .filter(Booking.booking_status == 'confirmed').count()
    reports_pending_return = Booking.query.filter_by(dispatch_report_done=True, return_report_done=False)\
        .filter(Booking.booking_status.in_(['dispatched', 'confirmed'])).count()
    damage_flagged_count = Booking.query.filter_by(damage_flagged=True).count()
    all_reports = EquipmentReport.query.all()
    pending_payment_proofs = PaymentProof.query.filter_by(status='pending').count()
    
    # DPDP metrics
    pending_deletions_count = User.query.filter(
        User.deletion_requested_at != None,
        User.is_deleted == False
    ).count()
    
    return render_template('admin/dashboard.html',
                         total_bookings=total_bookings, total_commission=total_commission,
                         total_revenue=total_revenue, total_items=total_items,
                         total_users=total_users, total_vendors=total_vendors,
                         total_customers=total_customers, total_verified_items=total_verified_items,
                         pending_bookings=pending_bookings, confirmed_bookings=confirmed_bookings,
                         dispatched_bookings=dispatched_bookings, completed_bookings=completed_bookings,
                         cancelled_bookings=cancelled_bookings, total_transport=total_transport,
                         total_deposits=total_deposits, total_base_rent=total_base_rent,
                         kyc_pending=kyc_pending, kyc_completed=kyc_completed,
                         tickets_open=tickets_open, tickets_in_progress=tickets_in_progress,
                         reports_pending_dispatch=reports_pending_dispatch,
                         reports_pending_return=reports_pending_return,
                         damage_flagged_count=damage_flagged_count,
                         total_reports=len(all_reports),
                         active_reports=sum(1 for r in all_reports if not r.expired and not r.keep_forever),
                         expired_reports=sum(1 for r in all_reports if r.expired),
                         protected_reports=sum(1 for r in all_reports if r.keep_forever),
                         disputed_reports=sum(1 for r in all_reports if r.damage_flagged),
                         pending_payment_proofs=pending_payment_proofs,
                         pending_deletions_count=pending_deletions_count,
                         recent_bookings=Booking.query.order_by(Booking.created_at.desc()).limit(10).all(),
                         recent_users=User.query.order_by(User.created_at.desc()).limit(5).all(),
                         recent_items=Item.query.order_by(Item.created_at.desc()).limit(5).all())


@app.route('/admin/bookings')
@login_required
def admin_bookings():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    status_filter = request.args.get('status', 'all')
    search = request.args.get('search', '').strip()
    sort = request.args.get('sort', 'newest')
    damage_filter = request.args.get('damage', 'all')
    query = Booking.query
    if status_filter != 'all':
        query = query.filter_by(booking_status=status_filter)
    if damage_filter == 'flagged':
        query = query.filter_by(damage_flagged=True)
    if search:
        query = query.join(User, Booking.customer_id == User.id).join(Item, Booking.item_id == Item.id).filter(
            db.or_(Booking.booking_reference.ilike(f'%{search}%'), Booking.utr_number.ilike(f'%{search}%'),
                   User.name.ilike(f'%{search}%'), User.mobile.ilike(f'%{search}%'), Item.title.ilike(f'%{search}%')))
    if sort == 'newest':
        query = query.order_by(Booking.created_at.desc())
    elif sort == 'oldest':
        query = query.order_by(Booking.created_at.asc())
    elif sort == 'highest':
        query = query.order_by(Booking.total_amount.desc())
    elif sort == 'lowest':
        query = query.order_by(Booking.total_amount.asc())
    return render_template('admin/bookings.html', bookings=query.all(),
                         status_filter=status_filter, damage_filter=damage_filter,
                         search=search, sort=sort)


@app.route('/admin/booking/<int:booking_id>')
@login_required
def admin_booking_detail(booking_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    booking = Booking.query.get_or_404(booking_id)
    payment_proofs = PaymentProof.query.filter_by(booking_id=booking_id)\
        .order_by(PaymentProof.created_at.desc()).all()
    return render_template('admin/booking_detail.html',
                         booking=booking, payment_proofs=payment_proofs)


@app.route('/admin/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def admin_update_booking_status(booking_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get('status', '')
    if new_status in ['confirmed', 'cancelled', 'completed', 'pending', 'dispatched', 'return_initiated']:
        old_status = booking.booking_status
        booking.booking_status = new_status

        active_statuses = ['pending', 'confirmed', 'dispatched', 'return_initiated']
        item = Item.query.get(booking.item_id)
        if item:
            # Restore stock when cancelling an active booking
            if new_status == 'cancelled' and old_status in active_statuses:
                item.stock += booking.quantity
                if item.stock > 0 and not item.is_available:
                    item.is_available = True
            # Re-reserve stock if a cancelled booking is reactivated (prevents double-restore)
            elif old_status == 'cancelled' and new_status in active_statuses:
                item.stock = max(0, item.stock - booking.quantity)
                if item.stock <= 0:
                    item.is_available = False

        db.session.commit()
        flash('Status updated.', 'success')
    return redirect(request.referrer or url_for('admin_bookings'))


@app.route('/admin/items')
@login_required
def admin_items():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    search = request.args.get('search', '').strip()
    category_filter = request.args.get('category', 'all')
    verified_filter = request.args.get('verified', 'all')
    query = Item.query
    if search:
        query = query.filter(db.or_(Item.title.ilike(f'%{search}%'), Item.description.ilike(f'%{search}%')))
    if category_filter != 'all':
        query = query.filter_by(category=category_filter)
    if verified_filter == 'verified':
        query = query.filter_by(is_verified=True)
    elif verified_filter == 'unverified':
        query = query.filter_by(is_verified=False)
    return render_template('admin/items.html', items=query.order_by(Item.created_at.desc()).all(),
                         search=search, category_filter=category_filter, verified_filter=verified_filter)


@app.route('/admin/item/add', methods=['GET', 'POST'])
@login_required
def admin_add_item():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        item = Item(
            title=request.form.get('title', '').strip(),
            description=request.form.get('description', '').strip(),
            category=request.form.get('category', ''),
            rate_per_day=float(request.form.get('rate', 0)),
            deposit_amount=float(request.form.get('deposit', 0)),
            stock=int(request.form.get('stock', 1)),
            image_url=request.form.get('image_url', '').strip() or None,
            vendor_id=current_user.id
        )
        db.session.add(item)
        db.session.commit()
        flash('✅ Item added!', 'success')
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
        db.session.commit()
        flash('✅ Item updated!', 'success')
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
    flash('Item deleted.', 'success')
    return redirect(url_for('admin_items'))


@app.route('/admin/item/<int:item_id>/verify', methods=['POST'])
@login_required
def admin_verify_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    item.is_verified = True
    item.verified_until = datetime.utcnow() + timedelta(days=90)
    db.session.commit()
    flash(f'✅ {item.title} verified for 90 days.', 'success')
    return redirect(url_for('admin_items'))


@app.route('/admin/item/<int:item_id>/unverify', methods=['POST'])
@login_required
def admin_unverify_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    item.is_verified = False
    item.verified_until = None
    db.session.commit()
    flash('Item unverified.', 'info')
    return redirect(url_for('admin_items'))


@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    role_filter = request.args.get('role', 'all')
    search = request.args.get('search', '').strip()
    query = User.query
    if role_filter != 'all':
        query = query.filter_by(role=role_filter)
    if search:
        query = query.filter(db.or_(User.name.ilike(f'%{search}%'),
                                    User.mobile.ilike(f'%{search}%'),
                                    User.email.ilike(f'%{search}%')))
    return render_template('admin/users.html', users=query.order_by(User.created_at.desc()).all(),
                         role_filter=role_filter, search=search)


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
        flash('Role updated.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/user/<int:user_id>')
@login_required
def admin_user_detail(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    user_bookings = Booking.query.filter_by(customer_id=user.id).order_by(Booking.created_at.desc()).all() if user.role == 'customer' else []
    user_items = Item.query.filter_by(vendor_id=user.id).order_by(Item.created_at.desc()).all() if user.role in ['vendor', 'admin'] else []
    vendor_bookings = []
    if user.role in ['vendor', 'admin'] and user_items:
        item_ids = [i.id for i in user_items]
        vendor_bookings = Booking.query.filter(Booking.item_id.in_(item_ids)).order_by(Booking.created_at.desc()).all()
    return render_template('admin/user_detail.html', user=user,
                         user_bookings=user_bookings, user_items=user_items,
                         vendor_bookings=vendor_bookings)


@app.route('/admin/export/bookings')
@login_required
def admin_export_bookings():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    import csv, io
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Reference', 'Date', 'Customer', 'Mobile', 'Item', 'Vendor',
                     'Start Date', 'End Date', 'Qty', 'Base Rent', 'Commission',
                     'Deposit', 'Transport', 'Total', 'UTR', 'Status'])
    for b in bookings:
        writer.writerow([b.booking_reference, b.created_at.strftime('%Y-%m-%d %H:%M'),
                        b.customer.name, b.customer.mobile, b.item.title, b.item.vendor.name,
                        b.start_date, b.end_date, b.quantity, b.base_rent, b.commission,
                        b.deposit, b.transport_fee, b.total_amount, b.utr_number, b.booking_status])
    return Response(output.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': 'attachment; filename=vyahmandap_bookings.csv'})


# ============================================
# CONTEXT PROCESSOR
# ============================================
@app.context_processor
def utility_processor():
    def unread_count():
        if not current_user.is_authenticated:
            return 0
        return Message.query.filter_by(receiver_id=current_user.id, is_read=False).count()
    
    def open_tickets_count():
        if not current_user.is_authenticated:
            return 0
        if current_user.role == 'admin':
            return Ticket.query.filter_by(status='open').count()
        return 0
    
    def pending_payments_count():
        if not current_user.is_authenticated:
            return 0
        if current_user.role == 'admin':
            return PaymentProof.query.filter_by(status='pending').count()
        return 0
    
    return dict(
        app_name=Config.APP_NAME, app_tagline=Config.APP_TAGLINE,
        business_phone=Config.BUSINESS_PHONE,
        business_phone_alt=Config.BUSINESS_PHONE_ALT,
        business_location=Config.BUSINESS_LOCATION,
        dpo_mobile=Config.DPO_MOBILE, dpo_email=Config.DPO_EMAIL,
        categories=Config.CATEGORIES, format_currency=format_currency,
        unread_message_count=unread_count(),
        open_tickets_count=open_tickets_count(),
        pending_payments_count=pending_payments_count(),
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
        try:
            from sqlalchemy import text
            migrations = [
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dispatch_report_done BOOLEAN DEFAULT FALSE",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS return_report_done BOOLEAN DEFAULT FALSE",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS damage_flagged BOOLEAN DEFAULT FALSE",
                "ALTER TABLE equipment_reports ADD COLUMN IF NOT EXISTS expired BOOLEAN DEFAULT FALSE",
                "ALTER TABLE equipment_reports ADD COLUMN IF NOT EXISTS expired_at TIMESTAMP",
                "ALTER TABLE equipment_reports ADD COLUMN IF NOT EXISTS keep_forever BOOLEAN DEFAULT FALSE",
                "ALTER TABLE equipment_reports ADD COLUMN IF NOT EXISTS video_url VARCHAR(500)",
                "ALTER TABLE equipment_reports ADD COLUMN IF NOT EXISTS video_public_id VARCHAR(200)",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS is_verified BOOLEAN DEFAULT FALSE",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS verified_until TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(50)",
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS vendor_response TEXT",
                "ALTER TABLE reviews ADD COLUMN IF NOT EXISTS vendor_responded_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS company_name VARCHAR(150)",
                # DPDP new columns
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS deletion_requested_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMP",
                # is_active is now a real mapped column (Flask-Login reads it)
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
                "UPDATE users SET is_active = TRUE WHERE is_active IS NULL",
            ]
            with db.engine.connect() as conn:
                for sql in migrations:
                    try:
                        conn.execute(text(sql))
                    except Exception:
                        pass
                conn.commit()
            print("✅ Auto-migration done")
        except Exception as mig_err:
            print(f"⚠️ Auto-migration skipped: {mig_err}")
        
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
                {'title': 'Maharaja Gold Carved Wedding Sofa', 'category': 'furniture',
                 'rate_per_day': 4500, 'deposit_amount': 3000, 'stock': 5,
                 'description': 'Elegant gold carved sofa.',
                 'image_url': 'https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=600',
                 'vendor_id': admin.id},
                {'title': 'Heavy Truss & LED Setup', 'category': 'lighting',
                 'rate_per_day': 2500, 'deposit_amount': 1500, 'stock': 12,
                 'description': 'Professional truss lighting system.',
                 'image_url': 'https://images.unsplash.com/photo-1516450360452-9312f5e86fc7?w=600',
                 'vendor_id': admin.id},
                {'title': 'Royal Floral Mandap Setup', 'category': 'mandap',
                 'rate_per_day': 12000, 'deposit_amount': 5000, 'stock': 3,
                 'description': 'Beautiful floral mandap.',
                 'image_url': 'https://images.unsplash.com/photo-1519741497674-611481863552?w=600',
                 'vendor_id': admin.id},
            ]
            for item_data in default_items:
                db.session.add(Item(**item_data))
            db.session.commit()
            print('✅ Default items created!')
        return True
    except Exception as e:
        print(f"❌ DB init error: {e}")
        import traceback
        traceback.print_exc()
        return False


# ============================================
# BATCH 1 — ADDITIVE FEATURES (24 Sep 2026)
# ============================================

@app.route('/booking/<int:booking_id>/receipt')
@login_required
def booking_receipt(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if (current_user.role != 'admin'
            and current_user.id != booking.customer_id
            and current_user.id != booking.item.vendor_id):
        flash('Not authorized to view this receipt.', 'error')
        return redirect(url_for('index'))
    return render_template('receipt.html', booking=booking)


@app.route('/booking/<int:booking_id>/timeline')
@login_required
def booking_timeline(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    if (current_user.role != 'admin'
            and current_user.id != booking.customer_id
            and current_user.id != booking.item.vendor_id):
        flash('Not authorized to view this timeline.', 'error')
        return redirect(url_for('index'))
    return render_template('timeline.html', booking=booking)


@app.route('/vendor/<int:vendor_id>/public')
def vendor_public_profile(vendor_id):
    vendor = User.query.get_or_404(vendor_id)
    if vendor.role not in ('vendor', 'admin'):
        flash('Vendor not found.', 'error')
        return redirect(url_for('index'))

    items = Item.query.filter_by(vendor_id=vendor.id, is_available=True)\
                      .order_by(Item.created_at.desc()).all()

    item_ids = [i.id for i in items]
    reviews = (Review.query.filter(Review.item_id.in_(item_ids)).all()
               if item_ids else [])
    avg_rating = (round(sum(r.rating for r in reviews) / len(reviews), 1)
                  if reviews else None)

    total_bookings = (Booking.query
                      .join(Item, Booking.item_id == Item.id)
                      .filter(Item.vendor_id == vendor.id)
                      .count())

    verified_item_count = sum(1 for i in items if i.is_currently_verified)

    return render_template(
        'vendor_public.html',
        vendor=vendor,
        items=items,
        reviews=reviews,
        avg_rating=avg_rating,
        total_reviews=len(reviews),
        total_bookings=total_bookings,
        verified_item_count=verified_item_count,
    )


@app.route('/faq')
def faq():
    return render_template('faq.html')


# ============================================
# STARTUP
# ============================================
with app.app_context():
    print("=" * 50)
    print(f"🚀 Starting {Config.APP_NAME}...")
    print(f"📱 Telegram: {'Enabled' if Config.TELEGRAM_BOT_TOKEN else 'Disabled'}")
    print(f"📷 Cloudinary: {'Configured' if Config.CLOUDINARY_CLOUD_NAME else 'Not configured'}")
    print(f"🗄️  DB: {'PostgreSQL' if 'postgres' in Config.SQLALCHEMY_DATABASE_URI else 'SQLite'}")
    print(f"🔐 DPO: {Config.DPO_EMAIL} / {Config.DPO_MOBILE}")
    print("=" * 50)
    init_database()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
