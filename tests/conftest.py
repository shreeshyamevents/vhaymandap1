"""Phase 4 — pytest fixtures (v2, verified against app.py).

Fixes vs v1:
- app.py binds `db = SQLAlchemy(app)` at import, so changing
  SQLALCHEMY_DATABASE_URI afterwards does nothing. v1 would have run
  create_all()/drop_all() against your real dev DB. v2 swaps in a per-test
  temp engine and ABORTS if the swap didn't take.
- Rate limiter disabled (/login 10/min, /register 5/min would trip across tests).
- Network/mail side effects stubbed (_is_password_breached, _send_verification_email).

Tests import from `app`, so through Stages C-H app.py must keep re-exporting
db, User, Item, Bundle, BundleItem, limiter, login_manager at module scope.
After Stage E, update the monkeypatch targets below (functions move to helpers/services).
"""
import os
import pytest

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-prod-0123456789abcdef')
os.environ.setdefault('ADMIN_PASSWORD', 'TestAdminPass123!')
os.environ.setdefault('SITE_URL', 'http://localhost:5000')
os.environ.pop('RENDER', None)
os.environ.pop('REDIS_URL', None)
os.environ.pop('CLOUDINARY_URL', None)

# NOTE: importing app runs init_database() once against your normal local DB
# (same as `python app.py`). Tests themselves never touch it — see `app` fixture.
from app import app as flask_app, db as _db, limiter as _limiter
from app import User, Item, Bundle, BundleItem
from sqlalchemy import create_engine


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Fresh app bound to an isolated SQLite file per test."""
    db_path = tmp_path / 'test.db'
    engine = create_engine(f'sqlite:///{db_path}')

    flask_app.config.update({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'MAIL_USERNAME': '', 'MAIL_PASSWORD': '',
        'SESSION_COOKIE_SECURE': False,
    })
    _limiter.enabled = False
    monkeypatch.setattr('app._is_password_breached', lambda *a, **k: False)
    monkeypatch.setattr('app._send_verification_email', lambda *a, **k: None)

    with flask_app.app_context():
        # Flask-SQLAlchemy 3.x keeps engines here; replace so nothing hits the dev DB.
        try:
            _db._app_engines[flask_app] = {None: engine}
        except AttributeError:
            pytest.fail('Cannot redirect DB engine (unexpected Flask-SQLAlchemy version). '
                        'Aborting to protect your dev database.')
        assert str(_db.engine.url).endswith('test.db'), 'DB swap failed — refusing to run'
        _db.create_all()
        yield flask_app
        _db.session.remove()
        _db.drop_all()
        engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def db(app):
    return _db


def _mk(db, **kw):
    pw = kw.pop('password')
    u = User(is_active=True, is_email_verified=True, **kw)
    u.set_password(pw)
    db.session.add(u); db.session.commit()
    return u


@pytest.fixture
def admin(app, db):
    return _mk(db, name='TestAdmin', mobile='8000000001', email='admin@test.local',
               role='admin', public_id='VM-A-00001', password='AdminPass123!')


@pytest.fixture
def vendor(app, db):
    return _mk(db, name='TestVendor', mobile='8000000002', email='vendor@test.local',
               role='vendor', public_id='VM-V-00001', company_name='Test Vendor Co',
               password='VendorPass123!')


@pytest.fixture
def customer(app, db):
    return _mk(db, name='TestCustomer', mobile='8000000003', email='cust@test.local',
               role='customer', public_id='VM-C-00001', password='CustPass123!')


@pytest.fixture
def transporter(app, db):
    return _mk(db, name='TestTransporter', mobile='8000000004', email='trans@test.local',
               role='transporter', public_id='VM-T-00001',
               transporter_kyc_status='verified', transporter_available=True,
               password='TransPass123!')


@pytest.fixture
def item(app, db, vendor):
    it = Item(title='Test Sofa', description='A test sofa', category='furniture',
              rate_per_day=1000, stock=5, is_available=True, vendor_id=vendor.id)
    db.session.add(it); db.session.commit()
    return it


@pytest.fixture
def bundle(app, db, vendor, item):
    b = Bundle(vendor_id=vendor.id, name='Test Bundle', tag='wedding',
               description='Test bundle', pricing_type='discount',
               discount_percent=10, is_active=True)
    db.session.add(b); db.session.flush()
    db.session.add(BundleItem(bundle_id=b.id, item_id=item.id, quantity=1))
    db.session.commit()
    return b


def _login_as(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['_fresh'] = True


@pytest.fixture
def logged_in_customer(client, customer):
    _login_as(client, customer.id); return customer


@pytest.fixture
def logged_in_vendor(client, vendor):
    _login_as(client, vendor.id); return vendor


@pytest.fixture
def logged_in_admin(client, admin):
    _login_as(client, admin.id); return admin


@pytest.fixture
def logged_in_transporter(client, transporter):
    _login_as(client, transporter.id); return transporter
