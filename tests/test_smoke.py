"""Page-load smoke tests. A 500 here means a missed url_for() rename or a broken route."""
import pytest

PUBLIC_PAGES = ['/', '/login', '/register', '/forgot-password', '/faq', '/privacy',
                '/terms', '/leaderboard', '/bundles']
# '/my-bookings' removed: no such route in app.py (customer bookings live elsewhere).
AUTH_PAGES = ['/dashboard', '/profile', '/my-data', '/cart', '/wishlist',
              '/recently-viewed', '/notifications', '/chat', '/tickets', '/tickets/new']
VENDOR_PAGES = ['/vendor', '/vendor/earnings', '/vendor/calendar', '/vendor/bundles',
                '/vendor/sales', '/vendor/rentals', '/vendor/item/add']
ADMIN_PAGES = ['/admin', '/admin/bookings', '/admin/items', '/admin/users',
               '/admin/payment-proofs', '/admin/payments', '/admin/payment-config',
               '/admin/tickets', '/admin/storage', '/admin/data-management',
               '/admin/verification-requests', '/admin/announcements', '/admin/kyc-audit-log']


@pytest.mark.parametrize('url', PUBLIC_PAGES)
def test_public_pages_load(client, url):
    assert client.get(url).status_code in (200, 302), url


@pytest.mark.parametrize('url', AUTH_PAGES)
def test_auth_pages_load(client, logged_in_customer, url):
    assert client.get(url).status_code in (200, 302), url


@pytest.mark.parametrize('url', VENDOR_PAGES)
def test_vendor_pages_load(client, logged_in_vendor, url):
    assert client.get(url).status_code in (200, 302), url


@pytest.mark.parametrize('url', ADMIN_PAGES)
def test_admin_pages_load(client, logged_in_admin, url):
    assert client.get(url).status_code in (200, 302), url


def test_homepage_content(client):
    rv = client.get('/')
    assert rv.status_code == 200 and b'VyahMandap' in rv.data


def test_health_endpoint(client):
    rv = client.get('/health')
    assert rv.status_code == 200 and rv.data.strip() == b'ok'


def test_404_for_unknown(client):
    assert client.get('/this-does-not-exist-xyz-12345').status_code == 404


def test_sitemap_xml(client):
    rv = client.get('/sitemap.xml')
    assert rv.status_code == 200 and b'<urlset' in rv.data


def test_robots_txt(client):
    rv = client.get('/robots.txt')
    assert rv.status_code == 200 and b'User-agent' in rv.data


def test_service_worker(client):
    assert client.get('/sw.js').status_code == 200


def test_api_items_flat(client, item):
    rv = client.get('/api/items')
    assert rv.status_code == 200 and isinstance(rv.get_json(), list)


def test_api_items_paginated(client, item):
    rv = client.get('/api/items?page=1')
    assert rv.status_code == 200
    data = rv.get_json()
    assert isinstance(data, dict) and 'items' in data and 'pages' in data


def test_item_detail_page(client, item):
    rv = client.get(f'/item/{item.id}')
    assert rv.status_code == 200 and b'Test Sofa' in rv.data


def test_bundle_detail_page(client, bundle):
    assert client.get(f'/bundle/{bundle.id}').status_code == 200


def test_vendor_public_profile(client, vendor):
    assert client.get(f'/vendor/{vendor.id}/public').status_code == 200
