import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils.crypto import get_random_string

from apps.documents.models import EmailOutbox, ZohoMailCredential
from apps.documents.zoho_mail import OAUTH_STATE_KEY, authorization_url, complete_authorization
from apps.organization.authentication import DMS_ADMIN_GROUP


@override_settings(
    ZOHO_OAUTH_CLIENT_ID="client-id", ZOHO_OAUTH_CLIENT_SECRET="client-secret",
    ZOHO_OAUTH_REDIRECT_URI="https://dms.example.test/integrations/zoho/callback/",
    ZOHO_OAUTH_AUTH_URL="https://accounts.zoho.com/oauth/v2/auth",
    ZOHO_OAUTH_TOKEN_URL="https://accounts.zoho.com/oauth/v2/token",
    ZOHO_MAIL_TOKEN_ENCRYPTION_KEY="pK7qj7xQYJ7z8X8y6DqH6cL1b4QfY0S5K0vE9kR8n2M=",
)
class ZohoMailOAuthTests(TestCase):
    def test_oauth_endpoints_require_authenticated_dms_authorization(self):
        response = self.client.get(reverse("zoho-connect"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_authorization_uses_pkce_least_privilege_and_single_use_state(self):
        request = RequestFactory().get(reverse("zoho-connect"))
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda request: None).process_request(request)
        request.session.save()
        url = authorization_url(request)
        self.assertIn("scope=ZohoMail.messages.CREATE", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn(OAUTH_STATE_KEY, request.session)

    @patch("apps.documents.zoho_mail._post_form")
    def test_callback_encrypts_tokens_and_consumes_state(self, post_form):
        request = RequestFactory().get(reverse("zoho-callback"))
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda request: None).process_request(request)
        request.session[OAUTH_STATE_KEY] = {"value": "state", "verifier": "verifier", "created": int(time.time())}
        request.session.save()
        post_form.return_value = {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600}
        complete_authorization(request, code="one-time-code", state="state")
        credential = ZohoMailCredential.objects.get(singleton=1)
        self.assertNotIn("access-secret", credential.access_token)
        self.assertNotIn("refresh-secret", credential.refresh_token)
        self.assertNotIn(OAUTH_STATE_KEY, request.session)

    def test_callback_rejects_reused_or_mismatched_state(self):
        request = RequestFactory().get(reverse("zoho-callback"))
        from django.contrib.sessions.middleware import SessionMiddleware
        SessionMiddleware(lambda request: None).process_request(request)
        request.session[OAUTH_STATE_KEY] = {"value": "state", "verifier": "verifier", "created": int(time.time())}
        request.session.save()
        with self.assertRaises(PermissionError):
            complete_authorization(request, code="code", state="wrong")
        with self.assertRaises(PermissionError):
            complete_authorization(request, code="code", state="state")

    @override_settings(EMAIL_DELIVERY_PROVIDER="zoho_api", DEFAULT_FROM_EMAIL="dms@example.test",
                        ZOHO_MAIL_API_BASE_URL="https://mail.zoho.com/api", ZOHO_MAIL_ACCOUNT_ID="12345")
    @patch("apps.documents.zoho_mail.urlopen")
    @patch("apps.documents.zoho_mail._access_token", return_value="access-secret")
    def test_api_acceptance_uses_https_and_does_not_fallback(self, access_token, urlopen):
        from apps.documents.email_service import send_dms_email
        response = type("Response", (), {"status": 202, "read": lambda self: b"{}", "__enter__": lambda self: self, "__exit__": lambda *args: None})()
        urlopen.return_value = response
        self.assertEqual(send_dms_email(
            recipient="employee@example.test", subject="Subject", text_body="Text",
            html_body="<p>Text</p>", from_email="dms@example.test", provider="zoho_api",
        ), 1)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://"))
        self.assertIn("Zoho-oauthtoken", request.headers.get("Authorization", ""))
