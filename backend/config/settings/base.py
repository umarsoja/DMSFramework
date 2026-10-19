"""
TradeFlow Base Settings

Shared settings for all environments.

- Development
- Testing
- Production

Environment-specific overrides should be placed in:
    development.py
    testing.py
    production.py
"""

from pathlib import Path

import environ

# =============================================================================
# PATHS
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# =============================================================================
# ENVIRONMENT
# =============================================================================

env = environ.Env()

# Read .env file
environ.Env.read_env(BASE_DIR / ".env")

# =============================================================================
# SECURITY
# =============================================================================

SECRET_KEY = env("SECRET_KEY")

DEBUG = env.bool("DEBUG", default=False)

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

# =============================================================================
# APPLICATIONS
# =============================================================================

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    # "django_htmx",
    # "django_filters",
]

LOCAL_APPS = [
    "apps.documents.apps.DocumentsConfig",
    "apps.organization.apps.OrganizationConfig",
    # "apps.dashboard",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# =============================================================================
# MIDDLEWARE
# =============================================================================

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# =============================================================================
# URL CONFIGURATION
# =============================================================================

ROOT_URLCONF = "config.urls"

# =============================================================================
# TEMPLATES
# =============================================================================

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.documents.context_processors.dms_notifications",
            ],
        },
    },
]

# =============================================================================
# WSGI / ASGI
# =============================================================================

WSGI_APPLICATION = "config.wsgi.application"

ASGI_APPLICATION = "config.asgi.application"

# =============================================================================
# DATABASE
# =============================================================================

DATABASES = {
    "default": env.db("DATABASE_URL")
}
# =============================================================================
# PASSWORD VALIDATION
# =============================================================================

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# =============================================================================
# INTERNATIONALIZATION
# =============================================================================

LANGUAGE_CODE = "en-us"

TIME_ZONE = "Africa/Lagos"

USE_I18N = True

USE_TZ = True

# =============================================================================
# STATIC FILES
# =============================================================================

STATIC_URL = "/static/"

STATICFILES_DIRS = [
    BASE_DIR / "static",
]

STATIC_ROOT = BASE_DIR / "staticfiles"

# =============================================================================
# MEDIA FILES
# =============================================================================

MEDIA_URL = "/media/"

MEDIA_ROOT = BASE_DIR / "media"

# =============================================================================
# EMAIL
# =============================================================================

EMAIL_BACKEND = env("EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtppro.zoho.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=465)
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=True)
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=False)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="dms@alliancepowerg.com")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="dms@alliancepowerg.com")
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=20)
DMS_BASE_URL = env("DMS_BASE_URL", default="")
EMAIL_DELIVERY_PROVIDER = env("EMAIL_DELIVERY_PROVIDER", default="smtp").strip().lower()
WORKFLOW_EMAIL_NOTIFICATIONS = env.bool("WORKFLOW_EMAIL_NOTIFICATIONS", default=False)
EMAIL_DELIVERY_SYNCHRONOUS = env.bool("EMAIL_DELIVERY_SYNCHRONOUS", default=False)
ZOHO_OAUTH_CLIENT_ID = env("ZOHO_OAUTH_CLIENT_ID", default="")
ZOHO_OAUTH_CLIENT_SECRET = env("ZOHO_OAUTH_CLIENT_SECRET", default="")
ZOHO_OAUTH_AUTH_URL = env("ZOHO_OAUTH_AUTH_URL", default="https://accounts.zoho.com/oauth/v2/auth")
ZOHO_OAUTH_TOKEN_URL = env("ZOHO_OAUTH_TOKEN_URL", default="https://accounts.zoho.com/oauth/v2/token")
ZOHO_OAUTH_REDIRECT_URI = env("ZOHO_OAUTH_REDIRECT_URI", default="")
ZOHO_MAIL_API_BASE_URL = env("ZOHO_MAIL_API_BASE_URL", default="https://mail.zoho.com/api")
ZOHO_MAIL_ACCOUNT_ID = env("ZOHO_MAIL_ACCOUNT_ID", default="")
ZOHO_MAIL_TOKEN_ENCRYPTION_KEY = env("ZOHO_MAIL_TOKEN_ENCRYPTION_KEY", default="")

if EMAIL_USE_SSL and EMAIL_USE_TLS:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("EMAIL_USE_SSL and EMAIL_USE_TLS cannot both be enabled.")
if EMAIL_DELIVERY_PROVIDER not in {"smtp", "zoho_api"}:
    raise ImproperlyConfigured("EMAIL_DELIVERY_PROVIDER must be smtp or zoho_api.")

# =============================================================================
# LOGGING
# =============================================================================

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
}

# =============================================================================
# DEFAULT PRIMARY KEY
# =============================================================================

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
# =============================================================================
# STORAGE
# =============================================================================

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

#Company and Product
TRADEFLOW = {
    "NAME": "TradeFlow",
    "VERSION": "0.1.0",
    "COMPANY": "Sojh Consult",
    "COPYRIGHT": "© 2026 Sojh Consult",
}

#Login URLs
LOGIN_URL = "dms-login"

LOGIN_REDIRECT_URL = "dashboard"

LOGOUT_REDIRECT_URL = "dms-login"

#CRSF
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[],
)
