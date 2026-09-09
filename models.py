from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(UserMixin, db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mobile = db.Column(db.String(10), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=True)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='customer')
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    items = db.relationship('Item', backref='vendor', lazy=True)
    bookings = db.relationship('Booking', backref='customer', lazy=True)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    # Flask-Login expects these properties
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
        from config import Config
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
