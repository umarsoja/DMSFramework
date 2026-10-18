"""Account activation and recovery email composition and outbox delivery."""

import hashlib
import re
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.documents.email_service import send_dms_email
from apps.documents.models import EmailOutbox

MAX_DELIVERY_ATTEMPTS = 5
PROCESSING_STALE_AFTER = timedelta(minutes=15)


def _canonical_link(name, user, token):
    base_url = (getattr(settings, "DMS_BASE_URL", "") or "").strip()
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("DMS_BASE_URL must be an absolute canonical URL without credentials, query, or fragment.")
    if not settings.DEBUG and parts.scheme != "https":
        raise ValueError("DMS_BASE_URL must use HTTPS outside development.")
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return f"{base_url.rstrip('/')}{reverse(name, args=[uid, token])}"


def activation_link(user, raw_token):
    return _canonical_link("employee-activate", user, raw_token)


def queue_activation_email(*, user, raw_token):
    from apps.organization.models import EmployeeActivationToken
    activation = EmployeeActivationToken.objects.get(user=user)
    outbox = EmailOutbox.objects.create(
        recipient=user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        delivery_provider=getattr(settings, "EMAIL_DELIVERY_PROVIDER", "smtp"),
        automatic_retry_enabled=True,
        source_reference=f"activation:{user.pk}:{activation.token_hash}",
    )
    if getattr(settings, "EMAIL_DELIVERY_SYNCHRONOUS", False):
        transaction.on_commit(lambda: deliver_activation_email(outbox.pk, raw_token=raw_token))
    return outbox


