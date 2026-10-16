from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.documents.handlers import INCOMING_CORRESPONDENCE_TYPE_CODE, INTERNAL_MEMO_TYPE_CODE
from apps.documents.models import (
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    ManagementDirective,
    WorkflowActionType,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from apps.organization.models import Department, EmployeeAssignment, EmployeeProfile, Position


@override_settings(LOGIN_REDIRECT_URL="dashboard")
class DashboardTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.department = Department.objects.create(code="DASH", name="Dashboard Office")
        self.position = Position.objects.create(code="DASH-POS", title="Dashboard employee", department=self.department)
        standard_user_group = Group.objects.get(name="DMS Standard User")
        self.user = User.objects.create_user(username="dashboard-user", password="safe-pass")
        self.profile = EmployeeProfile.objects.create(
            user=self.user, staff_id="DASH-001", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.assignment = EmployeeAssignment.objects.create(
            employee=self.profile, department=self.department, position=self.position,
            start_date=timezone.localdate(),
        )
        self.user.groups.add(standard_user_group)

        self.report_user = User.objects.create_user(username="dashboard-report", password="pw")
        self.report_profile = EmployeeProfile.objects.create(
            user=self.report_user, staff_id="DASH-002", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.report_assignment = EmployeeAssignment.objects.create(
            employee=self.report_profile, department=self.department, position=self.position,
            supervisor=self.profile, start_date=timezone.localdate(),
        )
        self.report_user.groups.add(standard_user_group)

        self.other_user = User.objects.create_user(username="dashboard-other", password="pw")
        self.other_profile = EmployeeProfile.objects.create(
            user=self.other_user, staff_id="DASH-003", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.other_assignment = EmployeeAssignment.objects.create(
            employee=self.other_profile, department=self.department, position=self.position,
            start_date=timezone.localdate(),
        )
        self.other_user.groups.add(standard_user_group)

        self.memo_type = DocumentType.objects.get(code=INTERNAL_MEMO_TYPE_CODE)
        self.incoming_type = DocumentType.objects.get(code=INCOMING_CORRESPONDENCE_TYPE_CODE)
        self.definition = WorkflowDefinition.objects.create(
            document_type=self.memo_type, name="Dashboard Test Workflow", version=90,
        )
        self.review_action = WorkflowActionType.objects.get(code="review")
        self.approve_action = WorkflowActionType.objects.get(code="approve")
        self.advise_action = WorkflowActionType.objects.get(code="advise")
        self.action_step = WorkflowStep.objects.create(
            definition=self.definition, sequence=1, name="Action", is_review=False,
            action_type=self.advise_action,
        )
        self.review_step = WorkflowStep.objects.create(
            definition=self.definition, sequence=2, name="Review", is_review=True,
            action_type=self.review_action,
        )
        self.approval_step = WorkflowStep.objects.create(
            definition=self.definition, sequence=3, name="Approval", is_review=True,
            action_type=self.approve_action,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def make_document(self, title, *, document_type=None, status=None, classification=Document.Classification.INTERNAL,
                      owner=None, grant_to=None):
        document_type = document_type or self.memo_type
        status = status or Document.Status.UNDER_REVIEW
        owner = owner or self.other_user
        document = Document.objects.create(
            document_type=document_type, title=title, classification=classification,
            status=status, created_by=owner,
        )
        version = DocumentVersion.objects.create(
            document=document, number=1, created_by=owner, content={},
        )
        if grant_to:
            DocumentAccess.objects.create(
                document=document, user=grant_to, can_view=True, can_download=False,
                granted_by=owner,
            )
        return document, version

    def add_task(self, document, version, *, assigned_user=None, action_type=None, step=None, due_at=None):
        assigned_user = assigned_user or self.user
        workflow, _ = WorkflowInstance.objects.get_or_create(
            document=document, defaults={"definition": self.definition},
        )
        if step is None:
            step = self.action_step
        return WorkflowTask.objects.create(
            instance=workflow, submitted_version=version, step=step,
            action_type=action_type or step.action_type, assigned_to=assigned_user,
            assigned_assignment=assigned_user.employee_profile.current_assignment,
            cycle=step.sequence, due_at=due_at,
        )

    def test_login_redirects_to_authenticated_dashboard(self):
        client = Client()
        response = client.post(reverse("dms-login"), {
            "username": self.user.username,
            "password": "safe-pass",
        })
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(client.get(reverse("dashboard")).status_code, 200)

    def test_dashboard_requires_authenticated_dms_access(self):
        response = Client().get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("dms-login"), response.url)

    def test_kpis_distinguish_actions_reviews_and_actual_approvals(self):
        action_doc, action_version = self.make_document("Action memo")
        self.add_task(action_doc, action_version, due_at=timezone.now() - timedelta(days=1))
        review_doc, review_version = self.make_document("Review memo")
        self.add_task(review_doc, review_version, step=self.review_step, action_type=self.review_action)
        approval_doc, approval_version = self.make_document("Approval memo")
        self.add_task(approval_doc, approval_version, step=self.approval_step, action_type=self.approve_action)
        review_without_catalog_action = WorkflowStep.objects.create(
            definition=self.definition, sequence=4, name="Configured review", is_review=True,
        )
        untyped_review_doc, untyped_review_version = self.make_document("Configured review memo")
        self.add_task(
            untyped_review_doc, untyped_review_version,
            step=review_without_catalog_action, action_type=None,
        )

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pending_actions_count"], 1)
        self.assertEqual(response.context["pending_reviews_count"], 2)
        self.assertEqual(response.context["pending_approvals_count"], 1)
        self.assertEqual(response.context["overdue_count"], 1)
        self.assertContains(response, "Pending Reviews")
        self.assertContains(response, "Pending Approvals")

    def test_direct_report_task_shows_only_when_document_is_independently_visible(self):
        visible_document, visible_version = self.make_document(
            "Shared team memo", grant_to=self.user,
        )
        self.add_task(visible_document, visible_version, assigned_user=self.report_user)
        hidden_document, hidden_version = self.make_document("Private team memo")
        self.add_task(hidden_document, hidden_version, assigned_user=self.report_user)
        unrelated_document, unrelated_version = self.make_document(
            "Unrelated employee memo", grant_to=self.user,
        )
        self.add_task(unrelated_document, unrelated_version, assigned_user=self.other_user)

        response = self.client.get(reverse("dashboard"))
        team_tasks = list(response.context["team_pending_tasks"])
        self.assertEqual([task.instance.document_id for task in team_tasks], [visible_document.pk])
        self.assertEqual(response.context["team_pending_count"], 1)
        self.assertContains(response, "Shared team memo")
        self.assertNotContains(response, "Private team memo")
        self.assertNotIn(unrelated_document.pk, [task.instance.document_id for task in team_tasks])

    def test_team_scope_uses_only_current_supervisor_assignment(self):
        today = timezone.localdate()
        self.report_assignment.end_date = today
        self.report_assignment.save(update_fields=["end_date", "updated_at"])
        EmployeeAssignment.objects.create(
            employee=self.report_profile, department=self.department, position=self.position,
            start_date=today + timedelta(days=1),
        )
        document, version = self.make_document("Former direct report task", grant_to=self.user)
        self.add_task(document, version, assigned_user=self.report_user)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["team_pending_count"], 0)
        self.assertEqual(list(response.context["team_pending_tasks"]), [])

    def test_incoming_and_memo_counts_follow_document_access_and_classification(self):
        awaiting, _ = self.make_document(
            "Visible incoming", document_type=self.incoming_type,
            status=Document.Status.REGISTERED, classification=Document.Classification.CONFIDENTIAL,
            grant_to=self.user,
        )
        AuditEvent.objects.create(
            document=awaiting, actor=self.other_user,
            action=AuditEvent.Action.INCOMING_REGISTERED,
        )
        managed, _ = self.make_document(
            "Managed incoming", document_type=self.incoming_type,
            status=Document.Status.REGISTERED, grant_to=self.user,
        )
        ManagementDirective.objects.create(
            target_document=managed, issued_by=self.user, action_type=self.advise_action,
            instruction="Management has reviewed this record.",
        )
        private, _ = self.make_document(
            "Restricted hidden correspondence", document_type=self.incoming_type,
            status=Document.Status.REGISTERED, classification=Document.Classification.RESTRICTED,
        )
        pending_private, _ = self.make_document(
            "Restricted hidden memo", classification=Document.Classification.RESTRICTED,
        )
        self.make_document("Visible draft", status=Document.Status.DRAFT, grant_to=self.user)
        self.make_document("Visible returned memo", status=Document.Status.RETURNED, grant_to=self.user)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["awaiting_management_count"], 1)
        self.assertEqual(response.context["recent_correspondence_count"], 1)
        self.assertEqual(response.context["pending_memos_count"], 2)
        self.assertContains(response, "Visible incoming")
        self.assertNotContains(response, "Restricted hidden correspondence")
        self.assertNotContains(response, "Restricted hidden memo")

        incoming_list = self.client.get(reverse("documents:incoming-list") + "?awaiting_management=1")
        self.assertContains(incoming_list, "Visible incoming")
        self.assertNotContains(incoming_list, "Managed incoming")
        memo_list = self.client.get(reverse("documents:memo-list") + "?status=PENDING")
        self.assertContains(memo_list, "Visible draft")
        self.assertContains(memo_list, "Visible returned memo")

    def test_recent_activity_excludes_inaccessible_document_events(self):
        visible, _ = self.make_document("Visible activity memo", grant_to=self.user)
        hidden, _ = self.make_document("Secret activity memo")
        AuditEvent.objects.create(document=visible, actor=self.other_user, action=AuditEvent.Action.SUBMITTED)
        AuditEvent.objects.create(document=hidden, actor=self.other_user, action=AuditEvent.Action.SUBMITTED)

        response = self.client.get(reverse("dashboard"))
        events = list(response.context["recent_activity"])
        self.assertTrue(any(event.document_id == visible.pk for event in events))
        self.assertFalse(any(event.document_id == hidden.pk for event in events))
        self.assertContains(response, "Visible activity memo")
        self.assertNotContains(response, "Secret activity memo")
