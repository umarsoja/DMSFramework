import tempfile
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.documents.models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentAccess,
    ManagementDirective,
    NotificationReceipt,
    WorkflowActionType,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from apps.documents.services import (
    create_supporting_workflow_task, issue_management_directive, reassign_task,
    register_incoming_correspondence,
)
from apps.documents.workflow_tasks import complete_workflow_task
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


class ManagementDirectiveTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        User = get_user_model()
        self.registrar = self.make_user("directive-registrar", "DIRECTIVE-REG", "DMS Records/Registry Officer")
        self.manager = self.make_user("directive-manager", "DIRECTIVE-MGR", "DMS Department Manager/HOD")
        self.assignee = self.make_user(
            "directive-assignee", "DIRECTIVE-ASSIGNEE", "DMS Standard User",
            supervisor=self.manager.employee_profile,
        )
        self.outsider = self.make_user("directive-outsider", "DIRECTIVE-OUTSIDER", "DMS Standard User")
        self.grant_permission(self.registrar, "register_incoming_correspondence")
        self.grant_permission(self.manager, "issue_management_directives")
        self.grant_permission(self.manager, "assign_workflow_tasks")
        self.office = self.manager.employee_profile.current_assignment.department
        self.office.head = self.manager.employee_profile
        self.office.full_clean()
        self.office.save(update_fields=["head"])
        self.advise = WorkflowActionType.objects.get(code="advise", is_active=True)
        self.acknowledge = WorkflowActionType.objects.get(code="acknowledge", is_active=True)
        self.document = register_incoming_correspondence(
            actor=self.registrar,
            values={
                "sender": "External Legal Counsel",
                "external_reference": "LEGAL-2026-51",
                "document_date": date(2026, 10, 5),
                "received_on": date(2026, 10, 7),
                "subject": "Proposed contract",
                "correspondence_type": "CONTRACT_AGREEMENT",
                "other_correspondence_type": "",
                "receiving_office": self.office,
                "priority": "HIGH",
                "classification": Document.Classification.CONFIDENTIAL,
                "original_document": SimpleUploadedFile("contract.pdf", b"external contract", content_type="application/pdf"),
                "supporting_documents": [],
            },
        )
        self.manager_access = DocumentAccess.objects.get(document=self.document, user=self.manager)

    def tearDown(self):
        self.settings_override.disable()
        self.media_dir.cleanup()

    def make_user(self, username, staff_id, group, *, supervisor=None, department=None):
        User = get_user_model()
        user = User.objects.create_user(username=username, password="pw")
        profile = EmployeeProfile.objects.create(
            user=user, staff_id=staff_id, account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        department = department or Department.objects.get_or_create(
            code="DIRECTIVE-UNIT", defaults={"name": "Legal Unit"},
        )[0]
        position = Position.objects.create(code=f"{staff_id}-POS", title=f"{username} position", department=department)
        create_assignment(employee=profile, department=department, position=position,
                          supervisor=supervisor, actor=user)
        user.groups.add(Group.objects.get(name=group))
        return user

    @staticmethod
    def grant_permission(user, codename):
        permission = Permission.objects.get(content_type__app_label="documents", codename=codename)
        user.user_permissions.add(permission)

    def issue(self, *, issuer=None, assignee=None, action_type=None, due_date=None, instruction="Review the proposed contract and advise."):
        return issue_management_directive(
            target_document=self.document,
            issuer=issuer or self.manager,
            action_type=action_type or self.advise,
            instruction=instruction,
            assignee=assignee,
            due_date=due_date,
        )

    def test_authorized_management_user_can_issue_directive_and_audit_records_it(self):
        directive = self.issue()
        self.assertEqual(directive.issued_by, self.manager)
        self.assertEqual(directive.target_document, self.document)
        self.assertEqual(directive.action_type, self.advise)
        self.assertIsNone(directive.workflow_task)
        event = self.document.audit_events.get(action=AuditEvent.Action.DIRECTIVE_ISSUED)
        self.assertEqual(event.actor, self.manager)
        self.assertEqual(event.context["directive_id"], directive.pk)
        self.assertEqual(event.context["instruction"], directive.instruction)
        self.assertIsNone(event.context["task_id"])

    def test_unauthorized_user_cannot_issue_directive(self):
        with self.assertRaises(PermissionDenied):
            self.issue(issuer=self.outsider)

    def test_issuer_must_have_target_document_access(self):
        DocumentAccess.objects.filter(document=self.document, user=self.manager).delete()
        with self.assertRaises(PermissionDenied):
            self.issue()

    def test_directive_only_is_valid_without_workflow_or_dummy_task(self):
        directive = self.issue(action_type=self.acknowledge, instruction="Noted. File for record.")
        self.assertIsNone(directive.workflow_task_id)
        self.assertFalse(WorkflowTask.objects.filter(instance__document=self.document).exists())
        self.assertFalse(WorkflowInstance.objects.filter(document=self.document).exists())

    def test_advise_exists_in_the_existing_action_catalog(self):
        self.assertEqual(self.advise.label, "Advise")
        self.assertTrue(self.advise.is_active)

    def test_actionable_directive_creates_task_bound_to_document_and_assignee(self):
        due = date(2026, 10, 12)
        directive = self.issue(assignee=self.assignee, due_date=due)
        task = directive.workflow_task
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.assignee)
        self.assertEqual(task.action_type, self.advise)
        self.assertEqual(task.instruction, directive.instruction)
        self.assertEqual(task.instance.document, self.document)
        self.assertEqual(task.submitted_version, self.document.current_version)
        self.assertEqual(task.assigned_assignment, self.assignee.employee_profile.current_assignment)
        self.assertEqual(task.due_at.date(), due)
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)

    def test_junior_cannot_assign_management_task_upward(self):
        self.grant_permission(self.assignee, "issue_management_directives")
        self.grant_permission(self.assignee, "assign_workflow_tasks")
        DocumentAccess.objects.create(
            document=self.document, user=self.assignee, can_view=True,
            can_download=False, granted_by=self.registrar,
        )
        with self.assertRaises(ValidationError):
            self.issue(issuer=self.assignee, assignee=self.manager)

    def test_manager_cannot_assign_to_unrelated_employee_or_self(self):
        with self.assertRaises(ValidationError):
            self.issue(assignee=self.outsider)

    def test_manager_cannot_assign_management_task_to_self(self):
        with self.assertRaises(ValidationError):
            self.issue(assignee=self.manager)

    def test_employee_without_current_supervisor_is_not_an_eligible_assignee(self):
        from apps.documents.forms import ManagementDirectiveForm

        form = ManagementDirectiveForm(can_assign=True, issuer=self.manager)
        self.assertNotIn(self.outsider.employee_profile, form.fields["assignee"].queryset)

    def test_cross_department_direct_report_is_eligible(self):
        other_department = Department.objects.create(code="DIRECTIVE-OTHER", name="Other Unit")
        cross_department_report = self.make_user(
            "directive-cross-unit", "DIRECTIVE-CROSS", "DMS Standard User",
            supervisor=self.manager.employee_profile, department=other_department,
        )
        directive = self.issue(assignee=cross_department_report)
        self.assertEqual(directive.workflow_task.assigned_to, cross_department_report)

    def test_explicit_executive_authority_can_assign_active_department_head(self):
        executive = self.make_user("directive-executive", "DIRECTIVE-EXEC", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, can_download=False, granted_by=self.registrar)
        department = Department.objects.create(code="DIRECTIVE-EXEC-UNIT", name="Executive Unit")
        hod = self.make_user("directive-hod", "DIRECTIVE-HOD", "DMS Department Manager/HOD", department=department)
        department.head = hod.employee_profile
        department.full_clean()
        department.save(update_fields=["head"])
        directive = self.issue(issuer=executive, assignee=hod)
        self.assertEqual(directive.workflow_task.assigned_to, hod)

    def test_executive_authority_cannot_assign_non_head(self):
        executive = self.make_user("directive-executive-2", "DIRECTIVE-EXEC-2", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, can_download=False, granted_by=self.registrar)
        department = Department.objects.create(code="DIRECTIVE-EXEC-UNIT-2", name="Executive Unit 2")
        hod = self.make_user("directive-hod-2", "DIRECTIVE-HOD-2", "DMS Department Manager/HOD", department=department)
        department.head = hod.employee_profile
        department.full_clean()
        department.save(update_fields=["head"])
        officer = self.make_user("directive-officer-2", "DIRECTIVE-OFFICER-2", "DMS Standard User", department=department, supervisor=hod.employee_profile)
        with self.assertRaises(ValidationError):
            self.issue(issuer=executive, assignee=officer)

    def test_executive_rejects_inactive_hod(self):
        executive = self.make_user("directive-executive-inactive", "DIRECTIVE-EXEC-INACTIVE", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, granted_by=self.registrar)
        department = Department.objects.create(code="DIRECTIVE-INACTIVE-HOD", name="Inactive HOD Unit")
        hod = self.make_user("directive-inactive-hod", "DIRECTIVE-INACTIVE-HOD", "DMS Department Manager/HOD", department=department)
        department.head = hod.employee_profile
        department.save(update_fields=["head"])
        hod.employee_profile.account_status = EmployeeProfile.AccountStatus.INACTIVE
        hod.employee_profile.save(update_fields=["account_status"])
        with self.assertRaises(ValidationError):
            self.issue(issuer=executive, assignee=hod)

    def test_executive_rejects_hod_with_mismatched_current_assignment(self):
        executive = self.make_user("directive-executive-mismatch", "DIRECTIVE-EXEC-MISMATCH", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, granted_by=self.registrar)
        headed_department = Department.objects.create(code="DIRECTIVE-HEADED", name="Configured Head Unit")
        other_department = Department.objects.create(code="DIRECTIVE-MISMATCH", name="Mismatch Unit")
        hod = self.make_user("directive-mismatch-hod", "DIRECTIVE-MISMATCH-HOD", "DMS Department Manager/HOD", department=other_department)
        headed_department.head = hod.employee_profile
        headed_department.save(update_fields=["head"])
        with self.assertRaises(ValidationError):
            self.issue(issuer=executive, assignee=hod)

    def test_executive_group_without_operational_role_is_ineligible(self):
        executive = self.make_user("directive-executive-no-role", "DIRECTIVE-EXEC-NOROLE", "DMS Standard User")
        executive.groups.remove(Group.objects.get(name="DMS Standard User"))
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, granted_by=self.registrar)
        with self.assertRaises(PermissionDenied):
            self.issue(issuer=executive)

    def test_revoked_executive_authority_is_rejected(self):
        executive = self.make_user("directive-executive-revoked", "DIRECTIVE-EXEC-REVOKED", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, granted_by=self.registrar)
        executive.groups.remove(Group.objects.get(name="DMS Executive Authority"))
        with self.assertRaises(PermissionDenied):
            self.issue(issuer=executive)

    def test_executive_can_assign_to_hod_who_heads_multiple_departments(self):
        executive = self.make_user("directive-executive-multi", "DIRECTIVE-EXEC-MULTI", "DMS Standard User")
        executive.groups.add(Group.objects.get(name="DMS Executive Authority"))
        DocumentAccess.objects.create(document=self.document, user=executive, can_view=True, granted_by=self.registrar)
        first = Department.objects.create(code="DIRECTIVE-MULTI-1", name="Multi Unit One")
        second = Department.objects.create(code="DIRECTIVE-MULTI-2", name="Multi Unit Two")
        hod = self.make_user("directive-multi-hod", "DIRECTIVE-MULTI-HOD", "DMS Department Manager/HOD", department=first)
        first.head = hod.employee_profile
        second.head = hod.employee_profile
        first.save(update_fields=["head"])
        second.save(update_fields=["head"])
        directive = self.issue(issuer=executive, assignee=hod)
        self.assertEqual(directive.workflow_task.assigned_to, hod)

    def test_dms_administrator_without_reporting_relationship_is_not_eligible(self):
        administrator = self.make_user(
            "directive-admin", "DIRECTIVE-ADMIN", "DMS Administrator",
        )
        with self.assertRaises(ValidationError):
            self.issue(assignee=administrator)

    def test_assignee_form_contains_only_current_direct_reports(self):
        from apps.documents.forms import ManagementDirectiveForm

        form = ManagementDirectiveForm(can_assign=True, issuer=self.manager)
        self.assertEqual(list(form.fields["assignee"].queryset), [self.assignee.employee_profile])

    def test_management_task_reassignment_cannot_bypass_issuer_reporting_line(self):
        self.grant_permission(self.manager, "reassign_workflow_tasks")
        directive = self.issue(assignee=self.assignee)
        with self.assertRaises(ValidationError):
            from apps.documents.services import reassign_task
            reassign_task(
                task_id=directive.workflow_task_id, actor=self.manager,
                new_user=self.outsider, reason="Try unrelated employee.",
            )
        staff_without_profile = get_user_model().objects.create_user(
            username="directive-staff-without-profile", password="pw", is_staff=True,
        )
        with self.assertRaises(ValidationError):
            reassign_task(
                task_id=directive.workflow_task_id, actor=self.manager,
                new_user=staff_without_profile, reason="Try staff without employee profile.",
            )

        other_department = Department.objects.create(code="DIRECTIVE-OTHER-2", name="Other Unit 2")
        cross_department_report = self.make_user(
            "directive-reassign-cross", "DIRECTIVE-REASSIGN-CROSS", "DMS Standard User",
            supervisor=self.manager.employee_profile, department=other_department,
        )
        reassigned = reassign_task(
            task_id=directive.workflow_task_id, actor=self.manager,
            new_user=cross_department_report, reason="Cross-unit coverage.",
        )
        self.assertEqual(reassigned.assigned_to, cross_department_report)

    def test_management_task_reassignment_ui_lists_only_issuer_direct_reports(self):
        self.grant_permission(self.manager, "reassign_workflow_tasks")
        directive = self.issue(assignee=self.assignee)
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("organization:task-reassign", args=[directive.workflow_task_id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "directive-assignee position")
        self.assertNotContains(response, "directive-outsider position")
        self.assertContains(response, 'x-ref="confirmationDialog"')
        self.assertContains(response, "Reassign Task")

    def test_task_assignment_requires_separate_capability(self):
        self.manager.user_permissions.remove(Permission.objects.get(
            content_type__app_label="documents", codename="assign_workflow_tasks",
        ))
        with self.assertRaises(PermissionDenied):
            self.issue(assignee=self.assignee)
        self.assertFalse(ManagementDirective.objects.filter(target_document=self.document).exists())

    def test_unassigned_directive_does_not_turn_correspondence_or_cc_into_task_ownership(self):
        directive = self.issue(instruction="Noted. File for record.")
        self.assertIsNone(directive.workflow_task_id)
        self.assertFalse(self.document.workflow.tasks.filter(assigned_to=self.assignee).exists()
                         if hasattr(self.document, "workflow") else False)

    def test_task_and_directive_creation_roll_back_together_on_audit_failure(self):
        with patch("apps.documents.services._record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.issue(assignee=self.assignee)
        self.assertFalse(ManagementDirective.objects.filter(target_document=self.document).exists())
        self.assertFalse(WorkflowInstance.objects.filter(document=self.document).exists())
        self.assertFalse(WorkflowTask.objects.filter(instance__document=self.document).exists())

    def test_assignee_completes_task_with_response_evidence_and_task_audit(self):
        directive = self.issue(assignee=self.assignee)
        evidence = Attachment.objects.create(
            version=self.document.current_version,
            file="private_documents/directive-evidence.pdf",
            original_filename="legal-amendments.pdf",
            media_type="application/pdf",
            size=15,
            uploaded_by=self.registrar,
        )
        task = complete_workflow_task(
            task_id=directive.workflow_task_id,
            actor=self.assignee,
            response="Legal review completed. Contract is acceptable subject to the attached amendments.",
            evidence_ids=[evidence.pk],
        )
        task.refresh_from_db()
        self.assertEqual(task.status, WorkflowTask.Status.COMPLETED)
        self.assertIsNotNone(task.completed_at)
        self.assertIn("Legal review completed", task.response)
        self.assertEqual(list(task.evidence.all()), [evidence])
        self.assertTrue(self.document.audit_events.filter(
            action=AuditEvent.Action.TASK_COMPLETED,
            context__task_id=task.pk,
        ).exists())

    def test_only_assignee_can_complete_directive_task(self):
        directive = self.issue(assignee=self.assignee)
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=directive.workflow_task_id, actor=self.outsider, response="No")
        directive.workflow_task.refresh_from_db()
        self.assertEqual(directive.workflow_task.status, WorkflowTask.Status.PENDING)

    def test_management_can_view_completed_response_from_correspondence(self):
        directive = self.issue(assignee=self.assignee)
        complete_workflow_task(
            task_id=directive.workflow_task_id,
            actor=self.assignee,
            response="Recommend acceptance subject to the listed amendments.",
        )
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Recommend acceptance subject to the listed amendments.")
        self.assertContains(response, "Completed")

    def test_inaccessible_user_cannot_view_directive_or_task_content(self):
        directive = self.issue(assignee=self.assignee)
        client = Client()
        client.force_login(self.outsider)
        response = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertEqual(response.status_code, 404)

    def test_assignee_opens_correspondence_and_submits_response_through_ui(self):
        directive = self.issue(assignee=self.assignee, due_date=date(2026, 10, 12))
        client = Client()
        client.force_login(self.assignee)
        detail = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, directive.instruction)
        self.assertContains(detail, 'x-ref="confirmationDialog"')
        self.assertContains(detail, "Submit & Complete", html=False)
        response = client.post(
            reverse("documents:incoming-complete-directive-task", args=[self.document.pk, directive.workflow_task_id]),
            {"response": "Legal recommends acceptance with amendments."},
        )
        self.assertEqual(response.status_code, 302)
        directive.workflow_task.refresh_from_db()
        self.assertEqual(directive.workflow_task.status, WorkflowTask.Status.COMPLETED)
        self.assertEqual(directive.workflow_task.response, "Legal recommends acceptance with amendments.")
        event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_COMPLETED,
            context__task_id=directive.workflow_task_id,
        )
        self.assertEqual(event.context["response"], directive.workflow_task.response)

    def test_assignee_can_complete_with_uploaded_evidence_only_and_secure_attachment_access(self):
        directive = self.issue(assignee=self.assignee)
        client = Client()
        client.force_login(self.assignee)
        response = client.post(
            reverse("documents:incoming-complete-directive-task", args=[
                self.document.pk, directive.workflow_task_id,
            ]),
            {
                "response": "",
                "uploads": [SimpleUploadedFile("site-photo.png", b"image evidence", content_type="image/png")],
            },
        )

        self.assertEqual(response.status_code, 302)
        task = directive.workflow_task
        task.refresh_from_db()
        evidence = task.evidence.get()
        self.assertEqual(task.status, WorkflowTask.Status.COMPLETED)
        self.assertEqual(task.response, "")
        self.assertEqual(evidence.version.document, self.document)
        self.assertEqual(evidence.version, task.submitted_version)
        self.assertEqual(evidence.uploaded_by, self.assignee)
        event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_COMPLETED,
            context__task_id=task.pk,
        )
        self.assertEqual(event.context["response"], "")
        self.assertEqual(event.context["evidence_attachment_ids"], [evidence.pk])

        view_url = reverse("documents:incoming-attachment-view", args=[self.document.pk, evidence.pk])
        self.assertEqual(client.get(view_url).status_code, 200)
        self.assertEqual(
            client.get(reverse("documents:incoming-attachment-download", args=[self.document.pk, evidence.pk])).status_code,
            403,
        )
        outsider = Client()
        outsider.force_login(self.outsider)
        self.assertEqual(outsider.get(view_url).status_code, 404)

    def test_assignee_can_complete_with_response_and_uploaded_evidence(self):
        directive = self.issue(assignee=self.assignee)
        client = Client()
        client.force_login(self.assignee)
        response = client.post(
            reverse("documents:incoming-complete-directive-task", args=[
                self.document.pk, directive.workflow_task_id,
            ]),
            {
                "response": "The site inspection is complete.",
                "uploads": [SimpleUploadedFile("inspection.txt", b"Inspection notes", content_type="text/plain")],
            },
        )

        self.assertEqual(response.status_code, 302)
        task = directive.workflow_task
        task.refresh_from_db()
        evidence = task.evidence.get()
        self.assertEqual(task.status, WorkflowTask.Status.COMPLETED)
        self.assertEqual(task.response, "The site inspection is complete.")
        event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_COMPLETED,
            context__task_id=task.pk,
        )
        self.assertEqual(event.context["response"], task.response)
        self.assertEqual(event.context["evidence_attachment_ids"], [evidence.pk])

    def test_unauthorized_user_cannot_upload_evidence_or_complete_task(self):
        directive = self.issue(assignee=self.assignee)
        client = Client()
        client.force_login(self.outsider)
        before_count = self.document.current_version.attachments.count()
        response = client.post(
            reverse("documents:incoming-complete-directive-task", args=[
                self.document.pk, directive.workflow_task_id,
            ]),
            {
                "response": "Unauthorized response.",
                "uploads": [SimpleUploadedFile("forbidden.txt", b"forbidden", content_type="text/plain")],
            },
        )

        self.assertEqual(response.status_code, 404)
        directive.workflow_task.refresh_from_db()
        self.assertEqual(directive.workflow_task.status, WorkflowTask.Status.PENDING)
        self.assertEqual(self.document.current_version.attachments.count(), before_count)

    def test_directive_form_supports_taskless_action_and_separate_assignment_permission(self):
        client = Client()
        client.force_login(self.manager)
        response = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertContains(response, "Issue Directive")
        self.assertContains(response, "Assignee")
        self.assertContains(response, "Leave Assignee blank")
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'x-ref="confirmationDialog"')
        self.assertContains(response, "Assign Task")
        self.manager.user_permissions.remove(Permission.objects.get(
            content_type__app_label="documents", codename="assign_workflow_tasks",
        ))
        response = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertContains(response, "without an assigned task")
        self.assertNotContains(response, "Assignee")

    def test_delegated_supporting_workflow_md_ed_hod_ed_md(self):
        directive = self.issue(assignee=self.assignee)
        ed_task = directive.workflow_task
        self.grant_permission(self.assignee, "assign_workflow_tasks")
        other_department = Department.objects.create(code="DIRECTIVE-HOD-UNIT", name="HOD Unit")
        hod = self.make_user(
            "directive-hod", "DIRECTIVE-HOD", "DMS Standard User",
            supervisor=self.assignee.employee_profile,
            department=other_department,
        )
        evidence_upload = SimpleUploadedFile(
            "hod-evidence.png", b"HOD supporting evidence", content_type="image/png",
        )

        ed_client = Client()
        ed_client.force_login(self.assignee)
        create_response = ed_client.post(
            reverse("documents:incoming-create-supporting-task", args=[self.document.pk, ed_task.pk]),
            {
                "action_type": self.advise.pk,
                "instruction": "Inspect the contract clauses and report findings.",
                "assignee": hod.employee_profile.pk,
            },
        )
        self.assertEqual(create_response.status_code, 302)
        hod_task = ed_task.supporting_tasks.get()
        self.assertEqual(hod_task.parent_task_id, ed_task.pk)
        self.assertEqual(hod_task.instance.document, self.document)
        self.assertEqual(hod_task.status, WorkflowTask.Status.PENDING)
        self.assertEqual(ed_task.status, WorkflowTask.Status.PENDING)
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=ed_task.pk, actor=self.manager, response="Issuer cannot complete.")
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=hod_task.pk, actor=self.assignee, response="Parent assignee cannot complete child.")
        assigned_event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_ASSIGNED, context__task_id=hod_task.pk,
        )
        self.assertEqual(assigned_event.actor, self.assignee)
        self.assertEqual(assigned_event.context["parent_task_id"], ed_task.pk)
        self.assertTrue(NotificationReceipt.objects.filter(recipient=hod, event=assigned_event).exists())

        hod_client = Client()
        hod_client.force_login(hod)
        response = hod_client.post(
            reverse("documents:incoming-complete-directive-task", args=[self.document.pk, hod_task.pk]),
            {
                "response": "Clause review complete; the indemnity language needs revision.",
                "uploads": [evidence_upload],
            },
        )
        self.assertEqual(response.status_code, 302)
        hod_task.refresh_from_db()
        completed_hod_task = hod_task
        evidence = completed_hod_task.evidence.get()
        self.assertEqual(evidence.version, ed_task.submitted_version)
        self.assertEqual(evidence.uploaded_by, hod)
        self.assertEqual(ed_task.status, WorkflowTask.Status.PENDING)
        child_event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_COMPLETED, context__task_id=hod_task.pk,
        )
        self.assertEqual(child_event.context["parent_task_id"], ed_task.pk)
        self.assertEqual(child_event.context["evidence_attachment_ids"], [evidence.pk])
        child_receipt = NotificationReceipt.objects.get(recipient=self.assignee, event=child_event)
        self.assertIsNone(child_receipt.seen_at)
        self.assertEqual(child_receipt.title, "Supporting task response received")

        client = Client()
        client.force_login(self.assignee)
        detail = client.get(reverse("documents:incoming-detail", args=[self.document.pk]))
        self.assertContains(detail, "Clause review complete; the indemnity language needs revision.")
        self.assertContains(detail, "hod-evidence.png")
        self.assertContains(detail, hod.get_username())
        self.assertNotContains(detail, self.outsider.get_username())
        self.assertContains(detail, "Assign Task")
        self.assertTrue(Attachment.objects.filter(pk=evidence.pk, version__document=self.document).exists())
        self.assertTrue(self.document.workflow.tasks.filter(pk=ed_task.pk, assigned_to=self.assignee).exists())
        evidence_url = reverse("documents:incoming-attachment-view", args=[self.document.pk, evidence.pk])
        self.assertEqual(client.get(evidence_url).status_code, 200)
        outsider_client = Client()
        outsider_client.force_login(self.outsider)
        self.assertEqual(outsider_client.get(evidence_url).status_code, 404)

        ed_task = complete_workflow_task(
            task_id=ed_task.pk, actor=self.assignee,
            response="Consolidated recommendation: revise the indemnity clause before acceptance.",
            evidence_ids=[evidence.pk],
        )
        self.assertEqual(ed_task.status, WorkflowTask.Status.COMPLETED)
        self.assertEqual(completed_hod_task.status, WorkflowTask.Status.COMPLETED)
        parent_event = self.document.audit_events.get(
            action=AuditEvent.Action.TASK_COMPLETED, context__task_id=ed_task.pk,
        )
        self.assertIsNone(parent_event.context["parent_task_id"])
        manager_receipt = NotificationReceipt.objects.get(recipient=self.manager, event=parent_event)
        self.assertIsNone(manager_receipt.seen_at)
        self.assertEqual(manager_receipt.title, "Task response received")
        self.assertEqual(child_event.actor, hod)
        self.assertEqual(parent_event.actor, self.assignee)
        self.assertEqual(list(ed_task.evidence.all()), [evidence])
        self.assertEqual(parent_event.context["evidence_attachment_ids"], [evidence.pk])
        manager_client = Client()
        manager_client.force_login(self.manager)
        self.assertContains(manager_client.get(reverse("notifications:list")), "Task response received")
        self.assertRedirects(
            manager_client.post(reverse("notifications:open", args=[manager_receipt.pk])),
            reverse("documents:incoming-detail", args=[self.document.pk]),
            fetch_redirect_response=False,
        )

    def test_supporting_task_authorization_and_parent_lifecycle_are_enforced(self):
        directive = self.issue(assignee=self.assignee)
        parent = directive.workflow_task
        self.grant_permission(self.assignee, "assign_workflow_tasks")
        subordinate = self.make_user(
            "directive-subordinate", "DIRECTIVE-SUB", "DMS Standard User",
            supervisor=self.assignee.employee_profile,
            department=self.assignee.employee_profile.current_assignment.department,
        )

        with self.assertRaises(PermissionDenied):
            create_supporting_workflow_task(
                parent_task_id=parent.pk, actor=self.manager, assignee=subordinate,
                action_type=self.advise, instruction="Unauthorized parent actor.",
            )
        self.assignee.user_permissions.remove(Permission.objects.get(
            content_type__app_label="documents", codename="assign_workflow_tasks",
        ))
        self.assignee = get_user_model().objects.get(pk=self.assignee.pk)
        with self.assertRaises(PermissionDenied):
            create_supporting_workflow_task(
                parent_task_id=parent.pk, actor=self.assignee, assignee=subordinate,
                action_type=self.advise, instruction="No assignment capability.",
            )
        self.grant_permission(self.assignee, "assign_workflow_tasks")
        self.assignee = get_user_model().objects.get(pk=self.assignee.pk)

        for target, instruction in (
            (self.assignee, "No self assignment."),
            (self.outsider, "No unrelated assignment."),
        ):
            with self.subTest(instruction=instruction), self.assertRaises(ValidationError):
                create_supporting_workflow_task(
                    parent_task_id=parent.pk, actor=self.assignee, assignee=target,
                    action_type=self.advise, instruction=instruction,
                )
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=parent.pk, actor=self.manager, response="Issuer cannot complete.")
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=parent.pk, actor=subordinate, response="Not the parent assignee.")

        child = create_supporting_workflow_task(
            parent_task_id=parent.pk, actor=self.assignee, assignee=subordinate,
            action_type=self.advise, instruction="A supporting task for reassignment validation.",
        )
        self.grant_permission(self.manager, "reassign_workflow_tasks")
        self.manager = get_user_model().objects.get(pk=self.manager.pk)
        with self.assertRaises(ValidationError):
            reassign_task(
                task_id=child.pk, actor=self.manager, new_user=self.manager,
                reason="Cannot reassign upward around the reporting hierarchy.",
            )
        child.refresh_from_db()
        self.assertEqual(child.assigned_to, subordinate)

        complete_workflow_task(task_id=parent.pk, actor=self.assignee, response="Parent complete.")
        with self.assertRaises(PermissionDenied):
            create_supporting_workflow_task(
                parent_task_id=parent.pk, actor=self.assignee, assignee=subordinate,
                action_type=self.advise, instruction="Cannot delegate after completion.",
            )

    def test_supporting_task_model_rejects_parent_from_another_document(self):
        parent_directive = self.issue(assignee=self.assignee)
        other_document = register_incoming_correspondence(
            actor=self.registrar,
            values={
                "sender": "Another sender", "external_reference": "LEGAL-OTHER-1",
                "document_date": date(2026, 10, 5), "received_on": date(2026, 10, 7),
                "subject": "Another proposed contract", "correspondence_type": "CONTRACT_AGREEMENT",
                "other_correspondence_type": "", "receiving_office": self.office, "priority": "MEDIUM",
                "classification": Document.Classification.CONFIDENTIAL,
                "original_document": SimpleUploadedFile("other-contract.pdf", b"other contract", content_type="application/pdf"),
                "supporting_documents": [],
            },
        )
        other_directive = issue_management_directive(
            target_document=other_document, issuer=self.manager, action_type=self.advise,
            instruction="Review this other contract.", assignee=self.assignee,
        )
        other_task = other_directive.workflow_task
        with self.assertRaises(ValidationError):
            WorkflowTask.objects.create(
                instance=other_task.instance,
                submitted_version=other_document.current_version,
                step=other_task.step,
                assigned_to=self.assignee,
                cycle=other_task.cycle + 1,
                parent_task=parent_directive.workflow_task,
            )

    def test_supporting_task_notification_does_not_leak_to_a_user_without_document_access(self):
        directive = self.issue(assignee=self.assignee)
        self.grant_permission(self.assignee, "assign_workflow_tasks")
        subordinate = self.make_user(
            "directive-notify-sub", "DIRECTIVE-NOTIFY-SUB", "DMS Standard User",
            supervisor=self.assignee.employee_profile,
            department=self.assignee.employee_profile.current_assignment.department,
        )
        task = create_supporting_workflow_task(
            parent_task_id=directive.workflow_task.pk, actor=self.assignee,
            assignee=subordinate, action_type=self.advise, instruction="Prepare a short report.",
        )
        complete_workflow_task(task_id=task.pk, actor=subordinate, response="Report complete.")
        event = self.document.audit_events.get(action=AuditEvent.Action.TASK_COMPLETED, context__task_id=task.pk)
        self.assertTrue(NotificationReceipt.objects.filter(recipient=self.assignee, event=event).exists())
        outsider_receipt = NotificationReceipt.objects.create(
            recipient=self.outsider, event=event, workflow_task=task,
        )
        client = Client()
        client.force_login(self.outsider)
        self.assertEqual(client.get(reverse("notifications:list")).status_code, 200)
        self.assertNotContains(client.get(reverse("notifications:list")), self.document.reference)
        self.assertEqual(client.post(reverse("notifications:open", args=[outsider_receipt.pk])).status_code, 404)

    def test_actionable_directive_ui_creates_task_and_taskless_ui_does_not(self):
        client = Client()
        client.force_login(self.manager)
        response = client.post(reverse("documents:incoming-issue-directive", args=[self.document.pk]), {
            "action_type": self.advise.pk,
            "instruction": "Review the proposed contract and advise.",
            "assignee": self.assignee.employee_profile.pk,
            "due_date": "2026-10-12",
        })
        self.assertEqual(response.status_code, 302)
        task_directive = ManagementDirective.objects.get(target_document=self.document)
        self.assertIsNotNone(task_directive.workflow_task_id)
        response = client.post(reverse("documents:incoming-issue-directive", args=[self.document.pk]), {
            "action_type": self.acknowledge.pk,
            "instruction": "Noted. File for record.",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ManagementDirective.objects.filter(target_document=self.document).count(), 2)
        self.assertIsNone(ManagementDirective.objects.get(instruction="Noted. File for record.").workflow_task_id)