def _safe_failure(exc, *, raw_token="", recipient=""):
    message = str(exc).replace("\r", " ").replace("\n", " ")
    for secret in (raw_token, recipient, getattr(settings, "EMAIL_HOST_PASSWORD", ""),
                   getattr(settings, "EMAIL_HOST_USER", "")):
        if secret:
            message = message.replace(secret, "[redacted]")
    message = re.sub(r"https?://\S+", "[redacted URL]", message)
    message = re.sub(r"(?i)(token|password|secret)=?[^\s,;]+", r"\1=[redacted]", message)
    message = message[:400]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def deliver_activation_email(outbox_id, *, raw_token=None):
    """Send current eligible recovery messages; never persist their secret links."""
    from apps.organization.models import EmployeeActivationToken, EmployeeProfile
    from apps.organization.services import activation_eligibility, issue_employee_activation, password_reset_eligibility

    now = timezone.now()
    with transaction.atomic():
        try:
            outbox = EmailOutbox.objects.select_for_update().select_related("recipient").get(pk=outbox_id)
        except EmailOutbox.DoesNotExist:
            return None
        if outbox.status == EmailOutbox.Status.SENT or outbox.attempt_count >= MAX_DELIVERY_ATTEMPTS:
            return outbox
        if (outbox.status == EmailOutbox.Status.PROCESSING and outbox.processing_started_at
                and outbox.processing_started_at > now - PROCESSING_STALE_AFTER):
            return outbox
        outbox.status = EmailOutbox.Status.PROCESSING
        outbox.processing_started_at = now
        outbox.last_attempt_at = now
        outbox.attempt_count += 1
        outbox.latest_failure_reason = ""
        outbox.save(update_fields=["status", "processing_started_at", "last_attempt_at", "attempt_count", "latest_failure_reason"])
        user, message_type, source_reference = outbox.recipient, outbox.message_type, outbox.source_reference

    try:
        profile = EmployeeProfile.objects.get(user=user)
        if not user.email:
            raise ValueError("No authoritative email address is configured for this account.")
        if message_type == EmailOutbox.MessageType.ACCOUNT_ACTIVATION:
            if not activation_eligibility(user):
                raise ValueError("Activation invitation is no longer eligible; message not sent.")
            activation = EmployeeActivationToken.objects.get(user=user)
            if raw_token is None:
                with transaction.atomic():
                    locked = EmailOutbox.objects.select_for_update().get(pk=outbox_id)
                    user = type(user).objects.select_for_update().get(pk=user.pk)
                    if not activation_eligibility(user):
                        raise ValueError("Activation invitation is no longer eligible; message not sent.")
                    activation = EmployeeActivationToken.objects.select_for_update().get(user=user)
                    if (locked.source_reference != f"activation:{user.pk}:{activation.token_hash}"
                            or activation.used_at or activation.expires_at <= timezone.now()):
                        raise ValueError("Activation invitation was superseded or expired; message not sent.")
                    raw_token = issue_employee_activation(user=user)
                    activation.refresh_from_db()
                    locked.source_reference = f"activation:{user.pk}:{activation.token_hash}"
                    locked.save(update_fields=["source_reference"])
            elif hashlib.sha256(raw_token.encode()).hexdigest() != activation.token_hash:
                raise ValueError("Activation invitation was superseded; message not sent.")
            context = {"employee_name": user.get_full_name().strip() or user.username,
                       "activation_url": activation_link(user, raw_token), "expires_in_hours": 24,
                       "staff_id": profile.staff_id}
            subject, template = "Your APGC DMS account is ready", "activation"
        elif message_type == EmailOutbox.MessageType.PASSWORD_RESET:
            if not password_reset_eligibility(user):
                raise ValueError("Password reset is no longer eligible; message not sent.")
            latest_id = EmailOutbox.objects.filter(
                recipient=user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
            ).order_by("-queued_at", "-pk").values_list("pk", flat=True).first()
            if latest_id != outbox_id:
                raise ValueError("Password reset request was superseded; message not sent.")
            token = _password_reset_token(user, outbox.queued_at)
            context = {"employee_name": user.get_full_name().strip() or user.username,
                       "reset_url": _canonical_link("password-reset-confirm", user, token)}
            subject, template = "Reset your APGC DMS password", "password_reset"
        else:
            raise ValueError("Unsupported recovery message type.")
        sent = send_dms_email(
            recipient=user.email, subject=subject,
            text_body=render_to_string(f"emails/{template}.txt", context),
            html_body=render_to_string(f"emails/{template}.html", context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            provider=outbox.delivery_provider,
        )
        if sent != 1:
            raise RuntimeError("The email backend did not accept the recovery message.")
    except Exception as exc:
        EmailOutbox.objects.filter(pk=outbox_id).update(
            status=EmailOutbox.Status.FAILED, failed_at=timezone.now(), processing_started_at=None,
            latest_failure_reason=_safe_failure(exc, raw_token=raw_token or "", recipient=user.email),
        )
        return EmailOutbox.objects.get(pk=outbox_id)

    EmailOutbox.objects.filter(pk=outbox_id).update(
        status=EmailOutbox.Status.SENT, sent_at=timezone.now(), failed_at=None,
        processing_started_at=None, latest_failure_reason="",
    )
    return EmailOutbox.objects.get(pk=outbox_id)


@transaction.atomic
def resend_activation_email(*, user, actor):
    from apps.organization.models import OrganizationAuditEvent
    from apps.organization.services import activation_eligibility, issue_employee_activation
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if not activation_eligibility(user):
        raise ValueError("This account is not eligible for an activation invitation.")
    recent = EmailOutbox.objects.filter(
        recipient=user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        status__in=[EmailOutbox.Status.QUEUED, EmailOutbox.Status.PROCESSING],
        queued_at__gte=timezone.now() - timedelta(minutes=2),
    ).exists()
    if recent:
        raise ValueError("An activation email was requested recently. Please wait before trying again.")
    raw_token = issue_employee_activation(user=user)
    outbox = queue_activation_email(user=user, raw_token=raw_token)
    OrganizationAuditEvent.objects.create(
        actor=actor, action=OrganizationAuditEvent.Action.ACTIVATION_EMAIL_RESENT,
        subject_user=user, context={"delivery_id": outbox.pk},
    )
    return outbox


@transaction.atomic
def request_activation_email(*, user):
    from apps.organization.services import activation_eligibility, issue_employee_activation
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if not activation_eligibility(user):
        return None
    token = issue_employee_activation(user=user)
    return queue_activation_email(user=user, raw_token=token)


@transaction.atomic
def queue_password_reset_email(*, user):
    from apps.organization.services import password_reset_eligibility
    user = type(user).objects.select_for_update().get(pk=user.pk)
    if not password_reset_eligibility(user):
        return None
    previous = EmailOutbox.objects.filter(
        recipient=user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
    ).order_by("-queued_at", "-pk").first()
    EmailOutbox.objects.filter(
        recipient=user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
    ).exclude(status=EmailOutbox.Status.SENT).update(
             status=EmailOutbox.Status.FAILED, attempt_count=MAX_DELIVERY_ATTEMPTS,
             failed_at=timezone.now(), processing_started_at=None,
             latest_failure_reason="Superseded by a newer password reset request.")
    queued_at = timezone.now()
    if previous:
        local_queued_at = timezone.localtime(queued_at).replace(microsecond=0)
        local_previous = timezone.localtime(previous.queued_at).replace(microsecond=0)
        if local_queued_at <= local_previous:
            queued_at = previous.queued_at.replace(microsecond=0) + timedelta(seconds=1)
    outbox = EmailOutbox.objects.create(
        recipient=user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
        delivery_provider=getattr(settings, "EMAIL_DELIVERY_PROVIDER", "smtp"),
        automatic_retry_enabled=True,
        source_reference=f"password-reset:{user.pk}",
        queued_at=queued_at,
    )
    if getattr(settings, "EMAIL_DELIVERY_SYNCHRONOUS", False):
        transaction.on_commit(lambda: deliver_activation_email(outbox.pk))
    return outbox


def _password_reset_token(user, queued_at):
    # Django's generator hashes password, last_login, email and timestamp. Use the
    # outbox timestamp as the deterministic token timestamp so retries reconstruct it.
    from django.utils import timezone as django_timezone
    timestamp = default_token_generator._num_seconds(django_timezone.localtime(queued_at).replace(tzinfo=None))
    return default_token_generator._make_token_with_timestamp(
        user, timestamp, default_token_generator.secret,
    )


def process_pending_email(*, limit=100):
    now, stale = timezone.now(), timezone.now() - PROCESSING_STALE_AFTER
    ids = list(EmailOutbox.objects.filter(
        status__in=[EmailOutbox.Status.QUEUED, EmailOutbox.Status.FAILED],
        automatic_retry_enabled=True,
        attempt_count__lt=MAX_DELIVERY_ATTEMPTS,
    ).order_by("queued_at", "pk").values_list("pk", flat=True)[:limit])
    stale_ids = list(EmailOutbox.objects.filter(
        status=EmailOutbox.Status.PROCESSING, processing_started_at__lt=stale,
        automatic_retry_enabled=True,
        attempt_count__lt=MAX_DELIVERY_ATTEMPTS,
    ).order_by("queued_at", "pk").values_list("pk", flat=True)[:max(0, limit - len(ids))])
    return [deliver_activation_email(pk) for pk in ids + stale_ids]
