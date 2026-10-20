from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import EmailOutbox
from apps.organization.models import Department, EmployeeAssignment, EmployeeProfile, Position
from apps.organization.services import onboard_employee


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="dms@example.test",
    DMS_BASE_URL="https://dms.example.test",
)
class EmployeeOnboardingWizardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("wizard-admin", "admin@example.test", "Admin-Password!2026")
        self.admin_profile = EmployeeProfile.objects.create(
            user=self.admin, staff_id="WIZARD-ADMIN", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.admin.groups.add(Group.objects.get(name="DMS Administrator"))
        self.department = Department.objects.create(code="WIZ-OPS", name="Wizard Operations")
        self.position = Position.objects.create(code="WIZ-STAFF", title="Operations Officer", department=self.department)
        EmployeeAssignment.objects.create(
            employee=self.admin_profile, department=self.department, position=self.position,
            start_date=timezone.localdate() - timedelta(days=1),
        )
        self.standard_group = Group.objects.get(name="DMS Standard User")
        self.client.force_login(self.admin)

    def payload(self, **overrides):
        data = {
            "account_mode": "create",
            "first_name": "Alkasim",
            "last_name": "Mohammed",
            "email": "alkasim@example.test",
            "staff_id": "WIZ-001",
            "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.INACTIVE,
            "department": str(self.department.pk),
            "position": str(self.position.pk),
            "supervisor": "",
            "start_date": timezone.localdate().isoformat(),
            "roles": [str(self.standard_group.pk)],
        }
        data.update(overrides)
        return data

    def test_wizard_has_three_steps_and_allows_editable_username(self):
        response = self.client.get(reverse("organization:employee-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account identity")
        self.assertContains(response, "Organisation and access")
        self.assertContains(response, "Review and onboard")
        self.assertContains(response, "The username is filled from the employee name")
        self.assertContains(response, 'name="username"')
        self.assertContains(response, '@input="markUsernameEdited()"')
        self.assertContains(response, '@input="syncSuggestedUsername($event)"')
        self.assertContains(response, "Use suggested username")

    def test_rendered_wizard_uses_cache_busted_registered_alpine_component(self):
        response = self.client.get(reverse("organization:employee-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'src="/static/js/app.js?v=onboarding-next-20261016"')
        self.assertContains(response, 'href="/static/apgc/css/dms-portal.css?v=onboarding-review-20261016"')
        self.assertContains(response, 'x-data="employeeOnboardingWizard({initialStep: 1, accountMode: \'create\'})"')
        self.assertContains(response, 'x-model="formValues.email"')
        self.assertContains(response, 'x-model="formValues.department"')
        self.assertContains(response, 'x-model="formValues.position"')
        self.assertContains(response, 'x-model="formValues.supervisor"')
        self.assertContains(response, 'x-model="formValues.roles"')
        self.assertContains(response, 'x-text="accountEmail()"')
        self.assertContains(response, "selectedLabel('position')")
        self.assertContains(response, "selectedItems('roles')")
        self.assertContains(response, 'data-wizard-step="1"')
        self.assertContains(response, 'data-wizard-step="2"')
        self.assertContains(response, 'data-wizard-step="3"')
        self.assertContains(response, "Edit Identity")
        self.assertContains(response, "Edit Organisation")

        html = response.content.decode()
        self.assertEqual(html.count('name="email"'), 1)
        self.assertIn('@wizard-next="next()"', html)
        self.assertIn('type="button" x-show="currentStep < 3" @click="$dispatch(\'wizard-next\')"', html)
        self.assertIn('id="onboarding-client-error-email"', html)
        self.assertIn('id="onboarding-client-error-staff_id"', html)
        self.assertIn('@click="$dispatch(\'wizard-step\', 1)"', html)

        app_source = Path(settings.BASE_DIR, "static", "js", "app.js").read_text(encoding="utf-8")
        self.assertIn("Alpine.data('employeeOnboardingWizard'", app_source)
        self.assertIn("formValues[name]", app_source)
        self.assertIn("['first_name', 'last_name', 'email']", app_source)
        self.assertIn("field.focus()", app_source)
        self.assertIn("field.getClientRects().length > 0", app_source)
        self.assertIn("this.focusInvalid(invalid, invalid.validationMessage", app_source)
        self.assertIn("step === this.currentStep + 1 && this.validateCurrentStep()", app_source)
        self.assertIn("document.getElementById('employee-onboarding-form')", app_source)

    def test_new_employee_gets_generated_username_unusable_password_and_activation_delivery(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("organization:employee-create"), self.payload(username="admin-choice"))

        self.assertEqual(response.status_code, 200)
        profile = EmployeeProfile.objects.get(staff_id="WIZ-001")
        self.assertEqual(profile.user.username, "admin-choice")
        self.assertEqual(profile.user.email, "alkasim@example.test")
        self.assertFalse(profile.user.has_usable_password())
        self.assertEqual(profile.current_assignment.department, self.department)
        self.assertIn(self.standard_group, profile.user.groups.all())
        self.assertContains(response, "Employee created successfully.")
        self.assertContains(response, "Activation available")
        self.assertContains(response, "Send a fresh activation link")
        self.assertEqual(EmailOutbox.objects.get(recipient=profile.user).status, EmailOutbox.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotContains(response, response.context["profile"]._activation_token)

    def test_manual_username_survives_wizard_review_and_persistence(self):
        data = self.payload(first_name="Abubakar", last_name="Alhaji Mohammed", username="abubakar.alhaji")
        response = self.client.get(reverse("organization:employee-create"))
        self.assertContains(response, 'x-text="formValues.username"')
        self.assertContains(response, 'x-model="formValues.username"')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("organization:employee-create"), data)
        profile = EmployeeProfile.objects.get(staff_id="WIZ-001")
        self.assertEqual(profile.user.username, "abubakar.alhaji")
        self.assertEqual(profile.user.get_full_name(), "Abubakar Alhaji Mohammed")
        self.assertIn("abubakar.alhaji", mail.outbox[-1].body)
        self.assertNotIn("abubakar.alhaji.mohammed", mail.outbox[-1].body)

    def test_invalid_position_returns_to_organisation_step_and_retains_email(self):
        data = self.payload(position="")

        response = self.client.post(reverse("organization:employee-create"), data)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["wizard_initial_step"], 2)
        self.assertContains(response, "Please correct the following")
        self.assertContains(response, "Position:")
        self.assertContains(response, 'value="alkasim@example.test"')
        self.assertFalse(EmployeeProfile.objects.filter(staff_id="WIZ-001").exists())

    def test_repeated_submission_does_not_create_duplicate_employee_or_user(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(reverse("organization:employee-create"), self.payload())
        second = self.client.post(reverse("organization:employee-create"), self.payload())

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(EmployeeProfile.objects.filter(staff_id="WIZ-001").count(), 1)
        self.assertEqual(get_user_model().objects.filter(email="alkasim@example.test").count(), 1)

    def test_username_collision_includes_unlinked_users(self):
        User = get_user_model()
        User.objects.create_user("alkasim.mohammed", "unlinked@example.test", "existing-password")

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("organization:employee-create"), self.payload())

        self.assertEqual(response.status_code, 200)
        profile = EmployeeProfile.objects.get(staff_id="WIZ-001")
        self.assertEqual(profile.user.username, "alkasim.mohammed2")

    def test_generated_username_excludes_middle_names(self):
        profile = onboard_employee(
            actor=self.admin, username=None, first_name="Ibrahim", last_name="Abdullahi",
            email="ibrahim@example.test", staff_id="WIZ-MIDDLE",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
            department=self.department, position=self.position, roles=[self.standard_group],
            start_date=timezone.localdate(),
        )
        self.assertEqual(profile.user.username, "ibrahim.abdullahi")

    def test_linked_account_keeps_username_password_and_email(self):
        User = get_user_model()
        linked = User.objects.create_user("existing.identity", "saved@example.test", "Existing-Password!2026")
        linked.groups.add(self.standard_group)
        password_hash = linked.password
        data = self.payload(
            account_mode="link", existing_user=str(linked.pk), first_name="", last_name="",
            email="submitted@example.test", staff_id="WIZ-LINKED", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )

        response = self.client.post(reverse("organization:employee-create"), data)

        self.assertEqual(response.status_code, 200)
        profile = EmployeeProfile.objects.get(staff_id="WIZ-LINKED")
        linked.refresh_from_db()
        self.assertEqual(profile.user_id, linked.pk)
        self.assertEqual(linked.username, "existing.identity")
        self.assertEqual(linked.password, password_hash)
        self.assertEqual(linked.email, "saved@example.test")
        self.assertContains(response, "Existing credentials were preserved")

    def test_privileged_and_unrelated_accounts_are_not_offered_for_linking(self):
        User = get_user_model()
        ordinary = User.objects.create_user("linkable", "linkable@example.test", "password")
        ordinary.groups.add(self.standard_group)
        elevated = User.objects.create_user("unlinked-admin", "elevated@example.test", "password")
        elevated.groups.add(Group.objects.get(name="DMS Administrator"))
        unrelated = User.objects.create_user("unrelated", "other@example.test", "password")
        staff = User.objects.create_user("staff-user", "staff@example.test", "password", is_staff=True)

        response = self.client.get(reverse("organization:employee-create"))

        self.assertContains(response, "linkable@example.test")
        self.assertNotContains(response, "elevated@example.test")
        self.assertNotContains(response, "other@example.test")
        self.assertNotContains(response, "staff@example.test")

    def test_duplicate_email_requires_link_or_administrator_review(self):
        User = get_user_model()
        existing = User.objects.create_user("email-match", "duplicate@example.test", "password")
        existing.groups.add(self.standard_group)

        response = self.client.post(reverse("organization:employee-create"), self.payload(email="DUPLICATE@example.test"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Choose Link an existing account")
        self.assertFalse(EmployeeProfile.objects.filter(staff_id="WIZ-001").exists())

    def test_username_normalization_unicode_punctuation_fallback_and_length(self):
        common = {
            "actor": self.admin, "staff_id": "WIZ-NAME", "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.INACTIVE, "department": self.department,
            "position": self.position, "start_date": timezone.localdate(), "email": "names@example.test",
        }
        accented = onboard_employee(**common, first_name="  Émilie-Marie ", last_name="O'Connor")
        self.assertEqual(accented.user.username, "emilie.marie.o.connor")

        unicode_only = onboard_employee(
            **{**common, "staff_id": "WIZ-FALLBACK", "email": "fallback@example.test"},
            first_name="山田", last_name="太郎",
        )
        self.assertEqual(unicode_only.user.username, "employee")

        long_name = onboard_employee(
            **{**common, "staff_id": "WIZ-LONG", "email": "long@example.test"},
            first_name="A" * 180, last_name="B" * 180,
        )
        username_field = get_user_model()._meta.get_field("username")
        self.assertLessEqual(len(long_name.user.username), username_field.max_length)
