from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import Document
from apps.documents.services import create_memo
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


class DMSAuthenticationAndRoleTests(TestCase):
    def setUp(self):
        self.department = Department.objects.create(code="AUTH-IT", name="Authentication Test")
        self.position = Position.objects.create(code="AUTH-EMP", title="Test employee", department=self.department)

    def make_employee(
        self,
        username,
        staff_id,
        *,
        role="DMS Standard User",
        account_status=EmployeeProfile.AccountStatus.ACTIVE,
        employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
        with_assignment=True,
        password="test-password",
    ):
        user = get_user_model().objects.create_user(username=username, password=password, is_staff=False)
        profile = EmployeeProfile.objects.create(
            user=user,
            staff_id=staff_id,
            employment_status=employment_status,
            account_status=account_status,
        )
        if role:
            user.groups.add(Group.objects.get(name=role))
        if with_assignment and employment_status == EmployeeProfile.EmploymentStatus.EMPLOYED:
            create_assignment(
                employee=profile,
                department=self.department,
                position=self.position,
                start_date=timezone.localdate(),
                actor=user,
            )
        return user, profile

    def login_to_dms(self, user, password="test-password"):
        return self.client.post(reverse("dms-login"), {"username": user.username, "password": password})

    def test_active_nonstaff_employee_can_sign_in_and_open_workspace(self):
        user, _ = self.make_employee("standard", "AUTH-001")

        response = self.login_to_dms(user)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("documents:memo-list"))
        self.assertFalse(user.is_staff)
        workspace = self.client.get(reverse("documents:memo-list"))
        self.assertEqual(workspace.status_code, 200)
        self.assertTemplateUsed(workspace, "layouts/dms_authenticated.html")
        self.assertContains(workspace, "Document Management System")
        self.assertContains(workspace, "Internal Memos")
        self.assertContains(workspace, "Notifications")
        self.assertContains(workspace, "No notifications are available.")
        self.assertContains(workspace, f'action="{reverse("dms-logout")}"')
        self.assertContains(workspace, reverse("account-profile"))
        self.assertContains(workspace, reverse("account-settings"))
        self.assertContains(workspace, reverse("account-password-change"))
        self.assertContains(workspace, "Sign out of APGC DMS?")

    def test_dms_login_is_a_dedicated_authentication_page(self):
        response = self.client.get(reverse("dms-login"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "layouts/authentication.html")
        self.assertContains(response, "APGC Digital Portal")
        self.assertContains(response, "name=\"username\"")
        self.assertContains(response, "name=\"password\"")
        self.assertContains(response, "name=\"csrfmiddlewaretoken\"")
        self.assertContains(response, "Back to APGC website")
        self.assertContains(response, "Alliance Power Generation Company")
        self.assertNotContains(response, 'id="public-navigation"')
        self.assertNotContains(response, "Quick Links")
        self.assertNotContains(response, "Sign in to your workspace.")

    def test_failed_login_keeps_user_anonymous(self):
        user, _ = self.make_employee("invalid-password", "AUTH-014")

        response = self.login_to_dms(user, password="incorrect")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter a correct username and password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_account_pages_are_dms_protected_and_show_authoritative_profile(self):
        profile_user, profile = self.make_employee("profile-user", "AUTH-015")
        urls = (
            reverse("account-profile"),
            reverse("account-settings"),
            reverse("account-password-change"),
        )
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("dms-login"), response["Location"])

        self.client.force_login(profile_user)
        profile_response = self.client.get(reverse("account-profile"))
        settings_response = self.client.get(reverse("account-settings"))
        password_response = self.client.get(reverse("account-password-change"))

        self.assertEqual(profile_response.status_code, 200)
        self.assertContains(profile_response, profile.staff_id)
        self.assertContains(profile_response, "Test employee")
        self.assertContains(profile_response, "Authentication Test")
        self.assertContains(profile_response, "DMS Standard User")
        self.assertNotContains(profile_response, "Save changes")
        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, reverse("account-password-change"))
        self.assertContains(settings_response, "Account status")
        self.assertEqual(password_response.status_code, 200)
        self.assertContains(password_response, "Current password")
        self.assertContains(password_response, "New password")
        self.assertContains(password_response, "Confirm new password")
        self.assertContains(password_response, 'method="post"')
        self.assertContains(password_response, 'name="csrfmiddlewaretoken"')

    def test_password_change_uses_django_validation_and_preserves_session(self):
        user, _ = self.make_employee("password-change", "AUTH-016")
        self.client.force_login(user)
        password_url = reverse("account-password-change")
        new_password = "another-very-strong-DMS-password-2026"

        mismatch_response = self.client.post(password_url, {
            "old_password": "test-password",
            "new_password1": new_password,
            "new_password2": "different-strong-DMS-password-2026",
        })
        self.assertEqual(mismatch_response.status_code, 200)
        self.assertIn("The two password fields didn’t match.", mismatch_response.context["form"].errors["new_password2"])
        user.refresh_from_db()
        self.assertTrue(user.check_password("test-password"))

        weak_response = self.client.post(password_url, {
            "old_password": "test-password",
            "new_password1": "password",
            "new_password2": "password",
        })
        self.assertEqual(weak_response.status_code, 200)
        self.assertTrue(weak_response.context["form"].errors["new_password2"])

        invalid_current_response = self.client.post(password_url, {
            "old_password": "wrong-current-password",
            "new_password1": new_password,
            "new_password2": new_password,
        })
        self.assertEqual(invalid_current_response.status_code, 200)
        self.assertTrue(invalid_current_response.context["form"].errors["old_password"])
        self.assertNotContains(invalid_current_response, "wrong-current-password")

        get_response = self.client.get(password_url)
        self.assertEqual(get_response.status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password("test-password"))

        response = self.client.post(password_url, {
            "old_password": "test-password",
            "new_password1": new_password,
            "new_password2": new_password,
        })

        self.assertRedirects(response, reverse("account-settings"))
        self.assertIn("_auth_user_id", self.client.session)
        user.refresh_from_db()
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password("test-password"))
        self.assertEqual(self.client.get(reverse("account-settings")).status_code, 200)
        self.client.logout()
        old_login = self.login_to_dms(user, password="test-password")
        self.assertEqual(old_login.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        new_login = self.login_to_dms(user, password=new_password)
        self.assertEqual(new_login.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_password_change_requires_csrf(self):
        user, _ = self.make_employee("csrf-password-change", "AUTH-017")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        url = reverse("account-password-change")
        response = client.get(url)
        token = response.cookies["csrftoken"].value
        data = {
            "old_password": "test-password",
            "new_password1": "another-very-strong-DMS-password-2026",
            "new_password2": "another-very-strong-DMS-password-2026",
        }

        self.assertEqual(client.post(url, data).status_code, 403)
        valid_response = client.post(url, data, HTTP_X_CSRFTOKEN=token)

        self.assertRedirects(valid_response, reverse("account-settings"))
        user.refresh_from_db()
        self.assertTrue(user.check_password(data["new_password1"]))

    def test_authenticated_employee_landing_renders_dms_logout_navbar(self):
        user, _ = self.make_employee("navbar-user", "AUTH-012")
        self.client.force_login(user)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'action="{reverse("dms-logout")}"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, "Sign out of APGC DMS?")
        self.assertContains(response, "Cancel")
        self.assertContains(response, 'type="button"')

    def test_dms_logout_reverses_and_requires_csrf_protected_post(self):
        user, _ = self.make_employee("logout-user", "AUTH-013")
        logout_url = reverse("dms-logout")
        self.assertEqual(logout_url, "/accounts/logout/")

        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        landing = client.get(reverse("home"))
        csrf_token = landing.cookies["csrftoken"].value

        self.assertEqual(client.get(logout_url).status_code, 405)
        self.assertEqual(client.post(logout_url).status_code, 403)
        self.assertIn("_auth_user_id", client.session)

        response = client.post(logout_url, HTTP_X_CSRFTOKEN=csrf_token)

        self.assertRedirects(response, reverse("dms-login"))
        self.assertNotIn("_auth_user_id", client.session)

    def test_nonstaff_dms_employee_does_not_gain_django_admin_access(self):
        user, _ = self.make_employee("nonstaff", "AUTH-002")
        self.login_to_dms(user)

        response = self.client.get(reverse("admin:index"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])

    def test_login_requires_an_active_employee_profile_current_assignment_and_dms_role(self):
        no_role, _ = self.make_employee("norole", "AUTH-003", role=None)
        no_assignment, _ = self.make_employee("noassignment", "AUTH-004", with_assignment=False)

        for user in (no_role, no_assignment):
            with self.subTest(user=user.username):
                response = self.login_to_dms(user)
                self.assertEqual(response.status_code, 200)
                self.assertFalse("_auth_user_id" in self.client.session)

    def test_inactive_employee_cannot_sign_in(self):
        user, _ = self.make_employee(
            "inactive",
            "AUTH-005",
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
        )

        response = self.login_to_dms(user)

        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertFalse("_auth_user_id" in self.client.session)

    def test_separated_employee_cannot_sign_in_even_if_user_active_flag_is_inconsistent(self):
        user, profile = self.make_employee(
            "separated",
            "AUTH-006",
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
            employment_status=EmployeeProfile.EmploymentStatus.SEPARATED,
        )
        # Simulate an inconsistent legacy row; DMS access checks the profile status too.
        get_user_model().objects.filter(pk=user.pk).update(is_active=True)
        user.refresh_from_db()
        self.assertEqual(profile.employment_status, EmployeeProfile.EmploymentStatus.SEPARATED)

        response = self.login_to_dms(user)

        self.assertEqual(response.status_code, 200)
        self.assertFalse("_auth_user_id" in self.client.session)

    def test_unusable_password_does_not_authenticate_employee(self):
        user, _ = self.make_employee("passwordless", "AUTH-007")
        user.set_unusable_password()
        user.save(update_fields=["password"])

        response = self.login_to_dms(user)

        self.assertEqual(response.status_code, 200)
        self.assertFalse("_auth_user_id" in self.client.session)

    def test_dms_administrator_role_is_not_django_admin_access(self):
        user, _ = self.make_employee("dms-admin", "AUTH-008", role="DMS Administrator")

        login_response = self.login_to_dms(user)
        admin_response = self.client.get(reverse("admin:index"))

        self.assertEqual(login_response.status_code, 302)
        self.assertFalse(user.is_staff)
        self.assertEqual(admin_response.status_code, 302)
        self.assertIn(reverse("admin:login"), admin_response["Location"])

    def test_approved_role_groups_receive_only_existing_enforced_permissions(self):
        expected = {
            "DMS Administrator": {("organization", "manage_roles")},
            "DMS Workflow Administrator": {("documents", "reassign_workflow_tasks")},
            "DMS Department Manager/HOD": set(),
            "DMS Document Officer": set(),
            "DMS Records/Registry Officer": set(),
            "DMS Standard User": set(),
            "DMS Viewer": set(),
        }
        for name, permissions in expected.items():
            with self.subTest(group=name):
                group = Group.objects.get(name=name)
                actual = set(group.permissions.values_list("content_type__app_label", "codename"))
                self.assertSetEqual(actual, permissions)

    def test_dms_viewer_is_read_only_and_cannot_create_through_direct_url(self):
        viewer, _ = self.make_employee("viewer", "AUTH-009", role="DMS Viewer")
        self.client.force_login(viewer)

        response = self.client.post(reverse("documents:memo-create"), {
            "to": "Director",
            "through": "",
            "cc": "",
            "subject": "Unauthorized draft",
            "classification": Document.Classification.INTERNAL,
            "body": "This must not be saved.",
            "action": "save",
        })

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Document.objects.filter(created_by=viewer).exists())

    def test_dms_role_does_not_bypass_document_visibility_or_organization_permission(self):
        owner, _ = self.make_employee("owner", "AUTH-010")
        other_user, _ = self.make_employee("other", "AUTH-011")
        document = create_memo(
            owner=owner,
            values={
                "to": "Director", "through": "", "cc": "", "subject": "Private",
                "body": "Private test memo.", "classification": Document.Classification.INTERNAL,
            },
        )
        self.client.force_login(other_user)

        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("organization:employee-list")).status_code, 403)
