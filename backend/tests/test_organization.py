from datetime import timedelta

from django.contrib.auth import get_user_model
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
        self.reviewer_user = User.objects.create_user(username="reviewer", first_name="Riley", is_staff=True, password="pw")
        self.replacement_user = User.objects.create_user(username="replacement", first_name="Jordan", is_staff=True, password="pw")
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
        today = timezone.localdate()
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

    def test_onboarding_page_creates_new_django_user_without_password(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("organization:employee-create"), {
            "username": "web-onboarded", "first_name": "Web", "last_name": "Employee",
            "email": "web@example.test", "staff_id": "WEB-001",
            "employment_status": EmployeeProfile.EmploymentStatus.EMPLOYED,
            "account_status": EmployeeProfile.AccountStatus.INACTIVE,
            "department": self.department.pk, "position": self.position.pk,
            "supervisor": "", "start_date": timezone.localdate().isoformat(), "roles": [],
        })
        self.assertEqual(response.status_code, 302)
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
            "roles": [],
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.creator.current_assignment.supervisor, self.reviewer)

