import re
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import EmailOutbox
from apps.organization.models import Department, EmployeeActivationToken, EmployeeProfile, Position
from apps.organization.services import onboard_employee


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
                   DMS_BASE_URL="https://dms.example.test", DEFAULT_FROM_EMAIL="dms@example.test")
class AccountRecoveryTests(TestCase):
    new_password = "Recovery-Strong-Password!2026"

    def setUp(self):
        User = get_user_model()
        self.actor = User.objects.create_superuser("recovery-admin", "admin@example.test", "Admin-Password!2026")
        self.department = Department.objects.create(code="RECOVERY", name="Recovery")
        self.position = Position.objects.create(code="RECOVERY-STAFF", title="Staff", department=self.department)
        self.profile = onboard_employee(
            actor=self.actor, username="recoverable", first_name="A", last_name="Employee",
            email="recoverable@example.test", staff_id="RECOVERABLE",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
            department=self.department, position=self.position, start_date=timezone.localdate(),
            roles=[Group.objects.get(name="DMS Standard User")],
        )

    def _activate(self):
        user = self.profile.user
        user.set_password("Existing-Password!2026")
        user.save(update_fields=["password"])
        self.profile.account_status = EmployeeProfile.AccountStatus.ACTIVE
        self.profile.save(update_fields=["account_status", "updated_at"])
        return user

    def _create_employee(self, *, username, email, account_status=EmployeeProfile.AccountStatus.INACTIVE):
        return onboard_employee(
            actor=self.actor, username=username, first_name="Recovery", last_name="Employee",
            email=email, staff_id=f"RECOVERY-{username}",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=account_status, department=self.department, position=self.position,
            start_date=timezone.localdate(), roles=[Group.objects.get(name="DMS Standard User")],
        )

    def _reset_url(self, message):
        match = re.search(r"https://dms\.example\.test/accounts/password-reset/([^\s]+)", message.body)
        self.assertIsNotNone(match)
        return "/accounts/password-reset/" + match.group(1)

    def _csrf_form_token(self, client, url, *, referrer_policy=None):
        response = client.get(url, HTTP_HOST="127.0.0.1:8000")
        self.assertEqual(response.status_code, 200)
        if referrer_policy:
            self.assertEqual(response["Referrer-Policy"], referrer_policy)
        match = re.search(rb'name="csrfmiddlewaretoken"\s+value="([^"]+)"', response.content)
        self.assertIsNotNone(match)
        self.assertIn("csrftoken", client.cookies)
        return match.group(1).decode("ascii")

    def test_recovery_requests_enforce_same_origin_csrf_and_reject_opaque_or_hostile_origins(self):
        paths = (reverse("password-reset-request"), reverse("request-activation-link"))
        for path in paths:
            with self.subTest(path=path):
                client = Client(enforce_csrf_checks=True)
                token = self._csrf_form_token(client, path, referrer_policy="same-origin")
                response = client.post(
                    path,
                    {"email": "csrf-check@example.test", "csrfmiddlewaretoken": token},
                    HTTP_HOST="127.0.0.1:8000",
                    HTTP_ORIGIN="http://127.0.0.1:8000",
                    HTTP_REFERER="http://127.0.0.1:8000" + path,
                    HTTP_SEC_FETCH_SITE="same-origin",
                    HTTP_SEC_FETCH_MODE="navigate",
                )
                self.assertEqual(response.status_code, 200)

                for origin in ("null", "https://attacker.example"):
                    rejected_client = Client(enforce_csrf_checks=True)
                    rejected_token = self._csrf_form_token(
                        rejected_client, path, referrer_policy="same-origin"
                    )
                    rejected = rejected_client.post(
                        path,
                        {"email": "csrf-check@example.test", "csrfmiddlewaretoken": rejected_token},
                        HTTP_HOST="127.0.0.1:8000",
                        HTTP_ORIGIN=origin,
                        HTTP_REFERER="http://127.0.0.1:8000" + path,
                    )
                    self.assertEqual(rejected.status_code, 403, origin)

    def test_activation_and_password_reset_confirmation_forms_accept_same_origin_csrf(self):
        activation_client = Client(enforce_csrf_checks=True)
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        activation_url = reverse("employee-activate", args=[
            urlsafe_base64_encode(force_bytes(self.profile.user_id)), self.profile._activation_token,
        ])
        activation_csrf = self._csrf_form_token(activation_client, activation_url)
        activation_response = activation_client.post(
            activation_url,
            {
                "new_password1": self.new_password,
                "new_password2": self.new_password,
                "csrfmiddlewaretoken": activation_csrf,
            },
            HTTP_HOST="127.0.0.1:8000",
            HTTP_ORIGIN="http://127.0.0.1:8000",
            HTTP_REFERER="http://127.0.0.1:8000" + activation_url,
        )
        self.assertEqual(activation_response.status_code, 200)
        self.assertContains(activation_response, "Password set")

        user = self._activate()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("password-reset-request"), {"email": user.email})
        reset_url = self._reset_url(mail.outbox[-1])
        reset_client = Client(enforce_csrf_checks=True)
        reset_csrf = self._csrf_form_token(reset_client, reset_url, referrer_policy="same-origin")
        reset_response = reset_client.post(
            reset_url,
            {
                "new_password1": self.new_password,
                "new_password2": self.new_password,
                "csrfmiddlewaretoken": reset_csrf,
            },
            HTTP_HOST="127.0.0.1:8000",
            HTTP_ORIGIN="http://127.0.0.1:8000",
            HTTP_REFERER="http://127.0.0.1:8000" + reset_url,
        )
        self.assertEqual(reset_response.status_code, 200)
        self.assertContains(reset_response, "Password updated")

    def test_activation_request_uses_existing_records_and_generic_response(self):
        user_id, profile_id = self.profile.user_id, self.profile.pk
        assignment_id = self.profile.current_assignment.pk
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("request-activation-link"), {"email": self.profile.user.email})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "If the address matches an eligible")
        self.assertEqual(self.profile.user.__class__.objects.get(pk=user_id).employee_profile.pk, profile_id)
        self.assertEqual(self.profile.current_assignment.pk, assignment_id)
        self.assertEqual(EmailOutbox.objects.filter(recipient=self.profile.user).count(), 2)

    def test_unknown_email_gets_same_confirmation_without_outbox(self):
        known = self.client.post(reverse("password-reset-request"), {"email": "nobody@example.test"})
        self.assertEqual(known.status_code, 200)
        self.assertContains(known, "If the address matches an eligible")
        self.assertEqual(EmailOutbox.objects.count(), 1)  # original onboarding only

    def test_activation_lookup_ignores_preceding_unlinked_user_and_matches_email_case_insensitively(self):
        email = "duplicate.activation@example.test"
        unlinked = get_user_model().objects.create_user("unlinked-before-employee", email=email)
        profile = self._create_employee(
            username="linked-after-unlinked", email=email, account_status=EmployeeProfile.AccountStatus.INACTIVE,
        )
        self.assertLess(unlinked.pk, profile.user_id)
        before_token = EmployeeActivationToken.objects.get(user=profile.user).token_hash
        before_count = EmailOutbox.objects.filter(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        ).count()

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("request-activation-link"), {"email": email.upper()})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "If the address matches an eligible")
        self.assertEqual(EmailOutbox.objects.filter(recipient=unlinked).count(), 0)
        self.assertEqual(EmailOutbox.objects.filter(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        ).count(), before_count + 1)
        self.assertEqual(EmailOutbox.objects.filter(
            recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
        ).order_by("-queued_at", "-pk").first().status, EmailOutbox.Status.SENT)
        self.assertNotEqual(EmployeeActivationToken.objects.get(user=profile.user).token_hash, before_token)
        self.assertEqual(len(mail.outbox), 1)

    def test_password_reset_lookup_ignores_preceding_unlinked_user(self):
        email = "duplicate.reset@example.test"
        unlinked = get_user_model().objects.create_user("unlinked-before-reset-employee", email=email)
        profile = self._create_employee(
            username="linked-reset-employee", email=email, account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        profile.user.set_password("Existing-Password!2026")
        profile.user.save(update_fields=["password"])
        self.assertLess(unlinked.pk, profile.user_id)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("password-reset-request"), {"email": email})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "If the address matches an eligible")
        self.assertEqual(EmailOutbox.objects.filter(
            recipient=unlinked, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
        ).count(), 0)
        reset = EmailOutbox.objects.get(
            recipient=profile.user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
        )
        self.assertEqual(reset.status, EmailOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_multiple_employee_profiles_for_email_fail_closed_without_token_or_email(self):
        email = "ambiguous-employees@example.test"
        first = self._create_employee(username="ambiguous-first", email=email)
        second = self._create_employee(username="ambiguous-second", email=email)
        initial_outbox_count = EmailOutbox.objects.count()
        initial_mail_count = len(mail.outbox)
        token_hashes = {
            first.user_id: EmployeeActivationToken.objects.get(user=first.user).token_hash,
            second.user_id: EmployeeActivationToken.objects.get(user=second.user).token_hash,
        }

        responses = []
        for route in ("request-activation-link", "password-reset-request"):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse(route), {"email": email})
            responses.append(response)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "If the address matches an eligible")
            self.assertEqual(EmailOutbox.objects.count(), initial_outbox_count)
            self.assertEqual(len(mail.outbox), initial_mail_count)
            self.assertEqual(
                EmployeeActivationToken.objects.get(user=first.user).token_hash,
                token_hashes[first.user_id],
            )
            self.assertEqual(
                EmployeeActivationToken.objects.get(user=second.user).token_hash,
                token_hashes[second.user_id],
            )
        self.assertEqual(responses[0].content, responses[1].content)
        unknown = self.client.post(
            reverse("request-activation-link"), {"email": "unknown@example.test"},
        )
        self.assertEqual(responses[0].content, unknown.content)

    def test_ineligible_employee_gets_generic_responses_without_recovery_outbox(self):
        profile = self._create_employee(username="ineligible-recovery", email="ineligible@example.test")
        profile.user.set_password("Existing-Password!2026")
        profile.user.save(update_fields=["password"])
        token_hash = EmployeeActivationToken.objects.get(user=profile.user).token_hash
        outbox_count = EmailOutbox.objects.count()
        mail_count = len(mail.outbox)

        responses = []
        for route in ("request-activation-link", "password-reset-request"):
            response = self.client.post(reverse(route), {"email": "ineligible@example.test"})
            responses.append(response)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "If the address matches an eligible")
            self.assertEqual(EmailOutbox.objects.count(), outbox_count)
            self.assertEqual(len(mail.outbox), mail_count)
            self.assertEqual(EmployeeActivationToken.objects.get(user=profile.user).token_hash, token_hash)
        self.assertEqual(responses[0].content, responses[1].content)

    def test_password_reset_changes_password_once_and_preserves_account_roles(self):
        user = self._activate()
        group_ids = list(user.groups.values_list("pk", flat=True))
        assignment_id = self.profile.current_assignment.pk
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("password-reset-request"), {"email": user.email})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1, list(EmailOutbox.objects.values_list("status", "latest_failure_reason")))
        reset_url = self._reset_url(mail.outbox[-1])
        outbox = EmailOutbox.objects.get(message_type=EmailOutbox.MessageType.PASSWORD_RESET)
        for stored_value in (outbox.source_reference, outbox.latest_failure_reason, str(outbox)):
            self.assertNotIn(reset_url, stored_value)
            self.assertNotIn("https://", stored_value)
        get_response = self.client.get(reset_url)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", get_response["Cache-Control"])
        response = self.client.post(reset_url, {
            "new_password1": self.new_password, "new_password2": self.new_password,
        })
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.new_password))
        self.assertEqual(list(user.groups.values_list("pk", flat=True)), group_ids)
        self.assertEqual(user.employee_profile.current_assignment.pk, assignment_id)
        self.assertEqual(self.client.get(reset_url).status_code, 400)

    def test_activation_link_cannot_be_used_as_password_reset_and_active_is_not_eligible(self):
        self._activate()
        previous_token = EmployeeActivationToken.objects.get(user=self.profile.user)
        previous_count = EmailOutbox.objects.filter(recipient=self.profile.user).count()
        response = self.client.post(reverse("request-activation-link"), {"email": self.profile.user.email})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailOutbox.objects.filter(recipient=self.profile.user).count(), previous_count)
        previous_token.refresh_from_db()
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        activation_url = reverse("employee-activate", args=[
            urlsafe_base64_encode(force_bytes(self.profile.user_id)), self.profile._activation_token,
        ])
        self.assertEqual(self.client.get(activation_url).status_code, 400)

    def test_new_reset_request_invalidates_older_reset_link(self):
        user = self._activate()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("password-reset-request"), {"email": user.email})
        first_link = self._reset_url(mail.outbox[-1])
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("password-reset-request"), {"email": user.email})
        second_link = self._reset_url(mail.outbox[-1])
        self.assertNotEqual(first_link, second_link, list(EmailOutbox.objects.filter(message_type=EmailOutbox.MessageType.PASSWORD_RESET).values_list("queued_at", "status", "latest_failure_reason")))
        self.assertEqual(self.client.get(first_link).status_code, 400)
        self.assertEqual(self.client.get(second_link).status_code, 200)

    def test_obsolete_failed_reset_email_is_not_retried_after_new_request(self):
        from unittest.mock import patch
        user = self._activate()
        with patch("apps.organization.activation_email.send_dms_email", side_effect=OSError("temporary SMTP outage")):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("password-reset-request"), {"email": user.email})
        failed = EmailOutbox.objects.filter(message_type=EmailOutbox.MessageType.PASSWORD_RESET).get()
        self.assertEqual(failed.status, EmailOutbox.Status.FAILED)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("password-reset-request"), {"email": user.email})
        sent_count = len(mail.outbox)
        from apps.organization.activation_email import deliver_activation_email
        deliver_activation_email(failed.pk)
        self.assertEqual(len(mail.outbox), sent_count)
        failed.refresh_from_db()
        self.assertIn("superseded", failed.latest_failure_reason.lower())

    def test_password_reset_delivery_errors_do_not_persist_recovery_urls(self):
        from unittest.mock import patch
        user = self._activate()
        secret_marker = "sensitive-reset-token"
        with patch("apps.organization.activation_email.send_dms_email", side_effect=RuntimeError(
            f"SMTP rejected https://dms.example.test/accounts/password-reset/uid/{secret_marker}/"
        )):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("password-reset-request"), {"email": user.email})
        outbox = EmailOutbox.objects.filter(message_type=EmailOutbox.MessageType.PASSWORD_RESET).get()
        self.assertEqual(outbox.status, EmailOutbox.Status.FAILED)
        self.assertNotIn(secret_marker, outbox.latest_failure_reason)
        self.assertNotIn("https://dms.example.test", outbox.latest_failure_reason)

    def test_account_throttle_is_persistent_and_does_not_reveal_eligibility(self):
        for _ in range(4):
            response = self.client.post(reverse("request-activation-link"), {"email": "absent@example.test"})
            self.assertContains(response, "If the address matches an eligible")
        from apps.organization.models import AuthRecoveryThrottle
        self.assertEqual(AuthRecoveryThrottle.objects.filter(key_hash__isnull=False).count(), 2)

    def test_superseded_activation_retry_does_not_rotate_or_send_an_old_invitation(self):
        from unittest.mock import patch
        from apps.organization.activation_email import deliver_activation_email, resend_activation_email
        with patch("apps.organization.activation_email.send_dms_email", side_effect=OSError("temporary SMTP outage")):
            with self.captureOnCommitCallbacks(execute=True):
                profile = self.profile
                original = EmailOutbox.objects.get(recipient=profile.user)
                deliver_activation_email(original.pk, raw_token=profile._activation_token)
        original.refresh_from_db()
        self.assertEqual(original.status, EmailOutbox.Status.FAILED)
        with self.captureOnCommitCallbacks(execute=True):
            resend_activation_email(user=profile.user, actor=self.actor)
        current_hash = EmployeeActivationToken.objects.get(user=profile.user).token_hash
        mail_count = len(mail.outbox)
        deliver_activation_email(original.pk)
        self.assertEqual(len(mail.outbox), mail_count)
        self.assertEqual(EmployeeActivationToken.objects.get(user=profile.user).token_hash, current_hash)
        original.refresh_from_db()
        self.assertIn("superseded", original.latest_failure_reason.lower())
