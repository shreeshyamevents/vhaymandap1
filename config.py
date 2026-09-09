import os

class Config:
    APP_NAME = "VyahMandap"
    APP_TAGLINE = "Taiyari Hamari, Celebration Aapka!"
    
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'vyahmandap-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///database/vyahmandap.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
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
    
    # Transport Rates (Harda region)
    TRANSPORT_RATES = {
        'city': {
            'till_20': 600,
            'above_20': 1100
        },
        'outskirts': {
            'till_20': 1500,
            'above_20': 2000
        }
    }
    
    # Platform Commission
    COMMISSION_RATE = 0.05  # 5%
    
    # Categories
    CATEGORIES = [
        ('furniture', 'Sofas & Furniture'),
        ('lighting', 'Truss & Lighting'),
        ('decor', 'Decor & Floral'),
        ('mandap', 'Mandap & Stage')
    ]