import os
import sys
import logging
import traceback
from functools import wraps
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import random
import requests  # Add this for ImgBB uploads

import stripe
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, redirect, url_for, flash, request, session, jsonify, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect, generate_csrf
import uuid
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
from sqlalchemy import func, text
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from config import Config
from models import db, CMP_User, CMP_Product, CMP_Cart, CMP_Order, CMP_OrderItem, CMP_Review, CMP_Notification, CMP_Coupon, CMP_UsedCoupon, CMP_Favorite
from forms import (RegistrationForm, LoginForm, ProductForm, ProfileForm, ReviewForm, 
                   CouponForm, ApplyCouponForm)

# Configure logging for production
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.config.from_object(Config)

# Handle proxy headers for Render
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Database configuration for production
# Parse DATABASE_URL for PostgreSQL on Render
database_url = os.environ.get('DATABASE_URL', 'sqlite:///community_marketplace.db')
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
}

# Production security settings
if not app.debug and not app.testing:
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=86400,  # 24 hours
        REMEMBER_COOKIE_SECURE=True,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE='Lax',
    )

# Initialize extensions
db.init_app(app)

# Initialize CSRF Protection
csrf = CSRFProtect()
csrf.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'warning'
login_manager.session_protection = 'strong'

# ImgBB API Key (free image hosting)
IMGBB_API_KEY = '1c33d0a9405da3f7aff6e93121b5d7a9'

# Initialize Cloudinary (keep for compatibility but won't use)
try:
    if app.config['CLOUDINARY_CLOUD_NAME']:
        cloudinary.config(
            cloud_name=app.config['CLOUDINARY_CLOUD_NAME'],
            api_key=app.config['CLOUDINARY_API_KEY'],
            api_secret=app.config['CLOUDINARY_API_SECRET'],
            secure=True
        )
        logger.info("Cloudinary initialized successfully")
    else:
        logger.warning("Cloudinary not configured - skipping initialization")
except Exception as e:
    logger.error(f"Failed to initialize Cloudinary: {str(e)}")

# Initialize Stripe
if app.config['STRIPE_SECRET_KEY']:
    stripe.api_key = app.config['STRIPE_SECRET_KEY']
    logger.info("Stripe initialized successfully")
else:
    logger.warning("Stripe not configured - skipping initialization")


# ========== CSRF HELPER ==========
@app.context_processor
def inject_csrf_token():
    return dict(csrf_token=lambda: generate_csrf())


@app.after_request
def add_csrf_headers(response):
    if request.endpoint and not request.endpoint.startswith('static'):
        try:
            response.headers['X-CSRFToken'] = generate_csrf()
        except Exception:
            pass
    return response


