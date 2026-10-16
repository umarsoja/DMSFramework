"""Small reusable wrapper around Django's configured email backend."""

from django.core.mail import EmailMultiAlternatives
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def send_dms_email(*, recipient, subject, text_body, html_body, from_email=None):
    """Send one multipart DMS email through Django's configured backend."""
    if settings.EMAIL_BACKEND == "django.core.mail.backends.console.EmailBackend":
        # The console backend writes message bodies (including activation URLs) to logs.
        raise ImproperlyConfigured("The console email backend is disabled for security-sensitive DMS messages.")
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=from_email,
        to=[recipient],
    )
    if html_body:
        message.attach_alternative(html_body, "text/html")
    return message.send(fail_silently=False)
