from .base import *
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

DEBUG = False

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

# Production must use a unique secret supplied by the deployment environment.
if len(SECRET_KEY) < 50 or any(marker in SECRET_KEY.lower() for marker in ("replace-with", "django-insecure")):
    raise ImproperlyConfigured("Production SECRET_KEY must be a strong, unique value of at least 50 characters.")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Set ALLOWED_HOSTS to the production DMS hostnames.")

# Set this to the canonical public HTTPS URL used when generating recovery links.
DMS_BASE_URL = env("DMS_BASE_URL", default="").rstrip("/")
parsed_dms_url = urlparse(DMS_BASE_URL)
if parsed_dms_url.scheme != "https" or not parsed_dms_url.netloc or parsed_dms_url.path or parsed_dms_url.query or parsed_dms_url.fragment:
    raise ImproperlyConfigured("DMS_BASE_URL must be the canonical origin, such as https://dms.example.com.")
if parsed_dms_url.username or parsed_dms_url.password:
    raise ImproperlyConfigured("DMS_BASE_URL must not contain credentials.")

# Initial rollout settings. Raise HSTS duration only after HTTPS is confirmed end to end.
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=300)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

# Enable only behind a trusted proxy/load balancer that replaces this header.
if env.bool("TRUST_X_FORWARDED_PROTO", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Keep uploaded correspondence outside the collected static tree. Files are
# delivered through permission-checked application views, never a public media alias.
MEDIA_ROOT = Path(env("PRIVATE_MEDIA_ROOT", default=str(BASE_DIR / "private-media")))
EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtppro.zoho.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=465)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=True)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="dms@alliancepowerg.com")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="dms@alliancepowerg.com")
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
