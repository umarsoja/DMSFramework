"""Zoho Mail OAuth and HTTPS delivery support.

Secrets are encrypted before they are stored.  No token is included in an
exception, response, audit context, or log message.
"""

import base64
import hashlib
import json
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from .models import ZohoMailCredential

OAUTH_STATE_KEY = "zoho_oauth_state"
OAUTH_STATE_TTL = timedelta(minutes=10)
TOKEN_REFRESH_SKEW = timedelta(minutes=2)


def _setting(name, *, required=True):
    value = str(getattr(settings, name, "") or "").strip()
    if required and not value:
        raise ImproperlyConfigured(f"{name} is required for Zoho Mail API delivery.")
    return value


def _fernet():
    key = _setting("ZOHO_MAIL_TOKEN_ENCRYPTION_KEY")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured("ZOHO_MAIL_TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def _seal(value):
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _open(value):
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise RuntimeError("Stored Zoho credential could not be decrypted.") from exc


def provider_enabled():
    return str(getattr(settings, "EMAIL_DELIVERY_PROVIDER", "smtp")) == "zoho_api"


def _post_form(url, values, *, timeout):
    body = urlencode(values).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"Zoho OAuth endpoint returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Zoho OAuth endpoint could not be reached or returned invalid data.") from exc


def authorization_url(request):
    client_id = _setting("ZOHO_OAUTH_CLIENT_ID")
    redirect_uri = _setting("ZOHO_OAUTH_REDIRECT_URI")
    auth_url = _setting("ZOHO_OAUTH_AUTH_URL")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    request.session[OAUTH_STATE_KEY] = {"value": state, "verifier": verifier, "created": int(time.time())}
    request.session.modified = True
    query = urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": "ZohoMail.messages.CREATE", "access_type": "offline", "prompt": "consent",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })
    return f"{auth_url}?{query}"


def complete_authorization(request, *, code, state):
    expected = request.session.pop(OAUTH_STATE_KEY, None)
    request.session.modified = True
    if not expected or not secrets.compare_digest(str(expected.get("value", "")), str(state or "")):
        raise PermissionError("Invalid or expired Zoho authorization state.")
    if int(time.time()) - int(expected.get("created", 0)) > int(OAUTH_STATE_TTL.total_seconds()):
        raise PermissionError("Invalid or expired Zoho authorization state.")
    if not code:
        raise ValueError("Zoho authorization did not return a code.")
    payload = _post_form(_setting("ZOHO_OAUTH_TOKEN_URL"), {
        "code": code, "grant_type": "authorization_code", "client_id": _setting("ZOHO_OAUTH_CLIENT_ID"),
        "client_secret": _setting("ZOHO_OAUTH_CLIENT_SECRET"), "redirect_uri": _setting("ZOHO_OAUTH_REDIRECT_URI"),
        "code_verifier": expected["verifier"],
    }, timeout=getattr(settings, "EMAIL_TIMEOUT", 20))
    refresh = payload.get("refresh_token")
    access = payload.get("access_token")
    if not refresh or not access:
        raise RuntimeError("Zoho authorization did not return the required token material.")
    expires = timezone.now() + timedelta(seconds=int(payload.get("expires_in", 3600)))
    with transaction.atomic():
        credential, _ = ZohoMailCredential.objects.select_for_update().get_or_create(singleton=1, defaults={"refresh_token": _seal(refresh)})
        credential.refresh_token = _seal(refresh)
        credential.access_token = _seal(access)
        credential.access_token_expires_at = expires
        credential.save(update_fields=["refresh_token", "access_token", "access_token_expires_at", "updated_at"])


def _access_token():
    with transaction.atomic():
        credential = ZohoMailCredential.objects.select_for_update().filter(singleton=1).first()
        if not credential:
            raise RuntimeError("Zoho Mail API is not connected.")
        now = timezone.now()
        if credential.access_token and credential.access_token_expires_at and credential.access_token_expires_at > now + TOKEN_REFRESH_SKEW:
            return _open(credential.access_token)
        payload = _post_form(_setting("ZOHO_OAUTH_TOKEN_URL"), {
            "refresh_token": _open(credential.refresh_token), "grant_type": "refresh_token",
            "client_id": _setting("ZOHO_OAUTH_CLIENT_ID"), "client_secret": _setting("ZOHO_OAUTH_CLIENT_SECRET"),
        }, timeout=getattr(settings, "EMAIL_TIMEOUT", 20))
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Zoho token refresh did not return an access token.")
        credential.access_token = _seal(token)
        credential.access_token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)))
        credential.save(update_fields=["access_token", "access_token_expires_at", "updated_at"])
        return token


def send_email(*, recipient, subject, html_body, text_body, from_email):
    token = _access_token()
    base = _setting("ZOHO_MAIL_API_BASE_URL").rstrip("/")
    account_id = _setting("ZOHO_MAIL_ACCOUNT_ID")
    payload = json.dumps({
        "fromAddress": from_email, "toAddress": recipient, "subject": subject,
        "content": html_body or text_body, "mailFormat": "html" if html_body else "plaintext",
    }).encode("utf-8")
    request = Request(
        f"{base}/accounts/{account_id}/messages", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Zoho-oauthtoken {token}"}, method="POST",
    )
    try:
        with urlopen(request, timeout=getattr(settings, "EMAIL_TIMEOUT", 20)) as response:
            status = response.status
            if status < 200 or status >= 300:
                raise RuntimeError(f"Zoho Mail API returned HTTP {status}.")
            return 1
    except HTTPError as exc:
        raise RuntimeError(f"Zoho Mail API returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Zoho Mail API request failed.") from exc
