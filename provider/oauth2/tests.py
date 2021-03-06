# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import json
import datetime
from mock import patch

try:
    import urlparse
except ImportError:
    from urllib import parse as urlparse

from django.conf import settings
from django.core.urlresolvers import reverse
from django.http import QueryDict
from django.test import TestCase
from django.utils.html import escape

from .. import constants, scope
from ..compat import skipIfCustomUser, get_user_model
from ..templatetags.scope import scopes
from ..views import OAuthError
from ..utils import now as date_now
from .forms import ClientForm
from .models import Client, Grant, AccessToken, RefreshToken
from .backends import BasicClientBackend, RequestParamsClientBackend, AccessTokenBackend


@skipIfCustomUser
class BaseOAuth2TestCase(TestCase):
    def login(self):
        self.client.login(username='test-user-1', password='test')

    def auth_url(self):
        return reverse('oauth2:capture')

    def auth_url2(self):
        return reverse('oauth2:authorize')

    def redirect_url(self):
        return reverse('oauth2:redirect')

    def access_token_url(self):
        return reverse('oauth2:access_token')

    def get_client(self, id=2):
        return Client.objects.get(id=id)

    def get_grant(self):
        return Grant.objects.all()[0]

    def get_user(self):
        return get_user_model().objects.get(id=1)

    def get_password(self):
        return 'test'

    def _login_and_authorize(self, url_func=None):
        if url_func is None:
            url_func = lambda: self.auth_url() + '?client_id={}&response_type=code&state=abc'.format(self.get_client().client_id)

        response = self.client.get(url_func())
        response = self.client.get(self.auth_url2())

        response = self.client.post(self.auth_url2(), {'authorize': True, 'scope': constants.SCOPES[0][1]})
        self.assertEqual(302, response.status_code, response.content)
        self.assertTrue(self.redirect_url() in response['Location'])


class AuthorizationTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2']

    def setUp(self):
        self._old_login = settings.LOGIN_URL
        settings.LOGIN_URL = '/login/'

    def tearDown(self):
        settings.LOGIN_URL = self._old_login

    def test_authorization_requires_login(self):
        response = self.client.get(self.auth_url())

        # Login redirect
        self.assertEqual(302, response.status_code)
        self.assertEqual('/login/', urlparse.urlparse(response['Location']).path)

        self.login()

        response = self.client.get(self.auth_url())

        self.assertEqual(302, response.status_code)

        self.assertTrue(self.auth_url2() in response['Location'])

    def test_authorization_requires_client_id(self):
        self.login()
        response = self.client.get(self.auth_url())
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue("An unauthorized client tried to access your resources." in response.content)

    def test_authorization_rejects_invalid_client_id(self):
        self.login()
        response = self.client.get(self.auth_url() + '?client_id=123')
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue("An unauthorized client tried to access your resources." in response.content)

    def test_authorization_requires_response_type(self):
        self.login()
        response = self.client.get(self.auth_url() + '?client_id={}'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue(escape("No 'response_type' supplied.") in response.content)

    def test_authorization_requires_supported_response_type(self):
        self.login()
        response = self.client.get(self.auth_url() + '?client_id={}&response_type=unsupported'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue(escape("'unsupported' is not a supported response type.") in response.content)

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())
        self.assertEqual(200, response.status_code, response.content)

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=token'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())
        self.assertEqual(200, response.status_code)

    def test_authorization_requires_a_valid_redirect_uri(self):
        self.login()

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
            self.get_client().client_id,
            self.get_client().redirect_uri + '-invalid'))
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue(escape("The requested redirect didn't match the client settings.") in response.content)

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
            self.get_client().client_id,
            self.get_client().redirect_uri))
        response = self.client.get(self.auth_url2())

        self.assertEqual(200, response.status_code)

    def test_authorization_multi_redirect_uri(self):
        self.login()

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
            self.get_client(3).client_id,
            self.get_client(3).redirect_uri.split(" ")[0]))
        response = self.client.get(self.auth_url2())

        self.assertEqual(200, response.status_code)

    def test_authorization_requires_a_valid_scope(self):
        self.login()

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&scope=invalid+invalid2'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertTrue(escape("'invalid' is not a valid scope.") in response.content)

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&scope={}'.format(
            self.get_client().client_id,
            constants.SCOPES[0][1]))
        response = self.client.get(self.auth_url2())
        self.assertEqual(200, response.status_code)

    def test_authorization_is_not_granted(self):
        self.login()

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code'.format(self.get_client().client_id))
        response = self.client.get(self.auth_url2())

        response = self.client.post(self.auth_url2(), {'authorize': False, 'scope': constants.SCOPES[0][1]})
        self.assertEqual(302, response.status_code, response.content)
        self.assertTrue(self.redirect_url() in response['Location'])

        response = self.client.get(self.redirect_url())

        self.assertEqual(302, response.status_code)
        self.assertTrue('error=access_denied' in response['Location'])
        self.assertFalse('code' in response['Location'])

    def test_authorization_is_granted(self):
        self.login()

        self._login_and_authorize()

        response = self.client.get(self.redirect_url())

        self.assertEqual(302, response.status_code)
        self.assertFalse('error' in response['Location'])
        self.assertTrue('code' in response['Location'])

    def test_preserving_the_state_variable(self):
        self.login()

        self._login_and_authorize()

        response = self.client.get(self.redirect_url())

        self.assertEqual(302, response.status_code)
        self.assertFalse('error' in response['Location'])
        self.assertTrue('code' in response['Location'])
        self.assertTrue('state=abc' in response['Location'])

    def test_redirect_requires_valid_data(self):
        self.login()
        response = self.client.get(self.redirect_url())
        self.assertEqual(400, response.status_code)


class ValidationAndExceptionTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2.json']

    def raise_unknown_exception(self, request, data):
        raise OAuthError({'unknown_field':'had some errors'})

    def test_validate_uri__bad_uri(self):
        self.login()

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
            self.get_client().client_id,
            'blurb'))
        response = self.client.get(self.auth_url2())

        self.assertEqual(400, response.status_code)
        self.assertIn(escape("Enter a valid URL."), response.content, response.content)

        response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
            self.get_client().client_id,
            self.get_client().redirect_uri))
        response = self.client.get(self.auth_url2())

        self.assertEqual(200, response.status_code)

    def test_unknown_exception(self):
        with patch('provider.views.Authorize._validate_client', self.raise_unknown_exception):
            self.login()

            response = self.client.get(self.auth_url() + '?client_id={}&response_type=code&redirect_uri={}'.format(
                self.get_client().client_id,
                self.get_client().redirect_uri))
            response = self.client.get(self.auth_url2())
            self.assertEqual(400, response.status_code, response.content)


class AccessTokenTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2.json']

    def test_access_token_get_expire_delta_value(self):
        user = self.get_user()
        client = self.get_client()
        token = AccessToken.objects.create(user=user, client=client)
        now = date_now()
        default_expiration_timedelta = constants.EXPIRE_DELTA
        current_expiration_timedelta = datetime.timedelta(seconds=token.get_expire_delta(reference=now))
        self.assertTrue(abs(current_expiration_timedelta - default_expiration_timedelta) <= datetime.timedelta(seconds=1))

    def test_fetching_access_token_with_invalid_client(self):
        self.login()
        self._login_and_authorize()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id + '123',
            'client_secret': self.get_client().client_secret, })

        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual('invalid_client', json.loads(response.content)['error'])

    def test_fetching_access_token_with_invalid_grant(self):
        self.login()
        self._login_and_authorize()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': '123'})

        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'])

    def _login_authorize_get_token(self):
        required_props = ['access_token', 'token_type']

        self.login()
        self._login_and_authorize()

        response = self.client.get(self.redirect_url())
        query = QueryDict(urlparse.urlparse(response['Location']).query)
        code = query['code']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': code})

        self.assertEqual(200, response.status_code, response.content)

        token = json.loads(response.content)

        for prop in required_props:
            self.assertIn(prop, token, "Access token response missing "
                    "required property: {}".format(prop))

        return token

    def test_fetching_access_token_with_valid_grant(self):
        self._login_authorize_get_token()

    def test_fetching_access_token_with_invalid_grant_type(self):
        self.login()
        self._login_and_authorize()
        response = self.client.get(self.redirect_url())

        query = QueryDict(urlparse.urlparse(response['Location']).query)
        code = query['code']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'invalid_grant_type',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': code
        })

        self.assertEqual(400, response.status_code)
        self.assertEqual('unsupported_grant_type', json.loads(response.content)['error'],
            response.content)

    def test_fetching_single_access_token(self):
        constants.SINGLE_ACCESS_TOKEN = True

        result1 = self._login_authorize_get_token()
        result2 = self._login_authorize_get_token()

        self.assertEqual(result1['access_token'], result2['access_token'])

        constants.SINGLE_ACCESS_TOKEN = False

    def test_fetching_single_access_token_after_refresh(self):
        constants.SINGLE_ACCESS_TOKEN = True

        token = self._login_authorize_get_token()

        self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        new_token = self._login_authorize_get_token()
        self.assertNotEqual(token['access_token'], new_token['access_token'])

        constants.SINGLE_ACCESS_TOKEN = False

    def test_fetching_access_token_multiple_times(self):
        self._login_authorize_get_token()
        code = self.get_grant().code

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': code})

        self.assertEqual(400, response.status_code)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'])

    def test_escalating_the_scope(self):
        self.login()
        self._login_and_authorize()
        code = self.get_grant().code

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': code,
            'scope': 'read write'})

        self.assertEqual(400, response.status_code)
        self.assertEqual('invalid_scope', json.loads(response.content)['error'])

    def test_refreshing_an_access_token(self):
        token = self._login_authorize_get_token()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(400, response.status_code)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'],
            response.content)

    def test_password_grant_public(self):
        c = self.get_client()
        c.client_type = constants.PUBLIC
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            # No secret needed
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertNotIn('refresh_token', json.loads(response.content))
        expires_in = json.loads(response.content)['expires_in']
        expires_in_days = round(expires_in / (60.0 * 60.0 * 24.0))
        self.assertEqual(expires_in_days, constants.EXPIRE_DELTA_PUBLIC.days)

    def test_password_grant_confidential(self):
        c = self.get_client()
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertTrue(json.loads(response.content)['refresh_token'])

    def test_email_and_password_grant_confidential(self):
        c = self.get_client()
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'email_and_password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'email': self.get_user().email,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertTrue(json.loads(response.content)['refresh_token'])

    def test_password_grant_confidential_no_secret(self):
        c = self.get_client()
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual('invalid_client', json.loads(response.content)['error'])

    def test_password_grant_invalid_password_public(self):
        c = self.get_client()
        c.client_type = constants.PUBLIC
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'username': self.get_user().username,
            'password': self.get_password() + 'invalid',
        })

        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual('invalid_client', json.loads(response.content)['error'])

    def test_password_grant_invalid_password_confidential(self):
        c = self.get_client()
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'username': self.get_user().username,
            'password': self.get_password() + 'invalid',
        })

        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'])

    def test_client_credentials_grant__public(self):
        """
        Public clients should not be able to get client credentials
        access as they can't use a client secret and client ids are
        public knowledge.
        """
        c = self.get_client()
        c.client_type = constants.PUBLIC
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'client_credentials',
            'client_id': c.client_id
            #no secrets for public clients
        })
        self.assertEqual(400, response.status_code, response.content)
        self.assertEqual('invalid_client', json.loads(response.content)['error'])

    def test_client_credentials_grant__confidential(self):
        c = self.get_client()
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'client_credentials',
            'client_id': c.client_id,
            'client_secret': c.client_secret
        })
        data = json.loads(response.content)
        self.assertEqual(200, response.status_code, response.content)
        self.assertIn('access_token', data, response.content)
        self.assertIn('expires_in', data, response.content)
        self.assertIn('scope', data, response.content)
        self.assertEqual('Bearer', data['token_type'], response.content)
        # No refresh token should be made for client_credentials grants
        self.assertEqual(0, RefreshToken.objects.filter(access_token__token=data['access_token']).count())

    def test_client_credentials_grant__no_user(self):
        c = self.get_client(id=4) # client.user = None
        c.client_type = constants.CONFIDENTIAL
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'client_credentials',
            'client_id': c.client_id,
            'client_secret': c.client_secret
        })
        data = json.loads(response.content)
        self.assertEqual(200, response.status_code, response.content)
        self.assertIn('access_token', data, response.content)
        self.assertIn('expires_in', data, response.content)
        self.assertIn('scope', data, response.content)
        self.assertEqual('Bearer', data['token_type'], response.content)
        # No refresh token should be made for client_credentials grants
        self.assertEqual(0, RefreshToken.objects.filter(access_token__token=data['access_token']).count())

    def test_limit_number_of_refresh_token_grant(self):
        constants.LIMIT_NUM_REFRESH_TOKEN = 2

        token = self._login_authorize_get_token()
        refresh_token_1 = token['refresh_token']

        token = self._login_authorize_get_token()
        refresh_token_2 = token['refresh_token']

        token = self._login_authorize_get_token()
        refresh_token_3 = token['refresh_token']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_1,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(400, response.status_code)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'],
            response.content)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_2,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_3,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        constants.LIMIT_NUM_REFRESH_TOKEN = 0

    def test_limit_number_of_refresh_token_password(self):
        constants.LIMIT_NUM_REFRESH_TOKEN = 2

        c = self.get_client()
        c.client_type = 0 # confidential
        c.save()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertTrue(json.loads(response.content)['refresh_token'])
        refresh_token_1 = json.loads(response.content)['refresh_token']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertTrue(json.loads(response.content)['refresh_token'])
        refresh_token_2 = json.loads(response.content)['refresh_token']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'password',
            'client_id': c.client_id,
            'client_secret': c.client_secret,
            'username': self.get_user().username,
            'password': self.get_password(),
        })

        self.assertEqual(200, response.status_code, response.content)
        self.assertTrue(json.loads(response.content)['refresh_token'])
        refresh_token_3 = json.loads(response.content)['refresh_token']

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_1,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(400, response.status_code)
        self.assertEqual('invalid_grant', json.loads(response.content)['error'],
            response.content)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_2,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token_3,
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        constants.LIMIT_NUM_REFRESH_TOKEN = 0

    def test_keeping_refresh_token(self):
        constants.KEEP_REFRESH_TOKEN = True

        token = self._login_authorize_get_token()

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })

        self.assertEqual(200, response.status_code)

        constants.KEEP_REFRESH_TOKEN = False


class AuthBackendTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2']

    def test_basic_client_backend(self):
        request = type('Request', (object,), {'META': {}})()
        request.META['HTTP_AUTHORIZATION'] = "Basic " + "{0}:{1}".format(
            self.get_client().client_id,
            self.get_client().client_secret).encode('base64')

        self.assertEqual(BasicClientBackend().authenticate(request).id,
                         2, "Didn't return the right client.")

    def test_request_params_client_backend(self):
        request = type('Request', (object,), {'REQUEST': {}})()

        request.REQUEST['client_id'] = self.get_client().client_id
        request.REQUEST['client_secret'] = self.get_client().client_secret

        self.assertEqual(RequestParamsClientBackend().authenticate(request).id,
                         2, "Didn't return the right client.'")

    def test_access_token_backend(self):
        user = self.get_user()
        client = self.get_client()
        backend = AccessTokenBackend()
        token = AccessToken.objects.create(user=user, client=client)
        authenticated = backend.authenticate(access_token=token.token,
                client=client)

        self.assertIsNotNone(authenticated)


class EnforceSecureTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2']

    def setUp(self):
        constants.ENFORCE_SECURE = True

    def tearDown(self):
        constants.ENFORCE_SECURE = False

    def test_authorization_enforces_SSL(self):
        self.login()

        response = self.client.get(self.auth_url())

        self.assertEqual(400, response.status_code)
        self.assertTrue("A secure connection is required." in response.content)

    def test_access_token_enforces_SSL(self):
        response = self.client.post(self.access_token_url(), {})

        self.assertEqual(400, response.status_code)
        self.assertTrue("A secure connection is required." in response.content)


