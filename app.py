import os
import sys
import re
import random
import string
import secrets as _secrets
import requests
import threading
import base64
import json as json_lib
import tempfile
import hashlib
import json as _json
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, flash, redirect, url_for, Response, session
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
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'temp-dev-only-do-not-use-in-prod'
    
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
    CRON_SECRET = os.environ.get('CRON_SECRET') or 'temp-cron-dev-only'
    
    BUSINESS_NAME = "VyahMandap"
    BUSINESS_PHONE = "8319337063"
    BUSINESS_PHONE_ALT = "9981845362"
    BUSINESS_EMAIL = "vyahmandap@gmail.com"
    BUSINESS_LOCATION = "Harda, Madhya Pradesh"
    MSME_NUMBER = "UDYAM-MP-21-0016639"
    
    ADMIN_MOBILE = "8319337063"
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')  # env-only, no fallback
    
    # DPDP compliance
    DPO_MOBILE = "8319337063"
    DPO_EMAIL = "privacy@vyahmandap.com"
    DELETION_GRACE_DAYS = 30
    
    TRANSPORT_RATES = {
        'city': {'till_20': 600, 'above_20': 1100},
        'outskirts': {'till_20': 1500, 'above_20': 2000}
    }
    
    COMMISSION_RATE = 0.05
    GATEWAY_RATE = 0.02
    DEPOSIT_RATE = 0.35
    SOS_FEE_RATE = 0.25
    SOS_VENDOR_BONUS_RATE = 0.10
    SOS_DURATION_MINUTES = 60

    # Mail settings (Gmail SMTP — use an App Password)
    MAIL_SERVER = 'smtp.gmail.com'
    MAIL_PORT = 587
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME') or ''
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD') or ''
    VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY') or ''
    VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY') or ''
    VAPID_EMAIL = os.environ.get('VAPID_EMAIL') or 'mailto:vyahmandap@gmail.com'
    MAIL_FROM_NAME = 'VyahMandap'
    RESET_TOKEN_EXPIRY_HOURS = 1
    
    # PART A — Categories expand
    CATEGORIES = [
        ('furniture', 'Sofas & Furniture'),
        ('lighting', 'Truss & Lighting'),
        ('decor', 'Decor & Floral'),
        ('mandap', 'Mandap & Stage'),
        ('tent', 'Tent & Canopy'),
        ('flooring', 'Flooring & Carpet'),
        ('catering', 'Catering Equipment'),
        ('sound', 'Sound & Music'),
        ('bar', 'Bar & Beverages'),
        ('entertainment', 'Entertainment'),
        ('electrical', 'Electrical & Power'),
        ('cooling', 'Cooling & Heating'),
    ]


app = Flask(__name__)
app.config.from_object(Config)

# Batch 6 — ProxyFix for real client IP behind Render proxy
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Batch 6 — CSRF protection
from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect(app)

# Batch 6 — Rate limiting
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

def _rate_limit_key():
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or 'unknown'

limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["500 per minute"],
    storage_uri="memory://",
)

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
    public_id = db.Column(db.String(20), unique=True, nullable=True, index=True)
    city = db.Column(db.String(100), default='Harda')
    sos_available = db.Column(db.Boolean, default=False)
    password_hash = db.Column(db.String(200), nullable=False)
    reset_token = db.Column(db.String(120), nullable=True, index=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)
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
    # Batch 7 — login throttling
    failed_login_attempts = db.Column(db.Integer, default=0)
    locked_until          = db.Column(db.DateTime, nullable=True)
    
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
    # Batch 8 — sale fields
    is_for_sale = db.Column(db.Boolean, default=False)
    sale_price = db.Column(db.Float, nullable=True)
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
    gateway_charge = db.Column(db.Float, default=0)
    installation_fee = db.Column(db.Float, default=0)
    sos_fee = db.Column(db.Float, default=0)
    sos_bonus = db.Column(db.Float, default=0)
    total_amount = db.Column(db.Float, nullable=False)
    reminder_sent = db.Column(db.Boolean, default=False)
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
    # Batch 4 — cancellation fields
    cancelled_at = db.Column(db.DateTime, nullable=True)
    cancellation_reason = db.Column(db.Text, nullable=True)
    refund_amount = db.Column(db.Float, nullable=True)
    cancelled_by = db.Column(db.String(20), nullable=True)
    # Batch 8 — order type ('rent' | 'sale' | 'bundle' | 'bundle_child' | 'sos')
    order_type = db.Column(db.String(20), default='rent')
    # Batch 9 — bundle linkage
    bundle_id = db.Column(db.Integer, db.ForeignKey('bundles.id'), nullable=True)
    parent_booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True, index=True)
    bundle = db.relationship('Bundle', foreign_keys=[bundle_id])


# Batch 8 — sales reduce stock permanently, so they must not count as rental occupancy
from sqlalchemy import or_ as _or
_RENTAL_ONLY = _or(Booking.order_type.is_(None), Booking.order_type != 'sale')


