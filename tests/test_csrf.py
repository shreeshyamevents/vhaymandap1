"""CSRF enforcement guard (Phase 1.1). Uses the shared isolated-DB `app` fixture."""


def test_post_without_csrf_token_rejected(app, client):
    app.config['WTF_CSRF_ENABLED'] = True   # `app` fixture resets this to False next test
    rv = client.post('/register', data={
        'name': 'X', 'mobile': '7770001111', 'email': 'x@x.com',
        'password': 'Xxxx1234', 'confirm_password': 'Xxxx1234',
        'account_type': 'customer',
    })
    assert rv.status_code == 400
