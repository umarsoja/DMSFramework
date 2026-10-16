from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import Document, WorkflowDecision, WorkflowTask
from apps.documents.services import create_memo, decide_review, reassign_task
from apps.organization.actors import resolve_workflow_actor
from apps.organization.models import Department, EmployeeAssignment, EmployeeProfile, OrganizationAuditEvent, Position
from apps.organization.services import create_assignment, onboard_employee, save_department, save_position, update_employee


class OrganizationManagementTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(username="org-admin", email="admin@example.test", password="pw")
        self.creator_user = User.objects.create_user(username="creator", first_name="Casey", password="pw")
        self.reviewer_user = User.objects.create_user(username="reviewer", first_name="Riley", password="pw")
        self.replacement_user = User.objects.create_user(username="replacement", first_name="Jordan", password="pw")
        self.viewer = User.objects.create_user(username="viewer", password="pw")
        self.department = Department.objects.create(code="D-001", name="Development")
        self.finance = Department.objects.create(code="D-002", name="Finance")
        self.position = Position.objects.create(code="P-001", title="Analyst")
        self.other_position = Position.objects.create(code="P-002", title="Senior Analyst")
        self.creator = EmployeeProfile.objects.create(
            user=self.creator_user, staff_id="STAFF-001", account_status=EmployeeProfile.AccountStatus.ACTIVE
        )
        self.reviewer = EmployeeProfile.objects.create(
            user=self.reviewer_user, staff_id="STAFF-002", account_status=EmployeeProfile.AccountStatus.ACTIVE
        )
        self.replacement = EmployeeProfile.objects.create(
            user=self.replacement_user, staff_id="STAFF-003", account_status=EmployeeProfile.AccountStatus.ACTIVE
        )
        self.admin_profile = EmployeeProfile.objects.create(
            user=self.admin, staff_id="STAFF-ADMIN", account_status=EmployeeProfile.AccountStatus.ACTIVE
        )
        for user, group_name in (
            (self.admin, "DMS Administrator"),
            (self.creator_user, "DMS Standard User"),
            (self.reviewer_user, "DMS Department Manager/HOD"),
            (self.replacement_user, "DMS Document Officer"),
            (self.viewer, "DMS Viewer"),
        ):
            user.groups.add(Group.objects.get(name=group_name))
        today = timezone.localdate()
        self.admin_assignment = create_assignment(
            employee=self.admin_profile, department=self.department, position=self.position,
            start_date=today - timedelta(days=30), actor=self.admin,
        )
        self.reviewer_assignment = create_assignment(
            employee=self.reviewer, department=self.department, position=self.other_position,
            start_date=today - timedelta(days=10), actor=self.admin,
        )
        self.creator_assignment = create_assignment(
            employee=self.creator, department=self.department, position=self.position,
            supervisor=self.reviewer, start_date=today - timedelta(days=20), actor=self.admin,
        )
        self.replacement_assignment = create_assignment(
            employee=self.replacement, department=self.finance, position=self.position,
            start_date=today - timedelta(days=8), actor=self.admin,
        )

    def memo_values(self):
        return {
            "to": "Director",
            "through": "",
            "cc": "",
            "subject": "Department planning",
            "body": "A memo body.",
            "classification": Document.Classification.INTERNAL,
        }

    def make_memo(self, reviewer=None):
        return create_memo(owner=self.creator_user, values=self.memo_values(), reviewer=reviewer)

    def test_profile_is_one_to_one_and_account_state_controls_django_login(self):
        self.assertEqual(self.creator_user.employee_profile, self.creator)
        self.assertTrue(self.creator_user.is_active)
        self.creator.account_status = EmployeeProfile.AccountStatus.SUSPENDED
        self.creator.save(update_fields=["account_status", "updated_at"])
        self.creator_user.refresh_from_db()
        self.assertFalse(self.creator_user.is_active)
        self.creator.account_status = EmployeeProfile.AccountStatus.INACTIVE
        self.creator.save(update_fields=["account_status", "updated_at"])
        self.creator_user.refresh_from_db()
        self.assertFalse(self.creator_user.is_active)

    def test_user_admin_account_status_changes_are_audited_and_employee_status_is_canonical(self):
        from apps.organization.admin import AuditedUserAdmin

        model_admin = AuditedUserAdmin(get_user_model(), AdminSite())
        request = RequestFactory().post("/admin/auth/user/")
        request.user = self.admin
        self.assertIn("is_active", model_admin.get_readonly_fields(request, self.creator_user))

        self.creator_user.is_active = False
        with self.assertRaises(ValidationError):
            model_admin.save_model(request, self.creator_user, form=None, change=True)
        self.creator_user.refresh_from_db()
        self.assertTrue(self.creator_user.is_active)

        self.viewer.is_active = False
        model_admin.save_model(request, self.viewer, form=None, change=True)
        event = OrganizationAuditEvent.objects.get(
            subject_user=self.viewer, action=OrganizationAuditEvent.Action.ACCOUNT_STATUS_CHANGED
        )
        self.assertEqual(event.context["from"], "ACTIVE")
        self.assertEqual(event.context["to"], "INACTIVE")

    def test_staff_id_is_unique(self):
        duplicate = EmployeeProfile(user=self.viewer, staff_id=self.creator.staff_id)
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_department_and_position_codes_are_unique_and_deactivation_is_non_destructive(self):
        with self.assertRaises(ValidationError):
            Department(code=self.department.code, name="Duplicate").full_clean()
        with self.assertRaises(ValidationError):
            Position(code=self.position.code, title="Duplicate").full_clean()
        self.department.is_active = False
        self.department.save(update_fields=["is_active", "updated_at"])
        self.assertTrue(Department.objects.filter(pk=self.department.pk).exists())
        self.assertTrue(EmployeeAssignment.objects.filter(department=self.department).exists())

    def test_assignment_transfer_closes_old_row_and_keeps_history(self):
        today = timezone.localdate()
        old_department_id = self.creator_assignment.department_id
        next_assignment = create_assignment(
            employee=self.creator, department=self.finance, position=self.other_position,
            supervisor=self.reviewer, start_date=today, actor=self.admin,
        )
        self.creator_assignment.refresh_from_db()
        self.assertEqual(self.creator_assignment.end_date, today - timedelta(days=1))
        self.assertEqual(self.creator_assignment.department_id, old_department_id)
        self.assertEqual(self.creator.current_assignment, next_assignment)
        self.assertEqual(next_assignment.supervisor, self.reviewer)
        self.assertEqual(self.creator_assignment.department_name_snapshot, "Development")
        self.assertEqual(self.creator_assignment.position_title_snapshot, "Analyst")

    def test_assignment_creation_rejects_overlaps_and_self_supervision(self):
        today = timezone.localdate()
        overlapping = EmployeeAssignment(employee=self.creator, department=self.finance, position=self.position,
                                         start_date=today - timedelta(days=1))
        with self.assertRaises(ValidationError):
            overlapping.full_clean()
        with self.assertRaises(ValidationError):
            EmployeeAssignment(employee=self.creator, department=self.department, position=self.position,
                               supervisor=self.creator, start_date=today).full_clean()

    def test_assignment_snapshot_fields_are_immutable_and_records_cannot_be_deleted(self):
        self.creator_assignment.position = self.other_position
        with self.assertRaises(ValidationError):
            self.creator_assignment.save()
        with self.assertRaises(ValidationError):
            self.creator_assignment.delete()

    def test_user_onboarding_uses_unusable_password_and_creates_profile_assignment_and_audit(self):
        profile = onboard_employee(
            actor=self.admin, username="new-employee", first_name="Taylor", last_name="Staff",
            email="taylor@example.test", staff_id="STAFF-NEW",
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
            department=self.department, position=self.position, start_date=timezone.localdate(),
        )
        self.assertTrue(profile.user.has_usable_password() is False)
        self.assertFalse(profile.user.is_active)
        self.assertEqual(profile.current_assignment.department, self.department)
        self.assertTrue(OrganizationAuditEvent.objects.filter(subject_user=profile.user,
                        action=OrganizationAuditEvent.Action.USER_CREATED).exists())

    def test_separated_employee_cannot_have_active_account(self):
        with self.assertRaises(ValidationError):
            EmployeeProfile(user=self.viewer, staff_id="STAFF-X",
                            employment_status=EmployeeProfile.EmploymentStatus.SEPARATED,
                            account_status=EmployeeProfile.AccountStatus.ACTIVE).full_clean()

    def test_separation_closes_current_assignment_and_deactivates_account(self):
        end_date = timezone.localdate()
        update_employee(
            actor=self.admin, profile=self.creator, staff_id=self.creator.staff_id,
            employment_status=EmployeeProfile.EmploymentStatus.SEPARATED,
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
            department=self.department, position=self.position, start_date=end_date,
        )
        self.creator.refresh_from_db()
        self.creator_assignment.refresh_from_db()
        self.creator_user.refresh_from_db()
        self.assertEqual(self.creator.employment_status, EmployeeProfile.EmploymentStatus.SEPARATED)
        self.assertEqual(self.creator_assignment.end_date, end_date)
        self.assertIsNone(self.creator.current_assignment)
        self.assertFalse(self.creator_user.is_active)

    def test_inactive_department_or_position_cannot_receive_new_assignment(self):
        self.finance.is_active = False
        self.finance.save(update_fields=["is_active", "updated_at"])
        with self.assertRaises(ValidationError):
            create_assignment(employee=self.creator, department=self.finance, position=self.position,
                              start_date=timezone.localdate(), actor=self.admin)

    def test_department_parent_cycle_is_rejected(self):
        parent = Department.objects.create(code="D-003", name="Parent")
        child = Department.objects.create(code="D-004", name="Child", parent=parent)
        parent.parent = child
        with self.assertRaises(ValidationError):
            parent.full_clean()

    def test_document_keeps_originating_department_after_employee_transfer(self):
        document = self.make_memo()
        self.assertEqual(document.originating_assignment, self.creator_assignment)
        today = timezone.localdate()
        create_assignment(employee=self.creator, department=self.finance, position=self.other_position,
                          start_date=today, actor=self.admin)
        document.refresh_from_db()
        self.assertEqual(document.originating_assignment.department, self.department)
        self.assertEqual(document.originating_assignment.position, self.position)
        self.assertEqual(document.originating_assignment.department_name_snapshot, "Development")
        self.client.force_login(self.creator_user)
        response = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertContains(response, "Development")
        self.assertNotContains(response, "Finance")

    def test_renaming_department_or_position_does_not_rewrite_historical_assignment_labels(self):
        original_department = self.creator_assignment.department_name_snapshot
        original_position = self.creator_assignment.position_title_snapshot
        save_department(actor=self.admin, department=self.department, values={
            "code": self.department.code, "name": "Engineering Group", "description": "",
            "parent": None, "head": None, "is_active": True,
        })
        save_position(actor=self.admin, position=self.position, values={
            "code": self.position.code, "title": "Engineering Analyst", "description": "",
            "department": None, "is_active": True,
        })
        self.creator_assignment.refresh_from_db()
        self.assertEqual(self.creator_assignment.department.name, "Engineering Group")
        self.assertEqual(self.creator_assignment.position.title, "Engineering Analyst")
        self.assertEqual(self.creator_assignment.department_name_snapshot, original_department)
        self.assertEqual(self.creator_assignment.position_title_snapshot, original_position)

    def test_assignment_actor_resolution_for_creator_supervisor_and_department_head(self):
        document = self.make_memo()
        self.department.head = self.reviewer
        self.department.save()
        step = document.document_type.workflows.first().steps.first()
        step.actor_type = step.ActorType.DOCUMENT_CREATOR
        user, assignment = resolve_workflow_actor(step=step, document=document)
        self.assertEqual(user, self.creator_user)
        self.assertEqual(assignment, self.creator_assignment)
        step.actor_type = step.ActorType.CREATOR_SUPERVISOR
        user, assignment = resolve_workflow_actor(step=step, document=document)
        self.assertEqual(user, self.reviewer_user)
        self.assertEqual(assignment, self.reviewer_assignment)
        step.actor_type = step.ActorType.DEPARTMENT_HEAD
        user, assignment = resolve_workflow_actor(step=step, document=document)
        self.assertEqual(user, self.reviewer_user)
        self.assertEqual(assignment, self.reviewer_assignment)

    def test_inactive_employee_cannot_be_resolved_as_workflow_actor(self):
        document = self.make_memo()
        self.reviewer.account_status = EmployeeProfile.AccountStatus.SUSPENDED
        self.reviewer.save(update_fields=["account_status", "updated_at"])
        step = document.document_type.workflows.first().steps.first()
        with self.assertRaises(ValidationError):
            resolve_workflow_actor(step=step, document=document, selected_user=self.reviewer_user)

    def test_inactive_position_cannot_become_a_new_workflow_target(self):
        document = self.make_memo()
        self.other_position.is_active = False
        self.other_position.save(update_fields=["is_active", "updated_at"])
        step = document.document_type.workflows.first().steps.first()
        with self.assertRaises(ValidationError):
            resolve_workflow_actor(step=step, document=document, selected_user=self.reviewer_user)

    def test_memo_task_and_approval_preserve_reviewer_assignment_context(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        self.assertEqual(task.assigned_assignment, self.reviewer_assignment)
        decide_review(task_id=task.pk, actor=self.reviewer_user, outcome=WorkflowDecision.Outcome.APPROVE)
        decision = task.decision
        self.assertEqual(decision.actor_assignment, self.reviewer_assignment)
        today = timezone.localdate()
        create_assignment(employee=self.reviewer, department=self.finance, position=self.position,
                          start_date=today, actor=self.admin)
        decision.refresh_from_db()
        self.assertEqual(decision.actor_assignment.department, self.department)

    def test_authorized_task_reassignment_records_both_organizational_contexts(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        original_assigned_at = task.assigned_at
        task = reassign_task(task_id=task.pk, actor=self.admin, new_user=self.replacement_user,
                             reason="Reviewer is unavailable.")
        self.assertEqual(task.assigned_to, self.replacement_user)
        self.assertEqual(task.assigned_at, original_assigned_at)
        event = task.reassignments.get()
        self.assertEqual(event.previous_user, self.reviewer_user)
        self.assertEqual(event.previous_assignment, self.reviewer_assignment)
        self.assertEqual(event.new_assignment, self.replacement_assignment)
        self.assertTrue(document.audit_events.filter(action="REASSIGNED").exists())

    def test_reassignment_page_uses_directory_labels_and_confirmation(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:task-reassign", args=[task.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jordan — Analyst (D-002)")
        self.assertNotContains(response, self.replacement_user.username)
        self.assertContains(response, "Reassign this workflow task?")
        self.assertContains(response, "You are about to change responsibility from")
        self.assertContains(response, "requestSubmit(action)")
        self.assertContains(response, "Cancel")

    def test_employee_access_changes_and_organization_deactivation_have_confirmations(self):
        self.client.force_login(self.admin)
        onboarding = self.client.get(reverse("organization:employee-create"))
        self.assertContains(onboarding, 'data-confirm-kind="employee"')
        self.assertContains(onboarding, 'data-confirm-mode="create"')
        self.assertContains(onboarding, "Create Employee")
        self.assertContains(onboarding, "confirmSubmit($event)")

        employee_edit = self.client.get(reverse("organization:employee-edit", args=[self.creator.pk]))
        self.assertContains(employee_edit, 'data-confirm-mode="update"')
        self.assertContains(employee_edit, 'data-initial-account-status="ACTIVE"')
        self.assertContains(employee_edit, 'x-text="confirmationLabel"')

        department_edit = self.client.get(reverse("organization:department-edit", args=[self.department.pk]))
        self.assertContains(department_edit, 'data-confirm-kind="department"')
        self.assertContains(department_edit, 'data-initial-active="true"')
        self.assertContains(department_edit, 'x-text="confirmationLabel"')
        department_create = self.client.get(reverse("organization:department-create"))
        self.assertNotContains(department_create, "organization-deactivation-confirm")
        self.assertContains(department_create, "Create Department")

        position_edit = self.client.get(reverse("organization:position-edit", args=[self.position.pk]))
        self.assertContains(position_edit, 'data-confirm-kind="position"')
        self.assertContains(position_edit, 'x-text="confirmationLabel"')
        position_create = self.client.get(reverse("organization:position-create"))
        self.assertContains(position_create, "Create Position")

    def test_organization_confirmation_cancel_and_confirm_controls_use_the_original_form(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("organization:department-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<button class="btn btn-outline-primary" type="button" autofocus')
        self.assertContains(response, '@click="$refs.confirmationDialog.close()"')
        self.assertContains(response, 'x-ref="actionForm"')
        self.assertContains(response, 'form.requestSubmit(action)')
        self.assertNotContains(response, 'type="submit" autofocus')

    def test_employee_update_confirmation_covers_assignment_and_account_status_audit(self):
        self.client.force_login(self.admin)
        assigned_role = Group.objects.get(name="DMS Document Officer")
        response = self.client.post(reverse("organization:employee-edit", args=[self.creator.pk]), {
            "staff_id": self.creator.staff_id,
            "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.INACTIVE,
            "department": self.finance.pk,
            "position": self.other_position.pk,
            "supervisor": self.reviewer.pk,
            "start_date": timezone.localdate().isoformat(),
            "roles": [assigned_role.pk],
        })

        self.assertEqual(response.status_code, 302)
        self.creator.refresh_from_db()
        self.creator_user.refresh_from_db()
        self.assertEqual(self.creator.account_status, EmployeeProfile.AccountStatus.INACTIVE)
        self.assertFalse(self.creator_user.is_active)
        self.assertEqual(self.creator.current_assignment.department, self.finance)
        self.assertEqual(self.creator.current_assignment.position, self.other_position)
        self.assertEqual(self.creator.current_assignment.supervisor, self.reviewer)
        self.assertIn(assigned_role, self.creator_user.groups.all())
        self.assertTrue(OrganizationAuditEvent.objects.filter(
            subject_user=self.creator_user,
            action=OrganizationAuditEvent.Action.ACCOUNT_STATUS_CHANGED,
        ).exists())
        self.assertTrue(OrganizationAuditEvent.objects.filter(
            subject_user=self.creator_user,
            action=OrganizationAuditEvent.Action.ASSIGNMENT_CREATED,
        ).exists())
        self.assertTrue(OrganizationAuditEvent.objects.filter(
            subject_user=self.creator_user,
            action=OrganizationAuditEvent.Action.ROLE_CHANGED,
        ).exists())

    def test_organization_form_validation_keeps_values_and_existing_feedback(self):
        self.client.force_login(self.admin)
        department_response = self.client.post(reverse("organization:department-create"), {
            "code": self.department.code, "name": "Entered department name", "description": "Retained text",
            "parent": "", "head": "", "is_active": "on",
        })
        self.assertEqual(department_response.status_code, 200)
        self.assertEqual(department_response.context["form"]["name"].value(), "Entered department name")
        self.assertEqual(department_response.context["form"]["description"].value(), "Retained text")
        self.assertTrue(department_response.context["form"]["code"].errors)

        employee_response = self.client.post(reverse("organization:employee-edit", args=[self.creator.pk]), {
            "staff_id": self.reviewer.staff_id,
            "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.ACTIVE,
            "department": self.department.pk,
            "position": self.position.pk,
            "supervisor": self.reviewer.pk,
            "start_date": "",
            "roles": [Group.objects.get(name="DMS Standard User").pk],
        })
        self.assertEqual(employee_response.status_code, 200)
        self.assertEqual(employee_response.context["form"]["staff_id"].value(), self.reviewer.staff_id)
        self.assertTrue(employee_response.context["form"]["staff_id"].errors)
        self.assertContains(employee_response, 'data-confirm-mode="update"')

    def test_department_create_remains_csrf_protected_and_authorized(self):
        from django.test import Client

        payload = {
            "code": "CSRF-DEPT", "name": "CSRF Department", "description": "",
            "parent": "", "head": "", "is_active": "on",
        }
        self.client.force_login(self.viewer)
        denied = self.client.post(reverse("organization:department-create"), payload)
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(Department.objects.filter(code="CSRF-DEPT").exists())

        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        url = reverse("organization:department-create")

        page = client.get(url)
        self.assertEqual(page.status_code, 200)
        rejected = client.post(url, payload)
        self.assertEqual(rejected.status_code, 403)
        self.assertFalse(Department.objects.filter(code="CSRF-DEPT").exists())

        token = client.cookies["csrftoken"].value
        accepted = client.post(url, payload, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(accepted.status_code, 302)
        self.assertTrue(Department.objects.filter(code="CSRF-DEPT").exists())

    def test_transfer_then_reassignment_preserves_the_pending_task_history(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        original_assigned_at = task.assigned_at
        original_assignment = task.assigned_assignment
        today = timezone.localdate()

        create_assignment(employee=self.reviewer, department=self.finance, position=self.position,
                          start_date=today, actor=self.admin)
        task.refresh_from_db()
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)
        self.assertEqual(task.assigned_to, self.reviewer_user)
        self.assertEqual(task.assigned_assignment, original_assignment)

        task = reassign_task(task_id=task.pk, actor=self.admin, new_user=self.replacement_user,
                             reason="Coverage after employee transfer.")
        history = task.reassignments.get()
        self.assertEqual(task.assigned_to, self.replacement_user)
        self.assertEqual(task.assigned_assignment, self.replacement_assignment)
        self.assertEqual(task.assigned_at, original_assigned_at)
        self.assertEqual(history.previous_user, self.reviewer_user)
        self.assertEqual(history.previous_assignment, original_assignment)
        self.assertEqual(history.new_user, self.replacement_user)
        self.assertEqual(history.new_assignment, self.replacement_assignment)
        self.assertEqual(history.actor, self.admin)
        self.assertEqual(history.reason, "Coverage after employee transfer.")
        self.assertIsNotNone(history.created_at)
        self.assertTrue(document.audit_events.filter(action="REASSIGNED").exists())

    def test_position_title_does_not_grant_permission_but_explicit_permission_does(self):
        from django.contrib.auth.models import Permission

        self.position.title = "HOD"
        self.position.save(update_fields=["title", "updated_at"])
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        self.assertFalse(self.creator_user.has_perm("documents.reassign_workflow_tasks"))
        with self.assertRaises(PermissionDenied):
            reassign_task(task_id=task.pk, actor=self.creator_user, new_user=self.replacement_user,
                          reason="Position title is not authorization.")

        permission = Permission.objects.get(
            content_type__app_label="documents", codename="reassign_workflow_tasks"
        )
        self.viewer.is_staff = True
        self.viewer.save(update_fields=["is_staff"])
        self.viewer.user_permissions.add(permission)
        self.assertTrue(self.viewer.has_perm("documents.reassign_workflow_tasks"))
        task = reassign_task(task_id=task.pk, actor=self.viewer, new_user=self.replacement_user,
                             reason="Explicit permission grants this action.")
        self.assertEqual(task.reassignments.get().actor, self.viewer)

    def test_task_reassignment_requires_permission_and_active_employee(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        with self.assertRaises(PermissionDenied):
            reassign_task(task_id=task.pk, actor=self.viewer, new_user=self.replacement_user, reason="Coverage")
        self.replacement.account_status = EmployeeProfile.AccountStatus.SUSPENDED
        self.replacement.save(update_fields=["account_status", "updated_at"])
        with self.assertRaises(ValidationError):
            reassign_task(task_id=task.pk, actor=self.admin, new_user=self.replacement_user, reason="Coverage")
        self.assertEqual(task.reassignments.count(), 0)

    def test_document_creator_cannot_be_selected_to_review_their_own_memo(self):
        from apps.documents.services import save_memo
        draft = self.make_memo()
        with self.assertRaises(ValidationError):
            save_memo(document=draft, actor=self.creator_user, values=self.memo_values(), reviewer=self.creator_user)

    def test_pending_task_remains_with_original_assignee_after_employee_transfer(self):
        document = self.make_memo(reviewer=self.reviewer_user)
        task = document.workflow.tasks.get()
        today = timezone.localdate()
        create_assignment(employee=self.reviewer, department=self.finance, position=self.position,
                          start_date=today, actor=self.admin)
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.reviewer_user)
        self.assertEqual(task.assigned_assignment, self.reviewer_assignment)

    def test_employee_list_detail_and_management_authorization(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("organization:employee-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "STAFF-001")
        response = self.client.get(reverse("organization:employee-detail", args=[self.creator.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Organisational history")
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("organization:employee-list"))
        self.assertEqual(response.status_code, 403)

    def test_employee_department_position_views_reject_unauthorized_users(self):
        self.client.force_login(self.viewer)
        for name in ("organization:department-list", "organization:position-list", "organization:roles"):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_employee_cannot_change_own_organisation(self):
        with self.assertRaises(PermissionDenied):
            update_employee(actor=self.creator_user, profile=self.creator, staff_id=self.creator.staff_id,
                            employment_status=self.creator.employment_status, account_status=self.creator.account_status,
                            department=self.department, position=self.position)

    def test_dms_group_roles_do_not_change_position_and_are_audited(self):
        from django.contrib.auth.models import Group
        role = Group.objects.get(name="DMS Document Officer")
        updated = update_employee(actor=self.admin, profile=self.creator, staff_id=self.creator.staff_id,
                                  employment_status=self.creator.employment_status,
                                  account_status=self.creator.account_status, department=self.department,
                                  position=self.position, roles=[role])
        self.assertIn(role, updated.user.groups.all())
        self.assertEqual(updated.current_assignment.position, self.position)
        self.assertTrue(OrganizationAuditEvent.objects.filter(subject_user=updated.user,
                        action=OrganizationAuditEvent.Action.ROLE_CHANGED).exists())

    def test_inactive_employee_account_cannot_log_in(self):
        self.creator.account_status = EmployeeProfile.AccountStatus.INACTIVE
        self.creator.save(update_fields=["account_status", "updated_at"])
        self.assertFalse(self.client.login(username="creator", password="pw"))

    def test_department_and_position_management_pages_render(self):
        self.client.force_login(self.admin)
        for name in ("organization:department-list", "organization:position-list", "organization:structure", "organization:roles"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)

    def test_organization_structure_uses_saved_reporting_lines_and_keeps_root_employees(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="org-card"')
        self.assertContains(response, "Casey")
        self.assertContains(response, "Riley")
        self.assertContains(response, 'aria-label="Direct reports to Riley"')
        self.assertContains(response, "Jordan")
        self.assertContains(response, reverse("organization:employee-detail", args=[self.creator.pk]))
        self.assertNotContains(response, 'class="org-status')

    def test_structure_nests_departments_by_parent_and_preserves_cross_department_supervisors(self):
        from apps.organization.models import EmployeeAssignment

        self.finance.parent = self.department
        self.finance.save(update_fields=["parent", "updated_at"])
        operations = Department.objects.create(code="D-003", name="Operations", parent=self.department)
        cross_department_user = get_user_model().objects.create_user(
            username="finance-report", first_name="Finance", last_name="Officer",
        )
        cross_department_profile = EmployeeProfile.objects.create(
            user=cross_department_user, staff_id="STAFF-FINANCE", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        EmployeeAssignment.objects.create(
            employee=cross_department_profile, department=self.finance, position=self.position,
            supervisor=self.reviewer, start_date=timezone.localdate(),
        )
        operations_user = get_user_model().objects.create_user(
            username="operations-report", first_name="Operations", last_name="Officer",
        )
        operations_profile = EmployeeProfile.objects.create(
            user=operations_user, staff_id="STAFF-OPERATIONS", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        EmployeeAssignment.objects.create(
            employee=operations_profile, department=operations, position=self.position,
            supervisor=self.reviewer, start_date=timezone.localdate(),
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        development = response.context["department_roots"][0]
        self.assertEqual(development["department"], self.department)
        self.assertEqual(development["children"][0]["department"], self.finance)
        self.assertEqual(development["children"][1]["department"], operations)
        self.assertIn('aria-label="Departments within Development"', html)
        self.assertIn('style="--department-count: 2"', html)
        self.assertNotIn("Reports to Riley", html)
        self.assertEqual(cross_department_profile.current_assignment.supervisor, self.reviewer)
        self.assertEqual(html.count(reverse("organization:employee-detail", args=[cross_department_profile.pk])), 1)
        self.assertNotIn("Reporting line not recorded", html)

    def test_structure_renders_empty_child_department_alongside_populated_sibling(self):
        from apps.organization.models import EmployeeAssignment

        executive_department = Department.objects.create(code="D-ED", name="Executive Director Branch")
        corporate_services = Department.objects.create(
            code="D-CS", name="Corporate Services", parent=executive_department,
        )
        self.replacement_assignment.end_date = timezone.localdate() - timedelta(days=1)
        self.replacement_assignment.save(update_fields=["end_date"])
        self.finance.parent = executive_department
        self.finance.save(update_fields=["parent", "updated_at"])

        executive_user = get_user_model().objects.create_user(
            username="executive-director", first_name="Executive", last_name="Director",
        )
        executive_profile = EmployeeProfile.objects.create(
            user=executive_user, staff_id="STAFF-ED", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        executive_position = Position.objects.create(
            code="P-ED", title="Executive Director / Chief Executive Officer",
        )
        EmployeeAssignment.objects.create(
            employee=executive_profile, department=executive_department, position=executive_position,
            start_date=timezone.localdate(),
        )

        corporate_services_user = get_user_model().objects.create_user(
            username="corporate-services-head", first_name="Corporate", last_name="Services",
        )
        corporate_services_profile = EmployeeProfile.objects.create(
            user=corporate_services_user, staff_id="STAFF-CS", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        corporate_services_position = Position.objects.create(
            code="P-CS", title="Chief Corporate Service Officer",
        )
        EmployeeAssignment.objects.create(
            employee=corporate_services_profile, department=corporate_services,
            position=corporate_services_position, supervisor=executive_profile,
            start_date=timezone.localdate(),
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        executive_node = next(
            node for node in response.context["department_roots"]
            if node["department"] == executive_department
        )
        self.assertEqual(
            [child["department"] for child in executive_node["children"]],
            [corporate_services, self.finance],
        )
        finance_node = executive_node["children"][1]
        self.assertEqual(finance_node["employee_count"], 0)
        self.assertEqual(finance_node["employees"], [])
        self.assertIn('style="--department-count: 2"', html)
        self.assertLess(html.index("Corporate Services</h2>"), html.index("Finance</h2>"))
        self.assertIn("No employees assigned yet", html)
        self.assertIn("Chief Corporate Service Officer", html)
        for profile_id in EmployeeAssignment.objects.filter(
            end_date__isnull=True, start_date__lte=timezone.localdate(),
        ).values_list("employee_id", flat=True):
            self.assertEqual(html.count(reverse("organization:employee-detail", args=[profile_id])), 1)

    def test_structure_keeps_finance_sibling_of_corporate_services_and_it_nested_under_corporate(self):
        from apps.organization.models import EmployeeAssignment

        today = timezone.localdate()
        executive_department = Department.objects.create(
            code="D-ED", name="Executive Director Branch", parent=self.department,
        )
        corporate_services = Department.objects.create(
            code="D-CS", name="Corporate Services", parent=executive_department,
        )
        information_technology = Department.objects.create(
            code="D-IT", name="Information Technology", parent=corporate_services,
        )
        finance = Department.objects.create(
            code="D-FIN", name="Finance Department", parent=executive_department,
        )

        def add_employee(username, first_name, last_name, staff_id, department, title, code, supervisor=None):
            user = get_user_model().objects.create_user(
                username=username, first_name=first_name, last_name=last_name,
            )
            profile = EmployeeProfile.objects.create(
                user=user, staff_id=staff_id, account_status=EmployeeProfile.AccountStatus.ACTIVE,
            )
            position = Position.objects.create(code=code, title=title)
            EmployeeAssignment.objects.create(
                employee=profile, department=department, position=position,
                supervisor=supervisor, start_date=today,
            )
            return profile

        executive_director = add_employee(
            "structure-ed", "Executive", "Director", "STAFF-STRUCT-ED", executive_department,
            "Executive Director", "P-STRUCT-ED",
        )
        add_employee(
            "structure-cs-head", "Corporate", "Services", "STAFF-STRUCT-CS", corporate_services,
            "Chief Corporate Services Officer", "P-STRUCT-CS", executive_director,
        )
        it_head = add_employee(
            "structure-it-head", "Information", "Technology", "STAFF-STRUCT-IT", information_technology,
            "Head of Information Technology", "P-STRUCT-IT", executive_director,
        )
        it_officer = add_employee(
            "structure-it-officer", "IT", "Officer", "STAFF-STRUCT-ITO", information_technology,
            "Information Technology Officer", "P-STRUCT-ITO", it_head,
        )
        finance_officer = add_employee(
            "structure-cfo", "Finance", "Officer", "STAFF-STRUCT-CFO", finance,
            "Chief Financial Officer", "P-STRUCT-CFO", executive_director,
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        executive_node = next(
            node for root in response.context["department_roots"]
            for node in root["children"]
            if node["department"] == executive_department
        )
        self.assertEqual(
            [node["department"] for node in executive_node["children"]],
            [corporate_services, finance],
        )
        corporate_node = executive_node["children"][0]
        self.assertEqual(corporate_node["children"][0]["department"], information_technology)
        it_node = corporate_node["children"][0]
        self.assertEqual(it_node["employees"][0]["profile"], it_head)
        self.assertEqual(it_node["employees"][0]["children"][0]["profile"], it_officer)
        finance_node = executive_node["children"][1]
        self.assertEqual(finance_node["employees"][0]["profile"], finance_officer)

        html = response.content.decode()
        self.assertLess(html.index("Corporate Services</h2>"), html.index("Finance Department</h2>"))
        for profile in (executive_director, it_head, it_officer, finance_officer):
            self.assertEqual(html.count(reverse("organization:employee-detail", args=[profile.pk])), 1)

    def test_executive_office_assignment_is_rendered_once_and_not_marked_unplaced(self):
        from apps.organization.models import EmployeeAssignment

        self.position.title = "Managing Director / Chief Executive Officer"
        self.position.save(update_fields=["title", "updated_at"])
        office_position = Position.objects.create(code="P-EXEC-ASST", title="Executive Assistant to MD/CEO")
        office_user = get_user_model().objects.create_user(username="office-assistant", first_name="Fahd", last_name="Lawan")
        office_profile = EmployeeProfile.objects.create(
            user=office_user, staff_id="STAFF-OFFICE", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        EmployeeAssignment.objects.create(
            employee=office_profile, department=self.department, position=office_position,
            supervisor=self.admin_profile, start_date=timezone.localdate(),
        )
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count("Fahd Lawan"), 1)
        self.assertContains(response, 'aria-label="Executive Office reporting to org-admin"')
        self.assertNotContains(response, "Reporting line not recorded")

    def test_structure_excludes_bootstrap_admin_identity_but_keeps_employee_admins(self):
        from apps.organization.models import EmployeeBootstrapState

        bootstrap_user = get_user_model().objects.create_user(
            username="bootstrap-identity", first_name="Technical", last_name="Account",
        )
        bootstrap_profile = EmployeeProfile.objects.create(
            user=bootstrap_user, staff_id="BOOTSTRAP-001",
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        bootstrap_user.groups.add(Group.objects.get(name="DMS Administrator"))
        assignment = create_assignment(
            employee=bootstrap_profile, department=self.department, position=self.position,
            supervisor=self.admin_profile, start_date=timezone.localdate(), actor=self.admin,
        )
        for action in (OrganizationAuditEvent.Action.USER_CREATED, OrganizationAuditEvent.Action.ROLE_CHANGED):
            OrganizationAuditEvent.objects.create(
                actor=self.admin, action=action, subject_user=bootstrap_user,
            )
        EmployeeBootstrapState.objects.create(completed_at=timezone.now())
        self.client.force_login(self.admin)

        response = self.client.get(reverse("organization:structure"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, reverse("organization:employee-detail", args=[bootstrap_profile.pk]))
        self.assertContains(response, reverse("organization:employee-detail", args=[self.admin_profile.pk]))
        self.assertTrue(EmployeeAssignment.objects.filter(pk=assignment.pk).exists())

    def test_onboarding_page_creates_new_django_user_without_password(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("organization:employee-create"), {
            "first_name": "Web", "last_name": "Employee",
            "email": "web@example.test", "staff_id": "WEB-001",
            "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.INACTIVE,
            "department": self.department.pk, "position": self.position.pk,
            "supervisor": "", "start_date": timezone.localdate().isoformat(), "roles": [],
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Employee created successfully.")
        self.assertContains(response, "Activation not currently available")
        profile = EmployeeProfile.objects.get(staff_id="WEB-001")
        self.assertFalse(profile.user.has_usable_password())
        self.assertFalse(profile.user.is_active)
        self.assertEqual(profile.current_assignment.department, self.department)

    def test_department_and_position_forms_create_records_without_fictional_seed_data(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("organization:department-create"), {
            "code": "NEW-DEPT", "name": "New Department", "description": "",
            "parent": "", "head": "", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        department = Department.objects.get(code="NEW-DEPT")
        response = self.client.post(reverse("organization:position-create"), {
            "code": "NEW-POS", "title": "New Role", "description": "",
            "department": department.pk, "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Position.objects.filter(code="NEW-POS", department=department).exists())

    def test_employee_edit_page_blocks_self_change_even_for_permission_holder(self):
        from django.contrib.auth.models import Permission
        permission = Permission.objects.get(content_type__app_label="organization", codename="manage_employees")
        self.creator_user.user_permissions.add(permission)
        self.client.force_login(self.creator_user)
        response = self.client.post(reverse("organization:employee-edit", args=[self.creator.pk]), {
            "staff_id": self.creator.staff_id,
            "employment_status": self.creator.employment_status,
            "account_status": self.creator.account_status,
            "department": self.department.pk, "position": self.position.pk,
            "supervisor": self.reviewer.pk, "start_date": "",
            "roles": [Group.objects.get(name="DMS Standard User").pk],
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.creator.current_assignment.supervisor, self.reviewer)

