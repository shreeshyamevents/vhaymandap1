import os
import re
import random
import string
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import current_app
from config import Config

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS

def save_uploaded_file(file):
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        unique_filename = f"{timestamp}_{filename}"
        filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], unique_filename)
        
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        file.save(filepath)
        return unique_filename
    return None

def calculate_booking_total(item, start_date, end_date, quantity, area, weight_category):
    from config import Config
    
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

def get_category_icon(category):
    icons = {
        'furniture': 'fa-couch',
        'lighting': 'fa-lightbulb',
        'decor': 'fa-palette',
        'mandap': 'fa-archway'
    }
    return icons.get(category, 'fa-box')

def get_category_label(category):
    labels = {
        'furniture': 'Sofas & Furniture',
        'lighting': 'Truss & Lighting',
        'decor': 'Decor & Floral',
        'mandap': 'Mandap & Stage'
    }
    return labels.get(category, category.capitalize())