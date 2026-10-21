"""Application configuration + UI translations.

Phase 4 / Stage B: extracted verbatim from app.py (no behaviour change).
Imports are limited to what the moved code needs.
"""
import os
from datetime import timedelta


# ============================================
# CONFIGURATION
# ============================================
class Config:
    APP_NAME = "VyahMandap"
    APP_TAGLINE = "Taiyari Hamari, Celebration Aapka!"
    # No hardcoded fallback. On Render (RENDER env var is set) the app refuses
    # to start without SECRET_KEY; locally a random per-process key is used.
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY:
        if os.environ.get('RENDER'):
            raise RuntimeError('SECRET_KEY environment variable is not set')
        import secrets as _secrets
        SECRET_KEY = _secrets.token_hex(32)
    
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
    
    # Hard ceiling = the largest legit upload (video). Non-video routes are held to
    # MAX_IMAGE_UPLOAD_BYTES by _limit_non_video_uploads() below.
    MAX_VIDEO_UPLOAD_BYTES = 25 * 1024 * 1024
    MAX_IMAGE_UPLOAD_BYTES = 15 * 1024 * 1024
    MAX_CONTENT_LENGTH = MAX_VIDEO_UPLOAD_BYTES
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
    
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or ''
    
    CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME') or ''
    CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY') or ''
    CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET') or ''
    
    REPORT_RETENTION_DAYS = 30
    CRON_SECRET = os.environ.get('CRON_SECRET') or ''  # empty = cron endpoint disabled
    
    BUSINESS_NAME = "VyahMandap"
    BUSINESS_PHONE = "8319337063"
    BUSINESS_PHONE_ALT = "9981845362"
    BUSINESS_EMAIL = "vyahmandap@gmail.com"
    BUSINESS_LOCATION = "Harda, Madhya Pradesh"
    MSME_NUMBER = "UDYAM-MP-21-0016639"
    # 16 Oct 2026 — Google Sign-In (OAuth 2.0 server-side)
    GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID') or ''
    GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET') or ''
    # 16 Oct 2026 — Transporter system
    TRANSPORTER_VEHICLE_TYPES = [
        ('bike', 'Bike / Scooter'),
        ('auto', 'Auto / Three-wheeler'),
        ('tempo', 'Tempo / Mini Truck'),
        ('truck', 'Truck / Large Vehicle'),
        ('other', 'Other'),
    ]
    # 12 Oct 2026 — SEO
    SITE_URL = os.environ.get('SITE_URL') or 'https://vhaymandap1.onrender.com'
    SITE_NAME = 'VyahMandap'
    SITE_DESCRIPTION = 'Book wedding equipment on rent in Harda — mandap, sofa, lighting, decor, tent, and more from verified vendors.'
    
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
    # 08 Oct 2026 — SOS disabled (testing phase)
    SOS_ENABLED = False
    # 10 Oct 2026 — auto-cancel bookings with unpaid proof after TTL
    PAYMENT_PENDING_TTL_HOURS = 24
    # 10 Oct 2026 — OCR.space for PAN image verification (KYC Phase 2)
    OCR_SPACE_API_KEY = os.environ.get('OCR_SPACE_API_KEY') or ''
    # 10 Oct 2026 — KYC vault encryption (Fernet key)
    KYC_ENCRYPTION_KEY = os.environ.get('KYC_ENCRYPTION_KEY') or ''
    # 10 Oct 2026 — KYC reveal rate limit (per admin)
    KYC_REVEAL_RATE_LIMIT_PER_HOUR = 30
    # 10 Oct 2026 — i18n
    SUPPORTED_LANGS = [('en', 'English'), ('hi', 'Hinglish')]
    DEFAULT_LANG = 'en'
    # 10 Oct 2026 — Transport OTP (Option B, public /transport/verify page)
    OTP_TTL_DISPATCH_HOURS = 72        # OTP1 — customer receives item
    OTP_TTL_RETURN_HOURS = 72          # OTP2 — vendor receives item back
    OTP_MAX_ATTEMPTS = 3               # wrong tries before lock
    OTP_LOCK_HOURS = 24                # lock duration after max attempts

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
        ('sfx', 'SFX & Special Effects'),
    ]