class ClientFormTest(TestCase):
    def test_client_form(self):
        form = ClientForm({'name': 'TestName', 'url': 'http://127.0.0.1:8000',
            'redirect_uri': 'http://localhost:8000/'})

        self.assertFalse(form.is_valid())

        form = ClientForm({
            'name': 'TestName',
            'url': 'http://127.0.0.1:8000',
            'redirect_uri': 'http://localhost:8000/',
            'client_type': constants.CLIENT_TYPES[0][0]})
        self.assertTrue(form.is_valid())
        form.save()


class ScopeTest(TestCase):
    def setUp(self):
        self._scopes = constants.SCOPES
        constants.SCOPES = constants.DEFAULT_SCOPES

    def tearDown(self):
        constants.SCOPES = self._scopes

    def test_get_scope_names(self):
        names = scope.to_names(constants.READ)
        self.assertEqual('read', ' '.join(names))

        names = scope.names(constants.READ_WRITE)
        names.sort()

        self.assertEqual('read read+write write', ' '.join(names))

    def test_get_scope_ints(self):
        self.assertEqual(constants.READ, scope.to_int('read'))
        self.assertEqual(constants.WRITE, scope.to_int('write'))
        self.assertEqual(constants.READ_WRITE, scope.to_int('read', 'write'))
        self.assertEqual(0, scope.to_int('invalid'))
        self.assertEqual(1, scope.to_int('invalid', default=1))

    def test_template_filter(self):
        names = scopes(constants.READ)
        self.assertEqual('read', ' '.join(names))

        names = scope.names(constants.READ_WRITE)
        names.sort()

        self.assertEqual('read read+write write', ' '.join(names))