# ========== DECORATORS ==========
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Access denied. Admin privileges required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def seller_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_sell():
            flash('Seller access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


def buyer_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_buy():
            flash('Buyer access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function


@contextmanager
def db_transaction():
    """Context manager for database transactions with proper rollback"""
    try:
        yield
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"Database transaction failed: {str(e)}\n{traceback.format_exc()}")
        raise


# ========== IMAGE UPLOAD FUNCTION USING IMGBB ==========
def upload_image_to_imgbb(file):
    """Upload image to ImgBB and return the URL"""
    try:
        # Prepare the upload
        url = "https://api.imgbb.com/1/upload"
        payload = {
            'key': IMGBB_API_KEY,
        }
        
        # Read the file
        image_data = file.read()
        
        # Upload to ImgBB
        response = requests.post(
            url,
            data=payload,
            files={'image': (secure_filename(file.filename), image_data)}
        )
        
        if response.status_code == 200:
            result = response.json()
            if result.get('success'):
                image_url = result['data']['url']
                logger.info(f"Image uploaded successfully to ImgBB: {image_url}")
                return image_url
            else:
                logger.error(f"ImgBB upload failed: {result}")
                return None
        else:
            logger.error(f"ImgBB upload error: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"ImgBB upload exception: {str(e)}")
        return None


# ========== DATABASE SEEDING FUNCTION (RUNS ONLY ONCE FOR CMP TABLES) ==========
def seed_database_if_empty():
    """Seed the database with initial data only if CMP_users table is empty"""
    with app.app_context():
        # Check if CMP_users table already has users
        try:
            user_count = CMP_User.query.count()
        except Exception as e:
            logger.warning(f"Could not check user count (table might not exist yet): {str(e)}")
            user_count = 0
        
        if user_count > 0:
            logger.info(f"CMP_users table already has {user_count} users. Skipping seed.")
            return
        
        logger.info("CMP_users table is empty. Starting initial data seeding...")
        
        try:
            # Create super admin
            admin = CMP_User(
                full_name='Super Admin',
                email='admin@communitymarket.co.za',
                phone_number='0112345678',
                address='Admin Office, 123 Main Street, Johannesburg, 2000',
                role='admin',
                is_active=True
            )
            admin.set_password('Admin@123')
            db.session.add(admin)
            logger.info("✓ Super admin created!")
            
            # Create sample seller
            seller = CMP_User(
                full_name='John Seller',
                email='seller@example.com',
                phone_number='0821234567',
                address='123 Main St, Cape Town, 8001',
                role='seller',
                is_active=True
            )
            seller.set_password('Seller@123')
            db.session.add(seller)
            logger.info("✓ Sample seller created!")
            
            # Create second seller
            seller2 = CMP_User(
                full_name='Tech Guru',
                email='techseller@example.com',
                phone_number='0845566778',
                address='15 Silicon Ave, Johannesburg, 2196',
                role='seller',
                is_active=True
            )
            seller2.set_password('Tech@123')
            db.session.add(seller2)
            logger.info("✓ Second seller created!")
            
            # Create sample buyer
            buyer = CMP_User(
                full_name='Jane Buyer',
                email='buyer@example.com',
                phone_number='0837654321',
                address='456 Oak Ave, Durban, 4001',
                role='buyer',
                is_active=True
            )
            buyer.set_password('Buyer@123')
            db.session.add(buyer)
            logger.info("✓ Sample buyer created!")
            
            # Create additional buyers for reviews
            buyer_names = ['Sarah Johnson', 'Michael Brown', 'Lisa Anderson', 'David Wilson', 'Emma Thompson']
            buyers = []
            phone_numbers = ['0810014567', '0810024567', '0810034567', '0810044567', '0810054567']
            for i, name in enumerate(buyer_names):
                new_buyer = CMP_User(
                    full_name=name,
                    email=f'reviewer{i+1}@example.com',
                    phone_number=phone_numbers[i],
                    address=f'{i+1} Review St, Cape Town, 8001',
                    role='buyer',
                    is_active=True
                )
                new_buyer.set_password('Review@123')
                db.session.add(new_buyer)
                buyers.append(new_buyer)
                logger.info(f"✓ Reviewer {name} created!")
            
            # Create dual role user
            both_user = CMP_User(
                full_name='Mike Both',
                email='both@example.com',
                phone_number='0841122334',
                address='789 Pine Rd, Pretoria, 0001',
                role='both',
                is_active=True
            )
            both_user.set_password('Both@123')
            db.session.add(both_user)
            logger.info("✓ Sample dual-role user created!")
            
            db.session.commit()
            
            # Get user IDs after commit
            seller_id = seller.id
            seller2_id = seller2.id
            buyer_id = buyer.id
            
            # Products data
            products_data = [
                {'name': 'Handmade Leather Bag', 'description': 'Beautiful handmade leather bag crafted by local artisans.', 'price': 850.00, 'stock_quantity': 10, 'category': 'clothing', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1590874103328-eac38a683ce7?w=400&h=300&fit=crop'},
                {'name': 'Premium Denim Jacket', 'description': 'Classic denim jacket with modern fit.', 'price': 650.00, 'stock_quantity': 20, 'category': 'clothing', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1576995853123-5a10305d93c0?w=400&h=300&fit=crop'},
                {'name': 'Running Shoes', 'description': 'Lightweight running shoes with superior cushioning.', 'price': 899.00, 'stock_quantity': 15, 'category': 'sports', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=400&h=300&fit=crop'},
                {'name': 'Wireless Noise Cancelling Headphones', 'description': 'High-quality wireless headphones with active noise cancellation.', 'price': 1299.00, 'stock_quantity': 25, 'category': 'electronics', 'seller_id': seller2_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=400&h=300&fit=crop'},
                {'name': 'Smart Watch Pro', 'description': 'Fitness tracker and smartwatch with heart rate monitor.', 'price': 2499.00, 'stock_quantity': 12, 'category': 'electronics', 'seller_id': seller2_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=400&h=300&fit=crop'},
                {'name': 'Wireless Earbuds', 'description': 'True wireless earbuds with charging case.', 'price': 499.00, 'stock_quantity': 30, 'category': 'electronics', 'seller_id': seller2_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1590658268037-6bf12165a8df?w=400&h=300&fit=crop'},
                {'name': 'Handcrafted Ceramic Coffee Mug Set', 'description': 'Set of 4 beautiful handmade ceramic coffee mugs.', 'price': 320.00, 'stock_quantity': 15, 'category': 'furniture', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1514228742587-6b1558fcca3d?w=400&h=300&fit=crop'},
                {'name': 'Modern Floor Lamp', 'description': 'Elegant floor lamp with adjustable brightness.', 'price': 450.00, 'stock_quantity': 8, 'category': 'furniture', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1507473885765-e6ed057f782c?w=400&h=300&fit=crop'},
                {'name': 'Wooden Bookshelf', 'description': 'Solid wood bookshelf with 5 shelves.', 'price': 1899.00, 'stock_quantity': 5, 'category': 'furniture', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1594620302200-9a762244a156?w=400&h=300&fit=crop'},
                {'name': 'The Great Gatsby (Hardcover)', 'description': 'Classic American novel. Collector\'s edition.', 'price': 180.00, 'stock_quantity': 20, 'category': 'books', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=400&h=300&fit=crop'},
                {'name': 'Python Programming Guide', 'description': 'Complete guide to Python programming.', 'price': 450.00, 'stock_quantity': 18, 'category': 'books', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1532012197267-da84d127e765?w=400&h=300&fit=crop'},
                {'name': 'Yoga Mat Premium', 'description': 'Non-slip yoga mat with carrying strap.', 'price': 299.00, 'stock_quantity': 25, 'category': 'sports', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1601925260368-ae2f83cf8b7f?w=400&h=300&fit=crop'},
                {'name': 'Dumbbell Set 20kg', 'description': 'Adjustable dumbbell set for home workouts.', 'price': 1299.00, 'stock_quantity': 10, 'category': 'sports', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1584735935682-2f2b69dff9d2?w=400&h=300&fit=crop'},
                {'name': 'Decorative Wall Mirror', 'description': 'Elegant wall mirror with decorative frame.', 'price': 350.00, 'stock_quantity': 12, 'category': 'furniture', 'seller_id': seller_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1618220179428-22790b461013?w=400&h=300&fit=crop'},
                {'name': 'Portable Bluetooth Speaker', 'description': 'Compact wireless speaker with powerful sound.', 'price': 599.00, 'stock_quantity': 20, 'category': 'electronics', 'seller_id': seller2_id, 'is_approved': True, 'image_url': 'https://images.unsplash.com/photo-1608043152269-423dbba4e7e1?w=400&h=300&fit=crop'},
            ]
            
            for product_data in products_data:
                product = CMP_Product(**product_data)
                db.session.add(product)
            
            db.session.commit()
            logger.info(f"✓ {len(products_data)} sample products created!")
            
            # Reviews data (simplified)
            reviews_data = {
                'Handmade Leather Bag': [(5, "Absolutely stunning bag!", buyer_id), (4, "Beautiful craftsmanship.", buyers[0].id)],
                'Premium Denim Jacket': [(4, "Great quality denim.", buyer_id)],
                'Running Shoes': [(5, "Most comfortable shoes!", buyer_id)],
                'Wireless Noise Cancelling Headphones': [(5, "Best headphones!", buyer_id)],
            }
            
            for product_name, product_reviews in reviews_data.items():
                product = CMP_Product.query.filter_by(name=product_name).first()
                if product:
                    for rating, comment, user_id in product_reviews:
                        review = CMP_Review(
                            product_id=product.id,
                            user_id=user_id,
                            rating=rating,
                            comment=comment,
                            created_at=datetime.utcnow() - timedelta(days=random.randint(1, 30))
                        )
                        db.session.add(review)
            
            db.session.commit()
            
            # Coupons data
            expiry_2028 = datetime(2028, 12, 31, 23, 59, 59)
            now = datetime.utcnow()
            
            coupons_data = [
                {'code': 'WELCOME15', 'name': 'Welcome to Community Market!', 'description': 'Get 15% off your first purchase!', 'discount_type': 'percentage', 'discount_value': 15.00, 'min_order_amount': 100.00, 'max_discount_amount': 500.00, 'is_first_purchase_only': True},
                {'code': 'SAVE50', 'name': 'Save R50', 'description': 'Get R50 off when you spend R300.', 'discount_type': 'fixed', 'discount_value': 50.00, 'min_order_amount': 300.00, 'is_first_purchase_only': False},
                {'code': 'ELECTRO10', 'name': 'Electronics Sale', 'description': '10% off electronics!', 'discount_type': 'percentage', 'discount_value': 10.00, 'min_order_amount': 200.00, 'max_discount_amount': 500.00, 'applicable_category': 'electronics', 'is_first_purchase_only': False},
                {'code': 'FREESHIP', 'name': 'Free Shipping', 'description': 'R100 off orders over R400', 'discount_type': 'fixed', 'discount_value': 100.00, 'min_order_amount': 400.00, 'is_first_purchase_only': False},
            ]
            
            for coupon_data in coupons_data:
                coupon = CMP_Coupon(
                    code=coupon_data['code'],
                    name=coupon_data['name'],
                    description=coupon_data['description'],
                    discount_type=coupon_data['discount_type'],
                    discount_value=coupon_data['discount_value'],
                    min_order_amount=coupon_data.get('min_order_amount', 0),
                    max_discount_amount=coupon_data.get('max_discount_amount'),
                    applicable_category=coupon_data.get('applicable_category'),
                    valid_from=now,
                    valid_to=expiry_2028,
                    per_user_limit=1,
                    is_first_purchase_only=coupon_data['is_first_purchase_only'],
                    is_active=True,
                    created_by=admin.id
                )
                db.session.add(coupon)
            
            db.session.commit()
            
            logger.info("="*50)
            logger.info("DATABASE SEEDING COMPLETED SUCCESSFULLY!")
            logger.info("="*50)
            logger.info("Login Credentials:")
            logger.info("Admin: admin@communitymarket.co.za / Admin@123")
            logger.info("Seller: seller@example.com / Seller@123")
            logger.info("Buyer: buyer@example.com / Buyer@123")
            logger.info("Both: both@example.com / Both@123")
            
        except Exception as e:
            logger.error(f"Seeding error: {str(e)}")
            db.session.rollback()
            raise


# ========== NOTIFICATION HELPER FUNCTIONS ==========
def create_notification(user_id, title, message, notification_type='info', link_url=None):
    try:
        with db_transaction():
            notification = CMP_Notification(
                user_id=user_id,
                title=title,
                message=message,
                notification_type=notification_type,
                link_url=link_url
            )
            db.session.add(notification)
        return notification
    except Exception as e:
        logger.error(f"Failed to create notification: {str(e)}")
        return None


def create_order_notification(order):
    try:
        create_notification(
            user_id=order.buyer_id,
            title=f"Order #{order.order_number}",
            message=f"Your order has been placed successfully. Total: R{order.final_amount:.2f}",
            notification_type='order',
            link_url=url_for('order_detail', order_id=order.id)
        )
    except Exception as e:
        logger.error(f"Failed to create order notifications: {str(e)}")


def create_product_notification(product, is_new=True):
    try:
        if is_new:
            admin = CMP_User.query.filter_by(role='admin').first()
            if admin:
                create_notification(
                    user_id=admin.id,
                    title="New Product Pending Approval",
                    message=f"Product '{product.name}' needs your approval.",
                    notification_type='product',
                    link_url=url_for('admin_pending_products')
                )
        else:
            create_notification(
                user_id=product.seller_id,
                title="Product Approved!",
                message=f"Your product '{product.name}' has been approved!",
                notification_type='success',
                link_url=url_for('product_detail', product_id=product.id)
            )
    except Exception as e:
        logger.error(f"Failed to create product notification: {str(e)}")


def create_status_update_notification(order, old_status, new_status):
    try:
        status_messages = {
            'paid': "Your payment has been confirmed.",
            'shipped': "Great news! Your order has been shipped!",
            'delivered': "Your order has been delivered.",
            'cancelled': "Your order has been cancelled."
        }
        if new_status in status_messages:
            create_notification(
                user_id=order.buyer_id,
                title=f"Order #{order.order_number} - {new_status.upper()}",
                message=status_messages[new_status],
                notification_type='order',
                link_url=url_for('order_detail', order_id=order.id)
            )
    except Exception as e:
        logger.error(f"Failed to create status update notification: {str(e)}")


# ========== ERROR HANDLERS ==========
@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404


@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    logger.error(f"500 Error: {str(error)}\n{traceback.format_exc()}")
    return render_template('errors/500.html'), 500


@app.errorhandler(403)
def forbidden_error(error):
    return render_template('errors/403.html'), 403


@app.errorhandler(Exception)
def handle_exception(e):
    db.session.rollback()
    logger.error(f"Unhandled exception: {str(e)}\n{traceback.format_exc()}")
    if isinstance(e, HTTPException):
        return e
    if request.is_json:
        return jsonify({'error': 'Internal server error'}), 500
    flash('An unexpected error occurred.', 'danger')
    return redirect(url_for('index'))


# ========== USER LOADER ==========
@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(CMP_User, int(user_id))
    except (ValueError, TypeError):
        return None


# ========== CONTEXT PROCESSORS ==========
@app.context_processor
def utility_processor():
    def cart_count():
        if current_user.is_authenticated:
            try:
                return CMP_Cart.query.filter_by(user_id=current_user.id).count()
            except Exception:
                return 0
        return 0
    
    def unread_notifications_count():
        if current_user.is_authenticated:
            try:
                return current_user.unread_notifications_count()
            except Exception:
                return 0
        return 0
    
    def favorites_count():
        if current_user.is_authenticated:
            try:
                return CMP_Favorite.query.filter_by(user_id=current_user.id).count()
            except Exception:
                return 0
        return 0
    
    return dict(
        cart_count=cart_count,
        unread_notifications_count=unread_notifications_count,
        favorites_count=favorites_count,
        stripe_publishable_key=app.config['STRIPE_PUBLISHABLE_KEY']
    )


# ========== AUTHENTICATION ROUTES ==========
@app.route('/')
def index():
    try:
        products = CMP_Product.query.filter_by(
            is_approved=True, 
            is_active=True
        ).order_by(func.random()).limit(3).all()
        return render_template('index.html', products=products)
    except Exception as e:
        logger.error(f"Index route error: {str(e)}")
        return render_template('index.html', products=[])


@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    form = RegistrationForm()
    if form.validate_on_submit():
        try:
            if CMP_User.query.filter_by(email=form.email.data.lower().strip()).first():
                flash('Email already registered.', 'danger')
                return redirect(url_for('register'))
            
            if CMP_User.query.filter_by(phone_number=form.phone_number.data.strip()).first():
                flash('Phone number already registered.', 'danger')
                return redirect(url_for('register'))
            
            user = CMP_User(
                full_name=form.full_name.data.strip(),
                email=form.email.data.lower().strip(),
                phone_number=form.phone_number.data.strip(),
                address=form.address.data.strip(),
                role=form.role.data
            )
            user.set_password(form.password.data)
            
            with db_transaction():
                db.session.add(user)
            
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))
            
        except IntegrityError:
            db.session.rollback()
            flash('Registration failed. Please try again.', 'danger')
        except Exception as e:
            logger.error(f"Registration error: {str(e)}")
            flash('An error occurred during registration. Please try again.', 'danger')
    
    return render_template('register.html', form=form)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    
    form = LoginForm()
    if form.validate_on_submit():
        try:
            user = CMP_User.query.filter_by(email=form.email.data.lower().strip()).first()
            if user and user.check_password(form.password.data):
                if not user.is_active:
                    flash('Your account has been deactivated.', 'danger')
                    return redirect(url_for('login'))
                
                login_user(user, remember=True)
                session.permanent = True
                
                if user.role == 'both' and 'active_mode' not in session:
                    session['active_mode'] = 'buyer'
                
                flash(f'Welcome back, {user.full_name}!', 'success')
                
                next_page = request.args.get('next')
                if next_page and next_page.startswith('/'):
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            
            flash('Invalid email or password.', 'danger')
        except Exception as e:
            logger.error(f"Login error: {str(e)}")
            flash('An error occurred during login. Please try again.', 'danger')
    
    return render_template('login.html', form=form)


@app.route('/logout')
@login_required
def logout():
    session.clear()
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():
    try:
        if current_user.role == 'admin':
            return redirect(url_for('admin_dashboard'))
        
        if current_user.role == 'both':
            active_mode = session.get('active_mode', 'buyer')
            if active_mode == 'seller':
                return redirect(url_for('seller_dashboard'))
            return redirect(url_for('buyer_dashboard'))
        elif current_user.role == 'seller':
            return redirect(url_for('seller_dashboard'))
        else:
            return redirect(url_for('buyer_dashboard'))
    except Exception as e:
        logger.error(f"Dashboard error: {str(e)}")
        flash('Error loading dashboard.', 'danger')
        return redirect(url_for('index'))


@app.route('/upgrade-to-seller', methods=['POST'])
@login_required
def upgrade_to_seller():
    try:
        if current_user.can_sell():
            flash('You already have seller capabilities!', 'info')
            return redirect(url_for('dashboard'))
        
        with db_transaction():
            current_user.upgrade_to_seller()
        
        flash('Congratulations! You can now sell products on Community Market!', 'success')
    except Exception as e:
        logger.error(f"Upgrade error: {str(e)}")
        flash('Failed to upgrade. Please try again.', 'danger')
    
    return redirect(url_for('dashboard'))


@app.route('/switch-mode/<mode>')
@login_required
def switch_mode(mode):
    try:
        if current_user.role == 'both' and mode in ['buyer', 'seller']:
            session['active_mode'] = mode
            flash(f'Switched to {mode.upper()} mode', 'success')
        elif (current_user.role == 'buyer' and mode == 'buyer') or \
             (current_user.role == 'seller' and mode == 'seller'):
            session['active_mode'] = mode
            flash(f'You are in {mode} mode', 'info')
        else:
            flash('You do not have permission for that mode', 'danger')
    except Exception as e:
        logger.error(f"Switch mode error: {str(e)}")
        flash('Error switching mode.', 'danger')
    
    return redirect(url_for('dashboard'))


# ========== FAVORITES ROUTES ==========
@app.route('/api/favorite/toggle/<int:product_id>', methods=['POST'])
@login_required
def toggle_favorite(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        if not product or not product.is_active:
            return jsonify({'success': False, 'message': 'Product not found'}), 404
        
        favorite = CMP_Favorite.query.filter_by(user_id=current_user.id, product_id=product_id).first()
        
        if favorite:
            db.session.delete(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'removed', 'message': 'Removed from favorites'})
        else:
            favorite = CMP_Favorite(user_id=current_user.id, product_id=product_id)
            db.session.add(favorite)
            db.session.commit()
            return jsonify({'success': True, 'action': 'added', 'message': 'Added to favorites'})
    except Exception as e:
        logger.error(f"Toggle favorite error: {str(e)}")
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Server error'}), 500


@app.route('/api/favorites/count')
@login_required
def api_favorites_count():
    try:
        count = CMP_Favorite.query.filter_by(user_id=current_user.id).count()
        return jsonify({'count': count})
    except Exception as e:
        return jsonify({'count': 0}), 200


@app.route('/favorites')
@login_required
def favorites():
    try:
        favorites = CMP_Favorite.query.filter_by(user_id=current_user.id).order_by(CMP_Favorite.created_at.desc()).all()
        return render_template('favorites.html', favorites=favorites)
    except Exception as e:
        flash('Error loading favorites.', 'danger')
        return redirect(url_for('dashboard'))


# ========== COUPON ROUTES ==========
@app.route('/api/coupon/apply', methods=['POST'])
@login_required
def apply_coupon():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'message': 'Invalid request'}), 400
            
        coupon_code = data.get('coupon_code', '').strip().upper()
        cart_total = data.get('cart_total', 0)
        
        if not coupon_code:
            return jsonify({'success': False, 'message': 'Please enter a coupon code'}), 400
        
        coupon = CMP_Coupon.query.filter_by(code=coupon_code, is_active=True).first()
        
        if not coupon:
            return jsonify({'success': False, 'message': 'Invalid coupon code'}), 400
        
        is_valid, message = coupon.is_valid_for_user(current_user, cart_total)
        
        if not is_valid:
            return jsonify({'success': False, 'message': message}), 400
        
        discount = coupon.calculate_discount(cart_total)
        
        session['applied_coupon'] = {
            'id': coupon.id,
            'code': coupon.code,
            'discount': discount,
            'discount_type': coupon.discount_type,
            'discount_value': coupon.discount_value
        }
        
        final_total = cart_total - discount
        
        return jsonify({
            'success': True,
            'message': f'Coupon applied! You saved R{discount:.2f}',
            'discount': discount,
            'final_total': final_total,
            'coupon_code': coupon.code
        })
        
    except Exception as e:
        logger.error(f"Apply coupon error: {str(e)}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@app.route('/api/coupon/remove', methods=['POST'])
@login_required
def remove_coupon():
    try:
        if 'applied_coupon' in session:
            del session['applied_coupon']
        return jsonify({'success': True, 'message': 'Coupon removed'})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Server error'}), 500


@app.route('/coupons')
def coupons_page():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 6
        
        if page < 1:
            page = 1
        
        now = datetime.utcnow()
        
        pagination = CMP_Coupon.query.filter(
            CMP_Coupon.is_active == True,
            CMP_Coupon.valid_from <= now,
            CMP_Coupon.valid_to >= now
        ).order_by(CMP_Coupon.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
        
        coupons = pagination.items
        
        total_pages = pagination.pages
        current_page = pagination.page
        
        if total_pages <= 5:
            start_page = 1
            end_page = total_pages
        else:
            if current_page <= 3:
                start_page = 1
                end_page = 5
            elif current_page >= total_pages - 2:
                start_page = total_pages - 4
                end_page = total_pages
            else:
                start_page = current_page - 2
                end_page = current_page + 2
        
        pages_to_show = list(range(start_page, end_page + 1))
        
        pagination_data = {
            'current_page': current_page,
            'total_pages': total_pages,
            'has_prev': pagination.has_prev,
            'has_next': pagination.has_next,
            'prev_num': pagination.prev_num,
            'next_num': pagination.next_num,
            'pages_to_show': pages_to_show,
            'total': pagination.total,
            'start_item': (current_page - 1) * per_page + 1 if coupons else 0,
            'end_item': min(current_page * per_page, pagination.total)
        }
        
        return render_template('coupons.html', coupons=coupons, pagination=pagination_data)
    except Exception as e:
        flash('Error loading coupons.', 'danger')
        return redirect(url_for('index'))


@app.route('/admin/coupons')
@login_required
@admin_required
def admin_coupons():
    try:
        coupons = CMP_Coupon.query.order_by(CMP_Coupon.created_at.desc()).all()
        now = datetime.utcnow()
        return render_template('dashboard/admin_coupons.html', coupons=coupons, now=now)
    except Exception as e:
        flash('Error loading coupons.', 'danger')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/coupons/create', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_create_coupon():
    form = CouponForm()
    
    if form.validate_on_submit():
        try:
            coupon = CMP_Coupon(
                code=form.code.data.strip().upper(),
                name=form.name.data.strip(),
                description=form.description.data,
                discount_type=form.discount_type.data,
                discount_value=form.discount_value.data,
                min_order_amount=form.min_order_amount.data or 0,
                max_discount_amount=form.max_discount_amount.data,
                applicable_category=form.applicable_category.data or None,
                applicable_seller_id=form.applicable_seller_id.data or None,
                valid_from=form.valid_from.data,
                valid_to=form.valid_to.data,
                usage_limit=form.usage_limit.data,
                per_user_limit=form.per_user_limit.data or 1,
                is_first_purchase_only=form.is_first_purchase_only.data,
                is_active=form.is_active.data,
                created_by=current_user.id
            )
            
            with db_transaction():
                db.session.add(coupon)
            
            flash(f'Coupon {coupon.code} created successfully!', 'success')
            return redirect(url_for('admin_coupons'))
            
        except Exception as e:
            logger.error(f"Create coupon error: {str(e)}")
            flash('Error creating coupon.', 'danger')
    
    return render_template('dashboard/admin_create_coupon.html', form=form)


@app.route('/admin/coupons/<int:coupon_id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_coupon(coupon_id):
    try:
        coupon = db.session.get(CMP_Coupon, coupon_id)
        if not coupon:
            return jsonify({'success': False, 'message': 'Coupon not found'}), 404
        
        with db_transaction():
            coupon.is_active = not coupon.is_active
        
        return jsonify({'success': True, 'active': coupon.is_active})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Server error'}), 500


@app.route('/admin/coupons/<int:coupon_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_coupon(coupon_id):
    try:
        coupon = db.session.get(CMP_Coupon, coupon_id)
        if not coupon:
            return jsonify({'success': False, 'message': 'Coupon not found'}), 404
        
        with db_transaction():
            db.session.delete(coupon)
        
        return jsonify({'success': True, 'message': 'Coupon deleted'})
    except Exception as e:
        return jsonify({'success': False, 'message': 'Server error'}), 500


# ========== ADMIN ROUTES ==========
@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    try:
        total_users = CMP_User.query.count()
        total_sellers = CMP_User.query.filter(CMP_User.role.in_(['seller', 'both'])).count()
        total_buyers = CMP_User.query.filter(CMP_User.role.in_(['buyer', 'both'])).count()
        total_products = CMP_Product.query.count()
        pending_products = CMP_Product.query.filter_by(is_approved=False, is_active=True).count()
        total_orders = CMP_Order.query.count()
        total_revenue = db.session.query(db.func.coalesce(db.func.sum(CMP_Order.final_amount), 0)).scalar()
        total_coupons = CMP_Coupon.query.count()
        active_coupons = CMP_Coupon.query.filter_by(is_active=True).count()
        
        recent_orders = CMP_Order.query.order_by(CMP_Order.created_at.desc()).limit(10).all()
        recent_users = CMP_User.query.order_by(CMP_User.created_at.desc()).limit(10).all()
        
        return render_template('dashboard/admin_dashboard.html',
                             total_users=total_users,
                             total_sellers=total_sellers,
                             total_buyers=total_buyers,
                             total_products=total_products,
                             pending_products=pending_products,
                             total_orders=total_orders,
                             total_revenue=total_revenue or 0,
                             total_coupons=total_coupons,
                             active_coupons=active_coupons,
                             recent_orders=recent_orders,
                             recent_users=recent_users)
    except Exception as e:
        flash('Error loading admin dashboard.', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/admin/users')
@login_required
@admin_required
def admin_users():
    try:
        users = CMP_User.query.order_by(CMP_User.created_at.desc()).all()
        return render_template('dashboard/admin_users.html', users=users)
    except Exception as e:
        flash('Error loading users.', 'danger')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/products/pending')
@login_required
@admin_required
def admin_pending_products():
    try:
        products = CMP_Product.query.filter_by(is_approved=False, is_active=True).all()
        return render_template('dashboard/admin_pending_products.html', products=products)
    except Exception as e:
        flash('Error loading pending products.', 'danger')
        return redirect(url_for('admin_dashboard'))


@app.route('/admin/product/<int:product_id>/approve')
@login_required
@admin_required
def admin_approve_product(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('admin_pending_products'))
        
        with db_transaction():
            product.is_approved = True
        
        create_product_notification(product, is_new=False)
        
        flash(f'Product "{product.name}" has been approved.', 'success')
    except Exception as e:
        flash('Error approving product.', 'danger')
    
    return redirect(url_for('admin_pending_products'))


@app.route('/admin/user/<int:user_id>/toggle')
@login_required
@admin_required
def admin_toggle_user(user_id):
    try:
        if user_id == current_user.id:
            flash('You cannot deactivate yourself.', 'danger')
            return redirect(url_for('admin_users'))
        
        user = db.session.get(CMP_User, user_id)
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('admin_users'))
        
        with db_transaction():
            user.is_active = not user.is_active
        
        if not user.is_active:
            create_notification(
                user_id=user.id,
                title="Account Deactivated",
                message="Your account has been deactivated by an administrator.",
                notification_type='danger'
            )
        
        flash(f'User {user.full_name} has been {"activated" if user.is_active else "deactivated"}.', 'success')
    except Exception as e:
        flash('Error updating user status.', 'danger')
    
    return redirect(url_for('admin_users'))


@app.route('/admin/products')
@login_required
@admin_required
def admin_all_products():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 20
        
        if page < 1:
            page = 1
            
        search = request.args.get('search', '').strip()
        
        query = CMP_Product.query
        
        if search:
            query = query.filter(
                db.or_(
                    CMP_Product.name.ilike(f'%{search}%'),
                    CMP_Product.description.ilike(f'%{search}%')
                )
            )
        
        products = query.order_by(CMP_Product.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
        
        return render_template('dashboard/admin_products.html', products=products, search=search)
    except Exception as e:
        flash('Error loading products.', 'danger')
        return redirect(url_for('admin_dashboard'))


# ========== SELLER ROUTES ==========
@app.route('/seller/dashboard')
@login_required
@seller_required
def seller_dashboard():
    try:
        products = CMP_Product.query.filter_by(seller_id=current_user.id, is_active=True).all()
        
        orders = CMP_Order.query.join(CMP_OrderItem).join(CMP_Product).filter(
            CMP_Product.seller_id == current_user.id
        ).distinct().order_by(CMP_Order.created_at.desc()).all()
        
        total_sales = db.session.query(db.func.sum(CMP_OrderItem.price_at_time * CMP_OrderItem.quantity))\
            .join(CMP_Product)\
            .filter(CMP_Product.seller_id == current_user.id)\
            .scalar() or 0
        
        return render_template('dashboard/seller_dashboard.html',
                             products=products,
                             orders=orders,
                             total_sales=total_sales)
    except Exception as e:
        flash('Error loading seller dashboard.', 'danger')
        return redirect(url_for('dashboard'))


# ========== BUYER ROUTES ==========
@app.route('/buyer/dashboard')
@login_required
@buyer_required
def buyer_dashboard():
    try:
        orders = CMP_Order.query.filter_by(buyer_id=current_user.id)\
            .order_by(CMP_Order.created_at.desc()).all()
        cart_items = CMP_Cart.query.filter_by(user_id=current_user.id).all()
        
        return render_template('dashboard/buyer_dashboard.html',
                             orders=orders,
                             cart_items=cart_items)
    except Exception as e:
        flash('Error loading buyer dashboard.', 'danger')
        return redirect(url_for('dashboard'))


# ========== PRODUCT ROUTES ==========
@app.route('/products')
def products():
    try:
        page = request.args.get('page', 1, type=int)
        per_page = 6
        
        if page < 1:
            page = 1
        
        category = request.args.get('category', '').strip()
        search = request.args.get('search', '').strip()
        
        query = CMP_Product.query.filter_by(is_approved=True, is_active=True)
        
        if category and category != 'all':
            query = query.filter_by(category=category)
        if search:
            query = query.filter(
                db.or_(
                    CMP_Product.name.ilike(f'%{search}%'),
                    CMP_Product.description.ilike(f'%{search}%')
                )
            )
        
        total = query.count()
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        
        if page < 1:
            page = 1
        elif page > total_pages and total_pages > 0:
            page = total_pages
        
        offset = (page - 1) * per_page
        products = query.order_by(CMP_Product.created_at.desc()).offset(offset).limit(per_page).all()
        
        favorites = []
        if current_user.is_authenticated:
            favorites = [f.product_id for f in CMP_Favorite.query.filter_by(user_id=current_user.id).all()]
        
        start_item = offset + 1 if total > 0 else 0
        end_item = min(offset + per_page, total)
        
        pagination = {
            'current_page': page,
            'total_pages': total_pages,
            'total': total,
            'has_prev': page > 1,
            'has_next': page < total_pages,
            'prev_num': page - 1 if page > 1 else None,
            'next_num': page + 1 if page < total_pages else None,
            'start_item': start_item,
            'end_item': end_item
        }
        
        return render_template('products/products.html', products=products, pagination=pagination, favorites=favorites)
    except Exception as e:
        flash('Error loading products.', 'danger')
        return render_template('products/products.html', products=[], pagination={'total': 0, 'total_pages': 1, 'current_page': 1}, favorites=[])


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        if not product or not product.is_active or (not product.is_approved and not current_user.is_authenticated):
            abort(404)
        
        reviews = CMP_Review.query.filter_by(product_id=product_id).order_by(CMP_Review.created_at.desc()).all()
        form = ReviewForm()
        
        avg_rating = db.session.query(db.func.coalesce(db.func.avg(CMP_Review.rating), 0))\
            .filter_by(product_id=product_id).scalar()
        
        can_edit = current_user.is_authenticated and (current_user.id == product.seller_id or current_user.role == 'admin')
        
        is_favorited = False
        if current_user.is_authenticated:
            is_favorited = CMP_Favorite.query.filter_by(user_id=current_user.id, product_id=product_id).first() is not None
        
        return render_template('products/product_detail.html',
                             product=product,
                             reviews=reviews,
                             form=form,
                             avg_rating=float(avg_rating or 0),
                             can_edit=can_edit,
                             is_favorited=is_favorited)
    except Exception as e:
        flash('Error loading product details.', 'danger')
        return redirect(url_for('products'))


# ========== ADD PRODUCT WITH WORKING IMAGE UPLOAD USING IMGBB ==========
@app.route('/product/add', methods=['GET', 'POST'])
@login_required
@seller_required
def add_product():
    form = ProductForm()
    if form.validate_on_submit():
        try:
            image_url = None
            
            # Handle image upload using ImgBB
            if 'product_image' in request.files:
                file = request.files['product_image']
                if file and file.filename:
                    # Check if file is an image
                    allowed_extensions = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
                    file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
                    
                    if file_ext not in allowed_extensions:
                        flash('Invalid file type. Please upload PNG, JPG, JPEG, GIF, or WEBP.', 'danger')
                        return render_template('products/add_product.html', form=form)
                    
                    # Upload to ImgBB
                    image_url = upload_image_to_imgbb(file)
                    
                    if not image_url:
                        flash('Failed to upload image. Please try again.', 'danger')
                        return render_template('products/add_product.html', form=form)
                    
                    flash('Image uploaded successfully!', 'success')
            
            if not image_url:
                flash('Please select an image to upload.', 'danger')
                return render_template('products/add_product.html', form=form)
            
            product = CMP_Product(
                name=form.name.data.strip(),
                description=form.description.data.strip(),
                price=round(form.price.data, 2),
                stock_quantity=form.stock_quantity.data,
                category=form.category.data,
                seller_id=current_user.id,
                image_url=image_url,
                is_approved=False
            )
            
            with db_transaction():
                db.session.add(product)
                db.session.flush()
            
            create_product_notification(product, is_new=True)
            
            flash('Product added successfully! It will be visible after admin approval.', 'success')
            return redirect(url_for('seller_dashboard'))
            
        except Exception as e:
            logger.error(f"Add product error: {str(e)}")
            flash('Error adding product. Please try again.', 'danger')
    
    return render_template('products/add_product.html', form=form)


@app.route('/product/<int:product_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('dashboard'))
        
        if product.seller_id != current_user.id and current_user.role != 'admin':
            flash('You can only edit your own products.', 'danger')
            return redirect(url_for('dashboard'))
        
        form = ProductForm(obj=product)
        if form.validate_on_submit():
            product.name = form.name.data.strip()
            product.description = form.description.data.strip()
            product.price = round(form.price.data, 2)
            product.stock_quantity = form.stock_quantity.data
            product.category = form.category.data
            
            # Handle new image upload
            if 'product_image' in request.files:
                file = request.files['product_image']
                if file and file.filename:
                    image_url = upload_image_to_imgbb(file)
                    if image_url:
                        product.image_url = image_url
                        flash('Product image updated!', 'success')
                    else:
                        flash('Failed to upload new image. Keeping existing image.', 'warning')
            
            with db_transaction():
                pass
            
            flash('Product updated successfully!', 'success')
            return redirect(url_for('product_detail', product_id=product.id))
        
        return render_template('products/edit_product.html', form=form, product=product)
        
    except Exception as e:
        logger.error(f"Edit product error: {str(e)}")
        flash('Error editing product.', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/product/<int:product_id>/delete')
@login_required
def delete_product(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        if not product:
            flash('Product not found.', 'danger')
            return redirect(url_for('dashboard'))
        
        if product.seller_id != current_user.id and current_user.role != 'admin':
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))
        
        with db_transaction():
            product.is_active = False
        
        flash('Product has been removed.', 'success')
        
        if current_user.role == 'admin':
            return redirect(url_for('admin_all_products'))
        elif current_user.can_sell():
            return redirect(url_for('seller_dashboard'))
        return redirect(url_for('dashboard'))
        
    except Exception as e:
        logger.error(f"Delete product error: {str(e)}")
        flash('Error deleting product.', 'danger')
        return redirect(url_for('dashboard'))


# ========== CART ROUTES ==========
@app.route('/api/cart-count')
@login_required
def api_cart_count():
    try:
        count = CMP_Cart.query.filter_by(user_id=current_user.id).count()
        return jsonify({'count': count})
    except Exception as e:
        return jsonify({'count': 0}), 200


@app.route('/cart/add/<int:product_id>')
@login_required
@buyer_required
def add_to_cart(product_id):
    try:
        product = db.session.get(CMP_Product, product_id)
        
        if not product or not product.is_approved or not product.is_active:
            flash('This product is not available.', 'danger')
            return redirect(url_for('products'))
        
        if product.stock_quantity < 1:
            flash('This product is out of stock.', 'danger')
            return redirect(url_for('product_detail', product_id=product_id))
        
        cart_item = CMP_Cart.query.filter_by(user_id=current_user.id, product_id=product_id).first()
        
        with db_transaction():
            if cart_item:
                if cart_item.quantity + 1 <= product.stock_quantity:
                    cart_item.quantity += 1
                else:
                    flash('Not enough stock available.', 'danger')
                    return redirect(url_for('product_detail', product_id=product_id))
            else:
                cart_item = CMP_Cart(user_id=current_user.id, product_id=product_id)
                db.session.add(cart_item)
        
        if 'applied_coupon' in session:
            del session['applied_coupon']
        
        flash(f'{product.name} added to cart!', 'success')
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        db.session.rollback()
        flash('Error adding item to cart.', 'danger')
        return redirect(url_for('product_detail', product_id=product_id))


@app.route('/cart')
@login_required
def view_cart():
    try:
        cart_items = CMP_Cart.query.filter_by(user_id=current_user.id).all()
        subtotal = sum(item.product.price * item.quantity for item in cart_items if item.product)
        discount = session.get('applied_coupon', {}).get('discount', 0) if 'applied_coupon' in session else 0
        coupon_code = session.get('applied_coupon', {}).get('code') if 'applied_coupon' in session else None
        total = subtotal - discount
        
        return render_template('cart/cart.html', cart_items=cart_items, subtotal=subtotal, total=total, discount=discount, coupon_code=coupon_code)
    except Exception as e:
        flash('Error loading cart.', 'danger')
        return render_template('cart/cart.html', cart_items=[], subtotal=0, total=0, discount=0, coupon_code=None)


@app.route('/cart/update/<int:item_id>', methods=['POST'])
@login_required
def update_cart(item_id):
    try:
        cart_item = db.session.get(CMP_Cart, item_id)
        
        if not cart_item or cart_item.user_id != current_user.id:
            flash('Access denied.', 'danger')
            return redirect(url_for('view_cart'))
        
        quantity = request.form.get('quantity', type=int)
        
        with db_transaction():
            if quantity and quantity > 0 and quantity <= cart_item.product.stock_quantity:
                cart_item.quantity = quantity
            elif quantity <= 0:
                db.session.delete(cart_item)
            else:
                flash('Not enough stock available.', 'danger')
        
        if 'applied_coupon' in session:
            del session['applied_coupon']
        
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        db.session.rollback()
        flash('Error updating cart.', 'danger')
        return redirect(url_for('view_cart'))


@app.route('/cart/remove/<int:item_id>')
@login_required
def remove_from_cart(item_id):
    try:
        cart_item = db.session.get(CMP_Cart, item_id)
        
        if cart_item and cart_item.user_id == current_user.id:
            with db_transaction():
                db.session.delete(cart_item)
            
            if 'applied_coupon' in session:
                del session['applied_coupon']
            
            flash('Item removed from cart.', 'success')
        
        return redirect(url_for('view_cart'))
        
    except Exception as e:
        db.session.rollback()
        flash('Error removing item.', 'danger')
        return redirect(url_for('view_cart'))


# ========== CHECKOUT AND ORDER ROUTES ==========
@app.route('/checkout', methods=['GET', 'POST'])
@login_required
@buyer_required
def checkout():
    try:
        cart_items = CMP_Cart.query.filter_by(user_id=current_user.id).all()
        
        if not cart_items:
            flash('Your cart is empty.', 'warning')
            return redirect(url_for('products'))
        
        subtotal = sum(item.product.price * item.quantity for item in cart_items)
        discount = session.get('applied_coupon', {}).get('discount', 0) if 'applied_coupon' in session else 0
        coupon_code = session.get('applied_coupon', {}).get('code') if 'applied_coupon' in session else None
        total = subtotal - discount
        
        if request.method == 'GET':
            return render_template('orders/checkout.html', subtotal=subtotal, total=total, discount=discount, coupon_code=coupon_code)
        
        if not current_user.address:
            flash('Please update your shipping address before checkout.', 'warning')
            return redirect(url_for('profile'))
        
        order_number = f"CMP-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        
        with db_transaction():
            order = CMP_Order(
                order_number=order_number,
                buyer_id=current_user.id,
                total_amount=subtotal,
                discount_amount=discount,
                final_amount=total,
                coupon_code=coupon_code,
                shipping_address=current_user.address,
                status='paid'  # Auto-mark as paid for demo
            )
            db.session.add(order)
            db.session.flush()
            
            for cart_item in cart_items:
                order_item = CMP_OrderItem(
                    order_id=order.id,
                    product_id=cart_item.product_id,
                    quantity=cart_item.quantity,
                    price_at_time=cart_item.product.price
                )
                db.session.add(order_item)
            
            # Update stock
            for cart_item in cart_items:
                product = db.session.get(CMP_Product, cart_item.product_id)
                if product:
                    product.stock_quantity -= cart_item.quantity
            
            # Clear cart
            CMP_Cart.query.filter_by(user_id=current_user.id).delete()
            
            if not current_user.first_purchase_made:
                current_user.first_purchase_made = True
        
        create_order_notification(order)
        
        flash('Order placed successfully!', 'success')
        return redirect(url_for('order_detail', order_id=order.id))
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Checkout error: {str(e)}")
        flash('An error occurred during checkout. Please try again.', 'danger')
        return redirect(url_for('view_cart'))


@app.route('/order/success/<int:order_id>')
@login_required
def order_success(order_id):
    return redirect(url_for('order_detail', order_id=order_id))


@app.route('/order/cancel')
@login_required
def order_cancel():
    flash('Payment was cancelled.', 'warning')
    return redirect(url_for('view_cart'))


@app.route('/order/<int:order_id>')
@login_required
def order_detail(order_id):
    try:
        order = db.session.get(CMP_Order, order_id)
        if not order or (order.buyer_id != current_user.id and current_user.role != 'admin'):
            abort(404)
        return render_template('orders/order_detail.html', order=order)
    except Exception as e:
        flash('Error loading order details.', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/order/<int:order_id>/status', methods=['POST'])
@login_required
@admin_required
def update_order_status(order_id):
    try:
        order = db.session.get(CMP_Order, order_id)
        if not order:
            flash('Order not found.', 'danger')
            return redirect(url_for('admin_dashboard'))
        
        new_status = request.form.get('status')
        valid_statuses = ['pending', 'paid', 'shipped', 'delivered', 'cancelled']
        
        if new_status in valid_statuses:
            old_status = order.status
            order.status = new_status
            db.session.commit()
            if old_status != new_status:
                create_status_update_notification(order, old_status, new_status)
            flash('Order status updated.', 'success')
        else:
            flash('Invalid status.', 'danger')
        
        return redirect(url_for('order_detail', order_id=order.id))
    except Exception as e:
        db.session.rollback()
        flash('Error updating order status.', 'danger')
        return redirect(url_for('admin_dashboard'))


# ========== REVIEW ROUTES ==========
@app.route('/product/<int:product_id>/review', methods=['POST'])
@login_required
def add_review(product_id):
    form = ReviewForm()
    
    if form.validate_on_submit():
        try:
            existing_review = CMP_Review.query.filter_by(
                product_id=product_id,
                user_id=current_user.id
            ).first()
            
            with db_transaction():
                if existing_review:
                    existing_review.rating = form.rating.data
                    existing_review.comment = form.comment.data
                else:
                    review = CMP_Review(
                        product_id=product_id,
                        user_id=current_user.id,
                        rating=form.rating.data,
                        comment=form.comment.data
                    )
                    db.session.add(review)
            
            flash('Review added successfully!', 'success')
            
        except Exception as e:
            db.session.rollback()
            flash('Error adding review.', 'danger')
    
    return redirect(url_for('product_detail', product_id=product_id))


# ========== PROFILE ROUTES ==========
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    
    if form.validate_on_submit():
        try:
            with db_transaction():
                current_user.full_name = form.full_name.data.strip()
                current_user.phone_number = form.phone_number.data.strip()
                current_user.address = form.address.data.strip()
            
            flash('Profile updated successfully!', 'success')
            return redirect(url_for('profile'))
            
        except Exception as e:
            db.session.rollback()
            flash('Error updating profile.', 'danger')
    
    return render_template('profile.html', form=form)


# ========== NOTIFICATION ROUTES ==========
@app.route('/api/notifications')
@login_required
def api_get_notifications():
    try:
        notifications = CMP_Notification.query.filter_by(user_id=current_user.id)\
            .order_by(CMP_Notification.created_at.desc()).limit(50).all()
        total_unread = current_user.unread_notifications_count()
        return jsonify({'success': True, 'notifications': [n.to_dict() for n in notifications], 'total_unread': total_unread})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/notifications/mark-read', methods=['POST'])
@login_required
def api_mark_notifications_read():
    try:
        data = request.get_json() or {}
        notification_id = data.get('notification_id')
        
        with db_transaction():
            if notification_id:
                notification = db.session.get(CMP_Notification, notification_id)
                if notification and notification.user_id == current_user.id:
                    notification.is_read = True
            else:
                CMP_Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
        
        total_unread = current_user.unread_notifications_count()
        return jsonify({'success': True, 'total_unread': total_unread})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/notifications/unread-count')
@login_required
def api_unread_count():
    try:
        return jsonify({'success': True, 'count': current_user.unread_notifications_count()})
    except Exception as e:
        return jsonify({'success': False, 'count': 0}), 200


@app.route('/notifications')
@login_required
def notifications_page():
    return render_template('notifications.html')


@app.route('/api/notifications/clear-all', methods=['DELETE'])
@login_required
def api_clear_all_notifications():
    try:
        CMP_Notification.query.filter_by(user_id=current_user.id).delete()
        db.session.commit()
        return jsonify({'success': True, 'message': 'All notifications cleared'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# ========== WEBHOOK ==========
@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    return 'Success', 200


# ========== HEALTH CHECK ==========
@app.route('/health')
def health_check():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({'status': 'healthy', 'database': 'connected'}), 200
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


# ========== APPLICATION ENTRY POINT ==========
# THIS MUST RUN FOR RENDER/GUNICORN
with app.app_context():
    try:
        db.create_all()
        logger.info("✅ Database tables created/verified")
        seed_database_if_empty()
    except Exception as e:
        logger.error(f"❌ Database initialization error: {str(e)}")
        logger.error(traceback.format_exc())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)