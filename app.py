import os
import sys
import re
import html
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
from sqlalchemy.exc import IntegrityError  # UTR unique-index collisions

# Payment QR imports
import qrcode
from io import BytesIO
from urllib.parse import quote, urlencode

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
# CONFIGURATION  (extracted to config.py in Phase 4 / Stage B)
# ============================================
from config import Config, TRANSLATIONS  # noqa: E402



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

# Redis in production (limits shared across gunicorn workers); in-memory locally.
_REDIS_URL = os.environ.get('REDIS_URL', '').strip()
limiter = Limiter(
    app=app,
    key_func=_rate_limit_key,
    default_limits=["500 per minute"],
    storage_uri=_REDIS_URL or "memory://",
)
print("🚦 Rate limiting: Redis" if _REDIS_URL else "🚦 Rate limiting: in-memory (per-worker only)")

try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
except Exception as e:
    print(f"⚠️ Could not create upload folder: {e}")

if 'sqlite' in app.config['SQLALCHEMY_DATABASE_URI']:
    os.makedirs('database', exist_ok=True)

db = SQLAlchemy(app)

# Small cross-request cache (Redis when REDIS_URL is set, else per-process memory).
# Falls back to a no-op cache if Flask-Caching is not installed.
try:
    from flask_caching import Cache
    _cache_url = os.environ.get('REDIS_URL', '').strip()
    if _cache_url:
        app.config['CACHE_TYPE'] = 'RedisCache'
        app.config['CACHE_REDIS_URL'] = _cache_url
    else:
        app.config['CACHE_TYPE'] = 'SimpleCache'
    app.config['CACHE_DEFAULT_TIMEOUT'] = 60
    cache = Cache(app)
except ImportError:
    class _NullCache:
        def get(self, key): return None
        def set(self, key, value, timeout=None): return None
        def delete(self, key): return None
    cache = _NullCache()

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
    # Soft email verification (never blocks a flow). Default True so every non-register
    # creation path (seeding, admin, Google) is treated as verified; register() sets False.
    is_email_verified = db.Column(db.Boolean, default=True)
    email_verify_token = db.Column(db.String(120), nullable=True, index=True)
    email_verify_expires = db.Column(db.DateTime, nullable=True)
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
    # 09 Oct 2026 — per-user KYC (1-year validity, PAN OCR verified)
    kyc_status        = db.Column(db.String(20), default='unverified')  # unverified | verified | expired
    kyc_verified_at   = db.Column(db.DateTime, nullable=True)
    kyc_expires_at    = db.Column(db.DateTime, nullable=True)
    kyc_aadhaar_last4 = db.Column(db.String(4), nullable=True)
    kyc_pan_masked    = db.Column(db.String(10), nullable=True)
    # 10 Oct 2026 — full KYC numbers encrypted at rest (business-ready vault)
    kyc_aadhaar_full_enc = db.Column(db.Text, nullable=True)
    kyc_pan_full_enc     = db.Column(db.Text, nullable=True)
    # 16 Oct 2026 — Google Sign-In
    google_id = db.Column(db.String(100), nullable=True, unique=True, index=True)
    # 16 Oct 2026 — Transporter KYC (only used when role='transporter')
    transporter_kyc_status       = db.Column(db.String(20), default='unverified')  # unverified|pending|verified|rejected
    transporter_license_number   = db.Column(db.String(50), nullable=True)
    transporter_license_photo    = db.Column(db.String(500), nullable=True)   # Cloudinary URL
    transporter_vehicle_number   = db.Column(db.String(20), nullable=True)
    transporter_vehicle_type     = db.Column(db.String(20), nullable=True)    # bike|auto|tempo|truck|other
    transporter_vehicle_front    = db.Column(db.String(500), nullable=True)
    transporter_vehicle_back     = db.Column(db.String(500), nullable=True)
    transporter_kyc_notes        = db.Column(db.Text, nullable=True)          # admin rejection reason
    transporter_kyc_submitted_at = db.Column(db.DateTime, nullable=True)
    transporter_kyc_verified_at  = db.Column(db.DateTime, nullable=True)
    transporter_kyc_verified_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    transporter_trip_count       = db.Column(db.Integer, default=0)
    transporter_city             = db.Column(db.String(100), nullable=True)
    transporter_available        = db.Column(db.Boolean, default=True)  # self-service toggle
    transporter_upi_id           = db.Column(db.String(120), nullable=True)  # collects transport charges directly
    transporter_rating_avg       = db.Column(db.Float, nullable=True)   # cached avg
    transporter_rating_count     = db.Column(db.Integer, default=0)
    # 12 Oct 2026 — bank details (customers + vendors, for refunds/payouts)
    bank_account_name   = db.Column(db.String(150), nullable=True)
    bank_account_number = db.Column(db.String(40), nullable=True)
    bank_ifsc           = db.Column(db.String(20), nullable=True)
    bank_name           = db.Column(db.String(120), nullable=True)
    bank_branch         = db.Column(db.String(120), nullable=True)
    upi_id              = db.Column(db.String(120), nullable=True)
    # 09 Oct 2026 — user management (block/unblock)
    is_blocked        = db.Column(db.Boolean, default=False, index=True)
    blocked_at        = db.Column(db.DateTime, nullable=True)
    blocked_by        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    block_reason      = db.Column(db.Text, nullable=True)
    block_expires_at  = db.Column(db.DateTime, nullable=True)

    items = db.relationship('Item', backref='vendor', lazy=True)
    bookings = db.relationship('Booking', backref='customer', lazy=True,
                               foreign_keys='Booking.customer_id')
    
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
    # 25 Oct 2026 — Rent Only / Sell Only / Both
    is_for_rent = db.Column(db.Boolean, default=True)
    # 14 Oct 2026 — Amazon/Meesho style variants
    has_variants = db.Column(db.Boolean, default=False, index=True)
    # 14 Oct 2026 — dimensions (in cm/kg, easily measurable)
    height_cm = db.Column(db.Float, nullable=True)
    width_cm = db.Column(db.Float, nullable=True)
    length_cm = db.Column(db.Float, nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
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
    # Fernet ciphertext (see _encrypt_kyc), so wider than the raw 12/10 chars
    aadhaar_number = db.Column(db.String(255), nullable=True)
    pan_number = db.Column(db.String(255), nullable=True)
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
    # 14 Oct 2026 — variant selected at booking time (nullable for simple items)
    variant_id = db.Column(db.Integer, db.ForeignKey('item_variants.id'), nullable=True, index=True)
    variant = db.relationship('ItemVariant', foreign_keys=[variant_id])
    # 16 Oct 2026 — Transporter assignment (admin assigns; nullable)
    transporter_id          = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    transporter_assigned_at = db.Column(db.DateTime, nullable=True)
    transporter_assigned_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    transporter = db.relationship('User', foreign_keys=[transporter_id])
    transporter_ratings = db.relationship('TransporterRating', foreign_keys='TransporterRating.booking_id',
                                          backref='booking', lazy=True, cascade='all, delete-orphan')
    # Set once when a trip is credited; prevents double-count on completed -> confirmed -> completed
    transporter_trip_counted_at = db.Column(db.DateTime, nullable=True)
    otps = db.relationship('BookingOTP', lazy=True,
                           cascade='all, delete-orphan',
                           foreign_keys='BookingOTP.booking_id',
                           overlaps='booking')
    # 09 Oct 2026 — per-vendor attribution for bundle/cart children.
    # NULL means use base_rent (regular bookings). Parent rows are skipped
    # by vendor dashboard queries (order_type in ('bundle','cart')).
    vendor_share = db.Column(db.Float, nullable=True)


# Batch 8 — sales reduce stock permanently, so they must not count as rental occupancy
from sqlalchemy import or_ as _or
_RENTAL_ONLY = _or(Booking.order_type.is_(None), Booking.order_type != 'sale')
# Bookings that actually hold item-level units: no sales, no bundle/cart parents, no variant bookings
_ITEM_UNITS = db.and_(_RENTAL_ONLY,
                      _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
                      Booking.variant_id.is_(None))


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


class ItemVariant(db.Model):
    """Amazon/Meesho style — item ke variants (colour, size, material combination).
    Each variant has its own rate, stock, image. Simple items have no variants."""
    __tablename__ = 'item_variants'
    id              = db.Column(db.Integer, primary_key=True)
    item_id         = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False, index=True)
    variant_name    = db.Column(db.String(200), nullable=False)  # e.g. "Red / 2-seater", "Blue / Velvet"
    rate_per_day    = db.Column(db.Float, nullable=False)
    sale_price      = db.Column(db.Float, nullable=True)  # agar is_for_sale
    deposit_amount  = db.Column(db.Float, default=0)
    stock           = db.Column(db.Integer, default=1)
    image_url       = db.Column(db.String(500), nullable=True)
    is_available    = db.Column(db.Boolean, default=True)
    sort_order      = db.Column(db.Integer, default=0)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = db.relationship('Item', backref=db.backref('variants', lazy=True, cascade='all, delete-orphan'))

    @property
    def rate_with_commission(self):
        return round(self.rate_per_day * (1 + Config.COMMISSION_RATE), 2)

    def get_image(self):
        if self.image_url:
            return self.image_url
        if self.item:
            return self.item.get_image()
        return 'https://via.placeholder.com/400x300/e5e7eb/9ca3af?text=No+Image'


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


class ItemVerificationRequest(db.Model):
    """Vendor pays for the verified badge -> admin checks the UTR -> badge granted."""
    __tablename__ = 'item_verification_requests'
    id          = db.Column(db.Integer, primary_key=True)
    item_id     = db.Column(db.Integer, db.ForeignKey('items.id'), nullable=False)
    vendor_id   = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    utr         = db.Column(db.String(100), nullable=False)
    amount      = db.Column(db.Float, default=0)
    days        = db.Column(db.Integer, default=90)
    status      = db.Column(db.String(20), default='pending')  # pending | approved | rejected
    rejection_reason = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    reviewed_at = db.Column(db.DateTime)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    item     = db.relationship('Item', backref='verification_requests')
    vendor   = db.relationship('User', foreign_keys=[vendor_id])
    reviewer = db.relationship('User', foreign_keys=[reviewed_by])


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


class BookingOTP(db.Model):
    """Transport OTP — 3 per booking (dispatch, return, completion).
    One row per (booking, otp_type). Regenerate updates existing row."""
    __tablename__ = 'booking_otp'
    id           = db.Column(db.Integer, primary_key=True)
    booking_id   = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, index=True)
    otp_type     = db.Column(db.String(20), nullable=False)  # 'dispatch'|'return'|'completion'
    code         = db.Column(db.String(6), nullable=False)
    generated_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at   = db.Column(db.DateTime, nullable=False)
    verified_by  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    verified_at  = db.Column(db.DateTime, nullable=True)
    attempts     = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)
    status       = db.Column(db.String(20), default='active')  # active|verified|expired|locked

    booking      = db.relationship('Booking', foreign_keys=[booking_id], overlaps='otps')
    generator    = db.relationship('User', foreign_keys=[generated_by])
    verifier     = db.relationship('User', foreign_keys=[verified_by])

    __table_args__ = (
        db.UniqueConstraint('booking_id', 'otp_type', name='uq_booking_otp_type'),
    )


class TransporterRating(db.Model):
    """Post-trip rating of a transporter by customer or vendor."""
    __tablename__ = 'transporter_ratings'
    id              = db.Column(db.Integer, primary_key=True)
    booking_id      = db.Column(db.Integer, db.ForeignKey('bookings.id'), nullable=False, index=True)
    transporter_id  = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    rater_id        = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    rater_role      = db.Column(db.String(20), nullable=False)  # 'customer' | 'vendor'
    rating          = db.Column(db.Integer, nullable=False)  # 1-5
    comment         = db.Column(db.Text, nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    transporter = db.relationship('User', foreign_keys=[transporter_id])
    rater       = db.relationship('User', foreign_keys=[rater_id])

    __table_args__ = (
        db.UniqueConstraint('booking_id', 'rater_id', name='uq_rating_booking_rater'),
    )


class KYCViewLog(db.Model):
    """Audit log — every admin reveal of full KYC data."""
    __tablename__ = 'kyc_view_log'
    id             = db.Column(db.Integer, primary_key=True)
    admin_id       = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reason         = db.Column(db.String(80), nullable=True)
    ip_address     = db.Column(db.String(45), nullable=True)
    user_agent     = db.Column(db.String(300), nullable=True)
    viewed_at      = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    admin  = db.relationship('User', foreign_keys=[admin_id])
    target = db.relationship('User', foreign_keys=[target_user_id])


class AdminAuditLog(db.Model):
    """Audit trail for admin actions touching user data, privileges, KYC, or bulk deletes."""
    __tablename__ = 'admin_audit_log'
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    action = db.Column(db.String(60), nullable=False, index=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    target_ref = db.Column(db.String(100), nullable=True)
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    admin = db.relationship('User', foreign_keys=[admin_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])


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


@app.before_request
def _limit_non_video_uploads():
    """Everything except the video-report endpoint is capped at MAX_IMAGE_UPLOAD_BYTES."""
    if request.method in ('POST', 'PUT', 'PATCH') and request.endpoint != 'api_report_upload_video':
        if request.content_length and request.content_length > Config.MAX_IMAGE_UPLOAD_BYTES:
            from flask import abort
            abort(413)


@app.errorhandler(413)
def _handle_413(e):
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'error': 'Payload too large',
                        'message': 'Upload exceeds the maximum allowed size.'}), 413
    flash('⚠️ Upload too large. Please use a smaller file.', 'danger')
    return redirect(request.referrer or url_for('index'))


@app.before_request
def enforce_user_block():
    """Blocked OR disabled (is_active False) users are logged out immediately."""
    if not current_user.is_authenticated:
        return
    is_blocked = _user_blocked(current_user)
    is_disabled = current_user.is_active is False
    if not (is_blocked or is_disabled):
        return
    if request.endpoint in {'logout', 'static', 'login'}:
        return
    logout_user()
    flash('🚫 Your account has been disabled.' if is_disabled
          else '🚫 Your account has been blocked. Please contact support.', 'danger')
    return redirect(url_for('login'))


@app.before_request
def log_request_info():
    """Log requests to stdout so they appear in Render Free Tier logs."""
    # Static files ignore karo taaki log spam na ho
    if request.path.startswith('/static') or request.path.startswith('/favicon'):
        return
    
    role = current_user.role if current_user.is_authenticated else 'anonymous'
    # flush=True zaroori hai warna Render me log real-time nahi dikhega
    print(f"[VISITOR] {role} | {request.method} {request.path}", flush=True)


@app.before_request
def sos_killswitch():
    """SOS disabled 08 Oct 2026. Flip Config.SOS_ENABLED = True to re-enable.
    Code/routes untouched — only request gate."""
    if Config.SOS_ENABLED:
        return
    sos_endpoints = {
        'sos_create', 'sos_list', 'sos_cancel', 'sos_accept',
        'vendor_sos', 'vendor_sos_toggle', 'api_sos_pending',
    }
    if request.endpoint not in sos_endpoints:
        return
    if request.endpoint == 'api_sos_pending':
        return jsonify({'count': 0, 'items': []})
    if not current_user.is_authenticated:
        return redirect(url_for('login'))
    flash('⚠️ SOS feature is temporarily unavailable.', 'info')
    if current_user.role in ('vendor', 'admin'):
        return redirect(url_for('vendor_dashboard'))
    return redirect(url_for('dashboard'))


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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']), _RENTAL_ONLY
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
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']), _RENTAL_ONLY,
        Booking.start_date <= start_date,
        Booking.end_date >= start_date
    ).all()
    
    # item.stock is a LIVE counter (already reduced when each booking is made and
    # given back on cancel/complete) -> do NOT subtract overlapping bookings again.
    available_qty = max(0, item.stock or 0)
    
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


def calculate_booking_total(item, start_date, end_date, quantity, area, weight_category, rate_override=None):
    days = (end_date - start_date).days + 1
    if days < 1:
        days = 1
    # rate_override = selected variant's rate (None -> use the item's own rate)
    rate = rate_override if rate_override is not None else item.rate_per_day
    base_rent = rate * days * quantity
    commission = round(base_rent * Config.COMMISSION_RATE, 2)
    # Deposit = DEPOSIT_RATE of base rent (was: item.deposit_amount × qty)
    deposit = round(base_rent * Config.DEPOSIT_RATE, 2)
    transport_fee = 0  # Transport excluded — settled directly with vendor
    gateway_charge = round(base_rent * Config.GATEWAY_RATE, 2)
    total = base_rent + commission + deposit + gateway_charge
    return {'days': days, 'base_rent': base_rent, 'commission': commission,
            'deposit': deposit, 'gateway_charge': gateway_charge,
            'transport_fee': transport_fee, 'total': total}


_last_email_error = None  # last send_email failure reason (shown on /admin/test-email)


def send_email(to_email, subject, html_body):
    """Send HTML email via Gmail SMTP. Returns True on success."""
    global _last_email_error
    _last_email_error = None
    if not Config.MAIL_USERNAME or not Config.MAIL_PASSWORD:
        print("⚠️ Mail not configured — skipping email send")
        _last_email_error = 'MAIL_USERNAME / MAIL_PASSWORD not set in environment'
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
        _last_email_error = f'{type(e).__name__}: {e}'
        app.logger.error(f"Email send failed to {to_email}: {e}")
        return False


# ============================================
# EMAIL VERIFICATION HELPERS
# ============================================
def _send_verification_email(user):
    """Create a 48h token and email the link. Never raises. If mail is not configured the
    user is marked verified (avoids a dead end). Returns True if sent (or auto-verified),
    False if sending failed. Caller commits."""
    if not user.email:
        return False
    if not Config.MAIL_USERNAME or not Config.MAIL_PASSWORD:
        app.logger.warning('Mail not configured - skipping email verification')
        user.is_email_verified = True
        return True
    try:
        token = _secrets.token_urlsafe(32)
        user.email_verify_token = token
        user.email_verify_expires = datetime.utcnow() + timedelta(hours=48)
        verify_url = url_for('verify_email', token=token, _external=True)
        html_body = f"""
        <div style="font-family:Inter,Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;">
          <h2 style="color:#7b1f4f;margin-bottom:8px;">VyahMandap</h2>
          <p>Hi {html.escape(user.name or '')},</p>
          <p>Please confirm your email to finish setting up your account.</p>
          <p style="text-align:center;margin:28px 0;">
            <a href="{verify_url}" style="display:inline-block;background:#7b1f4f;color:#ffffff;text-decoration:none;font-weight:600;padding:12px 28px;border-radius:8px;">Verify Email</a>
          </p>
          <p style="color:#6b7280;font-size:13px;">This link expires in 48 hours. If you didn't create an account, you can ignore this email.</p>
          <p style="color:#6b7280;font-size:12px;">If the button doesn't work: <br><span style="word-break:break-all;color:#7b1f4f;">{verify_url}</span></p>
        </div>
        """
        return send_email(user.email, 'Verify your VyahMandap email', html_body)
    except Exception as e:
        app.logger.error(f'Verification email failed: {e}')
        return False


# ============================================
# PHASE 0 — SAFETY HELPERS
# ============================================

def _random_digits(n):
    """Cryptographically secure numeric string of length n."""
    return ''.join(_secrets.choice(string.digits) for _ in range(n))


def _safe_next_url(raw):
    """Same-origin relative path, or '' if unsafe (absolute, //, backslash tricks)."""
    if not raw or not isinstance(raw, str):
        return ''
    raw = raw.strip()
    if not raw.startswith('/') or raw.startswith('//') or '\\' in raw:
        return ''
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc:
            return ''
    except Exception:
        return ''
    return raw


def _is_utr_conflict(exc):
    """True if the IntegrityError is the UTR uniqueness violation."""
    msg = str(exc).lower()
    if 'uq_bookings_utr_active' in msg:
        return True
    return ('duplicate key' in msg or 'unique' in msg) and 'utr' in msg


def generate_booking_reference():
    return 'VM' + _random_digits(8)


def generate_ticket_number():
    return 'TK' + _random_digits(8)


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


def ist_now():
    """Return current datetime in India Standard Time (UTC+5:30)."""
    return datetime.utcnow() + timedelta(hours=5, minutes=30)


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

    roles = {user_a.role, user_b.role}

    # Customer <-> vendor allowed
    if roles == {'customer', 'vendor'}:
        return True

    # Vendor <-> vendor allowed (B2B coordination)
    if roles == {'vendor'}:
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
    # Reject SVG/HTML/etc. (stored XSS risk). Accept images and PDFs; tolerate a generic
    # content-type only when the file extension is a known image/PDF type.
    _mt = (getattr(file_obj, 'mimetype', '') or '').lower()
    _fn = (getattr(file_obj, 'filename', '') or '').lower()
    _ext = _fn.rsplit('.', 1)[1] if '.' in _fn else ''
    _ok_ext = _ext in {'png', 'jpg', 'jpeg', 'gif', 'webp', 'heic', 'heif', 'pdf'}
    _ok_mime = (_mt.startswith('image/') and 'svg' not in _mt) or _mt == 'application/pdf'
    if not (_ok_mime or (_mt in ('', 'application/octet-stream') and _ok_ext)):
        app.logger.warning(f'upload_file_to_cloudinary rejected mime={_mt!r} name={_fn!r}')
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
            except Exception:
                pass


def upload_video_to_cloudinary(file_obj, folder='vyahmandap/reports/videos'):
    if not Config.CLOUDINARY_CLOUD_NAME:
        print("⚠️ Cloudinary not configured")
        return None, None
    _mt = (getattr(file_obj, 'mimetype', '') or '').lower()
    if _mt and not _mt.startswith('video/') and _mt != 'application/octet-stream':
        app.logger.warning(f'upload_video_to_cloudinary rejected mime={_mt!r}')
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
            except Exception:
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
    user.mobile = f"DEL{user.id:07d}"  # unique, never a real mobile
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


def _user_blocked(user):
    """Return True if user is currently blocked or deleted (respects block expiry)."""
    if not user:
        return False
    # is_deleted is only set once anonymisation completes (after the grace period)
    if getattr(user, 'is_deleted', False):
        return True
    if not user.is_blocked:
        return False
    # Expired block = not blocked anymore
    if user.block_expires_at and user.block_expires_at <= datetime.utcnow():
        return False
    return True


# ============================================
# ADMIN AUDIT + RE-AUTH
# ============================================
def _audit_admin(action, target_user_id=None, target_ref=None, details=None, commit=True):
    """Write an AdminAuditLog row. Never raises."""
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr or '') or ''
        if ',' in ip:
            ip = ip.split(',')[0].strip()
        ua = (request.headers.get('User-Agent', '') or '')[:300] or None
        log = AdminAuditLog(
            admin_id=current_user.id,
            action=action[:60],
            target_user_id=target_user_id,
            target_ref=(str(target_ref)[:100] if target_ref else None),
            details=details,
            ip_address=ip[:45] or None,
            user_agent=ua,
        )
        db.session.add(log)
        if commit:
            db.session.commit()
        return log
    except Exception as e:
        app.logger.warning(f'Audit log write failed ({action}): {e}')
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def _check_reauth_password():
    """True iff the supplied confirm_password (form or JSON) matches the current admin's password."""
    pw = (request.form.get('confirm_password') or '').strip()
    if not pw and request.is_json:
        pw = ((request.get_json(silent=True) or {}).get('confirm_password') or '').strip()
    if not pw:
        return False
    try:
        return bool(current_user.check_password(pw))
    except Exception:
        return False