# ============================================
# TRANSLATIONS (English + Hinglish)
# ============================================
# Hinglish = Roman-script Hindi mixed with English words commonly used in
# Indian business speech (Dashboard, Booking, Cart stay English).
# Used via {{ t('key') }} in templates. Fallback: hi -> en -> key.
TRANSLATIONS = {
    'en': {
        # Nav
        'home': 'Home',
        'browse': 'Browse',
        'marketplace': 'Marketplace',
        'bundles': 'Event Bundles',
        'leaderboard': 'Leaderboard',
        'cart': 'Cart',
        'messages': 'Messages',
        'support': 'Support',
        'login': 'Login Karein',
        'register': 'Naya Account',
        'logout': 'Logout',
        'my_account': 'My Account',
        'my_dashboard': 'My Dashboard',
        'my_bookings': 'My Bookings',
        'wishlist': 'My Wishlist',
        'recently_viewed': 'Recently Viewed',
        # CTAs
        'book_now': 'Book Now',
        'buy_now': 'Buy Now',
        'add_to_cart': 'Add to Cart',
        'add_to_rent_cart': 'Add to Rent Cart',
        'add_to_buy_cart': 'Add to Buy Cart',
        'checkout': 'Checkout',
        'continue_shopping': 'Continue Shopping',
        'proceed_checkout': 'Proceed to Checkout',
        'submit': 'Submit',
        'cancel': 'Cancel',
        'confirm': 'Confirm',
        'back': 'Back',
        'save': 'Save',
        'close': 'Close',
        'search': 'Search',
        'clear_filters': 'Clear filters',
        # Common labels
        'start_date': 'Start Date',
        'end_date': 'End Date',
        'quantity': 'Quantity',
        'venue_address': 'Venue Address',
        'amount': 'Amount',
        'total': 'Total',
        'status': 'Status',
        'subtotal': 'Subtotal',
        'security_deposit': 'Security Deposit',
        'platform_fee': 'Platform Fee',
        # Messages
        'welcome_back': 'Welcome back, {name}!',
        'your_cart_empty': 'Your cart is empty',
        'no_items_found': 'No items found',
        'loading': 'Loading...',
        # Status
        'confirmed': 'Confirmed',
        'pending': 'Pending',
        'dispatched': 'Dispatched',
        'cancelled': 'Cancelled',
        'completed': 'Completed',
        'in_use': 'In Use',
        'return_initiated': 'Return Initiated',
        'return_received': 'Return Received',
        # Landing page
        'trusted_by_vendors': 'Trusted by 50+ vendors in Harda',
        'hero_subtitle_pre': 'Everything for your',
        'hero_subtitle_post': 'wedding — from verified vendors across Harda & Madhya Pradesh.',
        'search_placeholder': 'Search sofas, mandap, lighting...',
        'verified_vendors_short': 'Verified vendors',
        'cheaper_than_buying': '50% cheaper than buying',
        'damage_protection': 'Damage protection',
        'browse_equipment': 'Browse Equipment',
        'min_price_per_day': 'Min Price (₹/day)',
        'max_price_per_day': 'Max Price (₹/day)',
        'sort_by': 'Sort By',
        'newest_first': 'Newest First',
        'price_low_high': 'Price: Low to High',
        'price_high_low': 'Price: High to Low',
        'highest_rated': 'Highest Rated',
        'verified_only': 'Verified only',
        'all_items': 'All Items',
        'how_it_works': 'How it works',
        'how_it_works_subtitle': 'Booking wedding equipment has never been this simple.',
        'step1_title': 'Find what you need',
        'step1_desc': 'Browse thousands of items across furniture, lighting, decor, and mandap categories.',
        'step2_title': 'Book your dates',
        'step2_desc': 'Check live availability, select quantities, and lock in your dates instantly.',
        'step3_title': 'We deliver & pick up',
        'step3_desc': 'Vendors deliver to your venue and pick up after. You enjoy the celebration.',
        'for_vendors': 'For Vendors',
        'vendor_cta_title': 'Have equipment to rent out?',
        'vendor_cta_desc': 'Join hundreds of vendors earning on VyahMandap. Free listing, low commission.',
        'become_vendor': 'Become a Vendor',
        # JS runtime
        'js_items_available': '{count} items available',
        'js_item_available': '{count} item available',
        'js_no_image': 'No Image',
        'js_verified': 'Verified',
        'js_left': '{count} left',
        'js_no_items': 'No items found',
        # Category labels
        'cat_furniture': 'Sofas & Furniture',
        'cat_lighting': 'Truss & Lighting',
        'cat_decor': 'Decor & Floral',
        'cat_mandap': 'Mandap & Stage',
        'cat_tent': 'Tent & Canopy',
        'cat_flooring': 'Flooring & Carpet',
        'cat_catering': 'Catering Equipment',
        'cat_sound': 'Sound & Music',
        'cat_bar': 'Bar & Beverages',
        'cat_entertainment': 'Entertainment',
        'cat_electrical': 'Electrical & Power',
        'cat_cooling': 'Cooling & Heating',
        'cat_sfx': 'SFX & Special Effects',
        # Item detail page
        'back_marketplace': 'Back to Marketplace',
        'per_day': '/day',
        'rate': 'Rate',
        'deposit': 'Deposit',
        'stock': 'Stock',
        'rate_includes_commission': 'Rate includes 5% platform fee.',
        'description': 'Description',
        'vendor': 'Vendor',
        'view_profile': 'View profile',
        'verified_vendor': 'Verified vendor',
        'verified': 'Verified',
        'not_available': 'Not Available',
        'wishlist_btn': 'Wishlist',
        'in_wishlist': 'In Wishlist',
        'compare': 'Compare',
        'share': 'Share',
        'login_to_save': 'Login to Save',
        'reviews': 'Reviews',
        'no_reviews': 'No reviews yet.',
        'view_all': 'View all',
        'vendor_response': 'Vendor response',
        'compare_items': 'Compare Items',
        'compare_clear': 'Clear',
        # Booking form
        'book_equipment': 'Book Equipment',
        'check_availability': 'Check Availability',
        'availability_calendar': 'Availability Calendar',
        'available': 'Available',
        'fully_booked': 'Fully Booked',
        'partial': 'Partial',
        'click_any_date': 'Click on any date to see details.',
        'start_time': 'Start Time',
        'end_time': 'End Time',
        'max_available': 'Max: {count} available',
        'delivery_area': 'Delivery Area',
        'harda_city': 'Harda City',
        'outskirts_area': 'Outskirts',
        'weight_category': 'Weight Category',
        'upto_20_ton': 'Up to 20 Ton',
        'above_20_ton': '20 Ton+',
        'cost_breakdown': 'Cost Breakdown',
        'rental_period': 'Rental Period',
        'base_rent': 'Base Rent',
        'platform_fee_pct': 'Platform Fee (5%)',
        'payment_gateway_pct': 'Payment Gateway (2%)',
        'total_payable': 'Total Payable',
        'transport_note': 'Please note: Transport charges are not included in this bill. Transport team will coordinate and settle these separately.',
        'kyc_required_heading': 'KYC Verification Required',
        'kyc_booking_message': 'Your booking total is ₹30,000 or above. Please provide Aadhaar and PAN details.',
        'aadhaar_label': 'Aadhaar Number (12 digits)',
        'pan_label': 'PAN Number',
        'pan_image_label': 'PAN Card Image',
        'for_verification': 'for verification',
        'pan_image_hint': 'Upload a clear photo of your PAN card. The number on the image must match the PAN you entered.',
        'payment_details': 'Payment Details',
        'upi_id_label': 'UPI ID',
        'bank_transfer': 'Bank Transfer',
        'name_label': 'Name',
        'account_number_label': 'A/C',
        'ifsc_label': 'IFSC',
        'bank_label': 'Bank',
        'branch_label': 'Branch',
        'upload_payment_proof': 'Upload Payment Proof',
        'utr_transaction_label': 'UTR / Transaction ID',
        'payment_method_label': 'Payment Method',
        'amount_paid_label': 'Amount Paid (₹)',
        'payment_date_label': 'Payment Date',
        'payer_name_label': 'Payer Name',
        'payer_bank_label': 'Payer Bank (optional)',
        'notes_optional_label': 'Notes (optional)',
        'screenshot_label': 'Payment Screenshot',
        'mandatory_suffix': '(mandatory)',
        'screenshot_hint': 'Upload a PhonePe / GPay / bank screenshot',
        'confirm_booking_btn': 'Confirm Booking',
        'days_label': 'days',
        'day_label': 'day',
        'availability_details': 'Availability Details',
        'date_label': 'Date',
        'status_label': 'Status',
        'total_stock_label': 'Total Stock',
        'booked_label': 'Booked',
        'available_qty_label': 'Available',
        'return_day_note': 'Return day — Bookable from next day',
        'return_day_short': 'Return day',
        # JS error titles
        'js_item_not_available': 'Item Not Available for Selected Dates',
        'js_item_returning': 'Item Returning That Day',
        'js_item_fully_booked': 'Item Fully Booked',
        'js_insufficient_stock': 'Insufficient Stock',
        'js_limited_availability': 'Limited Availability',
        'js_next_available': 'Next Available Date',
        'js_not_available': 'Not Available',
        'js_check_input': 'Please Check Your Input',
        'js_calendar_load_fail': 'Calendar library failed to load. Please refresh the page.',
        'js_calendar_error': 'Calendar could not load. Please refresh the page.',
    },
    'hi': {
        # Nav
        'home': 'Home',
        'browse': 'Dekhein',
        'marketplace': 'Marketplace',
        'bundles': 'Event Packages',
        'leaderboard': 'Top Vendors',
        'cart': 'Cart',
        'messages': 'Messages',
        'support': 'Madad',
        'login': 'Login',
        'register': 'Register',
        'logout': 'Logout',
        'my_account': 'Mera Account',
        'my_dashboard': 'Mera Dashboard',
        'my_bookings': 'Meri Bookings',
        'wishlist': 'Meri Wishlist',
        'recently_viewed': 'Abhi Dekhe Hue',
        # CTAs
        'book_now': 'Book Karein',
        'buy_now': 'Buy Karein',
        'add_to_cart': 'Cart Mein Daalein',
        'add_to_rent_cart': 'Rent Cart Mein',
        'add_to_buy_cart': 'Buy Cart Mein',
        'checkout': 'Checkout Karein',
        'continue_shopping': 'Aur Dekhein',
        'proceed_checkout': 'Order Aage Badhayein',
        'submit': 'Submit Karein',
        'cancel': 'Cancel',
        'confirm': 'Confirm Karein',
        'back': 'Wapas',
        'save': 'Save Karein',
        'close': 'Band Karein',
        'search': 'Khojein',
        'clear_filters': 'Filters Hataayein',
        # Common labels
        'start_date': 'Shuru Date',
        'end_date': 'Aakhri Date',
        'quantity': 'Kitna',
        'venue_address': 'Venue Address',
        'amount': 'Rakam',
        'total': 'Total',
        'status': 'Status',
        'subtotal': 'Total Rakam',
        'security_deposit': 'Security Deposit',
        'platform_fee': 'Platform Fee',
        # Messages
        'welcome_back': 'Wapas Swagat, {name}!',
        'your_cart_empty': 'Aapka cart khaali hai',
        'no_items_found': 'Kuch nahi mila',
        'loading': 'Load ho raha hai...',
        # Status
        'confirmed': 'Confirmed',
        'pending': 'Pending',
        'dispatched': 'Bhej Diya',
        'cancelled': 'Cancel Kiya',
        'completed': 'Pura Hua',
        'in_use': 'Use Mein',
        'return_initiated': 'Wapas Bheja',
        'return_received': 'Wapas Mil Gaya',
        # Landing page
        'trusted_by_vendors': 'Harda ke 50+ vendors ka bharosa',
        'hero_subtitle_pre': 'Aapki',
        'hero_subtitle_post': 'shaadi ke liye sab kuch — Harda aur MP ke verified vendors se.',
        'search_placeholder': 'Sofa, mandap, lighting dhundhein...',
        'verified_vendors_short': 'Verified vendors',
        'cheaper_than_buying': 'Buying se 50% sasta',
        'damage_protection': 'Damage protection',
        'browse_equipment': 'Equipment Dekhein',
        'min_price_per_day': 'Min Rate (₹/day)',
        'max_price_per_day': 'Max Rate (₹/day)',
        'sort_by': 'Sort Karein',
        'newest_first': 'Naye Pehle',
        'price_low_high': 'Price: Kam se Jyada',
        'price_high_low': 'Price: Jyada se Kam',
        'highest_rated': 'Top Rating',
        'verified_only': 'Sirf Verified',
        'all_items': 'Sab Items',
        'how_it_works': 'Kaise Kaam Karta Hai',
        'how_it_works_subtitle': 'Shaadi ka equipment book karna itna aasan pehle kabhi nahi tha.',
        'step1_title': 'Jo Chahiye Wo Dhundhein',
        'step1_desc': 'Furniture, lighting, decor, aur mandap categories me hazaron items dekhein.',
        'step2_title': 'Apni Dates Book Karein',
        'step2_desc': 'Live availability dekhein, kitna chahiye batayein, aur turant dates lock karein.',
        'step3_title': 'Delivery Aur Pickup Humara Kaam',
        'step3_desc': 'Vendors aapke venue pe deliver karenge aur wapas pickup karenge. Aap celebration enjoy karein.',
        'for_vendors': 'Vendors Ke Liye',
        'vendor_cta_title': 'Equipment rent pe dena hai?',
        'vendor_cta_desc': 'VyahMandap pe kamane wale saikdon vendors me shaamil ho. Free listing, kam commission.',
        'become_vendor': 'Vendor Banein',
        # JS runtime
        'js_items_available': '{count} items available',
        'js_item_available': '{count} item available',
        'js_no_image': 'No Image',
        'js_verified': 'Verified',
        'js_left': '{count} bache',
        'js_no_items': 'Kuch nahi mila',
        # Category labels
        'cat_furniture': 'Sofa & Furniture',
        'cat_lighting': 'Truss & Lighting',
        'cat_decor': 'Decor & Floral',
        'cat_mandap': 'Mandap & Stage',
        'cat_tent': 'Tent & Canopy',
        'cat_flooring': 'Flooring & Carpet',
        'cat_catering': 'Catering Equipment',
        'cat_sound': 'Sound & Music',
        'cat_bar': 'Bar & Beverages',
        'cat_entertainment': 'Entertainment',
        'cat_electrical': 'Electrical & Power',
        'cat_cooling': 'Cooling & Heating',
        'cat_sfx': 'SFX & Special Effects',
        # Item detail page
        'back_marketplace': 'Marketplace Wapas',
        'per_day': '/din',
        'rate': 'Rate',
        'deposit': 'Deposit',
        'stock': 'Stock',
        'rate_includes_commission': 'Rate me 5% platform fee shaamil hai.',
        'description': 'Description',
        'vendor': 'Vendor',
        'view_profile': 'Profile Dekhein',
        'verified_vendor': 'Verified vendor',
        'verified': 'Verified',
        'not_available': 'Available Nahi',
        'wishlist_btn': 'Wishlist',
        'in_wishlist': 'Wishlist Me',
        'compare': 'Compare',
        'share': 'Share',
        'login_to_save': 'Save Karne Ke Liye Login',
        'reviews': 'Reviews',
        'no_reviews': 'Abhi koi review nahi.',
        'view_all': 'Sab Dekhein',
        'vendor_response': 'Vendor ka jawab',
        'compare_items': 'Items Compare Karein',
        'compare_clear': 'Clear',
        # Booking form
        'book_equipment': 'Equipment Book Karein',
        'check_availability': 'Availability Dekhein',
        'availability_calendar': 'Availability Calendar',
        'available': 'Available',
        'fully_booked': 'Pura Booked',
        'partial': 'Kuch Available',
        'click_any_date': 'Kisi bhi date pe click karein details ke liye.',
        'start_time': 'Shuru Time',
        'end_time': 'Aakhri Time',
        'max_available': 'Max: {count} available',
        'delivery_area': 'Delivery Area',
        'harda_city': 'Harda City',
        'outskirts_area': 'Bahar',
        'weight_category': 'Weight Category',
        'upto_20_ton': '20 Ton Tak',
        'above_20_ton': '20 Ton Se Jyada',
        'cost_breakdown': 'Kharcha Breakdown',
        'rental_period': 'Kitne Din',
        'base_rent': 'Base Rent',
        'platform_fee_pct': 'Platform Fee (5%)',
        'payment_gateway_pct': 'Payment Gateway (2%)',
        'total_payable': 'Total Dena Hai',
        'transport_note': 'Note: Transport charge is bill me nahi hai. Transport team alag se coordinate karke settle karegi.',
        'kyc_required_heading': 'KYC Verification Chahiye',
        'kyc_booking_message': 'Aapki booking ₹30,000 ya usse jyada hai. Aadhaar aur PAN details dein.',
        'aadhaar_label': 'Aadhaar Number (12 digit)',
        'pan_label': 'PAN Number',
        'pan_image_label': 'PAN Card Ki Photo',
        'for_verification': 'verification ke liye',
        'pan_image_hint': 'PAN card ki clear photo upload karein. Photo pe jo number hai, wahi aapke entered PAN se match hona chahiye.',
        'payment_details': 'Payment Details',
        'upi_id_label': 'UPI ID',
        'bank_transfer': 'Bank Transfer',
        'name_label': 'Naam',
        'account_number_label': 'A/C',
        'ifsc_label': 'IFSC',
        'bank_label': 'Bank',
        'branch_label': 'Branch',
        'upload_payment_proof': 'Payment Proof Upload Karein',
        'utr_transaction_label': 'UTR / Transaction ID',
        'payment_method_label': 'Payment Method',
        'amount_paid_label': 'Kitna Diya (₹)',
        'payment_date_label': 'Payment Date',
        'payer_name_label': 'Payer Ka Naam',
        'payer_bank_label': 'Payer Bank (optional)',
        'notes_optional_label': 'Notes (optional)',
        'screenshot_label': 'Payment Screenshot',
        'mandatory_suffix': '(zaroori hai)',
        'screenshot_hint': 'PhonePe / GPay / bank ka screenshot upload karein',
        'confirm_booking_btn': 'Booking Confirm Karein',
        'days_label': 'din',
        'day_label': 'din',
        'availability_details': 'Availability Details',
        'date_label': 'Date',
        'status_label': 'Status',
        'total_stock_label': 'Total Stock',
        'booked_label': 'Booked',
        'available_qty_label': 'Available',
        'return_day_note': 'Return day — agle din se book ho sakta hai',
        'return_day_short': 'Return day',
        # JS error titles
        'js_item_not_available': 'Ye Item In Dates Pe Available Nahi',
        'js_item_returning': 'Item Us Din Return Ho Raha Hai',
        'js_item_fully_booked': 'Item Pura Booked Hai',
        'js_insufficient_stock': 'Stock Kam Hai',
        'js_limited_availability': 'Sirf Kuch Available Hai',
        'js_next_available': 'Next Available Date',
        'js_not_available': 'Available Nahi',
        'js_check_input': 'Apni Details Check Karein',
        'js_calendar_load_fail': 'Calendar load nahi hua. Page refresh karein.',
        'js_calendar_error': 'Calendar load nahi ho paaya. Page refresh karein.',
    },
}
