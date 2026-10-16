from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.organization.authentication import DMS_ADMIN_GROUP
from apps.organization.models import (
    Department,
    EmployeeActivationToken,
    EmployeeProfile,
    OrganizationAuditEvent,
    Position,
)
from apps.organization.services import activation_eligibility, onboard_employee


class EmployeeOnboardingAndActivationTests(TestCase):
    password = "F4hd-Activation-Password!2026"

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="onboarding-admin", email="onboard-admin@example.test", password="bootstrap-password",
        )
        self.department = Department.objects.create(code="ONB-OPS", name="Operations")
        self.position = Position.objects.create(code="ONB-EA", title="Executive Assistant", department=self.department)
        self.admin_profile = EmployeeProfile.objects.create(
            user=self.admin, staff_id="ONB-ADMIN", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.admin.groups.add(Group.objects.get(name=DMS_ADMIN_GROUP))
        self.admin_assignment = self._assignment(self.admin_profile)

    def _assignment(self, profile, *, department=None, position=None, supervisor=None):
        from apps.organization.models import EmployeeAssignment

        return EmployeeAssignment.objects.create(
            employee=profile,
            department=department or self.department,
            position=position or self.position,
            supervisor=supervisor,
            start_date=timezone.localdate() - timedelta(days=1),
        )

    def create_employee(self, *, role="DMS Standard User", account_status=EmployeeProfile.AccountStatus.INACTIVE,
                        department=None, position=None, supervisor=None, username="new-staff"):
        roles = [Group.objects.get(name=role)] if role else []
        return onboard_employee(
            actor=self.admin,
            username=username,
            first_name="Fahd",
            last_name="Test",
            email=f"{username}@example.test",
            staff_id=f"STAFF-{username}",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=account_status,
            department=department or self.department,
            position=position or self.position,
            supervisor=supervisor,
            start_date=timezone.localdate(),
            roles=roles,
        )

    def activation_url(self, profile):
        uid = urlsafe_base64_encode(force_bytes(profile.user_id))
        return reverse("employee-activate", args=[uid, profile._activation_token])

    def test_authorized_dms_administrator_can_open_employee_creation(self):
        User = get_user_model()
        admin_user = User.objects.create_user("role-admin", password="role-admin-password")
        admin_profile = EmployeeProfile.objects.create(
            user=admin_user, staff_id="ONB-ROLE-ADMIN", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        admin_user.groups.add(Group.objects.get(name=DMS_ADMIN_GROUP))
        self._assignment(admin_profile)
        self.client.force_login(admin_user)

        response = self.client.get(reverse("organization:employee-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Account identity")

    def test_unauthorized_dms_user_cannot_open_employee_creation(self):
        User = get_user_model()
        user = User.objects.create_user("regular", password="user-password")
        profile = EmployeeProfile.objects.create(
            user=user, staff_id="ONB-REG", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        user.groups.add(Group.objects.get(name="DMS Standard User"))
        self._assignment(profile)
        self.client.force_login(user)

        response = self.client.get(reverse("organization:employee-create"))

        self.assertEqual(response.status_code, 403)

    def test_bootstrap_superuser_can_provision_only_the_first_dms_administrator(self):
        User = get_user_model()
        bootstrap = User.objects.create_superuser("bootstrap", "bootstrap@example.test", "bootstrap-password")
        self.admin.groups.remove(Group.objects.get(name=DMS_ADMIN_GROUP))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("bootstrap-login")).status_code, 200)
        non_superuser = User.objects.create_user("not-bootstrap", password="password")
        rejected = self.client.post(reverse("bootstrap-login"), {
            "username": non_superuser.username, "password": "password",
        })
        self.assertEqual(rejected.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        login_response = self.client.post(reverse("bootstrap-login"), {
            "username": bootstrap.username, "password": "bootstrap-password",
        })
        self.assertEqual(login_response.status_code, 302)
        onboarding_page = self.client.get(reverse("bootstrap-onboard"))
        self.assertContains(onboarding_page, 'data-confirm-kind="bootstrap"')
        self.assertContains(onboarding_page, "Create Administrator")
        self.assertContains(onboarding_page, "Create first DMS Administrator")
        payload = {
            "first_name": "First", "last_name": "Administrator",
            "email": "first-admin@example.test", "staff_id": "FIRST-ADMIN",
            "department": self.department.pk, "position": self.position.pk,
            "supervisor": "", "start_date": timezone.localdate().isoformat(),
        }
        created = self.client.post(reverse("bootstrap-onboard"), payload)
        self.assertEqual(created.status_code, 200)
        first_admin = EmployeeProfile.objects.get(staff_id="FIRST-ADMIN")
        self.assertIn(Group.objects.get(name=DMS_ADMIN_GROUP), first_admin.user.groups.all())
        self.assertFalse(first_admin.user.has_usable_password())
        self.assertEqual(self.client.get(reverse("bootstrap-login")).status_code, 302)
        self.assertEqual(self.client.get(reverse("bootstrap-onboard")).status_code, 404)
        first_admin.user.groups.remove(Group.objects.get(name=DMS_ADMIN_GROUP))
        self.assertEqual(self.client.get(reverse("bootstrap-login")).status_code, 302)

    def test_mismatched_position_department_rolls_back_whole_onboarding(self):
        other_department = Department.objects.create(code="ONB-LEGAL", name="Legal")
        user_count = get_user_model().objects.count()

        with self.assertRaises(ValidationError):
            self.create_employee(
                username="mismatch", department=other_department, position=self.position,
            )

        User = get_user_model()
        self.assertEqual(User.objects.count(), user_count)
        self.assertFalse(EmployeeProfile.objects.filter(staff_id="STAFF-mismatch").exists())

    def test_optional_supervisor_and_role_are_recorded_separately_from_position(self):
        profile = self.create_employee(username="with-supervisor", supervisor=self.admin_profile)

        assignment = profile.current_assignment
        self.assertEqual(assignment.position, self.position)
        self.assertEqual(assignment.supervisor, self.admin_profile)
        self.assertEqual(list(profile.user.groups.values_list("name", flat=True)), ["DMS Standard User"])
        self.assertTrue(hasattr(profile, "_activation_token"))

    def test_new_account_stores_only_a_hash_of_its_activation_token(self):
        profile = self.create_employee(username="hashed-token")
        stored = EmployeeActivationToken.objects.get(user=profile.user)

        self.assertFalse(profile.user.has_usable_password())
        self.assertNotEqual(stored.token_hash, profile._activation_token)
        self.assertEqual(len(stored.token_hash), 64)
        self.assertGreater(stored.expires_at, timezone.now())

    def test_activation_sets_password_and_enables_otherwise_eligible_employee(self):
        profile = self.create_employee(username="activate-ready")

        response = self.client.post(self.activation_url(profile), {
            "new_password1": self.password, "new_password2": self.password,
        })

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.check_password(self.password))
        self.assertEqual(profile.account_status, EmployeeProfile.AccountStatus.ACTIVE)
        self.assertTrue(profile.user.is_active)
        self.assertTrue(OrganizationAuditEvent.objects.filter(
            subject_user=profile.user,
            action=OrganizationAuditEvent.Action.CREDENTIALS_ESTABLISHED,
        ).exists())
        self.assertTrue(self.client.login(username=profile.user.username, password=self.password))

    def test_enabled_employee_without_password_can_complete_initial_setup(self):
        profile = self.create_employee(
            username="enabled-awaiting-credentials",
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        profile.user.refresh_from_db()
        assignment_id = profile.current_assignment.pk
        role_ids = set(profile.user.groups.values_list("pk", flat=True))
        self.assertTrue(profile.user.is_active)
        self.assertFalse(profile.user.has_usable_password())
        self.assertTrue(activation_eligibility(profile.user))

        response = self.client.post(self.activation_url(profile), {
            "new_password1": self.password, "new_password2": self.password,
        })

        self.assertEqual(response.status_code, 200)
        profile.refresh_from_db()
        profile.user.refresh_from_db()
        self.assertEqual(profile.account_status, EmployeeProfile.AccountStatus.ACTIVE)
        self.assertTrue(profile.user.is_active)
        self.assertTrue(profile.user.check_password(self.password))
        self.assertEqual(profile.current_assignment.pk, assignment_id)
        self.assertEqual(set(profile.user.groups.values_list("pk", flat=True)), role_ids)
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        self.assertIsNotNone(activation.used_at)

    def test_disabled_suspended_separated_unassigned_and_unroled_accounts_are_ineligible(self):
        suspended = self.create_employee(
            username="suspended-no-password",
            account_status=EmployeeProfile.AccountStatus.SUSPENDED,
        )
        self.assertFalse(activation_eligibility(suspended.user))

        disabled = self.create_employee(
            username="disabled-active-state",
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        get_user_model().objects.filter(pk=disabled.user_id).update(is_active=False)
        disabled.user.refresh_from_db()
        self.assertFalse(activation_eligibility(disabled.user))

        separated = self.create_employee(username="separated-no-password")
        separated.employment_status = EmployeeProfile.EmploymentStatus.SEPARATED
        separated.account_status = EmployeeProfile.AccountStatus.INACTIVE
        separated.save()
        self.assertFalse(activation_eligibility(separated.user))

        unassigned = self.create_employee(username="unassigned-no-password")
        unassigned.assignments.update(end_date=timezone.localdate())
        self.assertFalse(activation_eligibility(unassigned.user))

        unroled = self.create_employee(username="unroled-no-password", role=None)
        self.assertFalse(activation_eligibility(unroled.user))

    def test_activation_token_is_single_use(self):
        profile = self.create_employee(username="single-use")
        url = self.activation_url(profile)
        data = {"new_password1": self.password, "new_password2": self.password}

        activation_page = self.client.get(url)
        self.assertContains(activation_page, 'data-confirm-kind="activation"')
        self.assertContains(activation_page, "Set Password")
        self.assertContains(activation_page, "Cancel")

        first = self.client.post(url, data)
        second = self.client.post(url, {"new_password1": "Another-Password!2026", "new_password2": "Another-Password!2026"})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        self.assertIsNotNone(activation.used_at)
        profile.user.refresh_from_db()
        self.assertTrue(profile.user.check_password(self.password))

    def test_expired_activation_token_is_rejected(self):
        profile = self.create_employee(username="expired-token")
        activation = EmployeeActivationToken.objects.get(user=profile.user)
        activation.expires_at = timezone.now() - timedelta(seconds=1)
        activation.save(update_fields=["expires_at"])

        response = self.client.get(self.activation_url(profile))

        self.assertEqual(response.status_code, 400)
        profile.user.refresh_from_db()
        self.assertFalse(profile.user.has_usable_password())

    def test_employee_without_approved_dms_role_is_not_offered_activation(self):
        profile = self.create_employee(role=None, username="no-role")
        profile.refresh_from_db()
        profile.user.refresh_from_db()
        self.assertFalse(EmployeeActivationToken.objects.filter(user=profile.user).exists())
        self.assertFalse(profile.user.has_usable_password())
        self.assertEqual(profile.account_status, EmployeeProfile.AccountStatus.INACTIVE)
        self.assertFalse(self.client.login(username=profile.user.username, password=self.password))

    def test_superuser_without_dms_identity_cannot_use_normal_login(self):
        User = get_user_model()
        unprovisioned = User.objects.create_superuser("unprovisioned", "unprovisioned@example.test", "password")

        response = self.client.post(reverse("dms-login"), {
            "username": unprovisioned.username, "password": "password",
        })

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