# ============================================
# BREACHED-PASSWORD CHECK (HIBP k-anonymity API)
# ============================================
def _is_password_breached(password, timeout=3):
    """True if the password appears in the HIBP corpus. Only the first 5 chars of the
    SHA-1 are sent. FAILS OPEN (returns False) on any network error."""
    if not password:
        return False
    try:
        import urllib.request
        sha1 = hashlib.sha1(password.encode('utf-8')).hexdigest().upper()
        prefix, suffix = sha1[:5], sha1[5:]
        req = urllib.request.Request(
            f'https://api.pwnedpasswords.com/range/{prefix}',
            headers={'User-Agent': 'VyahMandap-PasswordCheck/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8', errors='ignore')
        for line in body.splitlines():
            parts = line.split(':')
            if len(parts) == 2 and parts[0].strip().upper() == suffix:
                return True
        return False
    except Exception as e:
        app.logger.info(f'HIBP check skipped ({e})')
        return False


def _ocr_pan_image(image_file):
    """Extract PAN number from image via OCR.space API.
    Compresses image to fit free tier (<1MB). Returns (pan_or_none, error_kind)."""
    if not Config.OCR_SPACE_API_KEY:
        return None, 'no_api'
    temp_path = None
    raw_path = None
    try:
        # Save raw upload
        ext = '.jpg'
        if image_file.filename and '.' in image_file.filename:
            ext = '.' + image_file.filename.rsplit('.', 1)[1].lower()
            if ext not in ('.jpg', '.jpeg', '.png', '.webp'):
                ext = '.jpg'
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            image_file.save(tmp.name)
            raw_path = tmp.name

        # Compress + resize to fit OCR.space free tier (<1MB)
        # OCR.space free API: 1MB max file size, and needs sane dimensions
        try:
            from PIL import Image
            img = Image.open(raw_path)
            # Convert to RGB (handles PNG with alpha, etc.)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            # Resize if very large (keep aspect ratio, max 2000px on long side)
            max_dim = 2000
            if max(img.size) > max_dim:
                ratio = max_dim / float(max(img.size))
                new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
                img = img.resize(new_size, Image.LANCZOS)
            # Save as JPEG at decreasing quality until < 900KB
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp2:
                temp_path = tmp2.name
            quality = 85
            for q in [85, 75, 65, 55, 45, 35]:
                img.save(temp_path, 'JPEG', quality=q, optimize=True)
                if os.path.getsize(temp_path) < 900 * 1024:
                    quality = q
                    break
            app.logger.info(f'OCR compress: raw={os.path.getsize(raw_path)//1024}KB -> compressed={os.path.getsize(temp_path)//1024}KB (q={quality})')
        except Exception as e:
            app.logger.warning(f'Image compression failed, using raw: {e}')
            temp_path = raw_path
            raw_path = None

        # Final size sanity check (1MB OCR.space free tier limit)
        if os.path.getsize(temp_path) > 1024 * 1024:
            return None, 'read_error:file_too_large_even_after_compression'

        with open(temp_path, 'rb') as f:
            r = requests.post(
                'https://api.ocr.space/parse/image',
                files={'file': f},
                data={
                    'apikey': Config.OCR_SPACE_API_KEY,
                    'language': 'eng',
                    'isOverlayRequired': False,
                    'OCREngine': '2',
                    'scale': True,
                },
                timeout=30,
            )
        result = r.json()
        if result.get('IsErroredOnProcessing'):
            err_detail = result.get('ErrorMessage') or result.get('ErrorDetails') or 'unknown'
            app.logger.error(f'OCR.space error: {err_detail} | Full: {result}')
            return None, f'api_error:{str(err_detail)[:80]}'
        parsed = result.get('ParsedResults') or []
        if not parsed:
            app.logger.error(f'OCR.space empty parsed: {result}')
            return None, 'api_error:empty_parsed'
        text = (parsed[0].get('ParsedText') or '').upper()
        # Remove common OCR noise
        clean = re.sub(r'[^A-Z0-9]', '', text)
        m = re.search(r'[A-Z]{5}[0-9]{4}[A-Z]', clean)
        if not m:
            return None, 'no_pan'
        return m.group(0), 'ok'
    except requests.Timeout:
        return None, 'api_error'
    except Exception as e:
        app.logger.error(f'OCR PAN failed: {e}')
        return None, 'api_error'
    finally:
        for p in (temp_path, raw_path):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


def _validate_kyc_inputs(aadhaar, pan, pan_file):
    """Validate Aadhaar format + PAN number + PAN image OCR match.
    Returns (ok, error_msg). PAN image is deleted immediately after OCR."""
    if not aadhaar or not re.match(r'^\d{12}$', aadhaar):
        return False, 'Valid 12-digit Aadhaar required.'
    if not pan or not re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan):
        return False, 'Valid PAN required (format: ABCDE1234F).'
    if not pan_file or not pan_file.filename:
        return False, 'PAN card image is required for verification.'

    extracted, kind = _ocr_pan_image(pan_file)
    if kind == 'no_api':
        return False, 'KYC verification temporarily unavailable. Please try again in a few minutes.'
    if kind.startswith('api_error'):
        detail = kind.split(':', 1)[1] if ':' in kind else 'unknown'
        return False, f'Verification failed: {detail}'
    if kind.startswith('read_error'):
        return False, 'Image too large or unreadable. Try a smaller, clearer photo (under 2 MB).'
    if kind == 'no_pan':
        return False, 'Could not read PAN number from image. Please upload a clear, well-lit photo where the PAN number is clearly visible.'
    if kind == 'read_error':
        return False, 'Image too large or unreadable. Upload a clear photo under 5MB.'
    if kind == 'no_pan':
        return False, 'Could not read PAN number from image. Upload a clearer photo with PAN visible.'
    if extracted != pan:
        return False, f'PAN on image ({extracted}) does not match entered number ({pan}). Check the number or upload a clearer photo.'
    return True, None


def _mark_user_kyc_verified(user, aadhaar, pan):
    """Store KYC verification on user (1-year validity). Full numbers encrypted."""
    user.kyc_status = 'verified'
    user.kyc_verified_at = datetime.utcnow()
    user.kyc_expires_at = datetime.utcnow() + timedelta(days=365)
    user.kyc_aadhaar_last4 = aadhaar[-4:]
    # PAN: ABCDE1234F -> ABCDE****F
    user.kyc_pan_masked = pan[:5] + '****' + pan[-1]
    # Full numbers — encrypted at rest (Fernet)
    user.kyc_aadhaar_full_enc = _encrypt_kyc(aadhaar)
    user.kyc_pan_full_enc     = _encrypt_kyc(pan)


# ============================================
# TRANSPORT OTP HELPERS
# ============================================
OTP_TTL_MAP = {
    'dispatch':   lambda: timedelta(hours=Config.OTP_TTL_DISPATCH_HOURS),
    'return':     lambda: timedelta(hours=Config.OTP_TTL_RETURN_HOURS),
}

OTP_STATUS_MAP = {
    'dispatch':   ('dispatched',        'in_use'),
    'return':     ('return_initiated',  'completed'),  # 11 Oct 2026 — direct complete
}


def _generate_otp_code():
    """Return a 6-digit numeric OTP (leading zeros allowed)."""
    return _random_digits(6)


def _get_or_create_otp(booking_id, otp_type, generated_by_id):
    """Create a fresh OTP, or regenerate if one exists but not yet verified.
    Returns (otp, error_msg)."""
    if otp_type not in OTP_TTL_MAP:
        return None, 'Invalid OTP type.'
    existing = BookingOTP.query.filter_by(
        booking_id=booking_id, otp_type=otp_type
    ).first()
    if existing and existing.status == 'verified':
        return None, 'This OTP has already been verified.'
    if existing and existing.locked_until and existing.locked_until > datetime.utcnow():
        return None, f'Locked until {existing.locked_until.strftime("%d %b %H:%M")}. Admin override required.'

    now = datetime.utcnow()
    expires_at = now + OTP_TTL_MAP[otp_type]()
    code = _generate_otp_code()
    if existing:
        existing.code = code
        existing.generated_by = generated_by_id
        existing.generated_at = now
        existing.expires_at = expires_at
        existing.attempts = 0
        existing.locked_until = None
        existing.status = 'active'
        existing.verified_by = None
        existing.verified_at = None
        otp = existing
    else:
        otp = BookingOTP(
            booking_id=booking_id,
            otp_type=otp_type,
            code=code,
            generated_by=generated_by_id,
            generated_at=now,
            expires_at=expires_at,
            status='active',
        )
        db.session.add(otp)
    return otp, None


def _send_otp_notification(otp):
    """Notify donor with code (visible) + receiver with incoming notice (no code).
    Donor = whoever generated. Receiver = the other party."""
    booking = otp.booking
    if not booking:
        return
    try:
        expiry_str = otp.expires_at.strftime('%d %b, %I:%M %p')
        vendor_id = booking.item.vendor_id if booking.item else None
        customer_id = booking.customer_id
        ref = booking.booking_reference

        if otp.otp_type == 'dispatch':
            # Donor = vendor, Receiver = customer
            if vendor_id:
                create_notification(
                    vendor_id, 'otp',
                    f'🔐 Delivery OTP: {otp.code}',
                    f'{ref} — share with transporter only. Expires: {expiry_str}',
                    f'/vendor/booking/{booking.id}'
                )
            create_notification(
                customer_id, 'otp',
                f'🚚 Delivery incoming — {ref}',
                'Transporter will share a 6-digit code. Verify when delivered.',
                f'/my-booking/{booking.id}'
            )
        elif otp.otp_type == 'return':
            # Donor = customer, Receiver = vendor
            create_notification(
                customer_id, 'otp',
                f'🔐 Return OTP: {otp.code}',
                f'{ref} — share with transporter only. Expires: {expiry_str}',
                f'/my-booking/{booking.id}'
            )
            if vendor_id:
                create_notification(
                    vendor_id, 'otp',
                    f'📦 Return incoming — {ref}',
                    'Transporter will share a 6-digit code. Verify when delivered back.',
                    f'/vendor/booking/{booking.id}'
                )
        db.session.commit()
    except Exception:
        app.logger.warning('OTP notification failed', exc_info=True)


# _verify_otp_code() removed (dead code; its logic had drifted from the real verifier).
# The authoritative OTP verification lives inline in transport_verify_code().


def _apply_otp_status(otp, booking=None):
    """Move booking to next status based on verified OTP type.
    Pass booking explicitly when otp was just created (relationship not flushed)."""
    if booking is None:
        booking = otp.booking
    if booking is None:
        app.logger.error(f'_apply_otp_status: booking is None for OTP id={otp.id} type={otp.otp_type}')
        return False
    expected, new_status = OTP_STATUS_MAP.get(otp.otp_type, (None, None))
    if not new_status:
        return False
    # Idempotent — if already advanced, skip
    if booking.booking_status == new_status:
        return True
    # Sanity — only advance from expected status, else log and continue
    if expected and booking.booking_status not in (expected,):
        app.logger.warning(
            f'OTP {otp.otp_type} for booking {booking.id}: '
            f'expected status {expected}, got {booking.booking_status} — applying anyway'
        )
    booking.booking_status = new_status
    return True


def _notify_otp_event(otp, event='verified'):
    """Send notifications for OTP events. event: 'verified'|'locked'|'generated'."""
    booking = otp.booking
    if not booking:
        return
    item_title = booking.item.title if booking.item else 'item'
    ref = booking.booking_reference

    if event == 'locked':
        msg = f'🔒 OTP locked on {ref} — too many wrong attempts'
        for uid in [otp.generated_by, otp.verified_by]:
            if uid:
                try:
                    create_notification(uid, 'warning', 'OTP Locked', msg,
                                        f'/my-booking/{booking.id}')
                except Exception:
                    pass
        notify_all_admins(msg, link=url_for('admin_booking_detail', booking_id=booking.id))
        return

    # verified event
    otp_label = {
        'dispatch': 'Item received',
        'return': 'Item returned',
    }.get(otp.otp_type, 'OTP verified')

    if otp.otp_type == 'dispatch':
        # Customer confirmed receiving from vendor
        try:
            create_notification(booking.item.vendor_id if booking.item else None,
                                'booking', f'✅ {otp_label} — {ref}',
                                f'Customer confirmed delivery of {item_title}.',
                                f'/vendor/booking/{booking.id}')
        except Exception:
            pass
        try:
            create_notification(booking.customer_id, 'booking',
                                f'✅ You confirmed receipt — {ref}',
                                f'{item_title} is now marked in-use.',
                                f'/my-booking/{booking.id}')
        except Exception:
            pass
    elif otp.otp_type == 'return':
        try:
            create_notification(booking.customer_id, 'booking',
                                f'✅ Return confirmed — {ref}',
                                f'Vendor has received {item_title}. Your booking is now complete.',
                                f'/my-booking/{booking.id}')
        except Exception:
            pass
        try:
            create_notification(booking.item.vendor_id if booking.item else None,
                                'booking', f'✅ {otp_label} — {ref}',
                                f'You confirmed return of {item_title}. Booking marked complete.',
                                f'/vendor/booking/{booking.id}')
        except Exception:
            pass
    if otp.otp_type == 'return' and booking.booking_status == 'completed':
        try:
            notify_all_admins(
                f'🎉 Booking completed — {ref}',
                link=url_for('admin_booking_detail', booking_id=booking.id)
            )
        except Exception:
            pass
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.route('/admin/booking/<int:booking_id>/otp/force-verify/<otp_type>', methods=['POST'])
@login_required
def admin_force_verify_otp(booking_id, otp_type):
    """Admin override — verify an OTP without the code. Audit logged."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    if otp_type not in ('dispatch', 'return'):
        flash('Invalid OTP type.', 'danger')
        return redirect(url_for('admin_booking_detail', booking_id=booking_id))

    booking = Booking.query.get_or_404(booking_id)
    now = datetime.utcnow()

    otp = BookingOTP.query.filter_by(booking_id=booking.id, otp_type=otp_type).first()
    if not otp:
        # Create on the fly — admin override means they confirm the step happened
        otp = BookingOTP(
            booking_id=booking.id,
            otp_type=otp_type,
            code=_generate_otp_code(),
            generated_by=current_user.id,
            generated_at=now,
            expires_at=now + OTP_TTL_MAP[otp_type](),
            status='verified',
            verified_by=current_user.id,
            verified_at=now,
        )
        db.session.add(otp)
        db.session.flush()  # ensure ID + FK settled before further use
    else:
        if otp.status == 'verified':
            flash('This OTP is already verified.', 'info')
            return redirect(url_for('admin_booking_detail', booking_id=booking.id))
        otp.status = 'verified'
        otp.verified_by = current_user.id
        otp.verified_at = now
        otp.locked_until = None
        otp.attempts = 0

    _old_status = booking.booking_status
    _apply_otp_status(otp, booking)
    if booking.booking_status == 'completed':
        _restore_completed_stock(booking, _old_status)
        _bump_transporter_trip(booking, _old_status)
    db.session.commit()

    # Notify customer + vendor
    try:
        _notify_otp_event(otp, event='verified')
    except Exception:
        pass

    # OTP2 (return) now completes the booking directly — OTP3 no longer auto-generated.

    flash(f'✅ Force-verified {otp_type} OTP for {booking.booking_reference}.', 'success')
    return redirect(url_for('admin_booking_detail', booking_id=booking.id))


@app.route('/booking/<int:booking_id>/otp/generate/<otp_type>', methods=['POST'])
@login_required
def generate_booking_otp(booking_id, otp_type):
    """Generate or regenerate an OTP for a booking. Returns JSON with code."""
    if otp_type not in ('dispatch', 'return'):
        return jsonify({'error': 'invalid_type'}), 400

    booking = Booking.query.get_or_404(booking_id)
    is_admin = current_user.role == 'admin'
    is_customer = booking.customer_id == current_user.id
    is_vendor = (booking.item and booking.item.vendor_id == current_user.id)

    # Permission + status gates
    if otp_type == 'dispatch':
        if not (is_vendor or is_admin):
            return jsonify({'error': 'forbidden', 'message': 'Only the vendor can generate this OTP.'}), 403
        if booking.booking_status != 'dispatched':
            return jsonify({'error': 'wrong_status', 'message': 'Dispatch report must be uploaded first.'}), 400
    elif otp_type == 'return':
        if not (is_customer or is_admin):
            return jsonify({'error': 'forbidden', 'message': 'Only the customer can generate this OTP.'}), 403
        if booking.booking_status not in ('in_use', 'dispatched'):
            return jsonify({'error': 'wrong_status', 'message': 'Return report must be uploaded first.'}), 400

    otp, err = _get_or_create_otp(booking.id, otp_type, current_user.id)
    if err:
        return jsonify({'error': 'generate_failed', 'message': err}), 400
    db.session.commit()

    _send_otp_notification(otp)

    return jsonify({
        'success': True,
        'code': otp.code,
        'otp_type': otp_type,
        'expires_at': otp.expires_at.isoformat(),
    })


@app.route('/booking/<int:booking_id>/otp/mark-complete', methods=['POST'])
@login_required
def vendor_mark_complete(booking_id):
    """DEPRECATED (15 Oct 2026). Bookings now complete automatically when the
    vendor verifies the Return OTP. Kept only so old url_for() references
    don't break; safe to delete once no template references it."""
    Booking.query.get_or_404(booking_id)
    flash('This step is no longer needed — the booking completes automatically when the Return OTP is verified.', 'info')
    if current_user.role == 'admin':
        return redirect(url_for('admin_booking_detail', booking_id=booking_id))
    return redirect(url_for('vendor_booking_detail', booking_id=booking_id))


@app.route('/transport/verify', methods=['GET', 'POST'])
def transport_verify_landing():
    """Public landing — enter booking reference."""
    if request.method == 'POST':
        ref = (request.form.get('reference') or '').strip().upper()
        if not ref:
            flash('Enter a booking reference.', 'danger')
            return render_template('transport_verify.html', mode='landing')
        booking = Booking.query.filter_by(booking_reference=ref).first()
        if not booking:
            flash('Booking not found. Check the reference.', 'danger')
            return render_template('transport_verify.html', mode='landing')
        return redirect(url_for('transport_verify_code', ref=ref))
    return render_template('transport_verify.html', mode='landing')


@app.route('/transport/verify/<ref>', methods=['GET', 'POST'])
@limiter.limit("20 per hour", methods=["POST"])
def transport_verify_code(ref):
    """Public OTP entry — scoped to a single booking (brute-force safe)."""
    booking = Booking.query.filter_by(booking_reference=ref).first()
    if not booking:
        flash('Invalid booking reference.', 'danger')
        return redirect(url_for('transport_verify_landing'))

    otps = BookingOTP.query.filter_by(booking_id=booking.id).all()
    otp_map = {o.otp_type: o for o in otps}

    if request.method == 'GET':
        return render_template('transport_verify.html',
                               mode='code', booking=booking, otp_map=otp_map,
                               next_url=request.args.get('next', ''))

    # POST — verify
    code = (request.form.get('otp_code') or '').strip()
    next_url = _safe_next_url(request.form.get('next') or request.args.get('next') or '')

    def _back():
        if next_url:
            return redirect(next_url)
        return redirect(url_for('transport_verify_code', ref=ref))

    now = datetime.utcnow()

    # Login + role gate — public page can only CHECK, not verify
    if not current_user.is_authenticated:
        flash('⚠️ Login required to verify OTP. Only the receiving party can confirm — the code should be relayed to them.', 'warning')
        return _back()

    is_admin = current_user.role == 'admin'
    is_customer = booking.customer_id == current_user.id
    is_vendor = (booking.item and booking.item.vendor_id == current_user.id)

    if not code or len(code) != 6 or not code.isdigit():
        flash('Enter a valid 6-digit code.', 'danger')
        return _back()

    # Search ONLY within this booking's OTPs — prevents cross-booking guessing
    otp = BookingOTP.query.filter_by(booking_id=booking.id, code=code).first()

    if not otp:
        # Wrong code — count against the latest active OTP (if any)
        latest = BookingOTP.query.filter_by(
            booking_id=booking.id, status='active'
        ).order_by(BookingOTP.generated_at.desc()).first()
        if latest:
            latest.attempts = (latest.attempts or 0) + 1
            if latest.attempts >= Config.OTP_MAX_ATTEMPTS:
                latest.locked_until = now + timedelta(hours=Config.OTP_LOCK_HOURS)
                latest.status = 'locked'
                db.session.commit()
                _notify_otp_event(latest, event='locked')
                flash(f'🔒 Too many wrong attempts. Locked for {Config.OTP_LOCK_HOURS}h. Contact support.', 'danger')
            else:
                remaining = Config.OTP_MAX_ATTEMPTS - latest.attempts
                db.session.commit()
                flash(f'Invalid code. {remaining} attempt(s) left.', 'warning')
        else:
            flash('Invalid code.', 'danger')
        return _back()

    # Code matched — validate state
    if otp.status == 'verified':
        flash('This code has already been used.', 'info')
        return _back()
    if otp.locked_until and otp.locked_until > now:
        mins = int((otp.locked_until - now).total_seconds() / 60) + 1
        flash(f'Locked. Try again in {mins} minute(s).', 'danger')
        return _back()
    if otp.expires_at < now:
        otp.status = 'expired'
        db.session.commit()
        flash('Code expired. Ask the other party to regenerate.', 'warning')
        return _back()

    # Role gate — only the receiver (or admin) can verify this OTP
    if otp.otp_type == 'dispatch' and not (is_customer or is_admin):
        flash('⚠️ Only the customer can verify the delivery OTP (after receiving the item).', 'warning')
        return _back()
    if otp.otp_type == 'return' and not (is_vendor or is_admin):
        flash('⚠️ Only the vendor can verify the return OTP (after receiving the item back).', 'warning')
        return _back()

    # Success path
    otp.status = 'verified'
    otp.verified_by = current_user.id
    otp.verified_at = now
    _old_status = booking.booking_status
    _apply_otp_status(otp, booking)
    if booking.booking_status == 'completed':
        _restore_completed_stock(booking, _old_status)
        _bump_transporter_trip(booking, _old_status)
    db.session.commit()

    # OTP2 (return) now completes the booking directly — OTP3 no longer auto-generated.

    _notify_otp_event(otp, event='verified')

    flash(f'✅ Verified! Booking {booking.booking_reference} updated.', 'success')
    return _back()


def _google_oauth_url(state):
    """Return Google OAuth 2.0 authorize URL."""
    redirect_uri = Config.SITE_URL.rstrip('/') + '/auth/google/callback'
    params = {
        'client_id': Config.GOOGLE_CLIENT_ID,
        'redirect_uri': redirect_uri,
        'response_type': 'code',
        'scope': 'openid email profile',
        'state': state,
        'access_type': 'online',
        'prompt': 'select_account',
    }
    return 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)


def _exchange_google_code(code):
    """Exchange auth code for access token. Returns dict or None."""
    try:
        redirect_uri = Config.SITE_URL.rstrip('/') + '/auth/google/callback'
        r = requests.post(
            'https://oauth2.googleapis.com/token',
            data={
                'code': code,
                'client_id': Config.GOOGLE_CLIENT_ID,
                'client_secret': Config.GOOGLE_CLIENT_SECRET,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            },
            timeout=15,
        )
        if r.status_code != 200:
            app.logger.error(f'Google token exchange failed: {r.status_code} {r.text[:200]}')
            return None
        return r.json()
    except Exception as e:
        app.logger.error(f'Google token exchange exception: {e}')
        return None


def _fetch_google_user(access_token):
    """Fetch user info from Google. Returns dict or None."""
    try:
        r = requests.get(
            'https://openidconnect.googleapis.com/v1/userinfo',
            headers={'Authorization': f'Bearer {access_token}'},
            timeout=10,
        )
        if r.status_code != 200:
            app.logger.error(f'Google userinfo failed: {r.status_code}')
            return None
        return r.json()
    except Exception as e:
        app.logger.error(f'Google userinfo exception: {e}')
        return None


def _utr_already_used(utr):
    """True if this UTR is already on an active booking or a pending/approved
    verification request. Case-insensitive."""
    if not utr:
        return False
    u = utr.strip().lower()
    if Booking.query.filter(
        db.func.lower(Booking.utr_number) == u,
        Booking.booking_status != 'cancelled'
    ).first():
        return True
    if ItemVerificationRequest.query.filter(
        db.func.lower(ItemVerificationRequest.utr) == u,
        ItemVerificationRequest.status.in_(['pending', 'approved'])
    ).first():
        return True
    return False


def _kyc_cipher():
    """Return Fernet cipher, or None if key missing/invalid."""
    key = Config.KYC_ENCRYPTION_KEY
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        app.logger.error(f'KYC cipher init failed: {e}')
        return None


def _encrypt_kyc(plaintext):
    """Encrypt KYC field for storage. Returns None if encryption unavailable."""
    if not plaintext:
        return None
    c = _kyc_cipher()
    if not c:
        app.logger.warning('KYC encryption key not set — full data NOT stored')
        return None
    try:
        return c.encrypt(plaintext.encode()).decode()
    except Exception as e:
        app.logger.error(f'KYC encrypt failed: {e}')
        return None