class DeleteExpiredTest(BaseOAuth2TestCase):
    fixtures = ['test_oauth2']

    def setUp(self):
        self._delete_expired = constants.DELETE_EXPIRED
        constants.DELETE_EXPIRED = True

    def tearDown(self):
        constants.DELETE_EXPIRED = self._delete_expired

    def test_clear_expired(self):
        self.login()

        self._login_and_authorize()

        response = self.client.get(self.redirect_url())

        self.assertEqual(302, response.status_code)
        location = response['Location']
        self.assertFalse('error' in location)
        self.assertTrue('code' in location)

        # verify that Grant with code exists
        code = urlparse.parse_qs(location)['code'][0]
        self.assertTrue(Grant.objects.filter(code=code).exists())

        # use the code/grant
        response = self.client.post(self.access_token_url(), {
            'grant_type': 'authorization_code',
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
            'code': code})
        self.assertEquals(200, response.status_code)
        token = json.loads(response.content)
        self.assertTrue('access_token' in token)
        access_token = token['access_token']
        self.assertTrue('refresh_token' in token)
        refresh_token = token['refresh_token']

        # make sure the grant is gone
        self.assertFalse(Grant.objects.filter(code=code).exists())
        # and verify that the AccessToken and RefreshToken exist
        self.assertTrue(AccessToken.objects.filter(token=access_token)
                        .exists())
        self.assertTrue(RefreshToken.objects.filter(token=refresh_token)
                        .exists())

        # refresh the token
        response = self.client.post(self.access_token_url(), {
            'grant_type': 'refresh_token',
            'refresh_token': token['refresh_token'],
            'client_id': self.get_client().client_id,
            'client_secret': self.get_client().client_secret,
        })
        self.assertEqual(200, response.status_code)
        token = json.loads(response.content)
        self.assertTrue('access_token' in token)
        self.assertNotEquals(access_token, token['access_token'])
        self.assertTrue('refresh_token' in token)
        self.assertNotEquals(refresh_token, token['refresh_token'])

        # make sure the orig AccessToken and RefreshToken are gone
        self.assertFalse(AccessToken.objects.filter(token=access_token)
                         .exists())
        self.assertFalse(RefreshToken.objects.filter(token=refresh_token)
                         .exists())