class PushSubscription(db.Model):
    __tablename__ = 'push_subscriptions'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    endpoint = db.Column(db.Text, nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    user_agent = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    user = db.relationship('User', foreign_keys=[user_id])


class ItemView(db.Model):
    __tablename__ = 'item_views'
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    ip_hash = db.Column(db.String(64), nullable=True, index=True)
    viewed_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    item = db.relationship('Item', foreign_keys=[item_id])


class SOSRequest(db.Model):
    __tablename__ = 'sos_requests'
    id = db.Column(db.Integer, primary_key=True)
    reference = db.Column(db.String(20), unique=True, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(10), default='10:00')
    quantity = db.Column(db.Integer, default=1)
    venue_address = db.Column(db.Text, nullable=False)
    notes = db.Column(db.Text)
    status = db.Column(db.String(20), default='active', index=True)
    accepted_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    booking_id = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    customer = db.relationship('User', foreign_keys=[customer_id])
    vendor = db.relationship('User', foreign_keys=[accepted_by])
    item = db.relationship('Item', foreign_keys=[item_id])
    booking = db.relationship('Booking', foreign_keys=[booking_id])


class Bundle(db.Model):
    __tablename__ = 'bundles'
    id = db.Column(db.Integer, primary_key=True)
    vendor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    tag = db.Column(db.String(30))  # mehndi/haldi/sangeet/wedding/custom
    description = db.Column(db.Text)
    image_url = db.Column(db.String(500))
    pricing_type = db.Column(db.String(20), default='discount')  # 'fixed' | 'discount'
    fixed_price = db.Column(db.Float, nullable=True)
    discount_percent = db.Column(db.Float, nullable=True)
    installation_fee = db.Column(db.Float, default=0)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    vendor = db.relationship('User', foreign_keys=[vendor_id])
    bundle_items = db.relationship('BundleItem', backref='bundle', lazy=True,
                                   cascade='all, delete-orphan')

    @property
    def items_count(self):
        return len(self.bundle_items)

    def get_image(self):
        if self.image_url:
            return self.image_url
        if self.bundle_items:
            first_item = self.bundle_items[0].item
            if first_item:
                return first_item.get_image()
        return 'https://via.placeholder.com/400x300/e5e7eb/9ca3af?text=Bundle'


class BundleItem(db.Model):
    __tablename__ = 'bundle_items'
    id = db.Column(db.Integer, primary_key=True)
    bundle_id = db.Column(db.Integer, db.ForeignKey('bundles.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)

    item = db.relationship('Item', foreign_keys=[item_id])


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


# PART B — Notification model
class Notification(db.Model):
    __tablename__ = 'notifications'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    type = db.Column(db.String(30), default='info')
    title = db.Column(db.String(150), nullable=False)
    message = db.Column(db.Text)
    link = db.Column(db.String(300))
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    user = db.relationship('User', foreign_keys=[user_id])


class Announcement(db.Model):
    __tablename__ = 'announcements'
    id = db.Column(db.Integer, primary_key=True)
    message = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    creator = db.relationship('User', foreign_keys=[created_by])


class Wishlist(db.Model):
    __tablename__ = 'wishlist'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    item = db.relationship('Item', foreign_keys=[item_id])

    __table_args__ = (
        db.UniqueConstraint('user_id', 'item_id', name='uq_wishlist_user_item'),
    )


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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY
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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY,
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
    # Deposit = DEPOSIT_RATE of base rent (was: item.deposit_amount × qty)
    deposit = round(base_rent * Config.DEPOSIT_RATE, 2)
    transport_fee = 0  # Transport excluded — settled directly with vendor
    gateway_charge = round(base_rent * Config.GATEWAY_RATE, 2)
    total = base_rent + commission + deposit + gateway_charge
    return {'days': days, 'base_rent': base_rent, 'commission': commission,
            'deposit': deposit, 'gateway_charge': gateway_charge,
            'transport_fee': transport_fee, 'total': total}


def send_email(to_email, subject, html_body):
    """Send HTML email via Gmail SMTP. Returns True on success."""
    if not Config.MAIL_USERNAME or not Config.MAIL_PASSWORD:
        print("⚠️ Mail not configured — skipping email send")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        from email.utils import formataddr

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = formataddr((Config.MAIL_FROM_NAME, Config.MAIL_USERNAME))
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP(Config.MAIL_SERVER, Config.MAIL_PORT, timeout=15) as server:
            server.starttls()
            server.login(Config.MAIL_USERNAME, Config.MAIL_PASSWORD)
            server.sendmail(Config.MAIL_USERNAME, [to_email], msg.as_string())
        return True
    except Exception as e:
        app.logger.error(f"Email send failed to {to_email}: {e}")
        return False


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
    # Telegram removed 2026-10-01 — all notifications now in-app only
    return


def send_telegram_notification_async(chat_id, message):
    # Telegram removed 2026-10-01
    return


def send_push_notification(user_id, title, body, url=None):
    """Send web push to all active subscriptions for a user."""
    if not Config.VAPID_PRIVATE_KEY or not Config.VAPID_PUBLIC_KEY:
        return 0
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        app.logger.warning('pywebpush not installed')
        return 0

    subs = PushSubscription.query.filter_by(user_id=user_id).all()
    if not subs:
        return 0

    payload = _json.dumps({
        'title': title[:100],
        'body': (body or '')[:180],
        'url': url or '/',
    })

    sent = 0
    dead_ids = []
    for s in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': s.endpoint,
                    'keys': {'p256dh': s.p256dh, 'auth': s.auth},
                },
                data=payload,
                vapid_private_key=Config.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': Config.VAPID_EMAIL},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                dead_ids.append(s.id)
        except Exception as e:
            app.logger.warning(f'Push failed: {e}')

    if dead_ids:
        try:
            PushSubscription.query.filter(PushSubscription.id.in_(dead_ids)).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()

    return sent


def _push_in_context(user_id, title, body, url=None):
    """Thread target: background threads need their own app context for DB access."""
    try:
        with app.app_context():
            send_push_notification(user_id, title, body, url)
    except Exception as e:
        print(f"Push thread failed: {e}")


def notify_all_admins(message, link=None, ntype='admin'):
    """Create in-app notification for all admins. Optionally include a link."""
    try:
        admins = User.query.filter_by(role='admin').all()
        for admin_user in admins:
            create_notification(
                admin_user.id, ntype, 'Admin Alert',
                message[:300], link
            )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"notify_all_admins failed: {e}")


# PART B — Notification helpers
def create_notification(user_id, ntype, title, message, link=None):
    """Unified notification helper. Also fires web push (non-blocking). Caller commits."""
    if not user_id:
        return None
    n = None
    try:
        n = Notification(user_id=user_id, type=ntype, title=title,
                         message=message, link=link)
        db.session.add(n)
    except Exception:
        return None
    # Fire push in background — errors swallowed
    try:
        if Config.VAPID_PRIVATE_KEY:
            threading.Thread(
                target=_push_in_context,
                args=(user_id, title, message, link),
                daemon=True,
            ).start()
    except Exception:
        pass
    return n


def unread_notification_count():
    try:
        if current_user.is_authenticated:
            return Notification.query.filter_by(
                user_id=current_user.id, is_read=False
            ).count()
    except Exception:
        pass
    return 0


def _time_ago(dt):
    if not dt:
        return ''
    delta = datetime.utcnow() - dt
    s = int(delta.total_seconds())
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{s // 60}m ago'
    if s < 86400:
        return f'{s // 3600}h ago'
    if s < 604800:
        return f'{s // 86400}d ago'
    return dt.strftime('%d %b %Y')


# ============================================
# AUTH ROUTES
# ============================================
# ============================================
# PASSWORD RESET
# ============================================
@app.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit("5 per hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        if not email:
            flash('Email is required.', 'danger')
            return render_template('auth/forgot_password.html')

        user = User.query.filter_by(email=email).first()
        # Always show same message (don't leak whether email exists)
        if user and user.password_hash:
            token = _secrets.token_urlsafe(32)
            user.reset_token = token
            user.reset_token_expires = datetime.utcnow() + timedelta(hours=Config.RESET_TOKEN_EXPIRY_HOURS)
            db.session.commit()

            reset_url = url_for('reset_password', token=token, _external=True)
            html_body = f"""
            <div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;">
              <h2 style="color:#7b1f4f;margin-bottom:8px;">VyahMandap</h2>
              <p style="color:#6b7280;font-size:13px;margin-top:0;">Taiyari Hamari, Celebration Aapka!</p>
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
              <p>Hi {user.name},</p>
              <p>We received a request to reset your VyahMandap password. Click the button below to set a new password:</p>
              <p style="text-align:center;margin:28px 0;">
                <a href="{reset_url}" style="display:inline-block;background:#7b1f4f;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 28px;border-radius:8px;">Reset Password</a>
              </p>
              <p style="color:#6b7280;font-size:13px;">This link expires in {Config.RESET_TOKEN_EXPIRY_HOURS} hour(s). If you didn't request this, you can safely ignore this email.</p>
              <p style="color:#6b7280;font-size:12px;margin-top:24px;">If the button doesn't work, copy this link:<br><span style="word-break:break-all;color:#7b1f4f;">{reset_url}</span></p>
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
              <p style="color:#9ca3af;font-size:11px;text-align:center;">VyahMandap • Harda, Madhya Pradesh</p>
            </div>
            """
            send_email(user.email, 'Reset your VyahMandap password', html_body)

        flash('If that email is registered, a reset link has been sent. Check your inbox.', 'info')
        return redirect(url_for('login'))
    return render_template('auth/forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=["POST"])
def reset_password(token):
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    user = User.query.filter_by(reset_token=token).first()
    if not user or not user.reset_token_expires or user.reset_token_expires < datetime.utcnow():
        flash('This reset link is invalid or has expired. Please request a new one.', 'danger')
        return redirect(url_for('forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()

        errors = []
        if not password:
            errors.append('Password is required.')
        elif len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        elif not re.search(r'[A-Za-z]', password):
            errors.append('Password must contain at least one letter.')
        elif not re.search(r'\d', password):
            errors.append('Password must contain at least one number.')
        if password != confirm:
            errors.append('Passwords do not match.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('auth/reset_password.html', token=token)

        user.set_password(password)
        user.reset_token = None
        user.reset_token_expires = None
        user.failed_login_attempts = 0
        user.locked_until = None
        db.session.commit()

        flash('Password reset successful. Please login with your new password.', 'success')
        return redirect(url_for('login'))

    return render_template('auth/reset_password.html', token=token)


@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'POST':
        mobile = request.form.get('mobile', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(mobile=mobile).first()

        # Batch 7 — check lockout
        if user and user.locked_until and user.locked_until > datetime.utcnow():
            mins = int((user.locked_until - datetime.utcnow()).total_seconds() / 60) + 1
            flash(f'🔒 Account locked. Try again in {mins} minute(s).', 'danger')
            return render_template('auth/login.html')

        if user and user.check_password(password):
            # Reset failed attempts on success
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()

            login_user(user, remember=True)
            flash(f'🎉 Welcome back, {user.name}!', 'success')
            if user.role == 'admin':
                return redirect(url_for('admin_dashboard'))
            elif user.role == 'vendor':
                return redirect(url_for('vendor_dashboard'))
            else:
                return redirect(url_for('dashboard'))
        else:
            # Increment failed counter
            if user:
                user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
                if user.failed_login_attempts >= 5:
                    user.locked_until = datetime.utcnow() + timedelta(minutes=15)
                    user.failed_login_attempts = 0
                    db.session.commit()
                    flash('🔒 Too many failed attempts. Account locked for 15 minutes.', 'danger')
                    return render_template('auth/login.html')
                db.session.commit()
            flash('❌ Invalid mobile number or password.', 'danger')
    return render_template('auth/login.html')


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("5 per minute", methods=["POST"])
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
        if not password:
            errors.append('Password is required.')
        elif len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        elif not re.search(r'[A-Za-z]', password):
            errors.append('Password must contain at least one letter.')
        elif not re.search(r'\d', password):
            errors.append('Password must contain at least one number.')
        if password != confirm_password: errors.append('Passwords do not match.')
        if User.query.filter_by(mobile=mobile).first():
            errors.append('A user with this mobile number already exists.')
        
        if errors:
            for error in errors: flash(error, 'danger')
            return render_template('auth/register.html')
        
        role = 'vendor' if account_type == 'vendor' else 'customer'
        city = request.form.get('city', 'Harda').strip()[:100] or 'Harda'
        user = User(name=name, mobile=mobile, email=email, role=role, city=city)
        if role == 'vendor' and company_name:
            user.company_name = company_name
        user.set_password(password)
        db.session.add(user)
        db.session.flush()  # get user.id before commit
        try:
            user.public_id = generate_user_public_id(role)
        except Exception as e:
            print(f"public_id gen failed: {e}")
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
# Telegram routes removed 2026-10-01 — notifications now in-app only


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
        f"🗑️ Deletion Request — {user.name} ({user.mobile}) scheduled {user.deletion_scheduled_at.strftime('%d %b %Y')}",
        link=url_for('admin_users')
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
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY,
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
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY,
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
        .filter(Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY).all()
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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'return_initiated']), _RENTAL_ONLY,
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
@limiter.limit("20 per minute", methods=["POST"])
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

            # Point 4 — mandatory payment screenshot
            screenshot = request.files.get('screenshot')
            if not screenshot or not screenshot.filename:
                flash('Payment screenshot is required.', 'danger')
                return render_template('booking.html', item=item)
            if not Config.CLOUDINARY_CLOUD_NAME:
                flash('Cloudinary not configured.', 'danger')
                return render_template('booking.html', item=item)

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
                gateway_charge=calc['gateway_charge'],
                total_amount=calc['total'], utr_number=utr,
                payment_status='pending', booking_status='confirmed',
                kyc_required=kyc_required, aadhaar_number=aadhaar,
                pan_number=pan, kyc_verified=kyc_required
            )
            item.stock -= quantity
            if item.stock <= 0:
                item.is_available = False
            db.session.add(booking)
            db.session.flush()  # need booking.id for PaymentProof

            # Point 4 — upload screenshot + create PaymentProof
            folder = f"vyahmandap/payments/booking_{booking.id}"
            upload_result = upload_file_to_cloudinary(screenshot, folder=folder)
            if not upload_result or not upload_result.get('url'):
                db.session.rollback()
                flash('Screenshot upload failed. Please try again.', 'danger')
                return render_template('booking.html', item=item)

            try:
                amount_paid = float(request.form.get('amount_paid', 0) or 0)
            except (ValueError, TypeError):
                amount_paid = 0
            payment_date = None
            pd_str = request.form.get('payment_date', '').strip()
            if pd_str:
                try:
                    payment_date = datetime.strptime(pd_str, '%Y-%m-%d').date()
                except Exception:
                    payment_date = None

            proof = PaymentProof(
                booking_id=booking.id,
                uploaded_by=current_user.id,
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
            
            if current_user.telegram_chat_id:
                send_telegram_notification_async(current_user.telegram_chat_id,
                    f"🎉 Booking Confirmed!\n\nItem: {item.title}\nRef: {booking.booking_reference}")
            if item.vendor.telegram_chat_id:
                send_telegram_notification_async(item.vendor.telegram_chat_id,
                    f"📦 New Booking!\n\nRef: {booking.booking_reference}\nFrom: {current_user.name}")
            
            # PART J — Event 1: New booking -> vendor
            create_notification(
                booking.item.vendor_id, 'booking', 'New Booking Received',
                f'{booking.booking_reference} — {booking.item.title}',
                f'/my-booking/{booking.id}'
            )
            db.session.commit()
            
            flash(f'🎉 Booking confirmed! Reference: {booking.booking_reference}', 'success')
            return redirect(url_for('dashboard'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error creating booking: {str(e)}', 'danger')
            payment_config = AdminPaymentConfig.get()
            qr_base64 = None
            if payment_config.upi_id:
                qr_base64 = generate_upi_qr_base64(
                    payment_config.upi_id, payment_config.upi_name, amount=None
                )
            return render_template('booking.html', item=item,
                                   payment_config=payment_config, qr_base64=qr_base64)
    payment_config = AdminPaymentConfig.get()
    qr_base64 = None
    if payment_config.upi_id:
        qr_base64 = generate_upi_qr_base64(
            payment_config.upi_id, payment_config.upi_name, amount=None
        )
    return render_template('booking.html', item=item,
                           payment_config=payment_config, qr_base64=qr_base64)


@app.route('/dashboard')
@login_required
def dashboard():
    bookings = Booking.query.filter_by(customer_id=current_user.id)\
        .filter(Booking.parent_booking_id.is_(None))\
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
@limiter.limit("10 per hour")
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
        notify_all_admins(
            f"🚨 DAMAGE — {booking.booking_reference} — {booking.item.title} — Ticket {ticket.ticket_number}",
            link=url_for('admin_ticket_detail', ticket_id=ticket.id)
        )
    
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
    # If booking was reverted to pending due to prior reject, restore it
    if booking.booking_status == 'pending':
        booking.booking_status = 'confirmed'
    db.session.commit()
    
    if booking.customer.telegram_chat_id:
        send_telegram_notification_async(booking.customer.telegram_chat_id,
            f"✅ Payment Verified!\n\nBooking: {booking.booking_reference}\n"
            f"Amount: ₹{proof.amount_paid:,.2f}\nMethod: {proof.method.upper()}")
    
    # PART J — Event 3: Payment verified -> customer
    create_notification(
        proof.booking.customer_id, 'payment', 'Payment Verified',
        f'{proof.booking.booking_reference} — ₹{proof.amount_paid} verified',
        f'/my-booking/{proof.booking.id}'
    )
    db.session.commit()
    
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

    # If no other verified proof exists, revert booking to pending
    other_verified = PaymentProof.query.filter(
        PaymentProof.booking_id == proof.booking_id,
        PaymentProof.status == 'verified',
        PaymentProof.id != proof.id
    ).first()
    if not other_verified and proof.booking.booking_status == 'confirmed':
        proof.booking.booking_status = 'pending'
        proof.booking.payment_status = 'rejected'

    db.session.commit()
    
    if proof.booking.customer.telegram_chat_id:
        send_telegram_notification_async(proof.booking.customer.telegram_chat_id,
            f"❌ Payment Proof Rejected\n\nBooking: {proof.booking.booking_reference}\n"
            f"Reason: {reason}")
    
    # PART J — Event 4: Payment rejected -> customer
    create_notification(
        proof.booking.customer_id, 'payment', 'Payment Rejected',
        f'{proof.booking.booking_reference} — reason: {reason}',
        f'/my-booking/{proof.booking.id}'
    )
    db.session.commit()
    
    flash('Payment proof rejected.', 'info')
    return redirect(request.referrer or url_for('admin_payment_queue'))


@app.route('/booking/<int:booking_id>/payment-proof', methods=['POST'])
@limiter.limit("10 per hour")
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
        f"💰 New Payment Proof — {booking.booking_reference} — ₹{proof.amount_paid:,.0f} ({proof.method.upper()})",
        link=url_for('admin_payment_queue')
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
    bookings = Booking.query.filter(Booking.item_id.in_(item_ids), Booking.parent_booking_id.is_(None)).order_by(Booking.created_at.desc()).all() if item_ids else []
    total_earnings = sum((b.base_rent or 0) + (b.sos_bonus or 0) for b in bookings)
    total_sos_bonus = sum((b.sos_bonus or 0) for b in bookings)
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_sos_bonus=total_sos_bonus,
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
    total_earnings = sum((b.base_rent or 0) + (b.sos_bonus or 0) for b in bookings)
    total_sos_bonus = sum((b.sos_bonus or 0) for b in bookings)
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_sos_bonus=total_sos_bonus,
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
        try:
            rate = float(request.form.get('rate', 0))
        except (ValueError, TypeError):
            rate = 0
        try:
            stock = int(request.form.get('stock', 1))
        except (ValueError, TypeError):
            stock = 1
        deposit = 0  # Auto-calculated at booking (35% of base rent)
        image_url = request.form.get('image_url', '').strip()
        image_filename = None
        if 'image_file' in request.files and request.files['image_file'].filename:
            result = upload_file_to_cloudinary(request.files['image_file'], folder='vyahmandap/items')
            if result and result.get('url'):
                image_url = result['url']
            else:
                flash('Image upload failed. Please try again or paste an image URL.', 'warning')
        if not title or rate <= 0:
            flash('Title and rate are required.', 'danger')
            return render_template('vendor/item_form.html')
        item = Item(title=title, description=description, category=category,
                    rate_per_day=rate, deposit_amount=0, stock=stock,
                    image_url=image_url if image_url else None,
                    image_filename=image_filename, vendor_id=current_user.id)
        _apply_sale_fields(item)
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
        try:
            item.rate_per_day = float(request.form.get('rate', 0))
        except (ValueError, TypeError):
            item.rate_per_day = 0
        try:
            item.stock = int(request.form.get('stock', 1))
        except (ValueError, TypeError):
            item.stock = 1
        item.deposit_amount = 0  # Auto-calculated at booking
        item.is_available = 'is_available' in request.form
        _apply_sale_fields(item)
        if 'image_file' in request.files and request.files['image_file'].filename:
            result = upload_file_to_cloudinary(request.files['image_file'], folder='vyahmandap/items')
            if result and result.get('url'):
                item.image_url = result['url']
                item.image_filename = None
            else:
                flash('Image upload failed. Please try again or paste an image URL.', 'warning')
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

    # PART J — Event 7: Vendor response to review -> customer
    create_notification(
        review.customer_id, 'review', 'Vendor Responded',
        f'{item.vendor.name if item.vendor else "Vendor"} replied to your review',
        f'/item/{review.item_id}/reviews'
    )
    db.session.commit()

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
        
        # PART J — Event 6: New review -> vendor
        create_notification(
            booking.item.vendor_id, 'review', 'New Review Received',
            f'{rating}★ for {booking.item.title}',
            f'/item/{booking.item_id}/reviews'
        )
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
@limiter.limit("60 per minute")
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
    
    # PART J — Event 5: New chat message -> receiver
    create_notification(
        receiver_id, 'chat', f'New message from {current_user.name}',
        body[:80] + ('...' if len(body) > 80 else ''),
        url_for('chat_with', user_id=current_user.id)
    )
    db.session.commit()
    
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
@limiter.limit("5 per hour", methods=["POST"])
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
        notify_all_admins(
            f"🎫 New Ticket — {ticket.ticket_number} from {current_user.name}",
            link=url_for('admin_ticket_detail', ticket_id=ticket.id)
        )
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
            
            # PART J — Event 8: Ticket reply -> other party
            other_party_id = ticket.user_id if current_user.role == 'admin' else ticket.assigned_to
            if other_party_id and other_party_id != current_user.id:
                create_notification(
                    other_party_id, 'ticket', f'New reply on {ticket.ticket_number}',
                    message[:80],
                    url_for('ticket_detail', ticket_id=ticket.id)
                )
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
                
                # PART J — Event 8: Ticket reply -> other party
                other_party_id = ticket.user_id if current_user.role == 'admin' else ticket.assigned_to
                if other_party_id and other_party_id != current_user.id:
                    create_notification(
                        other_party_id, 'ticket', f'New reply on {ticket.ticket_number}',
                        message[:80],
                        url_for('ticket_detail', ticket_id=ticket.id)
                    )
                    db.session.commit()
                
                flash('✅ Reply sent.', 'success')
                return redirect(url_for('admin_ticket_detail', ticket_id=ticket.id))
    replies = TicketReply.query.filter_by(ticket_id=ticket.id).order_by(TicketReply.created_at.asc()).all()
    return render_template('admin/ticket_detail.html', ticket=ticket, replies=replies)


# ============================================
# LEADERBOARD
# ============================================
def calculate_vendor_analytics(vendor_id):
    """Compute analytics data for vendor dashboard."""
    items = Item.query.filter_by(vendor_id=vendor_id).all()
    item_ids = [i.id for i in items]

    empty = {
        'total_views': 0, 'unique_views': 0, 'views_30d': 0,
        'total_bookings': 0, 'conversion_rate': 0,
        'top_by_views': [], 'top_by_bookings': [], 'views_trend': [],
    }
    if not item_ids:
        return empty

    try:
        total_views = ItemView.query.filter(ItemView.item_id.in_(item_ids)).count()

        unique_users = db.session.query(
            db.func.count(db.func.distinct(ItemView.user_id))
        ).filter(
            ItemView.item_id.in_(item_ids),
            ItemView.user_id.isnot(None)
        ).scalar() or 0

        unique_anon = db.session.query(
            db.func.count(db.func.distinct(ItemView.ip_hash))
        ).filter(
            ItemView.item_id.in_(item_ids),
            ItemView.user_id.is_(None),
            ItemView.ip_hash.isnot(None)
        ).scalar() or 0

        unique_views = unique_users + unique_anon

        cutoff_30 = datetime.utcnow() - timedelta(days=30)
        views_30d = ItemView.query.filter(
            ItemView.item_id.in_(item_ids),
            ItemView.viewed_at >= cutoff_30
        ).count()

        total_bookings = Booking.query.filter(
            Booking.item_id.in_(item_ids)
        ).count()

        conversion_rate = round((total_bookings / total_views * 100), 1) if total_views > 0 else 0

        top_views_rows = db.session.query(
            Item.id, Item.title,
            db.func.count(ItemView.id).label('view_count')
        ).join(ItemView, ItemView.item_id == Item.id).filter(
            Item.vendor_id == vendor_id
        ).group_by(Item.id, Item.title).order_by(db.desc('view_count')).limit(5).all()

        top_book_rows = db.session.query(
            Item.id, Item.title,
            db.func.count(Booking.id).label('booking_count')
        ).join(Booking, Booking.item_id == Item.id).filter(
            Item.vendor_id == vendor_id
        ).group_by(Item.id, Item.title).order_by(db.desc('booking_count')).limit(5).all()

        views_trend = []
        now = datetime.utcnow()
        for i in range(29, -1, -1):
            day = now - timedelta(days=i)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)
            cnt = ItemView.query.filter(
                ItemView.item_id.in_(item_ids),
                ItemView.viewed_at >= day_start,
                ItemView.viewed_at < day_end
            ).count()
            views_trend.append({
                'label': day_start.strftime('%d %b'),
                'count': cnt,
            })

        return {
            'total_views': total_views,
            'unique_views': unique_views,
            'views_30d': views_30d,
            'total_bookings': total_bookings,
            'conversion_rate': conversion_rate,
            'top_by_views': [{'id': r.id, 'title': r.title, 'count': r.view_count} for r in top_views_rows],
            'top_by_bookings': [{'id': r.id, 'title': r.title, 'count': r.booking_count} for r in top_book_rows],
            'views_trend': views_trend,
        }
    except Exception as e:
        print(f"Analytics failed: {e}")
        return empty


def calculate_vendor_response_time(vendor_id, sample_size=20):
    """Average hours for vendor to reply to customer messages."""
    try:
        received = Message.query.filter_by(receiver_id=vendor_id)\
            .order_by(Message.created_at.desc()).limit(sample_size).all()
        if not received:
            return None
        gaps = []
        for msg in received:
            reply = Message.query.filter(
                Message.sender_id == vendor_id,
                Message.receiver_id == msg.sender_id,
                Message.created_at > msg.created_at
            ).order_by(Message.created_at.asc()).first()
            if reply:
                hours = (reply.created_at - msg.created_at).total_seconds() / 3600.0
                if 0 < hours < 168:  # within 7 days
                    gaps.append(hours)
        if not gaps:
            return None
        return round(sum(gaps) / len(gaps), 1)
    except Exception:
        return None


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
# ADMIN — DATA MANAGEMENT
# ============================================
@app.route('/admin/data-management', methods=['GET', 'POST'])
@login_required
def admin_data_management():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        action = request.form.get('action', '')
        confirm = request.form.get('confirm', '').strip()

        try:
            if action == 'clear_bookings':
                if confirm != 'DELETE_BOOKINGS':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                Message.query.update({'booking_id': None})
                db.session.commit()
                Booking.query.update({'parent_booking_id': None})
                db.session.commit()
                n1 = PaymentProof.query.delete()
                n2 = EquipmentReport.query.delete()
                n3 = Review.query.delete()
                n4 = Booking.query.delete()
                n5 = Notification.query.filter(
                    Notification.type.in_(['booking', 'payment', 'cancel', 'admin', 'review'])
                ).delete(synchronize_session=False)
                db.session.commit()
                flash(f'✅ Cleared: {n4} bookings, {n1} payment proofs, {n2} reports, {n3} reviews, {n5} notifications.', 'success')

            elif action == 'clear_notifications':
                if confirm != 'DELETE_NOTIFICATIONS':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                n = Notification.query.delete()
                db.session.commit()
                flash(f'✅ Cleared {n} notifications.', 'success')

            elif action == 'clear_messages':
                if confirm != 'DELETE_MESSAGES':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                n = Message.query.delete()
                db.session.commit()
                flash(f'✅ Cleared {n} messages.', 'success')

            elif action == 'clear_closed_tickets':
                if confirm != 'DELETE_TICKETS':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                closed_ids = [t.id for t in Ticket.query.filter(
                    Ticket.status.in_(['closed', 'resolved'])
                ).all()]
                if closed_ids:
                    TicketReply.query.filter(TicketReply.ticket_id.in_(closed_ids)).delete(synchronize_session=False)
                    n = Ticket.query.filter(Ticket.id.in_(closed_ids)).delete(synchronize_session=False)
                else:
                    n = 0
                db.session.commit()
                flash(f'✅ Cleared {n} closed/resolved tickets.', 'success')

            else:
                flash('Unknown action.', 'danger')

        except Exception as e:
            db.session.rollback()
            flash(f'❌ Error: {str(e)}', 'danger')

        return redirect(url_for('admin_data_management'))

    counts = {
        'users': User.query.count(),
        'customers': User.query.filter_by(role='customer').count(),
        'vendors': User.query.filter_by(role='vendor').count(),
        'items': Item.query.count(),
        'bundles': Bundle.query.count(),
        'bookings': Booking.query.count(),
        'payment_proofs': PaymentProof.query.count(),
        'reports': EquipmentReport.query.count(),
        'reviews': Review.query.count(),
        'messages': Message.query.count(),
        'notifications': Notification.query.count(),
        'tickets': Ticket.query.count(),
        'tickets_closed': Ticket.query.filter(Ticket.status.in_(['closed', 'resolved'])).count(),
        'wishlist': Wishlist.query.count(),
    }
    return render_template('admin/data_management.html', counts=counts)


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

    # 4. Booking reminders — notify vendor 1 day before start
    from datetime import date as _date
    tomorrow = _date.today() + timedelta(days=1)
    bookings_tomorrow = Booking.query.filter(
        Booking.start_date == tomorrow,
        Booking.reminder_sent == False,
        Booking.booking_status.in_(['confirmed', 'pending']),
        Booking.parent_booking_id.is_(None)
    ).all()
    booking_reminders = {'sent': 0}
    for b in bookings_tomorrow:
        try:
            if b.item and b.item.vendor_id:
                create_notification(
                    b.item.vendor_id,
                    'booking',
                    '⏰ Booking Starts Tomorrow',
                    f'{b.booking_reference} — {b.item.title} for {b.customer.name if b.customer else "customer"}',
                    f'/my-booking/{b.id}'
                )
            b.reminder_sent = True
            booking_reminders['sent'] += 1
        except Exception as e:
            app.logger.error(f'Booking reminder failed for {b.id}: {e}')
    db.session.commit()

    return jsonify({
        'success': True,
        'report_cleanup': stats,
        'account_deletions': deletion_stats,
        'deletion_reminders': reminder_stats,
        'booking_reminders': booking_reminders,
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
    query = Booking.query.filter(Booking.parent_booking_id.is_(None))
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

    # One-time backfill: users without public_id
    try:
        missing = User.query.filter(
            (User.public_id.is_(None)) | (User.public_id == '')
        ).all()
        for u in missing:
            try:
                u.public_id = generate_user_public_id(u.role)
            except Exception:
                pass
        if missing:
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.warning(f'public_id backfill failed: {e}')

    role_filter = request.args.get('role', 'all')
    search = request.args.get('search', '').strip()
    query = User.query
    if role_filter != 'all':
        query = query.filter_by(role=role_filter)
    if search:
        query = query.filter(db.or_(User.name.ilike(f'%{search}%'),
                                    User.mobile.ilike(f'%{search}%'),
                                    User.email.ilike(f'%{search}%')))
    return render_template('admin/users.html',
                           users=query.order_by(User.created_at.desc()).all(),
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
    
    vendor_sos_available = False
    if current_user.is_authenticated and current_user.role in ('vendor', 'admin'):
        vendor_sos_available = bool(current_user.sos_available)

    def disp_name(user):
        """Own name for self/admin viewers, public_id for everyone else."""
        if not user:
            return '—'
        if current_user.is_authenticated:
            if current_user.id == user.id or current_user.role == 'admin':
                return user.name
        return user.public_id or user.name

    return dict(
        disp_name=disp_name,
        cart_count=cart_count(),
        vendor_sos_available=vendor_sos_available,
        app_name=Config.APP_NAME, app_tagline=Config.APP_TAGLINE,
        business_phone=Config.BUSINESS_PHONE,
        business_phone_alt=Config.BUSINESS_PHONE_ALT,
        business_email=Config.BUSINESS_EMAIL,
        business_location=Config.BUSINESS_LOCATION,
        msme_number=Config.MSME_NUMBER,
        dpo_mobile=Config.DPO_MOBILE, dpo_email=Config.DPO_EMAIL,
        categories=Config.CATEGORIES, format_currency=format_currency,
        unread_message_count=unread_count(),
        open_tickets_count=open_tickets_count(),
        pending_payments_count=pending_payments_count(),
        unread_notification_count=unread_notification_count(),
        get_category_icon=lambda c: {
            'furniture': 'fa-couch', 'lighting': 'fa-lightbulb',
            'decor': 'fa-palette', 'mandap': 'fa-archway',
            'tent': 'fa-campground', 'flooring': 'fa-layer-group',
            'catering': 'fa-utensils', 'sound': 'fa-music',
            'bar': 'fa-wine-glass', 'entertainment': 'fa-dice',
            'electrical': 'fa-bolt', 'cooling': 'fa-wind'
        }.get(c, 'fa-box')
    )


# ============================================
# INIT DATABASE
# ============================================
# Register helpers as Jinja globals
app.jinja_env.globals['_time_ago'] = _time_ago


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
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancellation_reason TEXT",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS refund_amount FLOAT",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cancelled_by VARCHAR(20)",
                # Batch 5 Notification table
                "CREATE TABLE IF NOT EXISTS notifications (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL, type VARCHAR(30), title VARCHAR(150) NOT NULL, message TEXT, link VARCHAR(300), is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_user_id ON notifications(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_is_read ON notifications(is_read)",
                "CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at)",
                # Batch 7 — login throttling
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS failed_login_attempts INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR(120)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token_expires TIMESTAMP",
                # Batch 8 — sell items
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS is_for_sale BOOLEAN DEFAULT FALSE",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS sale_price FLOAT",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS order_type VARCHAR(10) DEFAULT 'rent'",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS gateway_charge FLOAT DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE",
                "ALTER TABLE bookings ALTER COLUMN order_type TYPE VARCHAR(20)",
                # Batch 9 — bundle linkage
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS bundle_id INTEGER",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS parent_booking_id INTEGER",
                "CREATE INDEX IF NOT EXISTS idx_bookings_parent_booking_id ON bookings(parent_booking_id)",
                # Package + installation fee
                "ALTER TABLE bundles ADD COLUMN IF NOT EXISTS installation_fee FLOAT DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS installation_fee FLOAT DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS sos_bonus FLOAT DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS sos_fee FLOAT DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS city VARCHAR(100) DEFAULT 'Harda'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS public_id VARCHAR(20)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_public_id ON users(public_id)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS sos_available BOOLEAN DEFAULT FALSE",
                """CREATE TABLE IF NOT EXISTS sos_requests (
                    id SERIAL PRIMARY KEY,
                    reference VARCHAR(20) UNIQUE NOT NULL,
                    customer_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    city VARCHAR(100) NOT NULL,
                    start_date DATE NOT NULL,
                    start_time VARCHAR(10),
                    quantity INTEGER DEFAULT 1,
                    venue_address TEXT NOT NULL,
                    notes TEXT,
                    status VARCHAR(20) DEFAULT 'active',
                    accepted_by INTEGER,
                    accepted_at TIMESTAMP,
                    booking_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_sos_status ON sos_requests(status)",
                "CREATE INDEX IF NOT EXISTS idx_sos_city ON sos_requests(city)",
                "CREATE INDEX IF NOT EXISTS idx_sos_expires ON sos_requests(expires_at)",
                """CREATE TABLE IF NOT EXISTS item_views (
                    id SERIAL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    user_id INTEGER,
                    ip_hash VARCHAR(64),
                    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                "CREATE INDEX IF NOT EXISTS idx_item_views_item ON item_views(item_id)",
                "CREATE INDEX IF NOT EXISTS idx_item_views_user ON item_views(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_item_views_viewed ON item_views(viewed_at)",
                """CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    endpoint TEXT UNIQUE NOT NULL,
                    p256dh VARCHAR(255) NOT NULL,
                    auth VARCHAR(255) NOT NULL,
                    user_agent VARCHAR(300),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                "CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id)",
            ]
            with db.engine.connect() as conn:
                for sql in migrations:
                    try:
                        conn.execute(text(sql))
                        conn.commit()
                    except Exception:
                        conn.rollback()
            print("✅ Auto-migration done")
        except Exception as mig_err:
            print(f"⚠️ Auto-migration skipped: {mig_err}")
        
        db.create_all()
        print("✅ Database tables created!")
        
        admin = User.query.filter_by(mobile=Config.ADMIN_MOBILE).first()
        if not admin:
            if Config.ADMIN_PASSWORD:
                admin = User(name='VyahMandap Admin', mobile=Config.ADMIN_MOBILE,
                            email='admin@vyahmandap.com', role='admin')
                admin.set_password(Config.ADMIN_PASSWORD)
                db.session.add(admin)
                db.session.flush()
                try:
                    admin.public_id = generate_user_public_id('admin')
                except Exception:
                    pass
                db.session.commit()
                print('✅ Admin created!')
            else:
                print('⚠️ ADMIN_PASSWORD env var not set — admin NOT created')
        
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
        
        # Backfill public_id for existing users
        try:
            users_without = User.query.filter(
                (User.public_id.is_(None)) | (User.public_id == '')
            ).order_by(User.id).all()
            for u in users_without:
                try:
                    u.public_id = generate_user_public_id(u.role)
                    db.session.flush()
                except Exception as e:
                    print(f"Backfill failed for user {u.id}: {e}")
            if users_without:
                db.session.commit()
                print(f"✅ Backfilled {len(users_without)} user public_id(s)")
        except Exception as e:
            print(f"Backfill error: {e}")
            db.session.rollback()

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
# BATCH 5 — NOTIFICATION SYSTEM (25 Sep 2026)
# ============================================
@app.route('/api/notifications/recent')
@login_required
def api_notifications_recent():
    notifs = (Notification.query.filter_by(user_id=current_user.id)
              .order_by(Notification.created_at.desc()).limit(10).all())
    unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({
        'notifications': [{
            'id': n.id, 'type': n.type, 'title': n.title,
            'message': n.message or '', 'link': n.link or '',
            'is_read': n.is_read, 'time_ago': _time_ago(n.created_at),
            'created_at': n.created_at.strftime('%d %b %Y, %I:%M %p'),
        } for n in notifs],
        'unread_count': unread,
    })


@app.route('/notifications')
@login_required
def notifications_page():
    notifs = (Notification.query.filter_by(user_id=current_user.id)
              .order_by(Notification.created_at.desc()).limit(200).all())
    return render_template('notifications.html', notifications=notifs)


@app.route('/notifications/<int:nid>/open')
@login_required
def notification_open(nid):
    n = Notification.query.get_or_404(nid)
    if n.user_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    n.is_read = True
    db.session.commit()
    return redirect(n.link or url_for('notifications_page'))


@app.route('/notifications/clear-all', methods=['POST'])
@login_required
def notifications_clear_all():
    Notification.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True})
    flash('All notifications cleared.', 'success')
    return redirect(url_for('notifications_page'))


@app.route('/notifications/mark-all-read', methods=['POST'])
@login_required
def notifications_mark_all_read():
    Notification.query.filter_by(user_id=current_user.id, is_read=False)\
        .update({'is_read': True})
    db.session.commit()
    if request.headers.get('X-Requested-With') == 'fetch':
        return jsonify({'ok': True})
    return redirect(url_for('notifications_page'))


# ============================================
# BATCH 9A — BUNDLES (28 Sep 2026)
# ============================================

def calculate_bundle_total(bundle, start_date, end_date, area, weight_category):
    """Batch 9 — bundle pricing. Returns dict with per-item breakdown."""
    days = (end_date - start_date).days + 1
    if days < 1:
        days = 1

    item_details = []
    individual_sum = 0.0
    for bi in bundle.bundle_items:
        item = bi.item
        if not item:
            continue
        subtotal = item.rate_per_day * days * bi.quantity
        individual_sum += subtotal
        item_details.append({
            'item': item,
            'quantity': bi.quantity,
            'subtotal': round(subtotal, 2),
        })

    if bundle.pricing_type == 'fixed' and bundle.fixed_price:
        bundle_base = float(bundle.fixed_price)
    elif bundle.pricing_type == 'discount' and bundle.discount_percent:
        bundle_base = round(individual_sum * (1 - bundle.discount_percent / 100.0), 2)
    else:
        bundle_base = round(individual_sum, 2)

    commission = round(bundle_base * Config.COMMISSION_RATE, 2)
    deposit = round(bundle_base * Config.DEPOSIT_RATE, 2)
    transport_fee = 0  # Transport excluded
    gateway_charge = round(bundle_base * Config.GATEWAY_RATE, 2)
    installation_fee = round(float(bundle.installation_fee or 0), 2)
    total = round(bundle_base + commission + deposit + gateway_charge + installation_fee, 2)

    return {
        'days': days,
        'individual_sum': round(individual_sum, 2),
        'base_rent': bundle_base,
        'commission': commission,
        'deposit': deposit,
        'transport_fee': transport_fee,
        'gateway_charge': gateway_charge,
        'installation_fee': installation_fee,
        'total': total,
        'item_details': item_details,
        'savings': round(max(0, individual_sum - bundle_base), 2),
    }


def bundle_stock_available(bundle):
    """Check if all items in bundle have enough stock."""
    for bi in bundle.bundle_items:
        if not bi.item or not bi.item.is_available:
            return False, bi.item.title if bi.item else 'Unknown'
        if (bi.item.stock or 0) < bi.quantity:
            return False, bi.item.title
    return True, None


# ============================================
# BATCH 9B — BUNDLE BOOKING ROUTE
# ============================================

@app.route('/bundle/<int:bundle_id>/book', methods=['GET', 'POST'])
@login_required
def book_bundle(bundle_id):
    bundle = Bundle.query.get_or_404(bundle_id)
    if not bundle.is_active:
        flash('This bundle is not available.', 'warning')
        return redirect(url_for('bundles_list'))

    available, blocking_item = bundle_stock_available(bundle)
    if not available:
        flash(f'Bundle not available — {blocking_item} is out of stock.', 'danger')
        return redirect(url_for('bundle_detail', bundle_id=bundle_id))

    if request.method == 'POST':
        try:
            start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
            end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()
            area = request.form.get('area', 'city')
            weight = request.form.get('weight', 'till_20')
            venue_address = request.form.get('address', '').strip()
            utr = request.form.get('utr', '').strip()
            start_time = request.form.get('start_time', '10:00')
            end_time = request.form.get('end_time', '20:00')
        except Exception:
            flash('Invalid form data. Please check dates.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))

        # Date validation
        today_ist = ist_today()
        if start_date < today_ist:
            flash('Start date cannot be in the past.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))
        if end_date < start_date:
            flash('End date must be on or after start date.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))
        if (end_date - start_date).days > 365:
            flash('Bookings are limited to 365 days.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))
        if not venue_address:
            flash('Venue address is required.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))
        if not utr:
            flash('UTR number is required.', 'danger')
            return redirect(url_for('book_bundle', bundle_id=bundle_id))
        if area not in ('city', 'outskirts'):
            area = 'city'
        if weight not in ('till_20', 'above_20'):
            weight = 'till_20'

        # Re-check stock right before booking (race condition guard)
        available, blocking_item = bundle_stock_available(bundle)
        if not available:
            flash(f'Bundle sold out — {blocking_item} is no longer available.', 'danger')
            return redirect(url_for('bundle_detail', bundle_id=bundle_id))

        calc = calculate_bundle_total(bundle, start_date, end_date, area, weight)

        first_item = bundle.bundle_items[0].item if bundle.bundle_items else None
        if not first_item:
            flash('Bundle has no items. Cannot book.', 'danger')
            return redirect(url_for('bundle_detail', bundle_id=bundle_id))

        # Parent booking — main container
        parent = Booking(
            booking_reference=generate_booking_reference(),
            customer_id=current_user.id,
            item_id=first_item.id,
            bundle_id=bundle.id,
            parent_booking_id=None,
            order_type='bundle',
            start_date=start_date,
            start_time=start_time,
            end_date=end_date,
            end_time=end_time,
            quantity=1,
            venue_address=venue_address,
            delivery_area=area,
            weight_category=weight,
            base_rent=calc['base_rent'],
            commission=calc['commission'],
            deposit=calc['deposit'],
            transport_fee=calc['transport_fee'],
            gateway_charge=calc['gateway_charge'],
            installation_fee=calc['installation_fee'],
            total_amount=calc['total'],
            utr_number=utr,
            payment_status='pending',
            booking_status='confirmed',
            kyc_required=(calc['total'] >= 30000),
        )
        db.session.add(parent)
        db.session.flush()  # get parent.id

        # Child bookings — one per item, zero financials, stock decrement
        for bi in bundle.bundle_items:
            item = bi.item
            if not item:
                continue
            child = Booking(
                booking_reference=generate_booking_reference(),
                customer_id=current_user.id,
                item_id=item.id,
                bundle_id=bundle.id,
                parent_booking_id=parent.id,
                order_type='bundle_child',
                start_date=start_date,
                start_time=start_time,
                end_date=end_date,
                end_time=end_time,
                quantity=bi.quantity,
                venue_address=venue_address,
                delivery_area=area,
                weight_category=weight,
                base_rent=0,
                commission=0,
                deposit=0,
                transport_fee=0,
                total_amount=0,
                utr_number=utr,
                payment_status='pending',
                booking_status='confirmed',
            )
            db.session.add(child)

            # Stock decrement
            item.stock = (item.stock or 0) - bi.quantity
            if item.stock <= 0:
                item.stock = 0
                item.is_available = False

        db.session.commit()

        # Notification to vendor
        try:
            create_notification(
                bundle.vendor_id, 'booking', 'New Bundle Booking',
                f'{parent.booking_reference} — {bundle.name} ({bundle.items_count} items)',
                f'/my-booking/{parent.id}'
            )
            db.session.commit()
        except Exception:
            pass

        # Telegram to vendor
        try:
            vendor = bundle.vendor
            if vendor and vendor.telegram_chat_id:
                send_telegram_notification_async(
                    vendor.telegram_chat_id,
                    f"🎁 New Bundle Booking\n\nBundle: {bundle.name}\n"
                    f"Items: {bundle.items_count}\nRef: {parent.booking_reference}\n"
                    f"Dates: {start_date.strftime('%d %b')} → {end_date.strftime('%d %b %Y')}\n"
                    f"Total: ₹{parent.total_amount}"
                )
        except Exception:
            pass

        flash(f'🎉 Bundle booked! Reference: {parent.booking_reference}', 'success')
        return redirect(url_for('my_booking_detail', booking_id=parent.id))

    return render_template(
        'bundle_book.html',
        bundle=bundle,
        individual_sum=sum((bi.item.rate_per_day * bi.quantity) for bi in bundle.bundle_items if bi.item),
    )


# ============================================
# BATCH 9A — BUNDLE ROUTES
# ============================================

def _parse_bundle_items(item_ids, item_qtys):
    """Return (valid_items, error) — valid_items = [(Item, qty)], all owned by current vendor."""
    valid = []
    seen = set()
    for i, iid in enumerate(item_ids):
        if not iid:
            continue
        try:
            iid_int = int(iid)
            qty = int(item_qtys[i]) if i < len(item_qtys) else 1
        except (ValueError, TypeError):
            continue
        if qty < 1:
            qty = 1
        if iid_int in seen:
            continue
        item = Item.query.get(iid_int)
        if not item or item.vendor_id != current_user.id:
            return [], 'Invalid item in bundle.'
        seen.add(iid_int)
        valid.append((item, qty))
    return valid, None


def _read_bundle_pricing():
    """Return (pricing_type, fixed_price, discount_percent) or raise ValueError."""
    pricing_type = request.form.get('pricing_type', 'discount')
    if pricing_type not in ('fixed', 'discount'):
        pricing_type = 'discount'
    fixed_price = None
    discount_percent = None
    if pricing_type == 'fixed':
        fp = request.form.get('fixed_price', '').strip()
        fixed_price = float(fp) if fp else None
    else:
        dp = request.form.get('discount_percent', '').strip()
        discount_percent = float(dp) if dp else 10.0
    return pricing_type, fixed_price, discount_percent


def _upload_bundle_image():
    if 'image_file' in request.files and request.files['image_file'].filename:
        file = request.files['image_file']
        if file and allowed_file(file.filename):
            try:
                result = cloudinary.uploader.upload(file, folder='vyahmandap/bundles/')
                return result.get('secure_url')
            except Exception as e:
                print(f'Bundle image upload failed: {e}')
    return None


@app.route('/bundles')
def bundles_list():
    bundles = (Bundle.query.filter_by(is_active=True)
               .order_by(Bundle.created_at.desc()).all())
    return render_template('bundles.html', bundles=bundles)


@app.route('/bundle/<int:bundle_id>')
def bundle_detail(bundle_id):
    bundle = Bundle.query.get_or_404(bundle_id)
    if not bundle.is_active:
        flash('This bundle is not available.', 'warning')
        return redirect(url_for('bundles_list'))

    rv_bundles = session.get('recently_viewed_bundles', [])
    if bundle_id in rv_bundles:
        rv_bundles.remove(bundle_id)
    rv_bundles.insert(0, bundle_id)
    session['recently_viewed_bundles'] = rv_bundles[:10]

    available, blocking_item = bundle_stock_available(bundle)
    individual_sum = sum(
        (bi.item.rate_per_day * bi.quantity) for bi in bundle.bundle_items if bi.item
    )

    return render_template(
        'bundle_detail.html',
        bundle=bundle,
        available=available,
        blocking_item=blocking_item,
        individual_sum=individual_sum,
    )


@app.route('/vendor/bundles')
@login_required
def vendor_bundles():
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    bundles = (Bundle.query.filter_by(vendor_id=current_user.id)
               .order_by(Bundle.created_at.desc()).all())
    return render_template('vendor/bundles.html', bundles=bundles)


@app.route('/vendor/bundles/new', methods=['GET', 'POST'])
@login_required
def vendor_bundle_new():
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        tag = request.form.get('tag', 'custom').strip()
        description = request.form.get('description', '').strip()
        try:
            pricing_type, fixed_price, discount_percent = _read_bundle_pricing()
        except (ValueError, TypeError):
            flash('Invalid pricing value.', 'danger')
            return redirect(url_for('vendor_bundle_new'))

        if not name:
            flash('Bundle name is required.', 'danger')
            return redirect(url_for('vendor_bundle_new'))

        valid_items, err = _parse_bundle_items(
            request.form.getlist('item_ids'), request.form.getlist('item_qtys'))
        if err:
            flash(err, 'danger')
            return redirect(url_for('vendor_bundle_new'))
        if not valid_items:
            flash('Select at least one item for the bundle.', 'danger')
            return redirect(url_for('vendor_bundle_new'))

        image_url = request.form.get('image_url', '').strip() or None
        image_url = _upload_bundle_image() or image_url

        try:
            installation_fee = float(request.form.get('installation_fee', 0) or 0)
        except (ValueError, TypeError):
            installation_fee = 0
        if installation_fee < 0:
            installation_fee = 0

        bundle = Bundle(
            vendor_id=current_user.id, name=name, tag=tag, description=description,
            image_url=image_url, pricing_type=pricing_type, fixed_price=fixed_price,
            discount_percent=discount_percent, installation_fee=installation_fee,
            is_active=True,
        )
        db.session.add(bundle)
        db.session.flush()

        for item, qty in valid_items:
            db.session.add(BundleItem(bundle_id=bundle.id, item_id=item.id, quantity=qty))

        db.session.commit()
        flash('Bundle created successfully!', 'success')
        return redirect(url_for('vendor_bundles'))

    vendor_items = (Item.query.filter_by(vendor_id=current_user.id)
                    .order_by(Item.title.asc()).all())
    return render_template('vendor/bundle_form.html', bundle=None, vendor_items=vendor_items)


@app.route('/vendor/bundles/<int:bundle_id>/edit', methods=['GET', 'POST'])
@login_required
def vendor_bundle_edit(bundle_id):
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    bundle = Bundle.query.get_or_404(bundle_id)
    if bundle.vendor_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_bundles'))

    if request.method == 'POST':
        try:
            pricing_type, fixed_price, discount_percent = _read_bundle_pricing()
        except (ValueError, TypeError):
            flash('Invalid pricing value.', 'danger')
            return redirect(url_for('vendor_bundle_edit', bundle_id=bundle_id))

        name = request.form.get('name', '').strip()
        if not name:
            flash('Bundle name is required.', 'danger')
            return redirect(url_for('vendor_bundle_edit', bundle_id=bundle_id))

        # Validate items BEFORE touching the existing ones
        valid_items, err = _parse_bundle_items(
            request.form.getlist('item_ids'), request.form.getlist('item_qtys'))
        if err or not valid_items:
            flash(err or 'Select at least one item for the bundle.', 'danger')
            return redirect(url_for('vendor_bundle_edit', bundle_id=bundle_id))

        bundle.name = name
        bundle.tag = request.form.get('tag', 'custom').strip()
        bundle.description = request.form.get('description', '').strip()
        bundle.pricing_type = pricing_type
        bundle.fixed_price = fixed_price
        bundle.discount_percent = discount_percent
        try:
            ifee = float(request.form.get('installation_fee', 0) or 0)
        except (ValueError, TypeError):
            ifee = 0
        bundle.installation_fee = max(0, ifee)
        bundle.is_active = bool(request.form.get('is_active'))

        # Update items — delete old, add new
        BundleItem.query.filter_by(bundle_id=bundle.id).delete()
        for item, qty in valid_items:
            db.session.add(BundleItem(bundle_id=bundle.id, item_id=item.id, quantity=qty))

        new_url = request.form.get('image_url', '').strip()
        if new_url:
            bundle.image_url = new_url
        uploaded = _upload_bundle_image()
        if uploaded:
            bundle.image_url = uploaded

        db.session.commit()
        flash('Bundle updated.', 'success')
        return redirect(url_for('vendor_bundles'))

    vendor_items = (Item.query.filter_by(vendor_id=current_user.id)
                    .order_by(Item.title.asc()).all())
    return render_template('vendor/bundle_form.html',
                           bundle=bundle, vendor_items=vendor_items)


@app.route('/vendor/bundles/<int:bundle_id>/toggle', methods=['POST'])
@login_required
def vendor_bundle_toggle(bundle_id):
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    bundle = Bundle.query.get_or_404(bundle_id)
    if bundle.vendor_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_bundles'))
    bundle.is_active = not bundle.is_active
    db.session.commit()
    flash(f"Bundle {'activated' if bundle.is_active else 'deactivated'}.", 'success')
    return redirect(url_for('vendor_bundles'))


@app.route('/vendor/bundles/<int:bundle_id>/delete', methods=['POST'])
@login_required
def vendor_bundle_delete(bundle_id):
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    bundle = Bundle.query.get_or_404(bundle_id)
    if bundle.vendor_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_bundles'))
    db.session.delete(bundle)
    db.session.commit()
    flash('Bundle deleted.', 'success')
    return redirect(url_for('vendor_bundles'))


# ============================================
# BATCH 8 — SELL ITEMS (28 Sep 2026)
# ============================================

def _apply_sale_fields(item):
    """Batch 8 — read is_for_sale / sale_price from the vendor item form."""
    item.is_for_sale = bool(request.form.get('is_for_sale'))
    try:
        sp = request.form.get('sale_price', '').strip()
        item.sale_price = float(sp) if sp else None
    except (ValueError, TypeError):
        item.sale_price = None
    if not item.sale_price or item.sale_price <= 0:
        item.sale_price = None
        item.is_for_sale = False


def generate_user_public_id(role):
    """Generate unique public ID like VM-C-00001. Scales to 99,999 per role."""
    prefix = {'customer': 'C', 'vendor': 'V', 'admin': 'A'}.get(role, 'U')
    count = User.query.filter_by(role=role).count() + 1
    for _ in range(200):
        pid = f'VM-{prefix}-{count:05d}'
        if not User.query.filter_by(public_id=pid).first():
            return pid
        count += 1
    for _ in range(10):
        pid = f'VM-{prefix}-{random.randint(10000, 99999)}'
        if not User.query.filter_by(public_id=pid).first():
            return pid
    return None


def calculate_sos_total(item, start_date, quantity, area, weight_category):
    days = 1
    base_rent = round(item.rate_per_day * quantity, 2)
    sos_fee = round(base_rent * Config.SOS_FEE_RATE, 2)
    commission = round(base_rent * Config.COMMISSION_RATE, 2)
    deposit = round(base_rent * Config.DEPOSIT_RATE, 2)
    gateway_charge = round(base_rent * Config.GATEWAY_RATE, 2)
    transport_fee = 0  # Transport excluded
    total = round(base_rent + sos_fee + commission + deposit + gateway_charge, 2)
    vendor_bonus = round(base_rent * Config.SOS_VENDOR_BONUS_RATE, 2)
    return {
        'days': days, 'base_rent': base_rent, 'sos_fee': sos_fee,
        'commission': commission, 'deposit': deposit, 'gateway_charge': gateway_charge,
        'transport_fee': transport_fee, 'total': total, 'vendor_bonus': vendor_bonus,
    }


def get_cart():
    return session.get('cart', [])


def save_cart(cart):
    session['cart'] = cart
    session.modified = True


def cart_count():
    return sum(c.get('quantity', 1) for c in get_cart())


def _cart_item_ids():
    return [c['item_id'] for c in get_cart()]


def calculate_cart_total(start_date, end_date, area, weight_category):
    """Compute combined totals for all cart items."""
    cart = get_cart()
    if not cart:
        return None
    days = (end_date - start_date).days + 1
    if days < 1:
        days = 1

    item_details = []
    rental_base = 0.0
    sale_base = 0.0
    cart_items = []

    for c in cart:
        item = Item.query.get(c['item_id'])
        if not item:
            continue
        qty = c.get('quantity', 1)
        if item.is_for_sale and item.sale_price and c.get('type') == 'sale':
            sub = round((item.sale_price or 0) * qty, 2)
            sale_base += sub
            item_details.append({'item': item, 'quantity': qty,
                                 'subtotal': sub, 'type': 'sale', 'days': 0})
        else:
            sub = round((item.rate_per_day or 0) * days * qty, 2)
            rental_base += sub
            item_details.append({'item': item, 'quantity': qty,
                                 'subtotal': sub, 'type': 'rent', 'days': days})
        cart_items.append((item, qty))

    base_rent = round(rental_base + sale_base, 2)
    commission = round(base_rent * Config.COMMISSION_RATE, 2)
    deposit = round(rental_base * Config.DEPOSIT_RATE, 2)
    gateway_charge = round(base_rent * Config.GATEWAY_RATE, 2)
    transport_fee = 0  # Transport excluded
    total = round(base_rent + commission + deposit + gateway_charge, 2)

    return {
        'days': days,
        'rental_base': round(rental_base, 2),
        'sale_base': round(sale_base, 2),
        'base_rent': base_rent,
        'commission': commission,
        'deposit': deposit,
        'gateway_charge': gateway_charge,
        'transport_fee': transport_fee,
        'total': total,
        'item_details': item_details,
        'cart_items': cart_items,
    }


def calculate_sale(item, quantity, area, weight_category):
    """Batch 8 — Sale calculation (no deposit, no dates)."""
    base = round((item.sale_price or 0) * quantity, 2)
    commission = round(base * Config.COMMISSION_RATE, 2)
    transport_fee = 0  # Transport excluded
    gateway_charge = round(base * Config.GATEWAY_RATE, 2)
    total = base + commission + gateway_charge
    return {
        'base_rent': base,
        'commission': commission,
        'deposit': 0,
        'transport_fee': transport_fee,
        'gateway_charge': gateway_charge,
        'total': total,
    }


# ============================================
# SOS — Emergency Booking
# ============================================
@app.route('/sos/create/<int:item_id>', methods=['GET', 'POST'])
@limiter.limit("10 per hour", methods=["POST"])
@login_required
def sos_create(item_id):
    item = Item.query.get_or_404(item_id)
    if not item.is_available or (item.stock or 0) < 1:
        flash('Item out of stock.', 'danger')
        return redirect(url_for('item_detail', item_id=item_id))

    if request.method == 'POST':
        # SOS is always today — server-set
        start_date = ist_today()

        try:
            quantity = int(request.form.get('quantity', 1))
        except (ValueError, TypeError):
            quantity = 1
        if quantity < 1:
            quantity = 1
        if quantity > (item.stock or 0):
            flash(f'Only {item.stock} available.', 'danger')
            return redirect(url_for('sos_create', item_id=item_id))

        venue = request.form.get('venue_address', '').strip()
        if not venue:
            flash('Venue address required.', 'danger')
            return redirect(url_for('sos_create', item_id=item_id))

        existing = SOSRequest.query.filter_by(
            customer_id=current_user.id, item_id=item.id, status='active'
        ).first()
        if existing:
            flash('You already have an active SOS for this item.', 'warning')
            return redirect(url_for('sos_list'))

        now = datetime.utcnow()
        SOSRequest.query.filter(
            SOSRequest.status == 'active',
            SOSRequest.expires_at < now
        ).update({'status': 'expired'}, synchronize_session=False)
        db.session.commit()

        ref = 'SOS' + ''.join(random.choices(string.digits, k=8))
        req = SOSRequest(
            reference=ref,
            customer_id=current_user.id,
            item_id=item.id,
            city=(current_user.city or 'Harda'),
            start_date=start_date,
            start_time=request.form.get('start_time', '10:00'),
            quantity=quantity,
            venue_address=venue,
            notes=request.form.get('notes', '').strip() or None,
            status='active',
            expires_at=now + timedelta(minutes=Config.SOS_DURATION_MINUTES),
        )
        db.session.add(req)
        db.session.commit()

        vendors = User.query.filter(
            User.role == 'vendor',
            User.city == req.city,
            User.sos_available == True
        ).all()
        for v in vendors:
            create_notification(v.id, 'sos', '🚨 New SOS Request',
                                f'{item.title} — {req.city} — expire in {Config.SOS_DURATION_MINUTES}m',
                                url_for('vendor_sos'))
        db.session.commit()

        flash(f'🚨 SOS sent to {len(vendors)} vendor(s) in {req.city}.', 'success')
        return redirect(url_for('sos_list'))

    return render_template('sos_create.html', item=item, today=ist_today())

@app.route('/sos')
@login_required
def sos_list():
    now = datetime.utcnow()
    SOSRequest.query.filter(
        SOSRequest.status == 'active', SOSRequest.expires_at < now
    ).update({'status': 'expired'}, synchronize_session=False)
    db.session.commit()
    my_sos = SOSRequest.query.filter_by(customer_id=current_user.id)\
        .order_by(SOSRequest.created_at.desc()).limit(50).all()
    return render_template('sos_list.html', requests=my_sos, now=now)


@app.route('/sos/<int:sos_id>/cancel', methods=['POST'])
@login_required
def sos_cancel(sos_id):
    req = SOSRequest.query.get_or_404(sos_id)
    if req.customer_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('sos_list'))
    if req.status != 'active':
        flash('Cannot cancel.', 'warning')
        return redirect(url_for('sos_list'))
    req.status = 'cancelled'
    db.session.commit()
    flash('SOS cancelled.', 'info')
    return redirect(url_for('sos_list'))


@app.route('/vendor/sos')
@login_required
def vendor_sos():
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    now = datetime.utcnow()
    SOSRequest.query.filter(
        SOSRequest.status == 'active', SOSRequest.expires_at < now
    ).update({'status': 'expired'}, synchronize_session=False)
    db.session.commit()
    city = current_user.city or 'Harda'
    active_reqs = SOSRequest.query.filter(
        SOSRequest.city == city,
        SOSRequest.status == 'active',
        SOSRequest.expires_at > now
    ).order_by(SOSRequest.created_at.desc()).all()
    my_accepted = SOSRequest.query.filter(
        SOSRequest.accepted_by == current_user.id
    ).order_by(SOSRequest.accepted_at.desc()).limit(20).all()
    return render_template('vendor/sos.html',
                           active_reqs=active_reqs,
                           my_accepted=my_accepted,
                           now=now)


@app.route('/vendor/sos/toggle', methods=['POST'])
@login_required
def vendor_sos_toggle():
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    current_user.sos_available = not current_user.sos_available
    db.session.commit()
    flash(f"SOS alerts {'ON' if current_user.sos_available else 'OFF'}.", 'success')
    return redirect(url_for('vendor_sos'))


@app.route('/sos/<int:sos_id>/accept', methods=['POST'])
@login_required
def sos_accept(sos_id):
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    req = SOSRequest.query.get_or_404(sos_id)
    if req.status != 'active':
        flash('SOS is no longer active.', 'warning')
        return redirect(url_for('vendor_sos'))
    if req.expires_at < datetime.utcnow():
        req.status = 'expired'
        db.session.commit()
        flash('SOS expired.', 'warning')
        return redirect(url_for('vendor_sos'))
    if req.item.vendor_id != current_user.id:
        flash('You can only accept SOS for your own items.', 'danger')
        return redirect(url_for('vendor_sos'))

    # Calculate SOS pricing
    calc = calculate_sos_total(
        req.item, req.start_date, req.quantity, 'city', 'till_20'
    )

    # Auto-create Booking with SOS pricing
    booking = Booking(
        booking_reference=generate_booking_reference(),
        customer_id=req.customer_id,
        item_id=req.item_id,
        start_date=req.start_date,
        start_time=req.start_time or '10:00',
        end_date=req.start_date,
        end_time='20:00',
        quantity=req.quantity,
        venue_address=req.venue_address,
        delivery_area='city',
        weight_category='till_20',
        base_rent=calc['base_rent'],
        commission=calc['commission'],
        deposit=calc['deposit'],
        transport_fee=calc['transport_fee'],
        gateway_charge=calc['gateway_charge'],
        sos_fee=calc['sos_fee'],
        sos_bonus=calc['vendor_bonus'],
        total_amount=calc['total'],
        utr_number='SOS-PENDING',
        payment_status='pending',
        booking_status='pending',
        order_type='sos',
    )
    db.session.add(booking)
    db.session.flush()

    # Reduce stock
    req.item.stock = max(0, (req.item.stock or 0) - req.quantity)
    if req.item.stock <= 0:
        req.item.is_available = False

    req.status = 'accepted'
    req.accepted_by = current_user.id
    req.accepted_at = datetime.utcnow()
    req.booking_id = booking.id
    db.session.commit()

    # Notify customer
    try:
        create_notification(
            req.customer_id, 'sos', '✅ SOS Accepted — Pay Now',
            f'{current_user.name} accepted your SOS {req.reference}. '
            f'Complete payment ₹{calc["total"]:.0f} to confirm.',
            url_for('my_booking_detail', booking_id=booking.id)
        )
        db.session.commit()
    except Exception:
        pass

    flash(f'✅ SOS accepted! Customer notified for payment (₹{calc["total"]:.0f}).', 'success')
    return redirect(url_for('vendor_sos'))


@app.route('/sw.js')
def service_worker():
    """Serve service worker from root so it controls the whole origin."""
    sw_path = os.path.join(app.static_folder, 'sw.js')
    if not os.path.exists(sw_path):
        return Response('// no sw', mimetype='application/javascript')
    with open(sw_path, 'r', encoding='utf-8') as f:
        content = f.read()
    resp = Response(content, mimetype='application/javascript')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Service-Worker-Allowed'] = '/'
    return resp


@app.route('/api/push/vapid-public-key')
def api_push_vapid_key():
    return jsonify({'publicKey': Config.VAPID_PUBLIC_KEY or ''})


@app.route('/api/push/subscribe', methods=['POST'])
@login_required
def api_push_subscribe():
    data = request.get_json() or {}
    endpoint = (data.get('endpoint') or '').strip()
    keys = data.get('keys') or {}
    p256dh = (keys.get('p256dh') or '').strip()
    auth = (keys.get('auth') or '').strip()

    if not endpoint or not p256dh or not auth:
        return jsonify({'error': 'Invalid subscription data'}), 400

    try:
        existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
        if existing:
            existing.user_id = current_user.id
            existing.p256dh = p256dh
            existing.auth = auth
        else:
            sub = PushSubscription(
                user_id=current_user.id,
                endpoint=endpoint,
                p256dh=p256dh,
                auth=auth,
                user_agent=request.headers.get('User-Agent', '')[:300],
            )
            db.session.add(sub)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Subscribe failed: {e}')
        return jsonify({'error': 'Save failed'}), 500


@app.route('/api/push/unsubscribe', methods=['POST'])
@login_required
def api_push_unsubscribe():
    data = request.get_json() or {}
    endpoint = (data.get('endpoint') or '').strip()
    if not endpoint:
        return jsonify({'error': 'Missing endpoint'}), 400
    try:
        PushSubscription.query.filter_by(
            user_id=current_user.id, endpoint=endpoint
        ).delete(synchronize_session=False)
        db.session.commit()
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Delete failed'}), 500


@app.route('/api/push/test', methods=['POST'])
@login_required
def api_push_test():
    sent = send_push_notification(
        current_user.id,
        'Test Notification',
        'VyahMandap push notifications are working! 🎉',
        url_for('notifications_page')
    )
    return jsonify({'sent': sent})


@app.route('/api/sos/pending')
@login_required
def api_sos_pending():
    if current_user.role not in ('vendor', 'admin'):
        return jsonify({'count': 0, 'items': []})
    now = datetime.utcnow()
    city = current_user.city or 'Harda'
    reqs = SOSRequest.query.filter(
        SOSRequest.city == city,
        SOSRequest.status == 'active',
        SOSRequest.expires_at > now
    ).order_by(SOSRequest.created_at.desc()).limit(10).all()
    # Only show SOS for vendor's own items
    reqs = [r for r in reqs if r.item and r.item.vendor_id == current_user.id]
    return jsonify({
        'count': len(reqs),
        'items': [{
            'id': r.id, 'reference': r.reference,
            'item_title': r.item.title if r.item else '',
            'customer': r.customer.name if r.customer else '',
            'quantity': r.quantity,
            'venue': r.venue_address[:60],
            'created_at': _time_ago(r.created_at),
        } for r in reqs]
    })


@app.route('/cart')
@login_required
def cart_view():
    cart = get_cart()
    items = []
    for c in cart:
        item = Item.query.get(c['item_id'])
        if item:
            items.append({'item': item, 'quantity': c.get('quantity', 1),
                          'type': c.get('type', 'rent')})
    return render_template('cart.html', cart_items=items)


@app.route('/cart/add/<int:item_id>', methods=['POST'])
@login_required
def cart_add(item_id):
    item = Item.query.get_or_404(item_id)
    try:
        qty = int(request.form.get('quantity', 1))
    except (ValueError, TypeError):
        qty = 1
    if qty < 1:
        qty = 1
    if qty > (item.stock or 0):
        flash(f'Only {item.stock} unit(s) available.', 'danger')
        return redirect(request.referrer or url_for('item_detail', item_id=item_id))

    # Determine type: sale if item is_for_sale, else rent
    itype = 'sale' if item.is_for_sale and item.sale_price else 'rent'

    cart = get_cart()
    found = False
    for c in cart:
        if c['item_id'] == item_id and c.get('type') == itype:
            c['quantity'] = min(c['quantity'] + qty, item.stock or 1)
            found = True
            break
    if not found:
        cart.append({'item_id': item_id, 'quantity': qty, 'type': itype})
    save_cart(cart)
    flash(f'✅ {item.title} added to cart.', 'success')
    return redirect(request.referrer or url_for('cart_view'))


@app.route('/cart/remove/<int:item_id>', methods=['POST'])
@login_required
def cart_remove(item_id):
    cart = [c for c in get_cart() if c['item_id'] != item_id]
    save_cart(cart)
    flash('Item removed from cart.', 'info')
    return redirect(url_for('cart_view'))


@app.route('/cart/update', methods=['POST'])
@login_required
def cart_update():
    cart = get_cart()
    for c in cart:
        key = f"qty_{c['item_id']}"
        if key in request.form:
            try:
                q = int(request.form.get(key, c['quantity']))
            except (ValueError, TypeError):
                q = c['quantity']
            item = Item.query.get(c['item_id'])
            max_q = (item.stock or 1) if item else 1
            c['quantity'] = max(1, min(q, max_q))
    save_cart(cart)
    flash('Cart updated.', 'success')
    return redirect(url_for('cart_view'))


@app.route('/cart/clear', methods=['POST'])
@login_required
def cart_clear():
    session.pop('cart', None)
    flash('Cart cleared.', 'info')
    return redirect(url_for('cart_view'))


@app.route('/api/cart/calculate', methods=['POST'])
@login_required
def api_cart_calculate():
    data = request.get_json() or {}
    try:
        start_date = datetime.strptime(data.get('start_date'), '%Y-%m-%d').date()
        end_date = datetime.strptime(data.get('end_date'), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return jsonify({'error': True, 'message': 'Invalid dates.',
                        'conflict_type': 'validation_error'}), 400
    area = data.get('area', 'city')
    weight = data.get('weight', 'till_20')
    if area not in ('city', 'outskirts'):
        area = 'city'
    if weight not in ('till_20', 'above_20'):
        weight = 'till_20'

    today = ist_today()
    if start_date < today:
        return jsonify({'error': True, 'message': 'Start date cannot be past.',
                        'conflict_type': 'validation_error'}), 400
    if end_date < start_date:
        return jsonify({'error': True, 'message': 'End date must be after start.',
                        'conflict_type': 'validation_error'}), 400

    result = calculate_cart_total(start_date, end_date, area, weight)
    if not result:
        return jsonify({'error': True, 'message': 'Cart is empty.',
                        'conflict_type': 'validation_error'}), 400
    # Strip non-serializable
    return jsonify({
        'days': result['days'],
        'rental_base': result['rental_base'],
        'sale_base': result['sale_base'],
        'base_rent': result['base_rent'],
        'commission': result['commission'],
        'deposit': result['deposit'],
        'gateway_charge': result['gateway_charge'],
        'transport_fee': result['transport_fee'],
        'total': result['total'],
        'has_rentals': result['rental_base'] > 0,
    })


@app.route('/cart/checkout', methods=['GET', 'POST'])
@limiter.limit("20 per minute", methods=["POST"])
@login_required
def cart_checkout():
    cart = get_cart()
    if not cart:
        flash('Your cart is empty.', 'warning')
        return redirect(url_for('cart_view'))

    # Verify all items still exist + stock
    for c in cart:
        item = Item.query.get(c['item_id'])
        if not item:
            flash('An item in your cart no longer exists.', 'danger')
            return redirect(url_for('cart_view'))
        if not item.is_available or (item.stock or 0) < c.get('quantity', 1):
            flash(f'{item.title} is out of stock.', 'danger')
            return redirect(url_for('cart_view'))

    has_rentals = any(c.get('type') == 'rent' for c in cart)

    payment_config = AdminPaymentConfig.get()
    qr_base64 = None
    if payment_config.upi_id:
        qr_base64 = generate_upi_qr_base64(payment_config.upi_id,
                                           payment_config.upi_name, amount=None)

    if request.method == 'POST':
        from datetime import date as _date
        # Dates
        if has_rentals:
            try:
                start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
                end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()
            except (ValueError, TypeError):
                flash('Valid start/end dates required.', 'danger')
                return redirect(url_for('cart_checkout'))
            today = ist_today()
            if start_date < today or end_date < start_date:
                flash('Invalid dates.', 'danger')
                return redirect(url_for('cart_checkout'))
        else:
            start_date = end_date = _date.today()

        area = request.form.get('area', 'city')
        weight = request.form.get('weight', 'till_20')
        if area not in ('city', 'outskirts'): area = 'city'
        if weight not in ('till_20', 'above_20'): weight = 'till_20'

        venue = request.form.get('address', '').strip()
        utr = request.form.get('utr', '').strip()
        if not venue:
            flash('Venue address required.', 'danger')
            return redirect(url_for('cart_checkout'))
        if not utr:
            flash('UTR required.', 'danger')
            return redirect(url_for('cart_checkout'))

        screenshot = request.files.get('screenshot')
        if not screenshot or not screenshot.filename:
            flash('Payment screenshot is required.', 'danger')
            return redirect(url_for('cart_checkout'))
        if not Config.CLOUDINARY_CLOUD_NAME:
            flash('Cloudinary not configured.', 'danger')
            return redirect(url_for('cart_checkout'))

        calc = calculate_cart_total(start_date, end_date, area, weight)
        if not calc or not calc['cart_items']:
            flash('Cart empty.', 'danger')
            return redirect(url_for('cart_view'))

        first_item = calc['cart_items'][0][0]
        kyc_required = calc['total'] >= 30000

        # Parent booking
        parent = Booking(
            booking_reference=generate_booking_reference(),
            customer_id=current_user.id,
            item_id=first_item.id,
            parent_booking_id=None,
            order_type='cart',
            start_date=start_date,
            start_time=request.form.get('start_time', '10:00'),
            end_date=end_date,
            end_time=request.form.get('end_time', '20:00'),
            quantity=1,
            venue_address=venue,
            delivery_area=area,
            weight_category=weight,
            base_rent=calc['base_rent'],
            commission=calc['commission'],
            deposit=calc['deposit'],
            transport_fee=calc['transport_fee'],
            gateway_charge=calc['gateway_charge'],
            total_amount=calc['total'],
            utr_number=utr,
            payment_status='pending',
            booking_status='confirmed',
            kyc_required=kyc_required,
        )
        db.session.add(parent)
        db.session.flush()

        # Children — one per item
        for detail in calc['item_details']:
            item = detail['item']
            qty = detail['quantity']
            itype = detail['type']
            if itype == 'sale':
                # Stock permanently reduced
                item.stock = max(0, (item.stock or 0) - qty)
                if item.stock <= 0:
                    item.stock = 0
                    item.is_available = False
                child = Booking(
                    booking_reference=generate_booking_reference(),
                    customer_id=current_user.id,
                    item_id=item.id,
                    parent_booking_id=parent.id,
                    order_type='sale',
                    start_date=start_date, end_date=end_date,
                    start_time='10:00', end_time='20:00',
                    quantity=qty,
                    venue_address=venue,
                    delivery_area=area, weight_category=weight,
                    base_rent=0, commission=0, deposit=0,
                    transport_fee=0, gateway_charge=0,
                    total_amount=0, utr_number=utr,
                    payment_status='pending', booking_status='confirmed',
                )
            else:
                # Rental child
                item.stock = max(0, (item.stock or 0) - qty)
                if item.stock <= 0:
                    item.stock = 0
                    item.is_available = False
                child = Booking(
                    booking_reference=generate_booking_reference(),
                    customer_id=current_user.id,
                    item_id=item.id,
                    parent_booking_id=parent.id,
                    order_type='cart_child',
                    start_date=start_date, end_date=end_date,
                    start_time=request.form.get('start_time', '10:00'),
                    end_time=request.form.get('end_time', '20:00'),
                    quantity=qty,
                    venue_address=venue,
                    delivery_area=area, weight_category=weight,
                    base_rent=0, commission=0, deposit=0,
                    transport_fee=0, gateway_charge=0,
                    total_amount=0, utr_number=utr,
                    payment_status='pending', booking_status='confirmed',
                )
            db.session.add(child)

        # Payment proof on parent
        folder = f"vyahmandap/payments/booking_{parent.id}"
        upload_result = upload_file_to_cloudinary(screenshot, folder=folder)

        if not upload_result or not upload_result.get('url'):
            db.session.rollback()
            flash('Screenshot upload failed. Please try again.', 'danger')
            return redirect(url_for('cart_checkout'))

        try:
            amount_paid = float(request.form.get('amount_paid', 0) or 0)
        except (ValueError, TypeError):
            amount_paid = 0
        payment_date = None
        pd = request.form.get('payment_date', '').strip()
        if pd:
            try:
                payment_date = datetime.strptime(pd, '%Y-%m-%d').date()
            except Exception:
                payment_date = None

        proof = PaymentProof(
            booking_id=parent.id,
            uploaded_by=current_user.id,
            method=request.form.get('method', 'upi').strip() or 'upi',
            amount_paid=amount_paid,
            transaction_id=utr or None,
            payment_date=payment_date,
            payer_name=request.form.get('payer_name', '').strip() or None,
            payer_bank=request.form.get('payer_bank', '').strip() or None,
            notes=request.form.get('notes', '').strip() or None,
            screenshot_url=upload_result['url'],
            screenshot_public_id=upload_result.get('public_id'),
            status='pending',
        )
        db.session.add(proof)
        db.session.commit()

        # Notify each vendor
        seen_vendors = set()
        for item, qty in calc['cart_items']:
            if item.vendor_id and item.vendor_id not in seen_vendors:
                seen_vendors.add(item.vendor_id)
                try:
                    create_notification(
                        item.vendor_id, 'booking', 'New Cart Order',
                        f'{parent.booking_reference} — {len(calc["cart_items"])} item(s)',
                        f'/my-booking/{parent.id}'
                    )
                except Exception:
                    pass
        db.session.commit()

        # Clear cart
        session.pop('cart', None)

        flash(f'✅ Order placed! Reference: {parent.booking_reference}', 'success')
        return redirect(url_for('my_booking_detail', booking_id=parent.id))

    # GET
    cart_items_data = []
    for c in cart:
        item = Item.query.get(c['item_id'])
        if item:
            cart_items_data.append({'item': item, 'quantity': c.get('quantity', 1),
                                    'type': c.get('type', 'rent')})
    return render_template('cart_checkout.html',
                           cart_items=cart_items_data,
                           has_rentals=has_rentals,
                           payment_config=payment_config,
                           qr_base64=qr_base64)


@app.route('/buy/<int:item_id>', methods=['GET', 'POST'])
@limiter.limit("20 per minute", methods=["POST"])
@login_required
def buy_item(item_id):
    item = Item.query.get_or_404(item_id)

    if not item.is_for_sale or not item.sale_price:
        flash('This item is not available for purchase.', 'warning')
        return redirect(url_for('item_detail', item_id=item_id))
    if not item.is_available or (item.stock or 0) < 1:
        flash('This item is out of stock.', 'warning')
        return redirect(url_for('item_detail', item_id=item_id))

    payment_config = AdminPaymentConfig.get()
    qr_base64 = None
    if payment_config.upi_id:
        qr_base64 = generate_upi_qr_base64(
            payment_config.upi_id, payment_config.upi_name, amount=None
        )

    if request.method == 'POST':
        try:
            quantity = int(request.form.get('quantity', 1))
        except (ValueError, TypeError):
            quantity = 1
        if quantity < 1:
            quantity = 1
        if quantity > (item.stock or 0):
            flash(f'Only {item.stock} unit(s) available.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id))

        area = request.form.get('delivery_area', 'city')
        weight = request.form.get('weight_category', 'till_20')
        if area not in ('city', 'outskirts'):
            area = 'city'
        if weight not in ('till_20', 'above_20'):
            weight = 'till_20'

        venue = request.form.get('venue_address', '').strip()
        utr = request.form.get('utr_number', '').strip()
        if not venue or not utr:
            flash('Delivery address and UTR are required.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id))

        # Payment proof — mandatory
        screenshot = request.files.get('screenshot')
        if not screenshot or not screenshot.filename:
            flash('Payment screenshot is required.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id))
        if not Config.CLOUDINARY_CLOUD_NAME:
            flash('Cloudinary not configured.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id))

        calc = calculate_sale(item, quantity, area, weight)
        kyc_required = calc['total'] >= 30000

        from datetime import date as _date
        today = _date.today()

        booking = Booking(
            booking_reference=generate_booking_reference(),
            customer_id=current_user.id,
            item_id=item.id,
            start_date=today,
            end_date=today,
            start_time='10:00',
            end_time='20:00',
            quantity=quantity,
            venue_address=venue,
            delivery_area=area,
            weight_category=weight,
            base_rent=calc['base_rent'],
            commission=calc['commission'],
            deposit=0,
            transport_fee=calc['transport_fee'],
            gateway_charge=calc['gateway_charge'],
            total_amount=calc['total'],
            utr_number=utr,
            payment_status='pending',
            booking_status='confirmed',
            order_type='sale',
            kyc_required=kyc_required,
        )

        # Stock permanently reduced (no return)
        item.stock = (item.stock or 0) - quantity
        if item.stock <= 0:
            item.stock = 0
            item.is_available = False

        db.session.add(booking)
        db.session.flush()  # get booking.id

        # Upload screenshot + create PaymentProof
        folder = f"vyahmandap/payments/booking_{booking.id}"
        upload_result = upload_file_to_cloudinary(screenshot, folder=folder)
        if not upload_result or not upload_result.get('url'):
            db.session.rollback()
            flash('Screenshot upload failed. Please try again.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id))

        try:
            amount_paid = float(request.form.get('amount_paid', 0) or 0)
        except (ValueError, TypeError):
            amount_paid = 0
        payment_date = None
        pd_str = request.form.get('payment_date', '').strip()
        if pd_str:
            try:
                payment_date = datetime.strptime(pd_str, '%Y-%m-%d').date()
            except Exception:
                payment_date = None

        proof = PaymentProof(
            booking_id=booking.id,
            uploaded_by=current_user.id,
            method=request.form.get('method', 'upi').strip() or 'upi',
            amount_paid=amount_paid,
            transaction_id=utr or None,
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

        try:
            create_notification(
                item.vendor_id, 'booking', 'New Sale!',
                f'{booking.booking_reference} — {quantity}× {item.title}',
                f'/my-booking/{booking.id}'
            )
            db.session.commit()
        except Exception:
            pass

        try:
            vendor = item.vendor
            if vendor and vendor.telegram_chat_id:
                send_telegram_notification_async(
                    vendor.telegram_chat_id,
                    f"💰 New Sale\n\nItem: {item.title}\nQty: {quantity}\n"
                    f"Ref: {booking.booking_reference}\nAmount: ₹{booking.total_amount}"
                )
        except Exception:
            pass

        flash(f'Purchase confirmed! Reference: {booking.booking_reference}', 'success')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    return render_template('buy.html', item=item,
                           payment_config=payment_config, qr_base64=qr_base64)


# ============================================
# BATCH 7 — SECURITY HEADERS (25 Sep 2026)
# ============================================

@app.after_request
def add_security_headers(response):
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(self), camera=(self), microphone=()'
    if os.environ.get('RENDER'):
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    # CSP — allow self + CDNs used
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.tailwindcss.com "
        "https://cdnjs.cloudflare.com "
        "https://cdn.jsdelivr.net "
        "https://checkout.razorpay.com; "
        "style-src 'self' 'unsafe-inline' "
        "https://fonts.googleapis.com "
        "https://cdnjs.cloudflare.com "
        "https://cdn.jsdelivr.net; "
        "font-src 'self' data: "
        "https://fonts.gstatic.com "
        "https://cdnjs.cloudflare.com; "
        "img-src 'self' data: blob: https:; "
        "media-src 'self' blob: https:; "
        "connect-src 'self' "
        "https://api.cloudinary.com "
        "https://res.cloudinary.com; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers['Content-Security-Policy'] = csp
    return response


# ============================================
# BATCH 4 — ADDITIVE FEATURES (25 Sep 2026)
# ============================================

@app.context_processor
def inject_announcement():
    try:
        ann = Announcement.query.filter_by(is_active=True)\
                .order_by(Announcement.created_at.desc()).first()
        if ann and ann.expires_at and ann.expires_at < datetime.utcnow():
            ann = None
    except Exception:
        ann = None
    return {'active_announcement': ann}


@app.context_processor
def inject_wishlist_count():
    try:
        if current_user.is_authenticated:
            count = Wishlist.query.filter_by(user_id=current_user.id).count()
            return {'wishlist_count': count}
    except Exception:
        pass
    return {'wishlist_count': 0}


def _calculate_refund(booking):
    """Batch 4 — refund calc: deposit + (base_rent × pct)."""
    from datetime import date as _date
    days_until = (booking.start_date - _date.today()).days
    if days_until > 7:
        pct = 1.0
    elif days_until >= 3:
        pct = 0.5
    else:
        pct = 0.0
    refund = (booking.base_rent * pct) + (booking.deposit or 0)
    return round(refund, 2), pct, days_until


@app.route('/booking/<int:booking_id>/cancel', methods=['GET', 'POST'])
@login_required
def booking_cancel(booking_id):
    booking = Booking.query.get_or_404(booking_id)

    # Permission: customer owns it, or admin
    if booking.customer_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    # Eligibility
    if booking.booking_status in ('cancelled', 'completed', 'dispatched'):
        flash('This booking cannot be cancelled.', 'warning')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    from datetime import date as _date
    if booking.start_date < _date.today():
        flash('Cannot cancel a past booking.', 'warning')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    refund_amount, refund_pct, days_until = _calculate_refund(booking)

    if request.method == 'POST':
        reason = request.form.get('reason', '').strip()
        booking.booking_status = 'cancelled'
        booking.cancelled_at = datetime.utcnow()
        booking.cancellation_reason = reason or 'No reason provided'
        booking.refund_amount = refund_amount
        booking.cancelled_by = 'admin' if current_user.role == 'admin' else 'customer'

        # Restore stock
        item = booking.item
        if item:
            item.stock = (item.stock or 0) + (booking.quantity or 1)
            item.is_available = True

        db.session.commit()

        try:
            notify_all_admins(
                f"❌ Booking Cancelled — {booking.booking_reference} by {current_user.role} — Refund ₹{refund_amount}",
                link=url_for('admin_booking_detail', booking_id=booking.id)
            )
        except Exception:
            pass

        # PART J — Event 2: Booking cancelled -> vendor + customer
        create_notification(
            booking.item.vendor_id, 'cancel', 'Booking Cancelled',
            f'{booking.booking_reference} was cancelled. Stock restored.',
            f'/my-booking/{booking.id}'
        )
        if current_user.id != booking.customer_id:
            create_notification(
                booking.customer_id, 'cancel', 'Booking Cancelled',
                f'{booking.booking_reference} cancelled by admin.',
                f'/my-booking/{booking.id}'
            )
        db.session.commit()

        flash(f'Booking cancelled. Eligible refund: ₹{refund_amount}', 'success')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    return render_template(
        'cancel_booking.html',
        booking=booking,
        refund_amount=refund_amount,
        refund_pct=refund_pct,
        days_until=days_until,
    )


@app.route('/admin/announcements')
@login_required
def admin_announcements():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template('admin/announcements.html', announcements=announcements)


@app.route('/admin/announcements/new', methods=['GET', 'POST'])
@login_required
def admin_announcement_new():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        expires_raw = request.form.get('expires_at', '').strip()
        if not message:
            flash('Message required.', 'danger')
            return redirect(url_for('admin_announcement_new'))

        expires_at = None
        if expires_raw:
            try:
                expires_at = datetime.strptime(expires_raw, '%Y-%m-%d')
            except Exception:
                pass

        ann = Announcement(message=message, expires_at=expires_at,
                            is_active=True, created_by=current_user.id)
        db.session.add(ann)
        db.session.commit()
        flash('Announcement created.', 'success')
        return redirect(url_for('admin_announcements'))
    return render_template('admin/announcements.html', announcements=None, show_form=True)


@app.route('/admin/announcements/<int:ann_id>/toggle', methods=['POST'])
@login_required
def admin_announcement_toggle(ann_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    ann = Announcement.query.get_or_404(ann_id)
    ann.is_active = not ann.is_active
    db.session.commit()
    flash(f"Announcement {'activated' if ann.is_active else 'deactivated'}.", 'success')
    return redirect(url_for('admin_announcements'))


@app.route('/admin/announcements/<int:ann_id>/delete', methods=['POST'])
@login_required
def admin_announcement_delete(ann_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    ann = Announcement.query.get_or_404(ann_id)
    db.session.delete(ann)
    db.session.commit()
    flash('Announcement deleted.', 'success')
    return redirect(url_for('admin_announcements'))


@app.route('/vendor/earnings')
@login_required
def vendor_earnings():
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    completed = (Booking.query
                 .join(Item, Booking.item_id == Item.id)
                 .filter(Item.vendor_id == current_user.id,
                         Booking.booking_status == 'completed')
                 .order_by(Booking.created_at.desc())
                 .all())

    total_base_rent = round(sum((b.base_rent or 0) for b in completed), 2)
    total_sos_bonus = round(sum((b.sos_bonus or 0) for b in completed), 2)
    total_earnings = round(total_base_rent + total_sos_bonus, 2)
    total_bookings = len(completed)
    total_deposits = round(sum((b.deposit or 0) for b in completed), 2)

    # Monthly breakdown — last 6 months
    from collections import OrderedDict
    monthly = OrderedDict()
    now = datetime.utcnow()
    for i in range(5, -1, -1):
        y = now.year
        m = now.month - i
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y}-{m:02d}"
        monthly[key] = {'label': datetime(y, m, 1).strftime('%b %Y'),
                         'amount': 0.0, 'count': 0}

    for b in completed:
        key = b.created_at.strftime('%Y-%m')
        if key in monthly:
            monthly[key]['amount'] += (b.base_rent or 0) + (b.sos_bonus or 0)
            monthly[key]['count'] += 1

    monthly_list = [{'key': k, **v} for k, v in monthly.items()]
    max_amount = max([m['amount'] for m in monthly_list] + [1])

    analytics = calculate_vendor_analytics(current_user.id)

    return render_template(
        'vendor/earnings.html',
        total_earnings=total_earnings,
        total_base_rent=total_base_rent,
        total_sos_bonus=total_sos_bonus,
        total_bookings=total_bookings,
        total_deposits=total_deposits,
        completed=completed,
        monthly=monthly_list,
        max_amount=max_amount,
        analytics=analytics,
    )


# ============================================
# BATCH 2 — ADDITIVE FEATURES (24 Sep 2026)
# ============================================

@app.route('/item/<int:item_id>')
def item_detail(item_id):
    item = Item.query.get_or_404(item_id)

    # Track in recently viewed (session)
    rv = session.get('recently_viewed', [])
    if item_id in rv:
        rv.remove(item_id)
    rv.insert(0, item_id)
    session['recently_viewed'] = rv[:10]

    # Analytics — view tracking (with dedup)
    try:
        is_self_view = (
            current_user.is_authenticated and (
                current_user.id == item.vendor_id or current_user.role == 'admin'
            )
        )
        if not is_self_view:
            recent = session.get('recent_views', {})
            now_ts = int(datetime.utcnow().timestamp())
            key = str(item.id)
            if not (key in recent and now_ts - recent[key] < 1800):
                ip = request.headers.get('X-Forwarded-For', request.remote_addr or '')
                if ip and ',' in ip:
                    ip = ip.split(',')[0].strip()
                ip_hash = hashlib.sha256(ip.encode()).hexdigest()[:32] if ip else None
                view = ItemView(
                    item_id=item.id,
                    user_id=current_user.id if current_user.is_authenticated else None,
                    ip_hash=ip_hash,
                )
                db.session.add(view)
                db.session.commit()
                recent[key] = now_ts
                if len(recent) > 30:
                    recent = dict(sorted(recent.items(), key=lambda x: -x[1])[:30])
                session['recent_views'] = recent
    except Exception as e:
        db.session.rollback()
        print(f"View tracking failed: {e}")

    reviews = (Review.query.filter_by(item_id=item_id)
               .order_by(Review.created_at.desc()).limit(5).all())
    all_review_count = Review.query.filter_by(item_id=item_id).count()
    avg_rating = None
    if all_review_count:
        ratings = [r.rating for r in Review.query.filter_by(item_id=item_id).all()]
        avg_rating = round(sum(ratings) / len(ratings), 1)

    in_wishlist = False
    if current_user.is_authenticated:
        in_wishlist = Wishlist.query.filter_by(
            user_id=current_user.id, item_id=item_id
        ).first() is not None

    return render_template(
        'item_detail.html',
        item=item,
        vendor=item.vendor,
        reviews=reviews,
        all_review_count=all_review_count,
        avg_rating=avg_rating,
        in_wishlist=in_wishlist,
    )


@app.route('/wishlist')
@login_required
def wishlist():
    entries = (Wishlist.query.filter_by(user_id=current_user.id)
               .order_by(Wishlist.created_at.desc()).all())
    items = [e.item for e in entries if e.item is not None]
    return render_template('wishlist.html', items=items)


@app.route('/wishlist/add/<int:item_id>', methods=['POST'])
@login_required
def wishlist_add(item_id):
    Item.query.get_or_404(item_id)
    existing = Wishlist.query.filter_by(
        user_id=current_user.id, item_id=item_id
    ).first()
    if not existing:
        db.session.add(Wishlist(user_id=current_user.id, item_id=item_id))
        db.session.commit()
        flash('Added to wishlist.', 'success')
    else:
        flash('Already in your wishlist.', 'info')
    return redirect(request.referrer or url_for('wishlist'))


@app.route('/wishlist/remove/<int:item_id>', methods=['POST'])
@login_required
def wishlist_remove(item_id):
    entry = Wishlist.query.filter_by(
        user_id=current_user.id, item_id=item_id
    ).first()
    if entry:
        db.session.delete(entry)
        db.session.commit()
        flash('Removed from wishlist.', 'success')
    return redirect(request.referrer or url_for('wishlist'))


@app.route('/recently-viewed')
def recently_viewed():
    ids = session.get('recently_viewed', [])
    items = []
    if ids:
        found = Item.query.filter(Item.id.in_(ids)).all()
        item_map = {i.id: i for i in found}
        items = [item_map[i] for i in ids if i in item_map]
    return render_template('recently_viewed.html', items=items)


@app.route('/compare')
def compare():
    ids_param = request.args.get('ids', '')
    ids = []
    for x in ids_param.split(','):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    ids = ids[:4]

    items = []
    item_stats = {}
    if ids:
        found = Item.query.filter(Item.id.in_(ids)).all()
        item_map = {i.id: i for i in found}
        items = [item_map[i] for i in ids if i in item_map]

    for it in items:
        revs = Review.query.filter_by(item_id=it.id).all()
        avg = round(sum(r.rating for r in revs) / len(revs), 1) if revs else None
        item_stats[it.id] = {'avg_rating': avg, 'review_count': len(revs)}

    return render_template('compare.html', items=items, item_stats=item_stats)


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
    response_hours = calculate_vendor_response_time(vendor.id)

    return render_template(
        'vendor_public.html',
        vendor=vendor,
        items=items,
        reviews=reviews,
        avg_rating=avg_rating,
        total_reviews=len(reviews),
        total_bookings=total_bookings,
        verified_item_count=verified_item_count,
        response_hours=response_hours,
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