def _decrypt_kyc(ciphertext):
    """Decrypt KYC field. Returns None on failure."""
    if not ciphertext:
        return None
    c = _kyc_cipher()
    if not c:
        return None
    try:
        return c.decrypt(ciphertext.encode()).decode()
    except Exception as e:
        app.logger.error(f'KYC decrypt failed: {e}')
        return None


def _user_kyc_valid(user):
    """Return True if user has a non-expired verified KYC."""
    if not user:
        return False
    if user.kyc_status != 'verified':
        return False
    if user.kyc_expires_at and user.kyc_expires_at < datetime.utcnow():
        return False
    return True


def get_display_name(user):
    """Return public_id for others, real name for self/admin."""
    if not user:
        return '—'
    try:
        if current_user and current_user.is_authenticated:
            if current_user.id == user.id or current_user.role == 'admin':
                return user.name
    except Exception:
        pass
    return user.public_id or user.name


def t(key, **kwargs):
    """Translate key -> current language string. Fallback: hi -> en -> key.
    Optional kwargs interpolate via str.format (e.g. t('welcome_back', name='Raj'))."""
    try:
        lang = session.get('lang', Config.DEFAULT_LANG)
    except Exception:
        lang = Config.DEFAULT_LANG
    if lang not in TRANSLATIONS:
        lang = Config.DEFAULT_LANG
    val = None
    if lang != Config.DEFAULT_LANG:
        val = TRANSLATIONS.get(lang, {}).get(key)
    if not val:
        val = TRANSLATIONS.get(Config.DEFAULT_LANG, {}).get(key) or key
    if kwargs:
        try:
            return val.format(**kwargs)
        except Exception:
            return val
    return val


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
        if user and user.password_hash and user.password_hash != 'DELETED' and not user.is_deleted:
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
              <p>Hi {html.escape(user.name or '')},</p>
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
        elif _is_password_breached(password):
            errors.append('This password has appeared in a data breach. Please choose a different password.')
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
            # Block check — even with correct password, blocked users can't login
            if _user_blocked(user):
                reason = user.block_reason or 'Contact support for details.'
                flash(f'🚫 Your account is blocked. Reason: {reason}', 'danger')
                return render_template('auth/login.html')

            if user.is_active is False:
                flash('🚫 Your account has been disabled. Please contact support.', 'danger')
                return render_template('auth/login.html')

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
            elif user.role == 'transporter':
                return redirect(url_for('transporter_dashboard'))
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


@app.route('/set-language/<lang>')
def set_language(lang):
    """Switch UI language. Persists in session. Redirects to same-origin referrer."""
    if lang not in dict(Config.SUPPORTED_LANGS):
        lang = Config.DEFAULT_LANG
    session['lang'] = lang
    session.modified = True

    # Redirect to referrer (only if same origin) or home
    ref = request.referrer or ''
    if ref:
        try:
            from urllib.parse import urlparse
            ref_host = urlparse(ref).netloc
            this_host = urlparse(request.url_root).netloc
            if ref_host == this_host:
                return redirect(ref)
        except Exception:
            pass
    return redirect(url_for('index'))


@app.route('/google84e4c0bb0f809ec1.html')
def google_site_verification():
    """Google Search Console verification file. Safe to keep — one-time check."""
    return app.response_class(
        'google-site-verification: google84e4c0bb0f809ec1.html\n',
        mimetype='text/html'
    )


@app.route('/sitemap.xml')
def sitemap_xml():
    """Dynamic sitemap for search engines."""
    from flask import make_response
    base = Config.SITE_URL.rstrip('/')
    today = datetime.utcnow().strftime('%Y-%m-%d')

    urls = []

    static_pages = [
        ('/', '1.0', 'daily'),
        ('/bundles', '0.9', 'daily'),
        ('/leaderboard', '0.7', 'weekly'),
        ('/faq', '0.5', 'monthly'),
        ('/privacy', '0.4', 'yearly'),
        ('/terms', '0.4', 'yearly'),
    ]
    for path, priority, freq in static_pages:
        urls.append({'loc': base + path, 'lastmod': today, 'priority': priority, 'changefreq': freq})

    try:
        items = Item.query.filter_by(is_available=True).order_by(Item.updated_at.desc()).limit(5000).all()
        for it in items:
            lastmod = (it.updated_at or it.created_at or datetime.utcnow()).strftime('%Y-%m-%d')
            urls.append({'loc': f'{base}/item/{it.id}', 'lastmod': lastmod, 'priority': '0.8', 'changefreq': 'weekly'})
    except Exception as e:
        app.logger.warning(f'Sitemap items failed: {e}')

    try:
        bundles = Bundle.query.filter_by(is_active=True).order_by(Bundle.updated_at.desc()).limit(1000).all()
        for b in bundles:
            lastmod = (b.updated_at or b.created_at or datetime.utcnow()).strftime('%Y-%m-%d')
            urls.append({'loc': f'{base}/bundle/{b.id}', 'lastmod': lastmod, 'priority': '0.7', 'changefreq': 'weekly'})
    except Exception as e:
        app.logger.warning(f'Sitemap bundles failed: {e}')

    try:
        vendor_ids = db.session.query(Item.vendor_id).filter_by(is_available=True).distinct().all()
        for (vid,) in vendor_ids:
            if vid:
                urls.append({'loc': f'{base}/vendor/{vid}/public', 'lastmod': today, 'priority': '0.6', 'changefreq': 'weekly'})
    except Exception as e:
        app.logger.warning(f'Sitemap vendors failed: {e}')

    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        xml_parts.append(
            f'<url><loc>{u["loc"]}</loc><lastmod>{u["lastmod"]}</lastmod>'
            f'<changefreq>{u["changefreq"]}</changefreq><priority>{u["priority"]}</priority></url>'
        )
    xml_parts.append('</urlset>')

    resp = make_response('\n'.join(xml_parts))
    resp.headers['Content-Type'] = 'application/xml; charset=utf-8'
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


@app.route('/robots.txt')
def robots_txt():
    """Robots.txt — public crawler access, private blocked. Longest-rule-wins allows exceptions."""
    base = Config.SITE_URL.rstrip('/')
    lines = [
        'User-agent: *',
        'Allow: /',
        # Longest-match exceptions (must come AFTER generic Allow)
        'Allow: /vendor/*/public',
        'Allow: /item/',
        'Allow: /bundle/',
        # Block private
        'Disallow: /admin/',
        'Disallow: /vendor/',
        'Disallow: /my-booking/',
        'Disallow: /my-data/',
        'Disallow: /profile/',
        'Disallow: /cart/',
        'Disallow: /booking/',
        'Disallow: /book/',
        'Disallow: /buy/',
        'Disallow: /chat/',
        'Disallow: /tickets/',
        'Disallow: /notifications/',
        'Disallow: /wishlist/',
        'Disallow: /recently-viewed',
        'Disallow: /compare',
        'Disallow: /api/',
        'Disallow: /transport/',
        'Disallow: /set-language/',
        'Disallow: /sos/',
        'Disallow: /login',
        'Disallow: /register',
        'Disallow: /forgot-password',
        'Disallow: /reset-password',
        'Disallow: /logout',
        '',
        f'Sitemap: {base}/sitemap.xml',
        '',
    ]
    resp = app.response_class('\n'.join(lines), mimetype='text/plain')
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')


@app.route('/auth/google')
def google_login():
    """Initiate Google OAuth flow."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if not Config.GOOGLE_CLIENT_ID or not Config.GOOGLE_CLIENT_SECRET:
        flash('Google Sign-In not configured.', 'danger')
        return redirect(url_for('login'))

    state = _secrets.token_urlsafe(32)
    session['oauth_state'] = state
    session.modified = True
    return redirect(_google_oauth_url(state))


@app.route('/auth/google/callback')
def google_callback():
    """Handle Google OAuth callback."""
    # Validate state (CSRF protection)
    expected_state = session.pop('oauth_state', None)
    actual_state = request.args.get('state')
    if not expected_state or expected_state != actual_state:
        flash('⚠️ Invalid OAuth state. Please try again.', 'danger')
        return redirect(url_for('login'))

    error = request.args.get('error')
    if error:
        flash(f'Google sign-in cancelled: {error}', 'warning')
        return redirect(url_for('login'))

    code = request.args.get('code')
    if not code:
        flash('Google sign-in failed — no code returned.', 'danger')
        return redirect(url_for('login'))

    token_data = _exchange_google_code(code)
    if not token_data or not token_data.get('access_token'):
        flash('Google authentication failed. Please try again.', 'danger')
        return redirect(url_for('login'))

    user_info = _fetch_google_user(token_data['access_token'])
    if not user_info or not user_info.get('email'):
        flash('Could not fetch your Google profile.', 'danger')
        return redirect(url_for('login'))

    email = user_info.get('email', '').strip().lower()
    name = user_info.get('name', '').strip() or email.split('@')[0]
    google_id = user_info.get('sub') or user_info.get('id')

    # Check 1: google_id match (already linked)
    user = User.query.filter_by(google_id=google_id).first()
    if user:
        if _user_blocked(user):
            flash(f'🚫 Your account is blocked. Reason: {user.block_reason or "Contact support."}', 'danger')
            return redirect(url_for('login'))
        if user.is_active is False:
            flash('🚫 Your account has been disabled. Please contact support.', 'danger')
            return redirect(url_for('login'))
        login_user(user, remember=True)
        flash(f'🎉 Welcome back, {user.name}!', 'success')
        return redirect(url_for('admin_dashboard') if user.role == 'admin'
                        else url_for('vendor_dashboard') if user.role == 'vendor'
                        else url_for('transporter_dashboard') if user.role == 'transporter'
                        else url_for('dashboard'))

    # Check 2: email match (link Google to existing account)
    user = User.query.filter_by(email=email).first()
    if user:
        if _user_blocked(user):
            flash(f'🚫 Your account is blocked. Reason: {user.block_reason or "Contact support."}', 'danger')
            return redirect(url_for('login'))
        if user.is_active is False:
            flash('🚫 Your account has been disabled. Please contact support.', 'danger')
            return redirect(url_for('login'))
        user.google_id = google_id
        user.is_email_verified = True  # Google confirmed this email
        db.session.commit()
        login_user(user, remember=True)
        flash(f'✅ Google linked. Welcome back, {user.name}!', 'success')
        return redirect(url_for('admin_dashboard') if user.role == 'admin'
                        else url_for('vendor_dashboard') if user.role == 'vendor'
                        else url_for('transporter_dashboard') if user.role == 'transporter'
                        else url_for('dashboard'))

    # Check 3: brand new user — hold info in session, ask for mobile
    session['google_pending'] = {
        'email': email,
        'name': name,
        'google_id': google_id,
    }
    session.modified = True
    return redirect(url_for('google_complete_profile'))


@app.route('/auth/google/complete', methods=['GET', 'POST'])
def google_complete_profile():
    """New Google user — collect mobile + role."""
    pending = session.get('google_pending')
    if not pending:
        flash('Please start Google sign-in again.', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':
        mobile = request.form.get('mobile', '').strip()
        account_type = request.form.get('account_type', 'customer')
        city = request.form.get('city', 'Harda').strip()[:100] or 'Harda'
        company_name = request.form.get('company_name', '').strip() or None
        password = request.form.get('password', '').strip()
        confirm = request.form.get('confirm_password', '').strip()

        errors = []
        if not mobile or not validate_mobile(mobile):
            errors.append('Please enter a valid 10-digit mobile number.')
        elif User.query.filter_by(mobile=mobile).first():
            errors.append('This mobile number is already registered. Please login with mobile + password and link Google from your profile.')

        # Password validation
        if not password:
            errors.append('Password is required.')
        elif len(password) < 8:
            errors.append('Password must be at least 8 characters.')
        elif not re.search(r'[A-Za-z]', password):
            errors.append('Password must contain at least one letter.')
        elif not re.search(r'\d', password):
            errors.append('Password must contain at least one number.')
        elif _is_password_breached(password):
            errors.append('This password has appeared in a data breach. Please choose a different password.')
        if password != confirm:
            errors.append('Passwords do not match.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('auth/google_complete.html', pending=pending)

        if account_type == 'vendor':
            role = 'vendor'
        elif account_type == 'transporter':
            role = 'transporter'
        else:
            role = 'customer'
        # Retry loop guards against the public_id collision race. A fresh User is
        # built per attempt because a failed INSERT leaves the old object unusable.
        user = None
        _last_err = None
        for _attempt in range(3):
            _u = User(
                name=pending['name'][:100],
                mobile=mobile,
                email=pending['email'],
                role=role,
                city=city,
                google_id=pending['google_id'],
                public_id=generate_user_public_id(role),
                is_email_verified=True,  # Google already verified this address
            )
            if role == 'vendor' and company_name:
                _u.company_name = company_name
            if role == 'transporter':
                _u.transporter_city = city
                _u.transporter_kyc_status = 'unverified'
            _u.set_password(password)  # also allows mobile+password login
            db.session.add(_u)
            try:
                db.session.commit()
                user = _u
                _last_err = None
                break
            except IntegrityError as e:
                db.session.rollback()
                _last_err = e
                _msg = str(e).lower()
                if 'public_id' in _msg:
                    if _attempt == 2:
                        break
                    continue
                if 'mobile' in _msg:
                    flash('This mobile number is already registered. Please login with mobile + password.', 'danger')
                    return render_template('auth/google_complete.html', pending=pending)
                if 'email' in _msg:
                    flash('This email is already registered. Please login.', 'danger')
                    return render_template('auth/google_complete.html', pending=pending)
                raise
        if user is None:
            app.logger.error(f'Google registration failed after retries: {_last_err}')
            flash('Registration failed. Please try again.', 'danger')
            return render_template('auth/google_complete.html', pending=pending)

        # DPDP consent
        try:
            consent = ConsentLog(
                user_id=user.id,
                purpose='terms_google',
                granted=True,
                ip_address=request.headers.get('X-Forwarded-For', request.remote_addr),
                user_agent=request.headers.get('User-Agent', '')[:300]
            )
            db.session.add(consent)
            db.session.commit()
        except Exception:
            pass

        session.pop('google_pending', None)
        login_user(user, remember=True)
        flash(f'🎉 Account created via Google. Welcome, {user.name}!', 'success')
        if role == 'vendor':
            return redirect(url_for('vendor_dashboard'))
        if role == 'transporter':
            return redirect(url_for('transporter_kyc'))
        return redirect(url_for('dashboard'))

    return render_template('auth/google_complete.html', pending=pending)


# ============================================
# TRANSPORTER ROUTES
# ============================================
@app.route('/transporter')
@login_required
def transporter_dashboard():
    """Transporter dashboard — KYC status + assigned bookings."""
    if current_user.role != 'transporter':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    # KYC not submitted -> force KYC page
    if current_user.transporter_kyc_status in (None, 'unverified', 'rejected'):
        return redirect(url_for('transporter_kyc'))

    # Assigned bookings — active only
    active_statuses = ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']
    assigned = (Booking.query
                .filter_by(transporter_id=current_user.id)
                .filter(Booking.booking_status.in_(active_statuses))
                .order_by(Booking.start_date.asc())
                .all())
    recent = (Booking.query
              .filter_by(transporter_id=current_user.id)
              .filter(Booking.booking_status.in_(['completed', 'cancelled']))
              .order_by(Booking.updated_at.desc())
              .limit(10).all())

    return render_template('transporter/dashboard.html',
                           kyc_status=current_user.transporter_kyc_status,
                           assigned=assigned,
                           recent=recent,
                           trip_count=current_user.transporter_trip_count or 0)


@app.route('/transporter/booking/<int:booking_id>')
@login_required
def transporter_booking_detail(booking_id):
    """Transporter booking detail — shows item, route, OTP access."""
    if current_user.role != 'transporter':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    if current_user.transporter_kyc_status != 'verified':
        flash('KYC verify hone ke baad hi access milega.', 'warning')
        return redirect(url_for('transporter_kyc'))

    booking = Booking.query.get_or_404(booking_id)
    if booking.transporter_id != current_user.id:
        flash('Ye booking aapko assign nahi hai.', 'danger')
        return redirect(url_for('transporter_dashboard'))

    # Current OTPs (status only in the page; code is fetched on demand)
    otps = BookingOTP.query.filter_by(booking_id=booking.id).all()
    otp_map = {o.otp_type: o for o in otps}

    # UPI QR for transport charges (collected directly by transporter; amount left open)
    qr_base64 = None
    if current_user.transporter_upi_id:
        qr_base64 = generate_upi_qr_base64(
            upi_id=current_user.transporter_upi_id,
            name=current_user.name,
            amount=None
        )

    return render_template('transporter/booking_detail.html',
                           booking=booking,
                           otp_map=otp_map,
                           qr_base64=qr_base64)


@app.route('/transporter/booking/<int:booking_id>/view-otp/<otp_type>')
@login_required
def transporter_view_otp(booking_id, otp_type):
    """Return OTP code as JSON — view-only for the assigned transporter,
    until the receiving party verifies it."""
    if current_user.role != 'transporter' or current_user.transporter_kyc_status != 'verified':
        return jsonify({'error': 'Access denied'}), 403

    booking = Booking.query.get_or_404(booking_id)
    if booking.transporter_id != current_user.id:
        return jsonify({'error': 'Not assigned to you'}), 403

    if otp_type not in ('dispatch', 'return'):
        return jsonify({'error': 'Invalid OTP type'}), 400

    otp = BookingOTP.query.filter_by(booking_id=booking.id, otp_type=otp_type).first()
    if not otp:
        return jsonify({
            'error': 'not_generated',
            'message': f'{otp_type.title()} OTP abhi generate nahi hua hai. Sender se confirm karein.'
        }), 404

    if otp.status == 'verified':
        return jsonify({'error': 'already_verified',
                        'message': 'Ye OTP already verify ho chuka hai.'}), 400
    if otp.status == 'expired':
        return jsonify({'error': 'expired',
                        'message': 'OTP expire ho gaya. Sender se dubara generate karwayein.'}), 400
    if otp.status == 'locked':
        return jsonify({'error': 'locked',
                        'message': 'OTP locked hai. Admin se contact karein.'}), 400

    now = datetime.utcnow()
    if otp.expires_at < now:
        otp.status = 'expired'
        db.session.commit()
        return jsonify({'error': 'expired', 'message': 'OTP expire ho gaya.'}), 400

    return jsonify({
        'success': True,
        'code': otp.code,
        'otp_type': otp_type,
        'expires_at': otp.expires_at.isoformat(),
        'expires_display': (otp.expires_at + timedelta(hours=5, minutes=30)).strftime('%d %b, %I:%M %p'),
    })


@app.route('/booking/<int:booking_id>/rate-transporter', methods=['POST'])
@login_required
def rate_transporter(booking_id):
    """Customer or vendor rates the transporter after completion."""
    booking = Booking.query.get_or_404(booking_id)

    if booking.booking_status != 'completed':
        flash('Rating sirf completed booking pe possible hai.', 'warning')
        return redirect(url_for('dashboard'))
    if not booking.transporter_id:
        flash('Is booking pe koi transporter assign nahi tha.', 'info')
        return redirect(url_for('dashboard'))

    is_customer = (booking.customer_id == current_user.id)
    is_vendor = bool(booking.item and booking.item.vendor_id == current_user.id)
    if not (is_customer or is_vendor):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    rater_role = 'customer' if is_customer else 'vendor'

    def _back():
        if is_customer:
            return redirect(url_for('my_booking_detail', booking_id=booking.id))
        return redirect(url_for('vendor_booking_detail', booking_id=booking.id))

    existing = TransporterRating.query.filter_by(
        booking_id=booking.id, rater_id=current_user.id
    ).first()
    if existing:
        flash('Aap already rating de chuke hain is trip ke liye.', 'info')
        return _back()

    try:
        rating = int(request.form.get('rating', 0))
    except (ValueError, TypeError):
        rating = 0
    if rating < 1 or rating > 5:
        flash('Rating 1-5 ke beech hona chahiye.', 'danger')
        return _back()

    comment = (request.form.get('comment') or '').strip()[:500] or None

    db.session.add(TransporterRating(
        booking_id=booking.id,
        transporter_id=booking.transporter_id,
        rater_id=current_user.id,
        rater_role=rater_role,
        rating=rating,
        comment=comment,
    ))
    db.session.flush()

    all_ratings = TransporterRating.query.filter_by(
        transporter_id=booking.transporter_id
    ).all()
    if all_ratings:
        avg = sum(r.rating for r in all_ratings) / len(all_ratings)
        transporter = User.query.get(booking.transporter_id)
        if transporter:
            transporter.transporter_rating_avg = round(avg, 2)
            transporter.transporter_rating_count = len(all_ratings)

    db.session.commit()

    try:
        create_notification(
            booking.transporter_id, 'info',
            f'⭐ New {rating}-star rating',
            f'From {rater_role} on {booking.booking_reference}'
            + (f' — "{comment[:80]}"' if comment else ''),
            url_for('transporter_dashboard')
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    flash(f'✅ Thank you for rating ({rating}★)!', 'success')
    return _back()


@app.route('/transporter/toggle-availability', methods=['POST'])
@login_required
def transporter_toggle_availability():
    """Self-service: transporter marks self available / unavailable for new jobs."""
    if current_user.role != 'transporter':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    if current_user.transporter_kyc_status != 'verified':
        flash('KYC verify hone ke baad hi availability toggle kar sakte hain.', 'warning')
        return redirect(url_for('transporter_kyc'))

    current_user.transporter_available = not bool(current_user.transporter_available)
    db.session.commit()
    state = 'ON' if current_user.transporter_available else 'OFF'
    flash(f'✅ Availability {state}.', 'success')
    return redirect(request.referrer or url_for('transporter_dashboard'))


@app.route('/transporter/kyc', methods=['GET', 'POST'])
@login_required
def transporter_kyc():
    """Transporter KYC — license + vehicle photos."""
    if current_user.role != 'transporter':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    if request.method == 'POST':
        license_number = request.form.get('license_number', '').strip().upper()
        vehicle_number = request.form.get('vehicle_number', '').strip().upper().replace(' ', '')
        vehicle_type = request.form.get('vehicle_type', '').strip()
        upi_id = request.form.get('transporter_upi_id', '').strip().lower()

        valid_types = [v[0] for v in Config.TRANSPORTER_VEHICLE_TYPES]
        errors = []
        if not license_number or len(license_number) < 5:
            errors.append('Driving license number required (min 5 characters).')
        if not vehicle_number or len(vehicle_number) < 4:
            errors.append('Vehicle number required (e.g. MP05AB1234).')
        if vehicle_type not in valid_types:
            errors.append('Please select a valid vehicle type.')
        if upi_id and not re.match(r'^[a-z0-9._\-]{2,64}@[a-z][a-z0-9]{1,31}$', upi_id):
            errors.append('Invalid UPI ID (example: yourname@ybl).')

        license_photo = request.files.get('license_photo')
        vehicle_front = request.files.get('vehicle_front')
        vehicle_back = request.files.get('vehicle_back')

        if not license_photo or not license_photo.filename:
            errors.append('License photo required.')
        if not vehicle_front or not vehicle_front.filename:
            errors.append('Vehicle front photo required.')
        if not vehicle_back or not vehicle_back.filename:
            errors.append('Vehicle back photo required (number plate visible).')

        if not Config.CLOUDINARY_CLOUD_NAME:
            errors.append('Photo upload service not configured.')

        if errors:
            for e in errors:
                flash(e, 'danger')
            return render_template('transporter/kyc.html',
                                   vehicle_types=Config.TRANSPORTER_VEHICLE_TYPES,
                                   existing=current_user)

        # Upload 3 photos
        folder = f'vyahmandap/transporters/{current_user.id}'
        # All three uploads must succeed; otherwise delete whatever did upload (no orphans)
        _uploaded = []
        try:
            for _file in (license_photo, vehicle_front, vehicle_back):
                _res = upload_file_to_cloudinary(_file, folder=folder)
                if not _res or not _res.get('url'):
                    raise RuntimeError('photo upload failed')
                _uploaded.append(_res)
        except Exception as e:
            app.logger.warning(f'Transporter KYC upload aborted: {e}')
            for _u in _uploaded:
                _pid = _u.get('public_id')
                if _pid:
                    try:
                        cloudinary.uploader.destroy(_pid)
                    except Exception as _de:
                        app.logger.warning(f'Cleanup failed for {_pid}: {_de}')
            flash('Photo upload failed. Please try again.', 'danger')
            return render_template('transporter/kyc.html',
                                   vehicle_types=Config.TRANSPORTER_VEHICLE_TYPES,
                                   existing=current_user)
        up_license, up_front, up_back = _uploaded

        current_user.transporter_license_number = license_number[:50]
        current_user.transporter_license_photo = up_license.get('url')
        current_user.transporter_vehicle_number = vehicle_number[:20]
        current_user.transporter_vehicle_type = vehicle_type
        current_user.transporter_upi_id = upi_id or None
        current_user.transporter_vehicle_front = up_front.get('url')
        current_user.transporter_vehicle_back = up_back.get('url')
        current_user.transporter_kyc_status = 'pending'
        current_user.transporter_kyc_submitted_at = datetime.utcnow()
        current_user.transporter_kyc_notes = None
        db.session.commit()

        notify_all_admins(
            f'🚚 New transporter KYC — {current_user.name} ({current_user.public_id}) — {vehicle_number}',
            link=url_for('admin_users')
        )

        flash('✅ KYC submitted! Admin verification me 24 ghante lag sakte hain.', 'success')
        return redirect(url_for('transporter_dashboard'))

    return render_template('transporter/kyc.html',
                           vehicle_types=Config.TRANSPORTER_VEHICLE_TYPES,
                           existing=current_user)


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
        elif _is_password_breached(password):
            errors.append('This password has appeared in a data breach. Please choose a different password.')
        if password != confirm_password: errors.append('Passwords do not match.')
        if User.query.filter_by(mobile=mobile).first():
            errors.append('A user with this mobile number already exists.')
        
        if errors:
            for error in errors: flash(error, 'danger')
            return render_template('auth/register.html')
        
        if account_type == 'vendor':
            role = 'vendor'
        elif account_type == 'transporter':
            role = 'transporter'
        else:
            role = 'customer'
        city = request.form.get('city', 'Harda').strip()[:100] or 'Harda'
        # Retry loop guards against the public_id collision race. A fresh User is
        # built per attempt because a failed INSERT leaves the old object unusable.
        user = None
        _last_err = None
        for _attempt in range(3):
            _u = User(name=name, mobile=mobile, email=email, role=role, city=city)
            if role == 'vendor' and company_name:
                _u.company_name = company_name
            if role == 'transporter':
                _u.transporter_city = city
                _u.transporter_kyc_status = 'unverified'
            _u.set_password(password)
            _u.public_id = generate_user_public_id(role)
            db.session.add(_u)
            try:
                db.session.commit()
                user = _u
                _last_err = None
                break
            except IntegrityError as e:
                db.session.rollback()
                _last_err = e
                _msg = str(e).lower()
                if 'public_id' in _msg:
                    if _attempt == 2:
                        break
                    continue
                if 'mobile' in _msg:
                    flash('Mobile already registered. Please login.', 'danger')
                    return render_template('auth/register.html')
                if 'email' in _msg:
                    flash('Email already registered. Please login.', 'danger')
                    return render_template('auth/register.html')
                raise
        if user is None:
            app.logger.error(f'Registration failed after retries: {_last_err}')
            flash('Registration failed. Please try again.', 'danger')
            return render_template('auth/register.html')

        # Soft email verification: new accounts start unverified; never blocks signup.
        if user.email:
            try:
                user.is_email_verified = False
                _send_verification_email(user)
                db.session.commit()
            except Exception as e:
                app.logger.warning(f'Email verification dispatch failed: {e}')
                db.session.rollback()
        
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
        
        # Auto-login so transporter can immediately complete KYC
        if role == 'transporter':
            login_user(user, remember=True)
            flash('✅ Account created! Ab apna KYC complete karein.', 'success')
            return redirect(url_for('transporter_kyc'))

        flash('✅ Account created successfully! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('auth/register.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    # Clear user-specific session data so next login on same device is clean
    for key in ('cart', 'recent_views', 'recently_viewed', 'recently_viewed_bundles'):
        session.pop(key, None)
    flash('🔒 You have been logged out.', 'info')
    return redirect(url_for('index'))


# ============================================
# TELEGRAM LINKING ROUTES
# ============================================
# Telegram routes removed 2026-10-01 — notifications now in-app only


# ============================================
# DPDP — MY DATA ROUTES
# ============================================
@app.route('/verify-email/<token>')
def verify_email(token):
    """Confirm an email address from the emailed link."""
    user = User.query.filter_by(email_verify_token=token).first() if token else None
    if not user:
        flash('This verification link is invalid or already used.', 'warning')
        return redirect(url_for('login'))
    if user.email_verify_expires and user.email_verify_expires < datetime.utcnow():
        flash('This verification link has expired. Log in and request a new one from your profile.', 'warning')
        return redirect(url_for('login'))
    user.is_email_verified = True
    user.email_verify_token = None
    user.email_verify_expires = None
    db.session.commit()
    flash('✅ Email verified. Thank you!', 'success')
    if current_user.is_authenticated and current_user.id == user.id:
        return redirect(url_for('profile'))
    return redirect(url_for('login'))


@app.route('/resend-verification', methods=['POST'])
@login_required
@limiter.limit("3 per hour", methods=["POST"])
def resend_verification():
    if current_user.is_email_verified is not False or not current_user.email:
        flash('Your email is already verified.', 'info')
        return redirect(url_for('profile'))
    success = _send_verification_email(current_user)
    db.session.commit()

    if not success:
        flash('Verification email could not be sent right now. Please try again later or contact support.', 'danger')
    elif current_user.is_email_verified:
        flash('Email service is not configured, so your email was marked verified.', 'info')
    else:
        flash('Verification email sent. Check your inbox (and spam folder).', 'info')
    return redirect(url_for('profile'))


@app.route('/admin/test-email')
@login_required
def admin_test_email():
    """Admin-only SMTP check. Sends a test mail to the admin's own email (or MAIL_USERNAME)."""
    if current_user.role != 'admin':
        return "Access Denied", 403

    test_to = current_user.email or Config.MAIL_USERNAME
    if not test_to:
        return "<h2>❌ No recipient</h2><p>Admin account me email nahi hai aur MAIL_USERNAME bhi set nahi hai.</p>", 400

    success = send_email(
        test_to,
        "Test Email from VyahMandap",
        "<h1>It works!</h1><p>Agar ye email aa gayi, matlab SMTP sahi chal raha hai.</p>"
    )
    if success:
        return f"<h2>✅ Email sent to {html.escape(test_to)}</h2><p>Inbox/spam check karo.</p>"
    return (f"<h2>❌ Email failed</h2><p><b>Error:</b> <code>{html.escape(_last_email_error or 'unknown')}</code></p>"
            f"<p>MAIL_SERVER: {html.escape(str(Config.MAIL_SERVER))} &middot; MAIL_PORT: {html.escape(str(Config.MAIL_PORT))} "
            f"&middot; MAIL_USERNAME: {html.escape(str(Config.MAIL_USERNAME or ''))}</p>"), 500


