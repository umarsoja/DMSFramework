import hashlib
import io
import logging
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.utils import timezone

from apps.documents.models import EmailOutbox
from apps.organization.authentication import DMS_ADMIN_GROUP
from apps.organization.logging import ActivationTokenRedactionFilter
from apps.organization.models import Department, EmployeeActivationToken, EmployeeProfile, Position
from apps.organization.services import onboard_employee


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="dms@alliancepowerg.com",
    DMS_BASE_URL="https://dms.example.test",
)
class ActivationEmailDeliveryTests(TestCase):
    password = "Secure-Activation-Test-Password!2026"

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="mail-admin", email="mail-admin@example.test", password="admin-password",
        )
        self.department = Department.objects.create(code="MAIL-OPS", name="Email Operations")
        self.position = Position.objects.create(code="MAIL-EA", title="Executive Assistant", department=self.department)
        self.admin_profile = EmployeeProfile.objects.create(
            user=self.admin, staff_id="MAIL-ADMIN", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.admin.groups.add(Group.objects.get(name=DMS_ADMIN_GROUP))
        from apps.organization.models import EmployeeAssignment

        EmployeeAssignment.objects.create(
            employee=self.admin_profile,
            department=self.department,
            position=self.position,
            start_date=timezone.localdate() - timedelta(days=1),
        )

    def create_employee(self, username="mail-employee", *, email=None,
                        account_status=EmployeeProfile.AccountStatus.INACTIVE,
                        first_name="Fahd", last_name="Employee"):
        return onboard_employee(
            actor=self.admin,
            username=username,
            first_name=first_name,
            last_name=last_name,
            email=email or f"{username}@example.test",
            staff_id=f"STAFF-{username}",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=account_status,
            department=self.department,
            position=self.position,
            start_date=timezone.localdate(),
            roles=[Group.objects.get(name="DMS Standard User")],
        )

    def test_onboarding_sends_multipart_activation_email_from_canonical_url(self):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee()

        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [profile.user.email])
        self.assertEqual(message.from_email, "dms@alliancepowerg.com")
        self.assertEqual(message.subject, "Your APGC DMS account is ready")
        self.assertIn("Fahd Employee", message.body)
        self.assertIn("expires in 24 hours", message.body)
        self.assertIn("https://dms.example.test/accounts/activate/", message.body)
        self.assertEqual(len(message.alternatives), 1)
        html_body, mime_type = message.alternatives[0]
        self.assertEqual(mime_type, "text/html")
        self.assertIn("Activate your APGC DMS account", html_body)
        self.assertIn(profile.user.username, message.body + html_body)
        self.assertIn("Do not share this activation link", message.body + html_body)
        self.assertNotIn(self.password, message.body + html_body)

        outbox = EmailOutbox.objects.get(recipient=profile.user)
        self.assertEqual(outbox.status, EmailOutbox.Status.SENT)
        self.assertEqual(outbox.attempt_count, 1)
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        self.assertEqual(activation.expires_at - activation.created_at, timedelta(hours=24))
        for field in (outbox.source_reference, outbox.latest_failure_reason, str(outbox)):
            self.assertNotIn(profile._activation_token, field)
        self.assertFalse(hasattr(outbox, "body"))
        self.assertFalse(hasattr(outbox, "activation_url"))

    def test_activation_email_keeps_full_official_name_separate_from_short_username(self):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee(
                username="abubakar.alhaji",
                first_name="Abubakar Alhaji",
                last_name="Mohammed",
            )
        message = mail.outbox[0]
        self.assertIn("Hello Abubakar Alhaji Mohammed", message.body)
        self.assertIn("Your login username is: abubakar.alhaji", message.body)
        self.assertIn("Abubakar Alhaji Mohammed", message.alternatives[0][0])
        self.assertNotIn("Abubakar Alhaji Mohammed".replace(" ", ".").lower(), message.body.lower())
        self.assertEqual(profile.user.get_full_name(), "Abubakar Alhaji Mohammed")
    def test_linked_account_uses_its_authoritative_user_email(self):
        linked = get_user_model().objects.create_user(
            username="linked-account", email="authoritative@example.test",
        )
        with self.captureOnCommitCallbacks(execute=True):
            profile = onboard_employee(
                actor=self.admin,
                user=linked,
                username="",
                first_name="Fahd",
                last_name="Linked",
                email="submitted-but-not-authoritative@example.test",
                staff_id="STAFF-LINKED",
                employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
                account_status=EmployeeProfile.AccountStatus.INACTIVE,
                department=self.department,
                position=self.position,
                start_date=timezone.localdate(),
                roles=[Group.objects.get(name="DMS Standard User")],
            )

        self.assertEqual(profile.user.email, "authoritative@example.test")
        self.assertEqual(mail.outbox[-1].to, ["authoritative@example.test"])

    @override_settings(EMAIL_HOST_PASSWORD="smtp-test-secret")
    def test_email_failure_does_not_roll_back_onboarding_and_reason_is_sanitized(self):
        with patch("apps.organization.activation_email.send_dms_email", side_effect=RuntimeError(
            "smtp-test-secret password=secret https://dms.example.test/accounts/activate/secret token=secret"
        )):
            with self.captureOnCommitCallbacks(execute=True):
                profile = self.create_employee("failed-mail")

        self.assertTrue(EmployeeProfile.objects.filter(pk=profile.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=profile.user_id).exists())
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        self.assertEqual(activation.token_hash, hashlib.sha256(profile._activation_token.encode()).hexdigest())
        outbox = EmailOutbox.objects.get(recipient=profile.user)
        self.assertEqual(outbox.status, EmailOutbox.Status.FAILED)
        self.assertIn("RuntimeError", outbox.latest_failure_reason)
        for secret in ("smtp-test-secret", "password=secret", "https://dms.example.test", "token=secret"):
            self.assertNotIn(secret, outbox.latest_failure_reason)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_console_backend_never_prints_activation_token_or_message(self):
        from contextlib import redirect_stdout

        captured = io.StringIO()
        with redirect_stdout(captured), self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee("console-blocked")

        self.assertEqual(captured.getvalue(), "")
        outbox = EmailOutbox.objects.get(recipient=profile.user)
        self.assertEqual(outbox.status, EmailOutbox.Status.FAILED)
        self.assertNotIn(profile._activation_token, outbox.latest_failure_reason)

    def test_legacy_queued_and_failed_records_are_quarantined_from_timer(self):
        profile = self.create_employee(username="legacy-quarantine")
        EmailOutbox.objects.filter(automatic_retry_enabled=True).update(automatic_retry_enabled=False)
        queued = EmailOutbox.objects.create(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
            status=EmailOutbox.Status.QUEUED,
        )
        failed = EmailOutbox.objects.create(
            recipient=profile.user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
            status=EmailOutbox.Status.FAILED, attempt_count=1,
        )
        with patch("apps.organization.activation_email.deliver_activation_email") as deliver:
            call_command("process_email_outbox", stdout=io.StringIO())
        queued.refresh_from_db()
        failed.refresh_from_db()
        deliver.assert_not_called()
        self.assertEqual(queued.attempt_count, 0)
        self.assertEqual(failed.attempt_count, 1)
        self.assertFalse(queued.automatic_retry_enabled)
        self.assertFalse(failed.automatic_retry_enabled)

    def test_newly_queued_record_is_eligible_for_timer(self):
        profile = self.create_employee(username="fresh-queue")
        EmailOutbox.objects.filter(automatic_retry_enabled=True).update(automatic_retry_enabled=False)
        fresh = EmailOutbox.objects.create(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
            status=EmailOutbox.Status.QUEUED, automatic_retry_enabled=True,
        )
        with patch("apps.organization.activation_email.deliver_activation_email", return_value=fresh) as deliver:
            call_command("process_email_outbox", stdout=io.StringIO())
        deliver.assert_called_once_with(fresh.pk)

    def test_recovery_command_retries_failed_delivery_without_storing_old_token(self):
        with patch("apps.organization.activation_email.send_dms_email", side_effect=OSError("temporary SMTP outage")):
            with self.captureOnCommitCallbacks(execute=True):
                profile = self.create_employee("recovered-mail")
        first_token_hash = EmployeeActivationToken.objects.get(user=profile.user).token_hash
        outbox = EmailOutbox.objects.get(recipient=profile.user)
        self.assertEqual(outbox.status, EmailOutbox.Status.FAILED)

        call_command("process_email_outbox", stdout=io.StringIO())

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, EmailOutbox.Status.SENT)
        self.assertEqual(outbox.attempt_count, 2)
        self.assertNotEqual(EmployeeActivationToken.objects.get(user=profile.user).token_hash, first_token_hash)
        self.assertEqual(len(mail.outbox), 1)

    def test_authorized_resend_rotates_token_and_invalidates_previous_link(self):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee("resend-mail")
        old_token = profile._activation_token
        activation_path = reverse("employee-activate", args=[urlsafe_base64_encode(force_bytes(profile.user_id)), old_token])
        self.client.force_login(self.admin)
        employee_detail = self.client.get(reverse("organization:employee-detail", args=[profile.pk]))
        self.assertContains(employee_detail, 'data-confirm-kind="resend-activation"')
        self.assertContains(employee_detail, "Send Activation Email")
        self.assertContains(employee_detail, "previous activation link will be invalidated")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("organization:employee-resend-activation", args=[profile.pk]))

        self.assertEqual(response.status_code, 302)
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        self.assertNotEqual(activation.token_hash, hashlib.sha256(old_token.encode()).hexdigest())
        self.assertEqual(self.client.get(activation_path).status_code, 400)
        latest = EmailOutbox.objects.filter(recipient=profile.user).order_by("-queued_at", "-pk").first()
        self.assertEqual(latest.status, EmailOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 2)
        from apps.organization.models import OrganizationAuditEvent
        self.assertTrue(OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.ACTIVATION_EMAIL_RESENT,
            actor=self.admin, subject_user=profile.user,
        ).exists())

    def test_enabled_uncredentialed_employee_resend_is_visible_on_detail_and_edit(self):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee(
                "enabled-no-password",
                account_status=EmployeeProfile.AccountStatus.ACTIVE,
            )
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.is_active)
        self.assertFalse(profile.user.has_usable_password())
        self.client.force_login(self.admin)

        detail = self.client.get(reverse("organization:employee-detail", args=[profile.pk]))
        edit = self.client.get(reverse("organization:employee-edit", args=[profile.pk]))

        self.assertContains(detail, 'data-confirm-kind="resend-activation"')
        self.assertContains(edit, 'data-confirm-kind="resend-activation"')
        self.assertContains(edit, "Send Activation Email")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("organization:employee-resend-activation", args=[profile.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)
        profile.refresh_from_db()
        self.assertEqual(profile.account_status, EmployeeProfile.AccountStatus.ACTIVE)
        self.assertTrue(profile.user.is_active)

    def test_suspended_employee_has_no_resend_action_and_endpoint_rejects(self):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self.create_employee(
                "suspended-no-password",
                account_status=EmployeeProfile.AccountStatus.SUSPENDED,
            )
        self.client.force_login(self.admin)

        detail = self.client.get(reverse("organization:employee-detail", args=[profile.pk]))
        edit = self.client.get(reverse("organization:employee-edit", args=[profile.pk]))
        response = self.client.post(reverse("organization:employee-resend-activation", args=[profile.pk]))

        self.assertNotContains(detail, 'data-confirm-kind="resend-activation"')
        self.assertNotContains(edit, 'data-confirm-kind="resend-activation"')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(EmailOutbox.objects.filter(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        ).count(), 0)

    def test_unauthorized_user_cannot_resend_activation(self):
        profile = self.create_employee("protected-resend")
        profile.account_status = EmployeeProfile.AccountStatus.ACTIVE
        profile.save()
        profile.user.refresh_from_db()
        previous_hash = EmployeeActivationToken.objects.get(user=profile.user).token_hash
        regular = get_user_model().objects.create_user("regular-mail-user", password="password")
        self.client.force_login(regular)

        response = self.client.post(reverse("organization:employee-resend-activation", args=[profile.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(EmailOutbox.objects.filter(recipient=profile.user).count(), 1)
        self.assertEqual(EmployeeActivationToken.objects.get(user=profile.user).token_hash, previous_hash)

    def test_activation_pages_use_same_origin_referrer_policy_and_expired_links_stay_invalid(self):
        profile = self.create_employee("expired-mail")
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        activation.expires_at = timezone.now() - timedelta(seconds=1)
        activation.save(update_fields=["expires_at"])
        response = self.client.get(reverse("employee-activate", args=[
            urlsafe_base64_encode(force_bytes(profile.user_id)), profile._activation_token,
        ]))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response["Cache-Control"])

    def test_activation_link_tokens_are_redacted_from_request_logs(self):
        record = logging.LogRecord(
            name="django.server", level=logging.INFO, pathname="runserver", lineno=1,
            msg='"GET /accounts/activate/user-id/raw-token/ HTTP/1.1" 200 10', args=(), exc_info=None,
        )

        self.assertTrue(ActivationTokenRedactionFilter().filter(record))
        self.assertNotIn("user-id", record.getMessage())
        self.assertNotIn("raw-token", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())

    def test_password_reset_tokens_are_redacted_from_request_logs(self):
        record = logging.LogRecord(
            name="django.server", level=logging.INFO, pathname="runserver", lineno=1,
            msg='"GET /accounts/password-reset/user-id/raw-reset-token/ HTTP/1.1" 200 10',
            args=(), exc_info=None,
        )
        self.assertTrue(ActivationTokenRedactionFilter().filter(record))
        self.assertNotIn("user-id", record.getMessage())
        self.assertNotIn("raw-reset-token", record.getMessage())
        self.assertIn("[redacted]", record.getMessage())
