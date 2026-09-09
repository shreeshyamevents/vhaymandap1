from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User
from utils import validate_mobile

bp = Blueprint('auth', __name__, url_prefix='/auth')

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        mobile = request.form.get('mobile', '').strip()
        password = request.form.get('password', '').strip()
        
        user = User.query.filter_by(mobile=mobile).first()
        
        if user and user.check_password(password):
            login_user(user)
            flash(f'🎉 Welcome back, {user.name}!', 'success')
            
            if user.role == 'admin':
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('main.index'))
        else:
            flash('❌ Invalid mobile number or password.', 'danger')
    
    return render_template('auth/login.html')

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        mobile = request.form.get('mobile', '').strip()
        email = request.form.get('email', '').strip() or None
        password = request.form.get('password', '').strip()
        confirm_password = request.form.get('confirm_password', '').strip()
        
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
        
        if email and User.query.filter_by(email=email).first():
            errors.append('A user with this email already exists.')
        
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('auth/register.html')
        
        user = User(
            name=name,
            mobile=mobile,
            email=email,
            role='customer'
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        flash('✅ Account created successfully! Please login.', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('auth/register.html')

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('🔒 You have been logged out.', 'info')
    return redirect(url_for('main.index'))