@app.route('/profile')
@login_required
def profile():
    user = current_user

    # Quick counts for My Data section
    bookings_count = Booking.query.filter_by(customer_id=user.id).count()
    msg_count = Message.query.filter(
        (Message.sender_id == user.id) | (Message.receiver_id == user.id)
    ).count()
    payments_count = PaymentProof.query.filter_by(uploaded_by=user.id).count()
    tickets_count = Ticket.query.filter_by(user_id=user.id).count()

    return render_template('profile.html',
                         user=user,
                         bookings_count=bookings_count,
                         msg_count=msg_count,
                         payments_count=payments_count,
                         tickets_count=tickets_count,
                         now=datetime.utcnow())


@app.route('/profile/update', methods=['POST'])
@login_required
def profile_update():
    user = current_user

    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    company_name = request.form.get('company_name', '').strip() or None
    city = request.form.get('city', '').strip() or 'Harda'

    # Bank fields
    bank_account_name = request.form.get('bank_account_name', '').strip() or None
    bank_account_number = request.form.get('bank_account_number', '').strip() or None
    bank_ifsc = request.form.get('bank_ifsc', '').strip().upper() or None
    bank_name = request.form.get('bank_name', '').strip() or None
    bank_branch = request.form.get('bank_branch', '').strip() or None
    upi_id = request.form.get('upi_id', '').strip() or None

    errors = []
    if not name:
        errors.append('Name is required.')
    if email and not re.match(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email):
        errors.append('Please enter a valid email address.')
    elif email and User.query.filter(User.email == email, User.id != user.id).first():
        errors.append('This email is already used by another account.')
    if bank_ifsc and not re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', bank_ifsc):
        errors.append('IFSC format invalid (e.g. HDFC0001234).')
    if bank_account_number and not re.match(r'^[0-9]{6,30}$', bank_account_number):
        errors.append('Account number should be digits only (6-30 chars).')

    if errors:
        for e in errors:
            flash(e, 'danger')
        return redirect(url_for('profile'))

    _email_changed = bool(email) and email != (user.email or '').lower()
    user.name = name
    user.email = email or None
    if user.role == 'vendor':
        user.company_name = company_name
    user.city = city
    user.bank_account_name = bank_account_name
    user.bank_account_number = bank_account_number
    user.bank_ifsc = bank_ifsc
    user.bank_name = bank_name
    user.bank_branch = bank_branch
    user.upi_id = upi_id

    _verify_mail_failed = False
    if _email_changed:
        user.is_email_verified = False
        _verify_mail_failed = not _send_verification_email(user)

    db.session.commit()
    flash('✅ Profile updated.', 'success')
    if _verify_mail_failed:
        flash('⚠️ Verification email could not be sent. Use "Resend verification email" to try again.', 'warning')
    return redirect(url_for('profile'))


@app.route('/profile/change-password', methods=['POST'])
@login_required
def profile_change_password():
    user = current_user
    current = request.form.get('current_password', '').strip()
    new = request.form.get('new_password', '').strip()
    confirm = request.form.get('confirm_password', '').strip()

    errors = []
    if not current or not user.check_password(current):
        errors.append('Current password is incorrect.')
    if not new or len(new) < 8:
        errors.append('New password must be at least 8 characters.')
    elif not re.search(r'[A-Za-z]', new):
        errors.append('New password must contain at least one letter.')
    elif not re.search(r'\d', new):
        errors.append('New password must contain at least one number.')
    elif _is_password_breached(new):
        errors.append('This password has appeared in a data breach. Please choose a different password.')
    if new != confirm:
        errors.append('Passwords do not match.')

    if errors:
        for e in errors:
            flash(e, 'danger')
        return redirect(url_for('profile'))

    user.set_password(new)
    user.failed_login_attempts = 0
    user.locked_until = None
    db.session.commit()
    flash('✅ Password changed successfully.', 'success')
    return redirect(url_for('profile'))


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


