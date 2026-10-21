"""url_for() endpoint-name guards. Update names per blueprint in the SAME commit
as the Stage F/G move (login -> auth.login, etc.)."""
import pytest
from flask import url_for

AUTH_ENDPOINTS = ['login', 'logout', 'register', 'forgot_password', 'google_login']
MAIN_ENDPOINTS = ['index', 'health', 'dashboard', 'faq', 'privacy', 'terms', 'leaderboard',
                  'bundles_list', 'cart_view', 'get_items', 'api_leaderboard_top3',
                  'service_worker', 'sitemap_xml', 'robots_txt']
ADMIN_ENDPOINTS = ['admin_dashboard', 'admin_bookings', 'admin_items', 'admin_users',
                   'admin_payment_queue', 'admin_payments', 'admin_payment_config',
                   'admin_tickets', 'admin_storage', 'admin_data_management',
                   'admin_verification_requests', 'admin_announcements', 'admin_kyc_audit_log']
VENDOR_ENDPOINTS = ['vendor_dashboard', 'vendor_earnings', 'vendor_calendar', 'vendor_bundles',
                    'vendor_sales', 'vendor_rentals', 'vendor_add_item']
ALL_ENDPOINTS = AUTH_ENDPOINTS + MAIN_ENDPOINTS + ADMIN_ENDPOINTS + VENDOR_ENDPOINTS


@pytest.mark.parametrize('endpoint', ALL_ENDPOINTS)
def test_endpoint_resolves(app, endpoint):
    assert any(r.endpoint == endpoint for r in app.url_map.iter_rules()), \
        f'endpoint {endpoint!r} missing from url_map'


def test_login_view_endpoint_resolves(app):
    # v1 imported `extensions`, which doesn't exist until Stage C. Import from app
    # (it must keep re-exporting login_manager).
    from app import login_manager
    with app.test_request_context():
        url_for(login_manager.login_view)


def test_protected_page_redirects_to_login(client):
    rv = client.get('/dashboard', follow_redirects=False)
    assert rv.status_code == 302
    assert '/login' in rv.headers.get('Location', '')