@app.route('/health', methods=['GET', 'HEAD'])
@limiter.exempt
def health():
    """Lightweight keep-alive endpoint for cron-job.org / uptime pings.
    No DB, no template — tiny response so it never hits the size limit."""
    return 'ok', 200, {'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-store'}


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
        except Exception:
            pass
        next_available = get_next_available_date(item.id)

        # Card rate: commission-included (matches item page), variant-aware
        display_rate = item.rate_with_commission
        display_rate_raw = item.rate_per_day
        is_from = False
        if item.has_variants:
            _vars = ItemVariant.query.filter_by(item_id=item.id, is_available=True).all()
            if _vars:
                # "Standard" (base item) is also bookable, so it counts toward the minimum
                _lowest = min([v.rate_per_day for v in _vars] + [item.rate_per_day])
                display_rate = _lowest * (1 + Config.COMMISSION_RATE)
                display_rate_raw = _lowest
                is_from = True
        display_rate = int(round(display_rate))

        _for_rent = item.is_for_rent is not False
        if not _for_rent and item.is_for_sale and item.sale_price:
            display_rate = int(round(item.sale_price))   # sell-only: show sale price
            display_rate_raw = item.sale_price
            is_from = False

        result.append({
            'id': item.id, 'title': item.title, 'description': item.description,
            'category': item.category, 'rate': display_rate,
            'rate_raw': display_rate_raw,
            'rate_with_commission': item.rate_with_commission,
            'is_from': is_from,
            'has_variants': item.has_variants,
            'is_for_rent': _for_rent,
            'is_for_sale': bool(item.is_for_sale),
            'deposit': item.deposit_amount, 'stock': item.stock,
            'image': item.get_image(), 'vendor': get_display_name(item.vendor),
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

    # Optional pagination, backward compatible:
    #  no ?page=   -> legacy flat array (capped at 500 as a safety limit)
    #  ?page=N     -> {items, total, page, pages, per_page}
    page = request.args.get('page', type=int)
    if page and page >= 1:
        per_page = max(1, min(request.args.get('per_page', 50, type=int), 200))
        total = len(result)
        pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        return jsonify({'items': result[start:start + per_page], 'total': total,
                        'page': page, 'per_page': per_page, 'pages': pages})
    return jsonify(result[:500])


@app.route('/api/calculate', methods=['POST'])
def calculate():
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get('item_id'))
        start_date = datetime.strptime(data.get('start_date') or '', '%Y-%m-%d').date()
        end_date = datetime.strptime(data.get('end_date') or '', '%Y-%m-%d').date()
        quantity = int(data.get('quantity', 1))
    except (TypeError, ValueError):
        return jsonify({'error': True, 'message': 'Invalid or missing input.', 'conflict_type': 'validation_error'}), 400
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
    if item.is_for_rent is False:
        return jsonify({'error': True, 'message': 'This item is for sale only.',
                        'conflict_type': 'validation_error'}), 400

    # Variant items: price + stock come from the selected variant
    try:
        vid = int(data.get('variant_id') or 0)
    except (ValueError, TypeError):
        vid = 0
    # base=True + no variant -> "Standard" option: falls through to item-level pricing/stock
    if item.has_variants and (vid or not data.get('base')):
        variant = ItemVariant.query.filter_by(id=vid, item_id=item.id, is_available=True).first() if vid else None
        if not variant:
            return jsonify({'error': True, 'message': 'Please select a variant.', 'conflict_type': 'validation_error'}), 400
        vstock = variant.stock or 0
        if quantity > vstock:
            return jsonify({
                'error': True, 'message': f'Only {vstock} unit(s) of this variant available.',
                'conflict_type': 'insufficient_stock', 'available_qty': vstock,
                'requested_qty': quantity, 'next_available': None
            }), 400
        return jsonify(calculate_booking_total(item, start_date, end_date, quantity, area, weight,
                                               rate_override=variant.rate_per_day))

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
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']), _RENTAL_ONLY,
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
                Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']), _RENTAL_ONLY,
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


def _availability_scope(item):
    """Return (variant, active_bookings, live_stock) for the calendar APIs.
    Accepts ?variant= or ?variant_id=. Stock is a LIVE counter (already net of
    active bookings), so total physical units = live stock + units held by active
    bookings. Variant view counts only that variant's bookings; item-level view
    excludes variant bookings (they're tracked on the variant)."""
    vid = request.args.get('variant', type=int) or request.args.get('variant_id', type=int)
    variant = None
    if item.has_variants and vid:
        variant = ItemVariant.query.filter_by(id=vid, item_id=item.id).first()
    q = Booking.query.filter(
        Booking.item_id == item.id,
        Booking.booking_status.in_(['confirmed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']),
        _RENTAL_ONLY,
        _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
    )
    if variant:
        q = q.filter(Booking.variant_id == variant.id)
        live_stock = max(0, variant.stock or 0)
    else:
        q = q.filter(Booking.variant_id.is_(None))
        live_stock = max(0, item.stock or 0)
    return variant, q.all(), live_stock


@app.route('/api/item/<int:item_id>/availability-calendar')
def item_availability_calendar(item_id):
    item = Item.query.get_or_404(item_id)
    variant, bookings, live_stock = _availability_scope(item)
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
    
    # stock is a live counter -> physical total = free now + units held by active bookings
    total_units = live_stock + sum(b.quantity for b in bookings)
    events = []
    for date, booked_qty in date_booked.items():
        available_qty = live_stock
        if available_qty <= 0:
            status, color = 'booked', '#dc2626'
        elif available_qty < total_units:
            status, color = 'partial', '#eab308'
        else:
            status, color = 'available', '#16a34a'
        events.append({
            'title': f"{available_qty}/{total_units}",
            'start': date.isoformat(), 'allDay': True,
            'backgroundColor': color, 'borderColor': color,
            'extendedProps': {
                'status': status, 'total_stock': total_units,
                'booked_qty': booked_qty, 'available_qty': available_qty,
                'is_end_date': date in end_dates,
                'note': 'Return day' if date in end_dates else '',
                'variant_name': variant.variant_name if variant else None
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
    except Exception:
        return jsonify({'error': 'invalid date'}), 400

    variant, active, live_stock = _availability_scope(item)
    overlapping = [b for b in active if b.start_date <= check_date <= b.end_date]
    booked_qty = sum(b.quantity for b in overlapping)
    total_units = live_stock + sum(b.quantity for b in active)
    available_qty = live_stock
    status = 'booked' if available_qty <= 0 else ('partial' if available_qty < total_units else 'available')
    is_end_date = any(b.end_date == check_date for b in overlapping)
    
    return jsonify({
        'date': date_str, 'status': status, 'total_stock': total_units,
        'booked_qty': booked_qty, 'available_qty': available_qty, 'is_end_date': is_end_date,
        'variant_name': variant.variant_name if variant else None
    })


@app.route('/book/<int:item_id>', methods=['GET', 'POST'])
@limiter.limit("20 per minute", methods=["POST"])
@login_required
def book_item(item_id):
    item = Item.query.get_or_404(item_id)
    if not item.is_available:
        flash('⚠️ This item is currently unavailable for booking.', 'warning')
        return redirect(url_for('index'))
    if item.is_for_rent is False:
        flash('This item is for sale only — it cannot be rented.', 'warning')
        return redirect(url_for('item_detail', item_id=item.id))
    if not item.has_variants and (item.stock or 0) <= 0:
        flash('This item is out of stock.', 'danger')
        return redirect(url_for('index'))

    # Variant selection (optional). Variant items can also be booked as "Standard" (?base=1)
    selected_variant = None
    variant_id = request.args.get('variant', type=int) or request.form.get('variant_id', type=int)
    is_base_booking = (request.args.get('base') == '1' or request.form.get('base') == '1')
    if item.has_variants:
        if not variant_id and not is_base_booking:
            flash('Please select Standard or a variant to continue.', 'warning')
            return redirect(url_for('item_detail', item_id=item.id))
        if variant_id:
            selected_variant = ItemVariant.query.filter_by(id=variant_id, item_id=item.id, is_available=True).first()
            if not selected_variant:
                flash('Selected variant is not available.', 'warning')
                return redirect(url_for('item_detail', item_id=item.id))
            if (selected_variant.stock or 0) <= 0:
                flash('Selected variant is out of stock.', 'warning')
                return redirect(url_for('item_detail', item_id=item.id))
        elif (item.stock or 0) <= 0:
            flash('Standard option is out of stock.', 'warning')
            return redirect(url_for('item_detail', item_id=item.id))
    
    if request.method == 'POST':
        try:
            start_date = datetime.strptime(request.form.get('start_date'), '%Y-%m-%d').date()
            end_date = datetime.strptime(request.form.get('end_date'), '%Y-%m-%d').date()

            # Validate dates
            today_date = ist_today()
            if start_date < today_date:
                flash('⚠️ Start date cannot be in the past.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            if end_date < start_date:
                flash('⚠️ End date must be on or after start date.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            if (end_date - start_date).days > 365:
                flash('⚠️ Booking period cannot exceed 365 days.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)

            quantity = int(request.form.get('quantity', 1))
            if quantity <= 0:
                flash('⚠️ Quantity must be at least 1.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            area = request.form.get('area', 'city')
            weight = request.form.get('weight', 'till_20')
            venue_address = request.form.get('address', '').strip()
            utr = request.form.get('utr', '').strip()

            # Point 4 — mandatory payment screenshot
            screenshot = request.files.get('screenshot')
            if not screenshot or not screenshot.filename:
                flash('Payment screenshot is required.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            if not Config.CLOUDINARY_CLOUD_NAME:
                flash('Cloudinary not configured.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)

            if not venue_address:
                flash('Venue address is required.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            if not utr:
                flash('UTR number is required.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            if _utr_already_used(utr):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            # Lock the stock row(s) until commit so two simultaneous bookings
            # can't both take the last unit (no-op on SQLite).
            item = db.session.query(Item).filter_by(id=item.id).with_for_update().populate_existing().first()
            if selected_variant:
                selected_variant = db.session.query(ItemVariant).filter_by(
                    id=selected_variant.id).with_for_update().populate_existing().first()
            effective_stock = (selected_variant.stock or 0) if selected_variant else (item.stock or 0)
            if quantity > effective_stock:
                flash(f'Only {effective_stock} items available.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            
            if selected_variant:
                has_conflict, conflict_booking, available_qty, conflict_type = (False, None, effective_stock, None)
            else:
                has_conflict, conflict_booking, available_qty, conflict_type = check_availability_conflict(
                    item.id, start_date, quantity
                )
            if has_conflict:
                flash(f'⚠️ Only {available_qty} unit(s) available on {start_date.strftime("%d %b %Y")}.', 'warning')
                return render_template('booking.html', item=item, selected_variant=selected_variant)
            
            calc = calculate_booking_total(
                item, start_date, end_date, quantity, area, weight,
                rate_override=(selected_variant.rate_per_day if selected_variant else None)
            )
            kyc_required = calc['total'] >= 30000
            user_kyc_ok = kyc_required and _user_kyc_valid(current_user)
            aadhaar = None
            pan = None
            # Skip collection if user already has valid per-user KYC
            if kyc_required and not user_kyc_ok:
                aadhaar = request.form.get('aadhaar', '').strip()
                pan = request.form.get('pan', '').strip().upper()
                pan_file = request.files.get('pan_image')
                ok, err = _validate_kyc_inputs(aadhaar, pan, pan_file)
                if not ok:
                    flash(f'⚠️ {err}', 'danger')
                    return render_template('booking.html', item=item, selected_variant=selected_variant)
                # Auto-verify user for 1 year
                _mark_user_kyc_verified(current_user, aadhaar, pan)
                user_kyc_ok = True
            
            booking = Booking(
                booking_reference=generate_booking_reference(),
                customer_id=current_user.id, item_id=item.id,
                variant_id=(selected_variant.id if selected_variant else None),
                start_date=start_date, start_time=request.form.get('start_time', '10:00'),
                end_date=end_date, end_time=request.form.get('end_time', '20:00'),
                quantity=quantity, venue_address=venue_address,
                delivery_area=area, weight_category=weight,
                base_rent=calc['base_rent'], commission=calc['commission'],
                deposit=calc['deposit'], transport_fee=calc['transport_fee'],
                gateway_charge=calc['gateway_charge'],
                total_amount=calc['total'], utr_number=utr,
                payment_status='pending', booking_status='confirmed',
                kyc_required=kyc_required, aadhaar_number=_encrypt_kyc(aadhaar),
                pan_number=_encrypt_kyc(pan), kyc_verified=user_kyc_ok
            )
            # Decrement stock — variant if present, else item.
            # (Variants are not auto-disabled at 0: stock <= 0 already blocks booking.)
            if selected_variant:
                selected_variant.stock = max(0, (selected_variant.stock or 0) - quantity)
            else:
                item.stock -= quantity
                if item.stock <= 0 and not item.has_variants:
                    item.is_available = False
            db.session.add(booking)
            db.session.flush()  # need booking.id for PaymentProof

            # Point 4 — upload screenshot + create PaymentProof
            folder = f"vyahmandap/payments/booking_{booking.id}"
            upload_result = upload_file_to_cloudinary(screenshot, folder=folder)
            if not upload_result or not upload_result.get('url'):
                db.session.rollback()
                flash('Screenshot upload failed. Please try again.', 'danger')
                return render_template('booking.html', item=item, selected_variant=selected_variant)

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

            notify_all_admins(
                f"💰 New Payment Proof (Booking) — {booking.booking_reference} — ₹{proof.amount_paid:,.0f} ({proof.method.upper()})",
                link=url_for('admin_payment_queue')
            )

            if current_user.telegram_chat_id:
                send_telegram_notification_async(current_user.telegram_chat_id,
                    f"🎉 Booking Confirmed!\n\nItem: {item.title}\nRef: {booking.booking_reference}")
            # Vendor notification deferred until admin verifies payment
            db.session.commit()
            
            flash(f'🎉 Booking confirmed! Reference: {booking.booking_reference}', 'success')
            return redirect(url_for('dashboard'))
        except IntegrityError as e:
            db.session.rollback()
            if _is_utr_conflict(e):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            else:
                app.logger.exception('IntegrityError creating booking')
                flash('Could not create the booking. Please try again.', 'danger')
            payment_config = AdminPaymentConfig.get()
            qr_base64 = None
            if payment_config.upi_id:
                qr_base64 = generate_upi_qr_base64(
                    payment_config.upi_id, payment_config.upi_name, amount=None
                )
            return render_template('booking.html', item=item,
                                   selected_variant=selected_variant,
                                   payment_config=payment_config, qr_base64=qr_base64)
        except Exception as e:
            db.session.rollback()
            app.logger.exception('Error creating booking')
            flash('Could not create the booking. Please try again.', 'danger')
            payment_config = AdminPaymentConfig.get()
            qr_base64 = None
            if payment_config.upi_id:
                qr_base64 = generate_upi_qr_base64(
                    payment_config.upi_id, payment_config.upi_name, amount=None
                )
            return render_template('booking.html', item=item,
                                   selected_variant=selected_variant,
                                   payment_config=payment_config, qr_base64=qr_base64)
    payment_config = AdminPaymentConfig.get()
    qr_base64 = None
    if payment_config.upi_id:
        qr_base64 = generate_upi_qr_base64(
            payment_config.upi_id, payment_config.upi_name, amount=None
        )
    return render_template('booking.html', item=item,
                           selected_variant=selected_variant,
                           payment_config=payment_config, qr_base64=qr_base64)


@app.route('/dashboard')
@login_required
def dashboard():
    bookings = Booking.query.filter_by(customer_id=current_user.id)\
        .filter(Booking.parent_booking_id.is_(None))\
        .order_by(Booking.created_at.desc()).all()
    total_spent = sum(b.total_amount for b in bookings)
    active_bookings = sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received'])
    pending_return = [b for b in bookings 
                     if b.dispatch_report_done and not b.return_report_done 
                     and b.booking_status in ['dispatched', 'in_use', 'confirmed']]
    
    return render_template('dashboard.html', 
                         bookings=bookings, total_spent=total_spent, 
                         active_bookings=active_bookings, pending_return=pending_return)


@app.route('/my-booking/<int:booking_id>')
@login_required
def my_booking_detail(booking_id):
    booking = Booking.query.get_or_404(booking_id)

    is_customer = (booking.customer_id == current_user.id)
    is_admin = (current_user.role == 'admin')
    is_vendor_of_item = (
        booking.item and booking.item.vendor_id == current_user.id
    )
    # Vendor who accepted SOS
    is_sos_vendor = False
    try:
        sos = SOSRequest.query.filter_by(booking_id=booking.id).first()
        if sos and sos.accepted_by == current_user.id:
            is_sos_vendor = True
    except Exception:
        pass

    if not (is_customer or is_admin or is_vendor_of_item or is_sos_vendor):
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
    
    otps = BookingOTP.query.filter_by(booking_id=booking.id).all()
    otp_map = {o.otp_type: o for o in otps}

    return render_template('my_booking.html',
                         booking=booking, payment_config=payment_config,
                         qr_base64=qr_base64, proofs=proofs,
                         otp_map=otp_map)


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
    if request.content_length and request.content_length > Config.MAX_VIDEO_UPLOAD_BYTES:
        return jsonify({'error': f'Video too large (max {Config.MAX_VIDEO_UPLOAD_BYTES // (1024 * 1024)} MB)'}), 413
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

    # Auto-generate OTP after report submission
    try:
        auto_otp_type = 'dispatch' if report_type == 'dispatch' else 'return'
        auto_otp, auto_err = _get_or_create_otp(booking.id, auto_otp_type, current_user.id)
        if auto_otp and not auto_err:
            db.session.commit()
            _send_otp_notification(auto_otp)
    except Exception as e:
        app.logger.warning(f'Auto-OTP generation failed for booking {booking.id}: {e}')
    
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
    _was_unpaid = booking.payment_status not in ('paid', 'partial')
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
    # Vendor(s) hear about the booking only now (first verified payment)
    if _was_unpaid:
        try:
            _notify_vendors_of_payment_verified(booking)
        except Exception:
            app.logger.warning('Vendor payment-verified notification failed', exc_info=True)
    db.session.commit()
    
    flash('✅ Payment proof verified.', 'success')
    return redirect(request.referrer or url_for('admin_payment_queue'))


def _restore_units(item_id, variant_id, qty):
    """Give `qty` units back to a variant (if the booking was for one) or to the item.
    Variants are never auto-toggled via is_available — stock <= 0 already blocks booking,
    and is_available is reserved for the vendor (soft-disabled variants must stay disabled)."""
    if not item_id:
        return
    qty = qty or 1
    if variant_id:
        var = ItemVariant.query.get(variant_id)
        if var:
            var.stock = (var.stock or 0) + qty
        return
    item = Item.query.get(item_id)
    if item:
        item.stock = (item.stock or 0) + qty
        if item.stock > 0 and not item.is_available:
            item.is_available = True


def _reserve_units(item_id, variant_id, qty):
    """Opposite of _restore_units (used when a cancelled booking is re-activated)."""
    if not item_id:
        return
    qty = qty or 1
    if variant_id:
        var = ItemVariant.query.get(variant_id)
        if var:
            var.stock = max(0, (var.stock or 0) - qty)
        return
    item = Item.query.get(item_id)
    if item:
        item.stock = max(0, (item.stock or 0) - qty)
        if item.stock <= 0:
            item.is_available = False


def _restore_completed_stock(booking, old_status):
    """Rental finished -> units go back on the shelf (variant-aware).
    Call ONLY on a real transition INTO 'completed' (pass the status it had just before).
    Guard: completed/cancelled already settled their stock. Sales are permanent;
    bundle/cart parents never held stock (only their children do)."""
    if old_status in ('completed', 'cancelled'):
        return
    if (booking.order_type or 'rent') in ('sale', 'bundle', 'cart'):
        return
    _restore_units(booking.item_id, booking.variant_id, booking.quantity)


def _bump_transporter_trip(booking, old_status):
    """+1 trip for the assigned transporter on a real transition INTO 'completed'.
    Idempotent: a booking credits its transporter at most once, even across
    completed -> confirmed -> completed cycles. Caller commits."""
    if old_status in ('completed', 'cancelled'):
        return
    if not booking.transporter_id:
        return
    if booking.transporter_trip_counted_at:
        return
    t = User.query.get(booking.transporter_id)
    if t:
        t.transporter_trip_count = (t.transporter_trip_count or 0) + 1
        booking.transporter_trip_counted_at = datetime.utcnow()


def _restore_booking_stock(booking):
    """Restore item/variant stock when a booking is cancelled.
    Handles parent/child hierarchy (bundle, cart) correctly — parent has
    quantity=1 and stock is only decremented on children."""
    children = Booking.query.filter_by(parent_booking_id=booking.id).all()
    if children:
        for child in children:
            if child.booking_status == 'cancelled':
                continue  # already cancelled -> stock already restored
            child.booking_status = 'cancelled'
            child.cancelled_at = datetime.utcnow()
            child.cancelled_by = 'admin'
            child.refund_amount = 0
            _restore_units(child.item_id, child.variant_id, child.quantity)
    else:
        _restore_units(booking.item_id, booking.variant_id, booking.quantity)


def _reserve_booking_stock(booking):
    """Re-reserve stock when an admin re-activates a cancelled booking."""
    children = Booking.query.filter_by(parent_booking_id=booking.id).all()
    if children:
        for child in children:
            if child.booking_status != 'cancelled':
                continue
            child.booking_status = booking.booking_status
            _reserve_units(child.item_id, child.variant_id, child.quantity)
    else:
        _reserve_units(booking.item_id, booking.variant_id, booking.quantity)


def _notify_vendors_of_cancellation(booking, reason=''):
    """Notify vendor(s) when a booking is cancelled.
    Bundle/cart parents: notify each vendor with their child booking link
    (deduped by vendor — one notification per vendor)."""
    notified = set()
    children = Booking.query.filter_by(parent_booking_id=booking.id).all()
    if children:
        for child in children:
            item = Item.query.get(child.item_id)
            if not item or not item.vendor_id or item.vendor_id in notified:
                continue
            notified.add(item.vendor_id)
            try:
                create_notification(
                    item.vendor_id, 'cancel', 'Booking Cancelled',
                    f'{booking.booking_reference} cancelled. Reason: {reason or "—"}',
                    f'/vendor/booking/{child.id}'
                )
            except Exception:
                pass
    else:
        item = Item.query.get(booking.item_id)
        if item and item.vendor_id and item.vendor_id not in notified:
            notified.add(item.vendor_id)
            try:
                create_notification(
                    item.vendor_id, 'cancel', 'Booking Cancelled',
                    f'{booking.booking_reference} cancelled. Reason: {reason or "—"}',
                    f'/vendor/booking/{booking.id}'
                )
            except Exception:
                pass


def _notify_vendors_of_payment_verified(booking):
    """Tell vendor(s) about a new booking once payment is verified.
    Cart/bundle parents: one notification per vendor, linked to that vendor's child booking.
    Caller commits."""
    targets = {}  # vendor_id -> booking id to link
    children = Booking.query.filter_by(parent_booking_id=booking.id).all()
    if children:
        for child in children:
            item = Item.query.get(child.item_id) if child.item_id else None
            if item and item.vendor_id and item.vendor_id not in targets:
                targets[item.vendor_id] = child.id
    elif booking.item and booking.item.vendor_id:
        targets[booking.item.vendor_id] = booking.id

    is_sale = (booking.order_type == 'sale')
    title = 'New Sale!' if is_sale else 'New Booking Received'
    for vendor_id, link_id in targets.items():
        try:
            create_notification(
                vendor_id, 'booking', title,
                f'{booking.booking_reference} — payment verified',
                f'/vendor/booking/{link_id}'
            )
            vendor = User.query.get(vendor_id)
            if vendor and vendor.telegram_chat_id:
                send_telegram_notification_async(
                    vendor.telegram_chat_id,
                    f"📦 {title}\n\nRef: {booking.booking_reference}\nPayment verified ✅"
                )
        except Exception:
            app.logger.warning('Vendor notify failed', exc_info=True)


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

    # 2-strike policy:
    #   1st reject  → booking stays, customer can re-upload
    #   2nd reject  → booking cancelled + closed
    other_verified = PaymentProof.query.filter(
        PaymentProof.booking_id == proof.booking_id,
        PaymentProof.status == 'verified',
        PaymentProof.id != proof.id
    ).first()
    other_pending = PaymentProof.query.filter(
        PaymentProof.booking_id == proof.booking_id,
        PaymentProof.status == 'pending',
        PaymentProof.id != proof.id
    ).first()

    rejected_count = PaymentProof.query.filter(
        PaymentProof.booking_id == proof.booking_id,
        PaymentProof.status == 'rejected'
    ).count()

    booking_cancelled = False
    strike_count = 0

    if other_verified or other_pending:
        # Nothing to do — booking continues (verified/pending proof exists).
        # payment_status deliberately left untouched so a 'paid' booking stays 'paid'.
        pass
    elif rejected_count >= 2:
        # 2nd strike → cancel + close
        booking = proof.booking
        if booking.booking_status not in ('cancelled', 'completed'):
            booking_cancelled = True
            strike_count = 2
            booking.booking_status = 'cancelled'
            booking.payment_status = 'rejected'
            booking.cancelled_at = datetime.utcnow()
            booking.cancellation_reason = f'Payment rejected twice by admin: {reason}'
            booking.cancelled_by = 'admin'
            booking.refund_amount = 0
            _restore_booking_stock(booking)
        else:
            booking.payment_status = 'rejected'
    else:
        # 1st strike → allow re-upload, keep booking active
        strike_count = 1
        proof.booking.payment_status = 'rejected'
        # booking_status stays as-is (confirmed/pending) so customer can re-upload

    db.session.commit()
    
    if proof.booking.customer.telegram_chat_id:
        send_telegram_notification_async(proof.booking.customer.telegram_chat_id,
            f"❌ Payment Proof Rejected\n\nBooking: {proof.booking.booking_reference}\n"
            f"Reason: {reason}")
    
    # Customer notification — depends on strike count
    if booking_cancelled:
        create_notification(
            proof.booking.customer_id, 'payment', '❌ Booking Cancelled — Payment Rejected Twice',
            f'{proof.booking.booking_reference} cancelled after 2 rejected proofs. Reason: {reason}. '
            f'Please re-book or raise a support ticket.',
            f'/my-booking/{proof.booking.id}'
        )
        _notify_vendors_of_cancellation(
            proof.booking, f'Payment rejected twice: {reason}'
        )
    elif strike_count == 1:
        create_notification(
            proof.booking.customer_id, 'payment', '⚠️ Payment Rejected — 1 Attempt Left',
            f'{proof.booking.booking_reference} — reason: {reason}. '
            f'Upload correct proof now; a 2nd rejection will cancel the booking.',
            f'/my-booking/{proof.booking.id}'
        )
    else:
        create_notification(
            proof.booking.customer_id, 'payment', 'Payment Proof Rejected',
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
    if booking.booking_status == 'cancelled':
        flash('This booking is cancelled. Please re-book.', 'danger')
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
        except Exception:
            payment_date = None
    
    try:
        amount_paid = float(request.form.get('amount_paid', 0) or 0)
    except Exception:
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
    bookings = Booking.query.filter(
        Booking.item_id.in_(item_ids),
        _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
    ).order_by(Booking.created_at.desc()).all() if item_ids else []
    _active_statuses = ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']
    completed_bookings = [b for b in bookings if b.booking_status == 'completed']
    active_bookings = [b for b in bookings if b.booking_status in _active_statuses]
    total_base, total_sos_bonus = _vendor_share_sum(completed_bookings)
    total_earnings = total_base + total_sos_bonus
    expected_base, expected_sos = _vendor_share_sum(active_bookings)
    expected_earnings = expected_base + expected_sos
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_sos_bonus=total_sos_bonus,
                         expected_earnings=expected_earnings,
                         total_bookings=len(bookings),
                         active_rentals=sum(1 for b in bookings if b.booking_status in _active_statuses),
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
                         active_rentals=sum(1 for b in bookings if b.booking_status in ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']),
                         total_rentals=len(bookings),
                         pending_return=[b for b in bookings if b.dispatch_report_done and not b.return_report_done and b.booking_status in ['dispatched', 'in_use', 'confirmed']])


@app.route('/vendor')
@login_required
def vendor_dashboard():
    if current_user.role not in ['admin', 'vendor']:
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    items = Item.query.filter_by(vendor_id=current_user.id).order_by(Item.created_at.desc()).all()
    item_ids = [i.id for i in items]
    bookings = Booking.query.filter(
        Booking.item_id.in_(item_ids),
        _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
    ).order_by(Booking.created_at.desc()).all() if item_ids else []
    _active_statuses = ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']
    completed_bookings = [b for b in bookings if b.booking_status == 'completed']
    active_bookings = [b for b in bookings if b.booking_status in _active_statuses]
    total_base, total_sos_bonus = _vendor_share_sum(completed_bookings)
    total_earnings = total_base + total_sos_bonus
    expected_base, expected_sos = _vendor_share_sum(active_bookings)
    expected_earnings = expected_base + expected_sos
    return render_template('vendor/dashboard.html',
                         items=items, bookings=bookings,
                         total_earnings=total_earnings,
                         total_sos_bonus=total_sos_bonus,
                         expected_earnings=expected_earnings,
                         total_bookings=len(bookings),
                         active_rentals=sum(1 for b in bookings if b.booking_status in _active_statuses),
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
        _sell_only = (request.form.get('item_type') == 'sale')
        if not title or (rate <= 0 and not _sell_only):
            flash('Title and rate are required.', 'danger')
            return render_template('vendor/item_form.html')
        item = Item(title=title, description=description, category=category,
                    rate_per_day=rate, deposit_amount=0, stock=stock,
                    image_url=image_url if image_url else None,
                    image_filename=image_filename, vendor_id=current_user.id)
        _sale_err = _apply_sale_fields(item)
        if _sale_err:
            flash(f'⚠️ {_sale_err}', 'danger')
            return render_template('vendor/item_form.html')
        # 14 Oct 2026 — dimensions
        _apply_dimensions(item)
        # 14 Oct 2026 — variants flag
        has_variants = bool(request.form.get('has_variants'))
        item.has_variants = has_variants
        db.session.add(item)
        db.session.flush()  # get item.id
        # Save variants if flagged
        if has_variants:
            ok, err = _save_variants(item)
            if not ok:
                db.session.rollback()
                flash(f'⚠️ {err}', 'danger')
                return render_template('vendor/item_form.html')
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
        _sale_err = _apply_sale_fields(item)
        if _sale_err:
            db.session.rollback()
            flash(f'⚠️ {_sale_err}', 'danger')
            return render_template('vendor/item_form.html', item=Item.query.get(item_id))
        # 14 Oct 2026 — dimensions + variants flag
        _apply_dimensions(item)
        has_variants = bool(request.form.get('has_variants'))
        item.has_variants = has_variants
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
        # 14 Oct 2026 — variants (upsert). When the toggle is off, existing variant
        # rows are left untouched so booking history / re-enabling still works.
        if has_variants:
            ok, err = _save_variants(item)
            if not ok:
                db.session.rollback()
                flash(f'⚠️ {err} (Changes were not saved.)', 'danger')
                return render_template('vendor/item_form.html', item=Item.query.get(item_id))
        db.session.commit()
        flash('✅ Item updated!', 'success')
        return redirect(url_for('vendor_dashboard'))
    return render_template('vendor/item_form.html', item=item)


def _delete_item_safely(item):
    """Delete an item only if nothing important references it.
    Returns (ok, message, flash_category)."""
    if Booking.query.filter_by(item_id=item.id).first() or \
       SOSRequest.query.filter_by(item_id=item.id).first():
        return (False, '⚠️ This item has booking history and cannot be deleted. '
                       'Mark it as "Unavailable" instead.', 'warning')
    if BundleItem.query.filter_by(item_id=item.id).first():
        return (False, '⚠️ This item is used inside a Bundle/Package. '
                       'Remove it from the Bundle first, then delete.', 'warning')
    if ItemVerificationRequest.query.filter_by(item_id=item.id).first():
        return (False, '⚠️ This item has verification payment records and cannot be deleted. '
                       'Mark it as "Unavailable" instead.', 'warning')
    try:
        ItemView.query.filter_by(item_id=item.id).delete(synchronize_session=False)
        Wishlist.query.filter_by(item_id=item.id).delete(synchronize_session=False)
        Review.query.filter_by(item_id=item.id).delete(synchronize_session=False)
        ItemVariant.query.filter_by(item_id=item.id).delete(synchronize_session=False)
        # Chat messages may reference the item; keep the messages, drop the link
        Message.query.filter_by(item_id=item.id).update({'item_id': None}, synchronize_session=False)
        db.session.delete(item)
        db.session.commit()
        return (True, '✅ Item deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f'Delete item failed: {e}')
        return (False, '⚠️ Could not delete the item. Mark it as "Unavailable" instead.', 'danger')


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
    ok, msg, category = _delete_item_safely(item)
    flash(msg, category)
    return redirect(request.referrer or url_for('vendor_dashboard')) if not ok else redirect(url_for('vendor_dashboard'))


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


def _grant_item_verification(item, days):
    """Extend (or start) the verified badge. Caller commits."""
    item.is_verified = True
    if item.verified_until and item.verified_until > datetime.utcnow():
        item.verified_until += timedelta(days=days)
    else:
        item.verified_until = datetime.utcnow() + timedelta(days=days)


def _render_verify_item(item, pending=None):
    cfg = AdminPaymentConfig.get()
    qr_base64 = None
    if cfg.upi_id:
        qr_base64 = generate_upi_qr_base64(cfg.upi_id, cfg.upi_name, amount=VERIFICATION_PRICE)
    return render_template('vendor/verify_item.html',
                           item=item,
                           price=VERIFICATION_PRICE,
                           days=VERIFICATION_DAYS,
                           per_day=round(VERIFICATION_PRICE / VERIFICATION_DAYS, 1),
                           app_name=cfg.upi_name or 'VyahMandap',
                           qr_base64=qr_base64,
                           pending=pending)


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

    pending = ItemVerificationRequest.query.filter_by(
        item_id=item.id, status='pending').first()

    if request.method == 'POST':
        if pending:
            flash('⏳ Your payment is already waiting for admin approval.', 'info')
            return redirect(url_for('vendor_verify_item', item_id=item.id))
        utr = request.form.get('utr', '').strip()
        if not utr:
            flash('UTR required.', 'danger')
            return _render_verify_item(item)
        if not re.fullmatch(r'[A-Za-z0-9]{8,30}', utr):
            flash('UTR 8-30 letters/numbers ka hona chahiye (spaces ya symbols nahi).', 'danger')
            return _render_verify_item(item)
        # Same UTR cannot be reused for another request (pending or approved)
        dup = _utr_already_used(utr)
        if dup:
            flash('Ye UTR pehle hi use ho chuka hai. Sahi UTR daalein.', 'danger')
            return _render_verify_item(item)

        req = ItemVerificationRequest(
            item_id=item.id, vendor_id=item.vendor_id, utr=utr,
            amount=VERIFICATION_PRICE, days=VERIFICATION_DAYS)
        db.session.add(req)
        db.session.commit()
        notify_all_admins(
            f'Verification payment: "{item.title}" - Rs {VERIFICATION_PRICE}, UTR {utr}',
            link=url_for('admin_verification_requests'))
        flash('✅ Payment submit ho gaya. Admin check karke badge activate kar dega.', 'success')
        return redirect(url_for('vendor_dashboard'))
    return _render_verify_item(item, pending=pending)


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
    # BEHAVIOUR CHANGE: reviews only after completion (was: completed OR confirmed).
    if booking.booking_status != 'completed':
        flash('You can review this booking once it is completed.', 'warning')
        return redirect(url_for('dashboard'))
    if Review.query.filter_by(booking_id=booking_id).first():
        flash('Already reviewed.', 'info')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        try:
            rating = int(request.form.get('rating', 0))
        except (ValueError, TypeError):
            rating = 0
        comment = request.form.get('comment', '').strip()
        if rating < 1 or rating > 5:
            flash('Rating must be 1-5.', 'danger')
            return render_template('review_form.html', booking=booking)
        if len(comment) > 2000:
            flash('Review is too long (max 2000 characters).', 'danger')
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
    page = request.args.get('page', 1, type=int)
    pagination = query.order_by(Ticket.last_reply_at.desc()).paginate(page=page, per_page=50, error_out=False)
    tickets = pagination.items
    stats = {
        'total': Ticket.query.count(),
        'open': Ticket.query.filter_by(status='open').count(),
        'in_progress': Ticket.query.filter_by(status='in_progress').count(),
        'resolved': Ticket.query.filter_by(status='resolved').count(),
        'closed': Ticket.query.filter_by(status='closed').count(),
    }
    return render_template('admin/tickets.html', tickets=tickets, pagination=pagination, stats=stats,
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
def _vendor_share_sum(bookings):
    """Return (base_share, sos_bonus) for a vendor's bookings.
    Uses vendor_share if set (bundle/cart children), else base_rent.
    Caller must pre-filter out parents (order_type in ('bundle','cart'))."""
    base = 0.0
    sos = 0.0
    for b in bookings:
        if b.vendor_share is not None:
            base += (b.vendor_share or 0)
        else:
            base += (b.base_rent or 0)
        sos += (b.sos_bonus or 0)
    return round(base, 2), round(sos, 2)


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
            Booking.item_id.in_(item_ids),
            _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
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
            Item.vendor_id == vendor_id,
            _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
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
            Booking.booking_status.in_(['completed', 'return_initiated']),
            _or(Booking.order_type.is_(None), Booking.order_type.notin_(['bundle', 'cart'])),
        )
        if cutoff_date:
            booking_query = booking_query.filter(Booking.created_at >= cutoff_date)
        vendor_bookings = booking_query.all()
        total_bookings = len(vendor_bookings)
        if total_bookings < 5:
            continue
        total_earnings, _ = _vendor_share_sum(vendor_bookings)
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
    top_3 = rankings[:3] if len(rankings) >= 3 else rankings
    rest_full = rankings[3:] if len(rankings) > 3 else []
    per_page = 25
    total_rest = len(rest_full)
    pages = max(1, (total_rest + per_page - 1) // per_page)
    page = max(1, min(request.args.get('page', 1, type=int), pages))
    rest = rest_full[(page - 1) * per_page: page * per_page]
    return render_template('leaderboard.html', top_3=top_3, rest=rest,
                         total_vendors=len(rankings), page=page, pages=pages, total_rest=total_rest,
                         period=period, category_filter=category)


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

        if not _check_reauth_password():
            flash('⚠️ Re-enter your admin password to run a bulk data action.', 'danger')
            return redirect(url_for('admin_data_management'))
        # Log the attempt up front (committed immediately, survives later rollbacks)
        _audit_admin('data_mgmt_bulk', details=f'action={action}; confirm={confirm}')

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

            elif action == 'clear_wishlist':
                if confirm != 'DELETE_WISHLIST':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                n = Wishlist.query.delete()
                db.session.commit()
                flash(f'✅ Cleared {n} wishlist entries.', 'success')

            elif action == 'reset_item_stock':
                if confirm != 'RESET_STOCK':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                n = Item.query.update({'stock': 10, 'is_available': True})
                ItemVariant.query.update({'stock': 10, 'is_available': True})
                db.session.commit()
                flash(f'✅ Reset stock to 10 for {n} items.', 'success')

            elif action == 'delete_demo_customers':
                if confirm != 'DELETE_DEMO_USERS':
                    flash('Confirmation text mismatch.', 'danger')
                    return redirect(url_for('admin_data_management'))
                demo_users = User.query.filter(
                    User.role == 'customer',
                    User.name.in_(['Demo Customer', 'Test Customer'])
                ).all()
                deleted, skipped = 0, 0
                for u in demo_users:
                    if Booking.query.filter_by(customer_id=u.id).count() > 0:
                        skipped += 1  # bookings hain to pehle "Clear Bookings" chalao
                        continue
                    Wishlist.query.filter_by(user_id=u.id).delete()
                    Notification.query.filter_by(user_id=u.id).delete()
                    ConsentLog.query.filter_by(user_id=u.id).delete()
                    DataRequest.query.filter_by(user_id=u.id).delete()
                    PushSubscription.query.filter_by(user_id=u.id).delete()
                    ItemView.query.filter_by(user_id=u.id).delete()
                    Review.query.filter_by(customer_id=u.id).delete()
                    Message.query.filter(
                        (Message.sender_id == u.id) | (Message.receiver_id == u.id)
                    ).delete(synchronize_session=False)
                    t_ids = [t.id for t in Ticket.query.filter_by(user_id=u.id).all()]
                    if t_ids:
                        TicketReply.query.filter(TicketReply.ticket_id.in_(t_ids)).delete(synchronize_session=False)
                        Ticket.query.filter(Ticket.id.in_(t_ids)).delete(synchronize_session=False)
                    TicketReply.query.filter_by(user_id=u.id).delete()
                    db.session.delete(u)
                    deleted += 1
                db.session.commit()
                msg = f'✅ Deleted {deleted} demo customers.'
                if skipped:
                    msg += f' Skipped {skipped} (bookings exist, pehle Clear Bookings karo).'
                flash(msg, 'success')

            else:
                flash('Unknown action.', 'danger')

        except Exception as e:
            db.session.rollback()
            app.logger.exception('Data management action failed')
            _audit_admin('data_mgmt_bulk_failed', details=f'action={action}; error={str(e)[:200]}')
            flash('❌ Action failed. Check server logs for details.', 'danger')

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
    token = request.args.get('token') or request.form.get('token') or ''
    import hmac as _hmac
    if not Config.CRON_SECRET or not _hmac.compare_digest(token, Config.CRON_SECRET):
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

    # 4. Auto-cancel stale unpaid bookings (release stock)
    ttl_cutoff = NOW - timedelta(hours=Config.PAYMENT_PENDING_TTL_HOURS)
    stale_parents = Booking.query.filter(
        Booking.parent_booking_id.is_(None),
        Booking.booking_status.in_(['confirmed', 'pending']),
        Booking.payment_status == 'pending',
        Booking.created_at < ttl_cutoff,
        Booking.order_type.notin_(['bundle', 'cart']),  # only top-level simple bookings
    ).all()
    # Cart/bundle parents (order_type in ('bundle','cart'))
    stale_parents += Booking.query.filter(
        Booking.parent_booking_id.is_(None),
        Booking.booking_status.in_(['confirmed', 'pending']),
        Booking.payment_status == 'pending',
        Booking.created_at < ttl_cutoff,
        Booking.order_type.in_(['bundle', 'cart']),
    ).all()

    auto_cancel = {'cancelled': 0, 'restored_skipped': 0}
    for bk in stale_parents:
        try:
            # Skip if any proof is pending review (admin actively looking)
            has_pending_proof = PaymentProof.query.filter_by(
                booking_id=bk.id, status='pending'
            ).first()
            if has_pending_proof:
                continue
            # Skip if any verified proof (paid)
            has_verified_proof = PaymentProof.query.filter_by(
                booking_id=bk.id, status='verified'
            ).first()
            if has_verified_proof:
                continue

            bk.booking_status = 'cancelled'
            bk.payment_status = 'rejected'
            bk.cancelled_at = NOW
            bk.cancellation_reason = f'Auto-cancelled: payment not received within {Config.PAYMENT_PENDING_TTL_HOURS}h'
            bk.cancelled_by = 'system'
            bk.refund_amount = 0
            _restore_booking_stock(bk)
            auto_cancel['cancelled'] += 1

            # Notify customer
            create_notification(
                bk.customer_id, 'cancel', 'Booking Auto-Cancelled',
                f'{bk.booking_reference} was auto-cancelled — no payment received within {Config.PAYMENT_PENDING_TTL_HOURS} hours. Stock has been released.',
                f'/my-booking/{bk.id}'
            )
            # Notify vendors
            _notify_vendors_of_cancellation(bk, 'Payment not received (auto-cancel)')
        except Exception as e:
            app.logger.error(f'Auto-cancel failed for booking {bk.id}: {e}')
            auto_cancel['restored_skipped'] += 1

    db.session.commit()

    # 5. Auto-unblock users whose block has expired
    expired_blocks = User.query.filter(
        User.is_blocked == True,
        User.block_expires_at != None,
        User.block_expires_at <= NOW,
    ).all()
    unblock_stats = {'unblocked': 0}
    for u in expired_blocks:
        u.is_blocked = False
        u.blocked_at = None
        u.blocked_by = None
        u.block_reason = None
        u.block_expires_at = None
        if u.role == 'vendor':
            Item.query.filter_by(vendor_id=u.id).update({'is_available': True})
            Bundle.query.filter_by(vendor_id=u.id).update({'is_active': True})
        unblock_stats['unblocked'] += 1
    db.session.commit()

        # 5. Expire stale OTPs (past expires_at, still active)
    stale_otps = BookingOTP.query.filter(
        BookingOTP.status == 'active',
        BookingOTP.expires_at < NOW,
    ).all()
    otp_expired_count = 0
    for o in stale_otps:
        o.status = 'expired'
        otp_expired_count += 1

    # Unlock OTPs whose lock window has passed
    stale_locks = BookingOTP.query.filter(
        BookingOTP.status == 'locked',
        BookingOTP.locked_until != None,
        BookingOTP.locked_until < NOW,
    ).all()
    otp_unlocked_count = 0
    for o in stale_locks:
        o.status = 'active'
        o.locked_until = None
        o.attempts = 0
        otp_unlocked_count += 1

    if otp_expired_count or otp_unlocked_count:
        db.session.commit()

    otp_stats = {
        'expired': otp_expired_count,
        'unlocked': otp_unlocked_count,
    }

    # 6. Booking reminders — notify vendor 1 day before start
    from datetime import date as _date
    tomorrow = ist_today() + timedelta(days=1)
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
                    f'{b.booking_reference} — {b.item.title} for {(b.customer.public_id or b.customer.name) if b.customer else "customer"}',
                    f'/vendor/booking/{b.id}'
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
        'auto_unblocks': unblock_stats,
        'auto_cancels': auto_cancel,
        'otp_cleanup': otp_stats,
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
        .filter(Booking.booking_status.in_(['dispatched', 'in_use', 'confirmed'])).count()
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
    page = request.args.get('page', 1, type=int)
    pagination = query.paginate(page=page, per_page=50, error_out=False)
    return render_template('admin/bookings.html', bookings=pagination.items, pagination=pagination,
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

    # Transporter list — prefer available, then trip count desc
    available_transporters = (User.query
                              .filter_by(role='transporter', transporter_kyc_status='verified')
                              .order_by(User.transporter_available.desc(),
                                        User.transporter_trip_count.desc())
                              .all())

    return render_template('admin/booking_detail.html',
                         booking=booking, payment_proofs=payment_proofs,
                         available_transporters=available_transporters)


@app.route('/admin/booking/<int:booking_id>/assign-transporter', methods=['POST'])
@login_required
def admin_assign_transporter(booking_id):
    """Assign a KYC-verified transporter to a booking."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    booking = Booking.query.get_or_404(booking_id)
    transporter_id = request.form.get('transporter_id', type=int)
    if not transporter_id:
        flash('Please select a transporter.', 'danger')
        return redirect(url_for('admin_booking_detail', booking_id=booking.id))

    transporter = User.query.filter_by(id=transporter_id, role='transporter').first()
    if not transporter or transporter.transporter_kyc_status != 'verified':
        flash('Selected transporter is not verified.', 'danger')
        return redirect(url_for('admin_booking_detail', booking_id=booking.id))

    if booking.booking_status in ('completed', 'cancelled'):
        flash('Cannot assign transporter to a closed booking.', 'warning')
        return redirect(url_for('admin_booking_detail', booking_id=booking.id))

    booking.transporter_id = transporter.id
    booking.transporter_assigned_at = datetime.utcnow()
    booking.transporter_assigned_by = current_user.id
    db.session.commit()

    # Notify transporter
    try:
        create_notification(
            transporter.id, 'booking', '🚚 New Assignment',
            f'{booking.booking_reference} — {booking.item.title if booking.item else "item"}',
            url_for('transporter_dashboard')
        )
        db.session.commit()
    except Exception:
        pass

    flash(f'✅ {transporter.name} assigned to {booking.booking_reference}.', 'success')
    return redirect(url_for('admin_booking_detail', booking_id=booking.id))


@app.route('/admin/booking/<int:booking_id>/unassign-transporter', methods=['POST'])
@login_required
def admin_unassign_transporter(booking_id):
    """Remove transporter assignment."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    booking = Booking.query.get_or_404(booking_id)

    old_id = booking.transporter_id
    booking.transporter_id = None
    booking.transporter_assigned_at = None
    booking.transporter_assigned_by = None
    db.session.commit()

    if old_id:
        try:
            create_notification(
                old_id, 'info', 'Assignment Removed',
                f'You have been unassigned from {booking.booking_reference}.',
                url_for('transporter_dashboard')
            )
            db.session.commit()
        except Exception:
            pass

    flash('Transporter unassigned.', 'info')
    return redirect(url_for('admin_booking_detail', booking_id=booking.id))


@app.route('/admin/booking/<int:booking_id>/status', methods=['POST'])
@login_required
def admin_update_booking_status(booking_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    booking = Booking.query.get_or_404(booking_id)
    new_status = request.form.get('status', '')
    if new_status in ['confirmed', 'cancelled', 'completed', 'pending', 'dispatched', 'in_use', 'return_initiated', 'return_received']:
        old_status = booking.booking_status
        # Warn (don't block) when completing without a return report; admin may need to force-complete disputes
        if new_status == 'completed' and old_status != 'completed' and not booking.return_report_done:
            flash('⚠️ Completed without a return report. Stock is restored automatically; verify manually.', 'warning')
        booking.booking_status = new_status

        active_statuses = ['pending', 'confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']
        # Restore stock when cancelling an active booking (variant-aware, cart/bundle-aware)
        if new_status == 'cancelled' and old_status in active_statuses:
            _restore_booking_stock(booking)
        # Re-reserve stock if a cancelled booking is reactivated (prevents double-restore)
        elif old_status == 'cancelled' and new_status in active_statuses:
            _reserve_booking_stock(booking)
        # Completed -> stock was given back; re-take it if the booking is re-opened
        elif old_status == 'completed' and new_status in active_statuses:
            if (booking.order_type or 'rent') not in ('sale', 'bundle', 'cart'):
                _reserve_units(booking.item_id, booking.variant_id, booking.quantity)
        # Admin jumps an active booking straight to completed
        elif new_status == 'completed' and old_status in active_statuses:
            _restore_completed_stock(booking, old_status)
            _bump_transporter_trip(booking, old_status)

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
    page = request.args.get('page', 1, type=int)
    pagination = query.order_by(Item.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin/items.html', items=pagination.items, pagination=pagination,
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
            deposit_amount=0,  # auto-calculated at booking (matches vendor flow)
            stock=int(request.form.get('stock', 1)),
            image_url=request.form.get('image_url', '').strip() or None,
            vendor_id=current_user.id
        )
        _apply_sale_fields(item)
        _apply_dimensions(item)
        has_variants = bool(request.form.get('has_variants'))
        item.has_variants = has_variants
        db.session.add(item)
        db.session.flush()  # need item.id for variants
        if has_variants:
            ok, err = _save_variants(item)
            if not ok:
                db.session.rollback()
                flash(f'⚠️ {err}', 'danger')
                return render_template('admin/item_form.html')
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
        item.deposit_amount = 0  # auto-calculated at booking (matches vendor_edit_item)
        item.stock = int(request.form.get('stock', 1))
        item.is_available = 'is_available' in request.form
        _apply_sale_fields(item)
        _apply_dimensions(item)
        has_variants = bool(request.form.get('has_variants'))
        item.has_variants = has_variants
        if has_variants:
            ok, err = _save_variants(item)
            if not ok:
                db.session.rollback()
                flash(f'⚠️ {err} (Changes were not saved.)', 'danger')
                return render_template('admin/item_form.html', item=Item.query.get(item_id),
                                       variants=_sorted_variants(Item.query.get(item_id)))
        db.session.commit()
        flash('✅ Item updated!', 'success')
        return redirect(url_for('admin_items'))
    return render_template('admin/item_form.html', item=item, variants=_sorted_variants(item))


@app.route('/admin/item/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_delete_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    ok, msg, category = _delete_item_safely(item)
    flash(msg, category)
    return redirect(request.referrer or url_for('admin_items')) if not ok else redirect(url_for('admin_items'))


@app.route('/admin/verification-requests')
@login_required
def admin_verification_requests():
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    status_filter = request.args.get('status', 'pending')
    q = ItemVerificationRequest.query
    if status_filter != 'all':
        q = q.filter_by(status=status_filter)
    reqs = q.order_by(ItemVerificationRequest.created_at.desc()).all()
    counts = {st: ItemVerificationRequest.query.filter_by(status=st).count()
              for st in ('pending', 'approved', 'rejected')}
    return render_template('admin/verification_requests.html',
                           reqs=reqs, status_filter=status_filter, counts=counts)


@app.route('/admin/verification-request/<int:req_id>/approve', methods=['POST'])
@login_required
def admin_approve_verification(req_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    req = ItemVerificationRequest.query.get_or_404(req_id)
    if req.status != 'pending':
        flash('⚠️ Already reviewed.', 'info')
        return redirect(url_for('admin_verification_requests'))
    req.status = 'approved'
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    _grant_item_verification(req.item, req.days or VERIFICATION_DAYS)
    create_notification(
        req.vendor_id, 'payment', 'Item Verified',
        f'"{req.item.title}" ab verified hai (till {req.item.verified_until.strftime("%d %b, %Y")}).',
        url_for('vendor_dashboard'))
    db.session.commit()
    flash(f'✅ "{req.item.title}" verified.', 'success')
    return redirect(url_for('admin_verification_requests'))


@app.route('/admin/verification-request/<int:req_id>/reject', methods=['POST'])
@login_required
def admin_reject_verification(req_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    req = ItemVerificationRequest.query.get_or_404(req_id)
    if req.status != 'pending':
        flash('⚠️ Already reviewed.', 'info')
        return redirect(url_for('admin_verification_requests'))
    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('Rejection reason required.', 'danger')
        return redirect(url_for('admin_verification_requests'))
    req.status = 'rejected'
    req.rejection_reason = reason
    req.reviewed_by = current_user.id
    req.reviewed_at = datetime.utcnow()
    create_notification(
        req.vendor_id, 'warning', 'Verification Payment Rejected',
        f'"{req.item.title}": {reason}',
        url_for('vendor_verify_item', item_id=req.item_id))
    db.session.commit()
    flash('Request rejected.', 'info')
    return redirect(url_for('admin_verification_requests'))


@app.route('/admin/item/<int:item_id>/verify', methods=['POST'])
@login_required
def admin_verify_item(item_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    item = Item.query.get_or_404(item_id)
    _grant_item_verification(item, VERIFICATION_DAYS)
    db.session.commit()
    flash(f'✅ {item.title} verified for {VERIFICATION_DAYS} days.', 'success')
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
    blocked_filter = request.args.get('blocked', '')
    kyc_filter = request.args.get('kyc', '')
    query = User.query
    if role_filter != 'all':
        query = query.filter_by(role=role_filter)
    if blocked_filter == 'blocked':
        query = query.filter_by(is_blocked=True)
    elif blocked_filter == 'active':
        query = query.filter_by(is_blocked=False)
    if kyc_filter == 'verified':
        query = query.filter_by(kyc_status='verified')
    elif kyc_filter == 'unverified':
        query = query.filter((User.kyc_status != 'verified') | (User.kyc_status.is_(None)))
    if search:
        query = query.filter(db.or_(User.name.ilike(f'%{search}%'),
                                    User.mobile.ilike(f'%{search}%'),
                                    User.email.ilike(f'%{search}%'),
                                    User.public_id.ilike(f'%{search}%')))
    page = request.args.get('page', 1, type=int)
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin/users.html',
                           users=pagination.items, pagination=pagination,
                           role_filter=role_filter, search=search,
                           blocked_filter=blocked_filter, kyc_filter=kyc_filter)


@app.route('/admin/user/<int:user_id>/block', methods=['POST'])
@login_required
def admin_block_user(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('⚠️ You cannot block yourself.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))
    if user.role == 'admin':
        flash('⚠️ Admin accounts cannot be blocked.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))

    if not _check_reauth_password():
        flash('⚠️ Re-enter your admin password to block a user.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))

    reason = request.form.get('reason', '').strip()
    if not reason:
        flash('⚠️ Block reason is required.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))

    duration_raw = request.form.get('duration_days', '').strip()
    expires_at = None
    if duration_raw:
        try:
            days = int(duration_raw)
            if days > 0:
                expires_at = datetime.utcnow() + timedelta(days=days)
        except (ValueError, TypeError):
            pass

    user.is_blocked = True
    user.blocked_at = datetime.utcnow()
    user.blocked_by = current_user.id
    user.block_reason = reason
    user.block_expires_at = expires_at

    # Vendor: hide all items + deactivate bundles
    if user.role == 'vendor':
        Item.query.filter_by(vendor_id=user.id).update({'is_available': False})
        Bundle.query.filter_by(vendor_id=user.id).update({'is_active': False})

    db.session.commit()
    _audit_admin('block_user', target_user_id=user.id,
                 details=f'Reason: {reason}; Until: {expires_at.isoformat() if expires_at else "permanent"}')
    flash(f'🚫 {user.name} blocked. Reason: {reason}' + (f' (until {expires_at.strftime("%d %b %Y")})' if expires_at else ' (permanent).'), 'success')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/user/<int:user_id>/unblock', methods=['POST'])
@login_required
def admin_unblock_user(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)

    if not _check_reauth_password():
        flash('⚠️ Re-enter your admin password to unblock a user.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))

    user.is_blocked = False
    user.blocked_at = None
    user.blocked_by = None
    user.block_reason = None
    user.block_expires_at = None

    # Vendor: unhide items + reactivate bundles
    if user.role == 'vendor':
        Item.query.filter_by(vendor_id=user.id).update({'is_available': True})
        Bundle.query.filter_by(vendor_id=user.id).update({'is_active': True})

    db.session.commit()
    _audit_admin('unblock_user', target_user_id=user.id)
    flash(f'✅ {user.name} unblocked.', 'success')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/user/<int:user_id>/kyc/reveal', methods=['POST'])
@login_required
def admin_reveal_user_kyc(user_id):
    """Return full decrypted KYC numbers to admin. Every call is audit-logged."""
    if current_user.role != 'admin':
        return jsonify({'error': 'admin only'}), 403
    user = User.query.get_or_404(user_id)

    # Re-auth gate: admin must re-enter their password
    if not _check_reauth_password():
        return jsonify({
            'error': 'reauth_required',
            'message': 'Enter your admin password to reveal KYC data.'
        }), 403

    if not user.kyc_aadhaar_full_enc or not user.kyc_pan_full_enc:
        return jsonify({
            'error': 'no_vault',
            'message': 'Full KYC data not available. User may have verified before the KYC vault was enabled, or KYC was reset.'
        }), 404

    # Rate limit — per admin, rolling 1 hour
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = KYCViewLog.query.filter(
        KYCViewLog.admin_id == current_user.id,
        KYCViewLog.viewed_at >= one_hour_ago,
    ).count()
    if recent_count >= Config.KYC_REVEAL_RATE_LIMIT_PER_HOUR:
        return jsonify({
            'error': 'rate_limit',
            'message': f'Rate limit hit ({Config.KYC_REVEAL_RATE_LIMIT_PER_HOUR}/hour). Try again later.'
        }), 429

    aadhaar = _decrypt_kyc(user.kyc_aadhaar_full_enc)
    pan = _decrypt_kyc(user.kyc_pan_full_enc)
    if not aadhaar or not pan:
        return jsonify({
            'error': 'decrypt_failed',
            'message': 'Decryption failed. Encryption key may have changed.'
        }), 500

    # Log the reveal
    reason_raw = None
    if request.is_json:
        reason_raw = (request.get_json(silent=True) or {}).get('reason')
    if not reason_raw:
        reason_raw = request.form.get('reason')
    reason = reason_raw.strip()[:80] if reason_raw else None
    device, ip = get_client_info(request)
    log = KYCViewLog(
        admin_id=current_user.id,
        target_user_id=user.id,
        reason=reason,
        ip_address=ip,
        user_agent=device,
        viewed_at=datetime.utcnow(),
    )
    db.session.add(log)
    db.session.commit()
    _audit_admin('kyc_reveal', target_user_id=user.id,
                 details=f'KYC revealed; reason: {reason or "(none)"}')

    return jsonify({
        'success': True,
        'aadhaar': aadhaar,
        'pan': pan,
        'auto_hide_seconds': 60,
        'log_id': log.id,
    })


@app.route('/admin/kyc-audit-log')
@login_required
def admin_kyc_audit_log():
    """Audit log of every full-KYC reveal."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    admin_filter = request.args.get('admin_id', '').strip()
    target_filter = request.args.get('target_id', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50

    query = KYCViewLog.query
    if admin_filter.isdigit():
        query = query.filter_by(admin_id=int(admin_filter))
    if target_filter.isdigit():
        query = query.filter_by(target_user_id=int(target_filter))
    query = query.order_by(KYCViewLog.viewed_at.desc())

    total = query.count()
    logs = query.offset((page - 1) * per_page).limit(per_page).all()

    # Filter dropdown values — admins who have reveals, in last 90 days
    cutoff = datetime.utcnow() - timedelta(days=90)
    admins_with_logs = db.session.query(User).join(
        KYCViewLog, KYCViewLog.admin_id == User.id
    ).filter(KYCViewLog.viewed_at >= cutoff).distinct().all()

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    return render_template(
        'admin/kyc_audit_log.html',
        logs=logs,
        total=total,
        page=page,
        total_pages=total_pages,
        admin_filter=admin_filter,
        target_filter=target_filter,
        admins_with_logs=admins_with_logs,
    )


@app.route('/admin/transporter/<int:user_id>/verify', methods=['POST'])
@login_required
def admin_verify_transporter_kyc(user_id):
    """Verify a transporter's KYC."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    if user.role != 'transporter':
        flash('Not a transporter account.', 'danger')
        return redirect(url_for('admin_users'))

    user.transporter_kyc_status = 'verified'
    user.transporter_kyc_verified_at = datetime.utcnow()
    user.transporter_kyc_verified_by = current_user.id
    user.transporter_kyc_notes = None
    db.session.commit()

    try:
        create_notification(
            user.id, 'info', '✅ KYC Verified',
            'Aapka transporter KYC verify ho gaya. Ab aap deliveries accept kar sakte hain.',
            url_for('transporter_dashboard')
        )
        db.session.commit()
    except Exception:
        pass

    flash(f'✅ Transporter {user.name} verified.', 'success')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/transporter/<int:user_id>/reject', methods=['POST'])
@login_required
def admin_reject_transporter_kyc(user_id):
    """Reject transporter KYC with reason."""
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    if user.role != 'transporter':
        flash('Not a transporter account.', 'danger')
        return redirect(url_for('admin_users'))

    reason = request.form.get('rejection_reason', '').strip()
    if not reason:
        flash('Rejection reason required.', 'danger')
        return redirect(url_for('admin_user_detail', user_id=user.id))

    user.transporter_kyc_status = 'rejected'
    user.transporter_kyc_notes = reason[:500]
    user.transporter_kyc_verified_at = None
    user.transporter_kyc_verified_by = None
    db.session.commit()

    try:
        create_notification(
            user.id, 'warning', '⚠️ KYC Rejected',
            f'Reason: {reason}. Please correct and resubmit.',
            url_for('transporter_kyc')
        )
        db.session.commit()
    except Exception:
        pass

    flash(f'KYC rejected for {user.name}.', 'info')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/user/<int:user_id>/kyc/verify', methods=['POST'])
@login_required
def admin_verify_user_kyc(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    user.kyc_status = 'verified'
    user.kyc_verified_at = datetime.utcnow()
    user.kyc_expires_at = datetime.utcnow() + timedelta(days=365)
    db.session.commit()
    create_notification(
        user.id, 'info', 'KYC Verified',
        'Your KYC is verified. Future bookings above ₹30,000 will not ask for documents again.',
        '/my-data'
    )
    db.session.commit()
    flash(f'✅ KYC verified for {user.name}.', 'success')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/user/<int:user_id>/kyc/reset', methods=['POST'])
@login_required
def admin_reset_user_kyc(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    user.kyc_status = 'unverified'
    user.kyc_verified_at = None
    user.kyc_expires_at = None
    user.kyc_aadhaar_last4 = None
    user.kyc_pan_masked = None
    user.kyc_aadhaar_full_enc = None
    user.kyc_pan_full_enc = None
    db.session.commit()
    flash(f'KYC reset for {user.name}. Full KYC data deleted.', 'info')
    return redirect(url_for('admin_user_detail', user_id=user.id))


@app.route('/admin/user/<int:user_id>/role', methods=['POST'])
@login_required
def admin_update_user_role(user_id):
    if current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role', '')
    if new_role in ['admin', 'customer', 'vendor'] and user.id != current_user.id:
        if not _check_reauth_password():
            flash('⚠️ Re-enter your admin password to change a user role.', 'danger')
            return redirect(url_for('admin_user_detail', user_id=user.id))
        old_role = user.role
        user.role = new_role
        db.session.commit()
        _audit_admin('role_change', target_user_id=user.id, details=f'{old_role} -> {new_role}')
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
    last_kyc_view = None
    if user.kyc_status == 'verified':
        last_kyc_view = KYCViewLog.query.filter_by(target_user_id=user.id)\
            .order_by(KYCViewLog.viewed_at.desc()).first()

    return render_template('admin/user_detail.html', user=user,
                         user_bookings=user_bookings, user_items=user_items,
                         vendor_bookings=vendor_bookings,
                         last_kyc_view=last_kyc_view,
                         now=datetime.utcnow())


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
    
    def pending_verifications_count():
        if not current_user.is_authenticated:
            return 0
        if current_user.role == 'admin':
            return ItemVerificationRequest.query.filter_by(status='pending').count()
        return 0

    vendor_sos_available = False
    if (Config.SOS_ENABLED and current_user.is_authenticated
            and current_user.role in ('vendor', 'admin')):
        vendor_sos_available = bool(current_user.sos_available)

    def disp_name(user):
        """Own name for self/admin viewers, public_id for everyone else."""
        if not user:
            return '—'
        if current_user.is_authenticated:
            if current_user.id == user.id or current_user.role == 'admin':
                return user.name
        return user.public_id or user.name

    # Merged in from the former inject_announcement() / inject_wishlist_count()
    # Cached 60s as a plain dict (never cache ORM objects); invalidated on admin changes.
    _ann = None
    try:
        _snap = cache.get('active_announcement')
        if _snap is None:
            _a = Announcement.query.filter_by(is_active=True)\
                    .order_by(Announcement.created_at.desc()).first()
            _snap = ({'id': _a.id, 'message': _a.message, 'expires_at': _a.expires_at}
                     if _a else False)
            cache.set('active_announcement', _snap, timeout=60)
        if _snap and not (_snap.get('expires_at') and _snap['expires_at'] < datetime.utcnow()):
            _ann = _snap
    except Exception:
        _ann = None
    _wishlist_count = 0
    try:
        if current_user.is_authenticated:
            _wishlist_count = Wishlist.query.filter_by(user_id=current_user.id).count()
    except Exception:
        _wishlist_count = 0

    return dict(
        disp_name=disp_name,
        active_announcement=_ann,
        wishlist_count=_wishlist_count,
        cart_count=cart_count(),
        vendor_sos_available=vendor_sos_available,
        platform_fee_rate=Config.COMMISSION_RATE,
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
        pending_verifications_count=pending_verifications_count(),
        unread_notification_count=unread_notification_count(),
        kyc_valid=(lambda: (_user_kyc_valid(current_user) if current_user.is_authenticated else False)),
        t=t,
        current_lang=(session.get('lang', Config.DEFAULT_LANG) if hasattr(session, 'get') else Config.DEFAULT_LANG),
        site_url=Config.SITE_URL,
        google_enabled=bool(Config.GOOGLE_CLIENT_ID and Config.GOOGLE_CLIENT_SECRET),
        seo_title=None,
        seo_description=None,
        canonical_url=None,
        og_image=None,
        supported_langs=Config.SUPPORTED_LANGS,
        get_category_icon=lambda c: {
            'furniture': 'fa-couch', 'lighting': 'fa-lightbulb',
            'decor': 'fa-palette', 'mandap': 'fa-archway',
            'tent': 'fa-campground', 'flooring': 'fa-layer-group',
            'catering': 'fa-utensils', 'sound': 'fa-music',
            'bar': 'fa-wine-glass', 'entertainment': 'fa-dice',
            'electrical': 'fa-bolt', 'cooling': 'fa-wind',
            'sfx': 'fa-wand-magic-sparkles',
            'props': 'fa-camera-retro',
            'misc': 'fa-shapes'
        }.get(c, 'fa-box')
    )


# ============================================
# INIT DATABASE
# ============================================
# Register helpers as Jinja globals
app.jinja_env.globals['_time_ago'] = _time_ago


@app.template_filter('ist_dt')
def ist_dt_filter(dt):
    """Full datetime in IST: 05 Oct 2026, 06:30 PM"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%d %b %Y, %I:%M %p')


@app.template_filter('ist_date')
def ist_date_filter(dt):
    """Date in IST: 05 Oct 2026"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%d %b %Y')


@app.template_filter('ist_time')
def ist_time_filter(dt):
    """Time in IST: 06:30 PM"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%I:%M %p')


@app.template_filter('ist_hm')
def ist_hm_filter(dt):
    """24-hour HM: 18:30"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%H:%M')


@app.template_filter('ist_short')
def ist_short_filter(dt):
    """Short: 05 Oct, 06:30 PM"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%d %b, %I:%M %p')


@app.template_filter('ist_ym')
def ist_ym_filter(dt):
    """Year-Month: Oct 2026"""
    if not dt:
        return ''
    return (dt + timedelta(hours=5, minutes=30)).strftime('%b %Y')


def init_database():
    try:
        try:
            from sqlalchemy import text
            migrations = [
                "ALTER TABLE bookings ALTER COLUMN aadhaar_number TYPE VARCHAR(255)",
                "ALTER TABLE bookings ALTER COLUMN pan_number TYPE VARCHAR(255)",
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
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS is_for_rent BOOLEAN DEFAULT TRUE",
                "UPDATE items SET is_for_rent = TRUE WHERE is_for_rent IS NULL",
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
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS vendor_share FLOAT",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS sos_bonus FLOAT DEFAULT 0",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS sos_fee FLOAT DEFAULT 0",
                # 09 Oct 2026 — per-user KYC
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(20) DEFAULT 'unverified'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_verified_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_expires_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_aadhaar_last4 VARCHAR(4)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_pan_masked VARCHAR(10)",
                # 10 Oct 2026 — KYC vault (encrypted full numbers + audit log)
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_aadhaar_full_enc TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_pan_full_enc TEXT",
                """CREATE TABLE IF NOT EXISTS kyc_view_log (
                    id SERIAL PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    reason VARCHAR(80),
                    ip_address VARCHAR(45),
                    user_agent VARCHAR(300),
                    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                "CREATE INDEX IF NOT EXISTS idx_kyc_view_log_admin ON kyc_view_log(admin_id)",
                "CREATE INDEX IF NOT EXISTS idx_kyc_view_log_target ON kyc_view_log(target_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_kyc_view_log_viewed ON kyc_view_log(viewed_at)",
                # 10 Oct 2026 — Transport OTP
                """CREATE TABLE IF NOT EXISTS booking_otp (
                    id SERIAL PRIMARY KEY,
                    booking_id INTEGER NOT NULL,
                    otp_type VARCHAR(20) NOT NULL,
                    code VARCHAR(6) NOT NULL,
                    generated_by INTEGER NOT NULL,
                    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL,
                    verified_by INTEGER,
                    verified_at TIMESTAMP,
                    attempts INTEGER DEFAULT 0,
                    locked_until TIMESTAMP,
                    status VARCHAR(20) DEFAULT 'active',
                    UNIQUE (booking_id, otp_type)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_booking_otp_booking ON booking_otp(booking_id)",
                "CREATE INDEX IF NOT EXISTS idx_booking_otp_status ON booking_otp(status)",
                "CREATE INDEX IF NOT EXISTS idx_booking_otp_code ON booking_otp(code)",
                # 16 Oct 2026 — Google Sign-In
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS google_id VARCHAR(100)",
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id) WHERE google_id IS NOT NULL",
                # 16 Oct 2026 — Transporter system
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_kyc_status VARCHAR(20) DEFAULT 'unverified'",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_license_number VARCHAR(50)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_license_photo VARCHAR(500)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_vehicle_number VARCHAR(20)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_vehicle_type VARCHAR(20)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_vehicle_front VARCHAR(500)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_vehicle_back VARCHAR(500)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_kyc_notes TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_kyc_submitted_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_kyc_verified_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_kyc_verified_by INTEGER",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_trip_count INTEGER DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_city VARCHAR(100)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_available BOOLEAN DEFAULT TRUE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_upi_id VARCHAR(120)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_rating_avg FLOAT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS transporter_rating_count INTEGER DEFAULT 0",
                """CREATE TABLE IF NOT EXISTS transporter_ratings (
                    id SERIAL PRIMARY KEY,
                    booking_id INTEGER NOT NULL,
                    transporter_id INTEGER NOT NULL,
                    rater_id INTEGER NOT NULL,
                    rater_role VARCHAR(20) NOT NULL,
                    rating INTEGER NOT NULL,
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (booking_id, rater_id)
                )""",
                "CREATE INDEX IF NOT EXISTS idx_tr_booking ON transporter_ratings(booking_id)",
                "CREATE INDEX IF NOT EXISTS idx_tr_transporter ON transporter_ratings(transporter_id)",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS transporter_id INTEGER",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS transporter_assigned_at TIMESTAMP",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS transporter_assigned_by INTEGER",
                "CREATE INDEX IF NOT EXISTS idx_bookings_transporter ON bookings(transporter_id)",
                "CREATE INDEX IF NOT EXISTS idx_users_transporter_kyc ON users(transporter_kyc_status)",
                # 14 Oct 2026 — ItemVariant + dimensions
                """CREATE TABLE IF NOT EXISTS item_variants (
                    id SERIAL PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    variant_name VARCHAR(200) NOT NULL,
                    rate_per_day FLOAT NOT NULL,
                    sale_price FLOAT,
                    deposit_amount FLOAT DEFAULT 0,
                    stock INTEGER DEFAULT 1,
                    image_url VARCHAR(500),
                    is_available BOOLEAN DEFAULT TRUE,
                    sort_order INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                "CREATE INDEX IF NOT EXISTS idx_item_variants_item ON item_variants(item_id)",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS has_variants BOOLEAN DEFAULT FALSE",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS height_cm FLOAT",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS width_cm FLOAT",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS length_cm FLOAT",
                "ALTER TABLE items ADD COLUMN IF NOT EXISTS weight_kg FLOAT",
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS variant_id INTEGER",
                "CREATE INDEX IF NOT EXISTS idx_bookings_variant ON bookings(variant_id)",
                # 12 Oct 2026 — bank details
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_account_name VARCHAR(150)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_account_number VARCHAR(40)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_ifsc VARCHAR(20)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_name VARCHAR(120)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS bank_branch VARCHAR(120)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS upi_id VARCHAR(120)",
                # 09 Oct 2026 — user management
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked_by INTEGER",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS block_reason TEXT",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS block_expires_at TIMESTAMP",
                "CREATE INDEX IF NOT EXISTS idx_users_is_blocked ON users(is_blocked)",
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
                # UTR uniqueness: top-level, non-cancelled bookings only. Child bookings
                # (bundle/cart) intentionally repeat the parent's UTR, so they are excluded.
                # If old duplicates exist this fails silently - see pre-flight query.
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_bookings_utr_active
                   ON bookings (LOWER(utr_number))
                   WHERE booking_status <> 'cancelled'
                     AND parent_booking_id IS NULL
                     AND utr_number IS NOT NULL AND utr_number <> ''""",
                # Admin audit log
                """CREATE TABLE IF NOT EXISTS admin_audit_log (
                    id SERIAL PRIMARY KEY,
                    admin_id INTEGER NOT NULL,
                    action VARCHAR(60) NOT NULL,
                    target_user_id INTEGER,
                    target_ref VARCHAR(100),
                    details TEXT,
                    ip_address VARCHAR(45),
                    user_agent VARCHAR(300),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )""",
                "CREATE INDEX IF NOT EXISTS idx_admin_audit_admin ON admin_audit_log(admin_id)",
                "CREATE INDEX IF NOT EXISTS idx_admin_audit_action ON admin_audit_log(action)",
                "CREATE INDEX IF NOT EXISTS idx_admin_audit_target ON admin_audit_log(target_user_id)",
                "CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_log(created_at)",
                # Transporter trip idempotency
                "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS transporter_trip_counted_at TIMESTAMP",
                # Email verification (existing users grandfathered as verified)
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_email_verified BOOLEAN DEFAULT TRUE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_token VARCHAR(120)",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verify_expires TIMESTAMP",
                "CREATE INDEX IF NOT EXISTS idx_users_email_verify_token ON users(email_verify_token)",
                "UPDATE users SET is_email_verified = TRUE WHERE is_email_verified IS NULL",
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

        # 15 Oct 2026 — 'Inspection Done' step removed. Close any legacy bookings
        # stuck in return_received (stock was never given back for these).
        try:
            _legacy = Booking.query.filter_by(booking_status='return_received').all()
            for _b in _legacy:
                _b.booking_status = 'completed'
                _restore_completed_stock(_b, 'return_received')
            if _legacy:
                db.session.commit()
                print(f"✅ Closed {len(_legacy)} legacy return_received booking(s) as completed")
        except Exception as e:
            db.session.rollback()
            print(f"Legacy return_received migration error: {e}")
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
            'created_at': (n.created_at + timedelta(hours=5, minutes=30)).strftime('%d %b %Y, %I:%M %p'),
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
        if _utr_already_used(utr):
            flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
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

        # KYC — total ≥ ₹30,000 requires verification (same as booking/cart)
        kyc_required = calc['total'] >= 30000
        user_kyc_ok = kyc_required and _user_kyc_valid(current_user)
        aadhaar = None
        pan = None
        if kyc_required and not user_kyc_ok:
            aadhaar = request.form.get('aadhaar', '').strip()
            pan = request.form.get('pan', '').strip().upper()
            pan_file = request.files.get('pan_image')
            ok, err = _validate_kyc_inputs(aadhaar, pan, pan_file)
            if not ok:
                flash(f'⚠️ {err}', 'danger')
                return redirect(url_for('book_bundle', bundle_id=bundle_id))
            _mark_user_kyc_verified(current_user, aadhaar, pan)
            user_kyc_ok = True

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
            kyc_required=kyc_required,
            kyc_verified=user_kyc_ok,
            aadhaar_number=_encrypt_kyc(aadhaar),
            pan_number=_encrypt_kyc(pan),
        )
        db.session.add(parent)
        try:
            db.session.flush()  # get parent.id
        except IntegrityError as e:
            db.session.rollback()
            if _is_utr_conflict(e):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            else:
                app.logger.exception('IntegrityError in book_bundle flush')
                flash('Could not create bundle booking. Please try again.', 'danger')
            return redirect(url_for('bundle_detail', bundle_id=bundle_id))

        # Child bookings — one per item, zero financials on parent's books,
        # per-item vendor_share for correct vendor earnings attribution.
        _bundle_days = calc['days']
        _indiv_sum = calc['individual_sum'] or 0
        _bundle_base = calc['base_rent'] or 0

        for bi in bundle.bundle_items:
            if not bi.item:
                continue
            # Lock the stock row so concurrent bundle bookings can't oversell.
            _title = bi.item.title
            item = (db.session.query(Item).filter_by(id=bi.item.id)
                    .with_for_update().populate_existing().first())
            if not item or (item.stock or 0) < bi.quantity:
                db.session.rollback()
                flash(f'Sold out: {_title}. Please refresh and try again.', 'danger')
                return redirect(url_for('bundle_detail', bundle_id=bundle_id))
            # Proportional split: this item's raw cost × (bundle_base / indiv_sum)
            raw = (item.rate_per_day or 0) * bi.quantity * _bundle_days
            if _indiv_sum > 0:
                child_share = round(raw * (_bundle_base / _indiv_sum), 2)
            else:
                child_share = 0.0

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
                vendor_share=child_share,
            )
            db.session.add(child)

            # Stock decrement
            item.stock = (item.stock or 0) - bi.quantity
            if item.stock <= 0:
                item.stock = 0
                item.is_available = False

        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            if _is_utr_conflict(e):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            else:
                app.logger.exception('IntegrityError in book_bundle commit')
                flash('Could not create bundle booking. Please try again.', 'danger')
            return redirect(url_for('bundle_detail', bundle_id=bundle_id))

        # Vendor notification (in-app + Telegram) deferred until admin verifies payment

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

def _parse_bundle_items(item_ids, item_qtys, owner_id=None):
    """Return (valid_items, error) — valid_items = [(Item, qty)], all owned by owner_id
    (defaults to the current user; admins editing a vendor's bundle pass the bundle owner)."""
    owner_id = owner_id or current_user.id
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
        if not item or item.vendor_id != owner_id:
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
    # Admin may edit any bundle; vendors only their own
    if current_user.role != 'admin' and bundle.vendor_id != current_user.id:
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
            request.form.getlist('item_ids'), request.form.getlist('item_qtys'),
            owner_id=bundle.vendor_id)
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

    vendor_items = (Item.query.filter_by(vendor_id=bundle.vendor_id)
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
    if current_user.role != 'admin' and bundle.vendor_id != current_user.id:
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
    if current_user.role != 'admin' and bundle.vendor_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_bundles'))
    # Bookings reference the bundle: deactivate instead of hard-deleting (FK).
    if Booking.query.filter_by(bundle_id=bundle.id).first():
        bundle.is_active = False
        db.session.commit()
        flash('Bundle has booking history, so it was deactivated instead of deleted.', 'info')
        return redirect(url_for('vendor_bundles'))
    BundleItem.query.filter_by(bundle_id=bundle.id).delete(synchronize_session=False)
    db.session.delete(bundle)
    db.session.commit()
    flash('Bundle deleted.', 'success')
    return redirect(url_for('vendor_bundles'))


# ============================================
# BATCH 8 — SELL ITEMS (28 Sep 2026)
# ============================================

def _apply_dimensions(item):
    """Read optional dimension fields from form. Sets None if blank."""
    for field in ('height_cm', 'width_cm', 'length_cm', 'weight_kg'):
        raw = request.form.get(field, '').strip()
        if raw:
            try:
                val = float(raw)
                setattr(item, field, val if val >= 0 else None)
            except (ValueError, TypeError):
                setattr(item, field, None)
        else:
            setattr(item, field, None)


def _save_variants(item):
    """Parse variant rows from the vendor form and upsert them. Returns (ok, error_msg).
    Form fields per row: variant_id_<idx>, variant_name_<idx>, variant_rate_<idx>,
    variant_stock_<idx>, variant_image_<idx> (file).

    Upsert (not delete-all) so variant IDs stay stable once bookings reference them:
      - row with an id that belongs to this item -> updated in place
      - row without a valid id                    -> new variant
      - existing variant missing from the form    -> deleted if never booked,
                                                     else soft-disabled (is_available=False)
    All validation happens before any write."""
    idxs = set()
    for key in request.form.keys():
        if key.startswith('variant_name_'):
            try:
                idxs.add(int(key.rsplit('_', 1)[1]))
            except (ValueError, IndexError):
                continue

    parsed = []
    seen_names = set()
    for idx in sorted(idxs):
        name = request.form.get(f'variant_name_{idx}', '').strip()
        if not name:
            continue
        if name.lower() in seen_names:
            return False, f'Variant "{name}" do baar hai — naam alag rakhein.'
        seen_names.add(name.lower())
        try:
            vrate = float(request.form.get(f'variant_rate_{idx}', 0))
        except (ValueError, TypeError):
            vrate = 0
        try:
            vstock = int(request.form.get(f'variant_stock_{idx}', 1))
        except (ValueError, TypeError):
            vstock = -1
        if vrate <= 0 or vstock < 0:
            return False, f'Variant "{name}" — rate aur stock valid daalein.'
        try:
            vid = int(request.form.get(f'variant_id_{idx}') or 0)
        except (ValueError, TypeError):
            vid = 0
        parsed.append({'idx': idx, 'id': vid, 'name': name[:200],
                       'rate': vrate, 'stock': vstock})

    if not parsed:
        return False, 'Kam se kam ek variant add karein.'
    if len(parsed) > 15:
        return False, 'Max 15 variants allowed.'

    existing = {v.id: v for v in ItemVariant.query.filter_by(item_id=item.id).all()}
    kept_ids = set()
    sale_price = item.sale_price if item.is_for_sale else None

    for order, p in enumerate(parsed):
        # Only trust an id that belongs to THIS item (blocks cross-item tampering)
        var = existing.get(p['id']) if p['id'] else None
        if var is None:
            var = ItemVariant(item_id=item.id, deposit_amount=0)
            db.session.add(var)
        else:
            kept_ids.add(var.id)

        var.variant_name = p['name']
        var.rate_per_day = p['rate']
        var.stock = p['stock']
        var.sale_price = sale_price
        var.is_available = True
        var.sort_order = order

        file_field = f"variant_image_{p['idx']}"
        if file_field in request.files and request.files[file_field].filename:
            up = upload_file_to_cloudinary(request.files[file_field],
                                           folder=f'vyahmandap/items/{item.id}/variants')
            if up and up.get('url'):
                var.image_url = up['url']

    for vid, v in existing.items():
        if vid in kept_ids:
            continue
        if Booking.query.filter_by(variant_id=vid).first():
            v.is_available = False      # keep for booking history
        else:
            db.session.delete(v)
    return True, None


def _sorted_variants(item):
    """Active variants of an item in display order (for the admin/vendor form)."""
    if not item:
        return []
    return sorted([v for v in item.variants if v.is_available],
                  key=lambda v: (v.sort_order or 0, v.id))


def _apply_sale_fields(item):
    """Read listing type (rent / sale / both) + sale_price from the item form.
    Returns an error string if the form is invalid, else None.
    Old forms (admin) that only send the is_for_sale checkbox keep the legacy behaviour
    and never touch is_for_rent."""
    try:
        sp = request.form.get('sale_price', '').strip()
        sale_price = float(sp) if sp else None
    except (ValueError, TypeError):
        sale_price = None
    if sale_price is not None and sale_price <= 0:
        sale_price = None

    item_type = request.form.get('item_type')
    if item_type is None:
        # Legacy path (admin form): checkbox only
        item.is_for_sale = bool(request.form.get('is_for_sale'))
        item.sale_price = sale_price
        if not item.sale_price:
            item.is_for_sale = False
        return None

    if item_type not in ('rent', 'sale', 'both'):
        item_type = 'rent'
    if item_type in ('sale', 'both') and not sale_price:
        return 'Sale price daalna zaroori hai (Sell Only / Both ke liye).'

    item.is_for_rent = item_type in ('rent', 'both')
    item.is_for_sale = item_type in ('sale', 'both')
    item.sale_price = sale_price if item.is_for_sale else None
    # rate_per_day is NOT NULL and used for sort/filter -> sell-only falls back to sale price
    if not item.is_for_rent and not (item.rate_per_day and item.rate_per_day > 0):
        item.rate_per_day = item.sale_price
    return None


def generate_user_public_id(role):
    """Generate unique public ID like VM-C-00001. Not concurrency-safe by itself:
    callers must retry on IntegrityError (see register / google_complete_profile)."""
    prefix = {'customer': 'C', 'vendor': 'V', 'admin': 'A', 'transporter': 'T'}.get(role, 'U')
    count = User.query.filter_by(role=role).count() + 1
    for _ in range(200):
        pid = f'VM-{prefix}-{count:05d}'
        if not User.query.filter_by(public_id=pid).first():
            return pid
        count += 1
    for _ in range(50):
        pid = f'VM-{prefix}-{_secrets.randbelow(900000) + 100000}'
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
        variant = None
        if c.get('variant_id'):
            variant = ItemVariant.query.filter_by(id=c['variant_id'], item_id=item.id).first()
            if not variant:
                continue  # stale cart entry — checkout validates and blocks before this matters
        unit_sale = ((variant.sale_price or item.sale_price) if variant else item.sale_price)
        unit_rate = variant.rate_per_day if variant else item.rate_per_day
        if item.is_for_sale and unit_sale and c.get('type') == 'sale':
            sub = round((unit_sale or 0) * qty, 2)
            sale_base += sub
            item_details.append({'item': item, 'variant': variant, 'quantity': qty,
                                 'subtotal': sub, 'type': 'sale', 'days': 0})
        else:
            sub = round((unit_rate or 0) * days * qty, 2)
            rental_base += sub
            item_details.append({'item': item, 'variant': variant, 'quantity': qty,
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


def calculate_sale(item, quantity, area, weight_category, price_override=None):
    """Batch 8 — Sale calculation (no deposit, no dates).
    price_override = selected variant's sale price (None -> item.sale_price)."""
    unit_price = price_override if price_override else (item.sale_price or 0)
    base = round(unit_price * quantity, 2)
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

        # Validate time — must be in future (IST)
        start_time_raw = request.form.get('start_time', '').strip()
        try:
            hh, mm = map(int, start_time_raw.split(':')[:2])
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError
        except (ValueError, TypeError):
            flash('Invalid time format.', 'danger')
            return redirect(url_for('sos_create', item_id=item_id))

        ist_now_dt = ist_now()
        # Give 5-minute grace window
        now_minutes = ist_now_dt.hour * 60 + ist_now_dt.minute
        chosen_minutes = hh * 60 + mm
        if chosen_minutes < now_minutes - 5:
            flash(f'Time must be in the future. Current IST: {ist_now_dt.strftime("%I:%M %p")}', 'danger')
            return redirect(url_for('sos_create', item_id=item_id))

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

        # City from form, fallback to user.city, then Harda
        city = request.form.get('city', '').strip() or current_user.city or 'Harda'
        ref = 'SOS' + _random_digits(8)
        req = SOSRequest(
            reference=ref,
            customer_id=current_user.id,
            item_id=item.id,
            city=city,
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


@app.route('/vendor/booking/<int:booking_id>')
@login_required
def vendor_booking_detail(booking_id):
    if current_user.role not in ('vendor', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('index'))

    booking = Booking.query.get_or_404(booking_id)

    # Only item owner or admin can view
    if booking.item.vendor_id != current_user.id and current_user.role != 'admin':
        flash('Access denied.', 'danger')
        return redirect(url_for('vendor_dashboard'))

    # Dispatch/return reports for this booking
    dispatch_report = EquipmentReport.query.filter_by(
        booking_id=booking.id, report_type='dispatch'
    ).first()
    return_report = EquipmentReport.query.filter_by(
        booking_id=booking.id, report_type='return'
    ).first()

    otps = BookingOTP.query.filter_by(booking_id=booking.id).all()
    otp_map = {o.otp_type: o for o in otps}

    # Venue address stays hidden from the vendor until payment is verified.
    # Cart/bundle children inherit the parent's payment status.
    _pay_src = booking
    if booking.parent_booking_id:
        _pay_src = Booking.query.get(booking.parent_booking_id) or booking
    address_unlocked = (current_user.role == 'admin'
                        or _pay_src.payment_status in ('paid', 'verified', 'partial'))

    return render_template(
        'vendor/booking_detail.html',
        booking=booking,
        dispatch_report=dispatch_report,
        return_report=return_report,
        otp_map=otp_map,
        address_unlocked=address_unlocked,
    )


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
            'customer': get_display_name(r.customer) if r.customer else '',
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
            variant = ItemVariant.query.get(c['variant_id']) if c.get('variant_id') else None
            items.append({'item': item, 'variant': variant,
                          'quantity': c.get('quantity', 1),
                          'type': c.get('type', 'rent')})
    rent_items = [c for c in items if c['type'] == 'rent']
    sale_items = [c for c in items if c['type'] == 'sale']
    return render_template('cart.html',
                           cart_items=items,
                           rent_items=rent_items,
                           sale_items=sale_items)


def _cart_add_typed(item_id, itype, qty_default=1):
    """Internal helper — add item to cart with explicit type ('rent'|'sale').
    Variant items are keyed by (item_id, type, variant_id)."""
    item = Item.query.get_or_404(item_id)
    back = request.referrer or url_for('item_detail', item_id=item_id)

    variant_id = request.form.get('variant_id', type=int) or request.args.get('variant', type=int)
    is_base = (request.form.get('variant_id') == 'base' or request.form.get('base') == '1')
    selected_variant = None
    if item.has_variants:
        if not variant_id and not is_base:
            flash('⚠️ Please select Standard or a variant.', 'danger')
            return redirect(back)
        if variant_id:
            selected_variant = ItemVariant.query.filter_by(
                id=variant_id, item_id=item_id, is_available=True
            ).first()
            if not selected_variant:
                flash('⚠️ Selected variant is not available.', 'danger')
                return redirect(back)

    effective_stock = (selected_variant.stock or 0) if selected_variant else (item.stock or 0)

    try:
        qty = int(request.form.get('quantity', qty_default))
    except (ValueError, TypeError):
        qty = qty_default
    if qty < 1:
        qty = 1
    if qty > effective_stock:
        flash(f'Only {effective_stock} unit(s) available.', 'danger')
        return redirect(back)
    if itype == 'sale' and not (item.is_for_sale and (item.sale_price or (selected_variant and selected_variant.sale_price))):
        flash('This item is not available for purchase.', 'danger')
        return redirect(back)
    if itype == 'rent' and (not item.is_available or item.is_for_rent is False):
        flash('This item is not available for rent.', 'danger')
        return redirect(back)

    vid = selected_variant.id if selected_variant else None
    cart = get_cart()
    found = False
    for c in cart:
        if (c['item_id'] == item_id and c.get('type') == itype
                and c.get('variant_id') == vid):
            c['quantity'] = min(c['quantity'] + qty, effective_stock or 1)
            found = True
            break
    if not found:
        entry = {'item_id': item_id, 'quantity': qty, 'type': itype}
        if vid:
            entry['variant_id'] = vid
        cart.append(entry)
    save_cart(cart)
    label = 'Buy Cart' if itype == 'sale' else 'Rent Cart'
    vtag = f' ({selected_variant.variant_name})' if selected_variant else ''
    flash(f'✅ {item.title}{vtag} added to {label}.', 'success')
    return redirect(request.referrer or url_for('cart_view'))


@app.route('/cart/add-rent/<int:item_id>', methods=['POST'])
@login_required
def cart_add_rent(item_id):
    return _cart_add_typed(item_id, 'rent')


@app.route('/cart/add-sale/<int:item_id>', methods=['POST'])
@login_required
def cart_add_sale(item_id):
    return _cart_add_typed(item_id, 'sale')


# Legacy route — kept for backward compat (auto-detects type)
@app.route('/cart/add/<int:item_id>', methods=['POST'])
@login_required
def cart_add(item_id):
    item = Item.query.get_or_404(item_id)
    itype = 'sale' if (item.is_for_sale and item.sale_price) else 'rent'
    return _cart_add_typed(item_id, itype)


@app.route('/cart/remove/<int:item_id>', methods=['POST'])
@login_required
def cart_remove(item_id):
    rtype = request.form.get('type')  # 'rent' | 'sale' | None
    variant_id = request.form.get('variant_id', type=int)
    if rtype in ('rent', 'sale'):
        if variant_id:
            cart = [c for c in get_cart()
                    if not (c['item_id'] == item_id and c.get('type') == rtype
                            and c.get('variant_id') == variant_id)]
        else:
            cart = [c for c in get_cart()
                    if not (c['item_id'] == item_id and c.get('type') == rtype
                            and not c.get('variant_id'))]
    else:
        # Backward-compat: no type -> remove both
        cart = [c for c in get_cart() if c['item_id'] != item_id]
    save_cart(cart)
    flash('Item removed from cart.', 'info')
    return redirect(url_for('cart_view'))


@app.route('/cart/update', methods=['POST'])
@login_required
def cart_update():
    cart = get_cart()
    for c in cart:
        # Key includes type + variant — same item/variant combo is unique
        vid = c.get('variant_id', '')
        key = f"qty_{c['item_id']}_{c.get('type', 'rent')}_{vid}"
        if key in request.form:
            try:
                q = int(request.form.get(key, c['quantity']))
            except (ValueError, TypeError):
                q = c['quantity']
            if c.get('variant_id'):
                var = ItemVariant.query.get(c['variant_id'])
                max_q = (var.stock or 1) if var else 1
            else:
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

    # Verify all items (and variants) still exist + stock
    for c in cart:
        item = Item.query.get(c['item_id'])
        if not item:
            flash('An item in your cart no longer exists.', 'danger')
            return redirect(url_for('cart_view'))
        qty_needed = c.get('quantity', 1)
        if c.get('variant_id'):
            var = (ItemVariant.query.filter_by(id=c['variant_id'], item_id=item.id, is_available=True).first()
                   if c.get('variant_id') else None)
            if not var:
                flash(f'Please re-select a variant for {item.title}.', 'danger')
                return redirect(url_for('cart_view'))
            if not item.is_available or (var.stock or 0) < qty_needed:
                flash(f'{item.title} ({var.variant_name}) is out of stock.', 'danger')
                return redirect(url_for('cart_view'))
        elif not item.is_available or (item.stock or 0) < qty_needed:
            flash(f'{item.title} is out of stock.', 'danger')
            return redirect(url_for('cart_view'))

    # Combined demand per (item, variant) — rent + buy lines of the same unit share one stock pool
    _demand = {}
    for c in cart:
        k = (c['item_id'], c.get('variant_id'))
        _demand[k] = _demand.get(k, 0) + c.get('quantity', 1)
    for (iid, vid), need in _demand.items():
        _it = Item.query.get(iid)
        _avail = ((ItemVariant.query.get(vid).stock or 0) if vid else (_it.stock or 0))
        if need > _avail:
            flash(f'Only {_avail} unit(s) of {_it.title} available — reduce quantity in your cart.', 'danger')
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
            start_date = end_date = ist_today()

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
        if _utr_already_used(utr):
            flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
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

        # KYC — total payable ≥ ₹30,000 (rental + sale + deposit + fees)
        kyc_required = calc['total'] >= 30000
        user_kyc_ok = kyc_required and _user_kyc_valid(current_user)
        aadhaar = None
        pan = None
        if kyc_required and not user_kyc_ok:
            aadhaar = request.form.get('aadhaar', '').strip()
            pan = request.form.get('pan', '').strip().upper()
            pan_file = request.files.get('pan_image')
            ok, err = _validate_kyc_inputs(aadhaar, pan, pan_file)
            if not ok:
                flash(f'⚠️ {err}', 'danger')
                return redirect(url_for('cart_checkout'))
            _mark_user_kyc_verified(current_user, aadhaar, pan)
            user_kyc_ok = True

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
            aadhaar_number=_encrypt_kyc(aadhaar),
            pan_number=_encrypt_kyc(pan),
            kyc_verified=user_kyc_ok,
        )
        db.session.add(parent)
        try:
            db.session.flush()
        except IntegrityError as e:
            db.session.rollback()
            if _is_utr_conflict(e):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            else:
                app.logger.exception('IntegrityError in cart_checkout flush')
                flash('Could not place order. Please try again.', 'danger')
            return redirect(url_for('cart_checkout'))

        # Children — one per item
        for detail in calc['item_details']:
            item = detail['item']
            variant = detail.get('variant')
            qty = detail['quantity']
            itype = detail['type']
            # Lock the stock row before decrementing so concurrent checkouts can't oversell.
            _title = item.title
            if variant:
                variant = (db.session.query(ItemVariant).filter_by(id=variant.id)
                           .with_for_update().populate_existing().first())
                if not variant or (variant.stock or 0) < qty:
                    db.session.rollback()
                    flash(f'Sold out: {_title}. Please refresh and try again.', 'danger')
                    return redirect(url_for('cart_view'))
                variant.stock = max(0, (variant.stock or 0) - qty)
            else:
                item = (db.session.query(Item).filter_by(id=item.id)
                        .with_for_update().populate_existing().first())
                if not item or (item.stock or 0) < qty:
                    db.session.rollback()
                    flash(f'Sold out: {_title}. Please refresh and try again.', 'danger')
                    return redirect(url_for('cart_view'))
                item.stock = max(0, (item.stock or 0) - qty)
                if item.stock <= 0 and not item.has_variants:
                    item.is_available = False
            if itype == 'sale':
                child = Booking(
                    booking_reference=generate_booking_reference(),
                    customer_id=current_user.id,
                    item_id=item.id,
                    variant_id=(variant.id if variant else None),
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
                    vendor_share=detail['subtotal'],
                )
            else:
                # Rental child
                child = Booking(
                    booking_reference=generate_booking_reference(),
                    customer_id=current_user.id,
                    item_id=item.id,
                    variant_id=(variant.id if variant else None),
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
                    vendor_share=detail['subtotal'],
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

        notify_all_admins(
            f"💰 New Payment Proof (Cart) — {parent.booking_reference} — ₹{proof.amount_paid:,.0f} ({proof.method.upper()})",
            link=url_for('admin_payment_queue')
        )

        # Vendor notifications deferred until admin verifies payment
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
            cvar = ItemVariant.query.get(c['variant_id']) if c.get('variant_id') else None
            cart_items_data.append({'item': item, 'variant': cvar,
                                    'quantity': c.get('quantity', 1),
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
    if not item.is_available:
        flash('This item is out of stock.', 'warning')
        return redirect(url_for('item_detail', item_id=item_id))

    # Variant selection — price + stock come from the chosen variant
    selected_variant = None
    if item.has_variants:
        variant_id = request.args.get('variant', type=int) or request.form.get('variant_id', type=int)
        is_base_booking = (request.args.get('base') == '1' or request.form.get('base') == '1')
        if not variant_id and not is_base_booking:
            flash('Please select Standard or a variant to continue.', 'warning')
            return redirect(url_for('item_detail', item_id=item_id))
        if variant_id:
            selected_variant = ItemVariant.query.filter_by(id=variant_id, item_id=item.id, is_available=True).first()
            if not selected_variant:
                flash('Selected variant is not available.', 'warning')
                return redirect(url_for('item_detail', item_id=item_id))
            if (selected_variant.stock or 0) < 1:
                flash('Selected variant is out of stock.', 'warning')
                return redirect(url_for('item_detail', item_id=item_id))
        elif (item.stock or 0) < 1:
            flash('Standard option is out of stock.', 'warning')
            return redirect(url_for('item_detail', item_id=item_id))
    elif (item.stock or 0) < 1:
        flash('This item is out of stock.', 'warning')
        return redirect(url_for('item_detail', item_id=item_id))
    # kwargs that keep the chosen option across error redirects back to this page
    buy_kw = ({'variant': selected_variant.id} if selected_variant
              else ({'base': 1} if item.has_variants else {}))
    unit_stock = (selected_variant.stock or 0) if selected_variant else (item.stock or 0)
    unit_price = ((selected_variant.sale_price or item.sale_price) if selected_variant else item.sale_price)

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
        if quantity > unit_stock:
            flash(f'Only {unit_stock} unit(s) available.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))

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
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))
        if _utr_already_used(utr):
            flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))

        # Payment proof — mandatory
        screenshot = request.files.get('screenshot')
        if not screenshot or not screenshot.filename:
            flash('Payment screenshot is required.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))
        if not Config.CLOUDINARY_CLOUD_NAME:
            flash('Cloudinary not configured.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))

        calc = calculate_sale(item, quantity, area, weight, price_override=unit_price)

        from datetime import date as _date
        today = ist_today()

        booking = Booking(
            booking_reference=generate_booking_reference(),
            customer_id=current_user.id,
            item_id=item.id,
            variant_id=(selected_variant.id if selected_variant else None),
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
        )

        # Stock permanently reduced (no return) — variant if present, else item
        if selected_variant:
            selected_variant.stock = max(0, (selected_variant.stock or 0) - quantity)
        else:
            item.stock = (item.stock or 0) - quantity
            if item.stock <= 0:
                item.stock = 0
                if not item.has_variants:
                    item.is_available = False

        db.session.add(booking)
        try:
            db.session.flush()  # get booking.id
        except IntegrityError as e:
            db.session.rollback()
            if _is_utr_conflict(e):
                flash('This UTR has already been used. Please enter the correct UTR.', 'danger')
            else:
                app.logger.exception('IntegrityError in buy_item flush')
                flash('Could not complete the purchase. Please try again.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))

        # Upload screenshot + create PaymentProof
        folder = f"vyahmandap/payments/booking_{booking.id}"
        upload_result = upload_file_to_cloudinary(screenshot, folder=folder)
        if not upload_result or not upload_result.get('url'):
            db.session.rollback()
            flash('Screenshot upload failed. Please try again.', 'danger')
            return redirect(url_for('buy_item', item_id=item_id, **buy_kw))

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

        notify_all_admins(
            f"💰 New Payment Proof (Sale) — {booking.booking_reference} — ₹{proof.amount_paid:,.0f} ({proof.method.upper()})",
            link=url_for('admin_payment_queue')
        )

        # Vendor notification (in-app + Telegram) deferred until admin verifies payment

        flash(f'Purchase confirmed! Reference: {booking.booking_reference}', 'success')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    return render_template('buy.html', item=item, selected_variant=selected_variant,
                           unit_price=unit_price, unit_stock=unit_stock,
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
    # Trial run: same policy minus 'unsafe-eval', report-only (logs to the browser console,
    # blocks nothing). If the console stays clean on every page, drop 'unsafe-eval' above.
    response.headers['Content-Security-Policy-Report-Only'] = csp.replace(" 'unsafe-eval'", "")
    return response


# ============================================
# BATCH 4 — ADDITIVE FEATURES (25 Sep 2026)
# ============================================

def _calculate_refund(booking):
    """Refund calc — 0 if payment not verified."""
    from datetime import date as _date
    # No payment received → no refund
    if booking.payment_status not in ('paid', 'verified', 'partial'):
        return 0.0, 0.0, 0
    days_until = (booking.start_date - ist_today()).days
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

    # Eligibility — cannot cancel once physically moved
    if booking.booking_status in ('cancelled', 'completed', 'dispatched', 'in_use',
                                   'return_initiated', 'return_received'):
        flash('This booking cannot be cancelled at this stage.', 'warning')
        return redirect(url_for('my_booking_detail', booking_id=booking.id))

    from datetime import date as _date
    if booking.start_date < ist_today():
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

        # Restore stock (variant-aware; handles bundle/cart parents)
        _restore_booking_stock(booking)

        db.session.commit()

        try:
            notify_all_admins(
                f"❌ Booking Cancelled — {booking.booking_reference} by {current_user.role} — Refund ₹{refund_amount}",
                link=url_for('admin_booking_detail', booking_id=booking.id)
            )
        except Exception:
            pass

        # PART J — Event 2: Booking cancelled -> vendor(s) + customer
        # Uses helper — bundle/cart parents notify each vendor with their child link
        _notify_vendors_of_cancellation(
            booking,
            booking.cancellation_reason or 'Booking cancelled'
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
        cache.delete('active_announcement')
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
    cache.delete('active_announcement')
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
    cache.delete('active_announcement')
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
                         Booking.booking_status == 'completed',
                         _or(Booking.order_type.is_(None),
                             Booking.order_type.notin_(['bundle', 'cart'])))
                 .order_by(Booking.created_at.desc())
                 .all())

    total_base_rent, total_sos_bonus = _vendor_share_sum(completed)
    total_earnings = round(total_base_rent + total_sos_bonus, 2)
    total_bookings = len(completed)
    total_deposits = 0  # Deposits handled by admin, not vendor

    # Expected from active bookings (in-progress, not yet completed)
    _active_statuses = ['confirmed', 'dispatched', 'in_use', 'return_initiated', 'return_received']
    active_for_expected = (Booking.query
                           .join(Item, Booking.item_id == Item.id)
                           .filter(Item.vendor_id == current_user.id,
                                   Booking.booking_status.in_(_active_statuses),
                                   _or(Booking.order_type.is_(None),
                                       Booking.order_type.notin_(['bundle', 'cart'])))
                           .all())
    expected_base, expected_sos = _vendor_share_sum(active_for_expected)
    expected_earnings = round(expected_base + expected_sos, 2)

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
        expected_earnings=expected_earnings,
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

    # SEO — use visible price (rate_with_commission) to match customer-facing rate
    visible_rate = int(item.rate_with_commission or item.rate_per_day or 0)
    vname = item.vendor.company_name or item.vendor.public_id or 'a verified vendor'
    seo_title = f'{item.title} — Rent ₹{visible_rate}/day in Harda | VyahMandap'
    if item.description and item.description.strip():
        desc = item.description.strip()[:150]
    else:
        desc = f'{item.title} available on rent from {vname}. ₹{visible_rate}/day in Harda.'
    # Absolute image URL for OG
    raw_img = item.get_image() or ''
    if raw_img.startswith('http'):
        seo_image = raw_img
    elif raw_img:
        seo_image = Config.SITE_URL.rstrip('/') + raw_img
    else:
        seo_image = url_for('static', filename='images/logo.png', _external=True)

    # Variants — sorted by sort_order, only available
    item_variants = []
    if item.has_variants:
        item_variants = (ItemVariant.query
                         .filter_by(item_id=item.id, is_available=True)
                         .order_by(ItemVariant.sort_order.asc(), ItemVariant.id.asc())
                         .all())

    return render_template(
        'item_detail.html',
        item=item,
        vendor=item.vendor,
        reviews=reviews,
        all_review_count=all_review_count,
        avg_rating=avg_rating,
        in_wishlist=in_wishlist,
        variants=item_variants,
        has_dimensions=any([item.height_cm, item.width_cm, item.length_cm, item.weight_kg]),
        seo_title=seo_title,
        seo_description=desc,
        canonical_url=f'{Config.SITE_URL.rstrip("/")}/item/{item.id}',
        og_image=seo_image,
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
