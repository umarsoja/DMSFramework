import tempfile
from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.documents.handlers import INCOMING_CORRESPONDENCE_TYPE_CODE
from apps.documents.models import (
    AuditEvent, Document, DocumentAccess, DocumentType, DocumentVersion,
    EmailOutbox, NotificationReceipt, WorkflowActionType, WorkflowDecision, WorkflowDefinition, WorkflowInstance,
    WorkflowStep, WorkflowTask,
)
from apps.documents.notifications import notification_menu_data, record_notification
from apps.documents.services import (
    create_memo, decide_review, issue_management_directive,
    reassign_task, register_incoming_correspondence,
)
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


@override_settings(LOGIN_REDIRECT_URL="dashboard")
class NotificationExperienceTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        User = get_user_model()
        self.manager = self.make_user("notice-manager", "NOTICE-MGR", "DMS Department Manager/HOD")
        self.assignee = self.make_user("notice-assignee", "NOTICE-ASG", "DMS Standard User",
                                       supervisor=self.manager.employee_profile)
        self.registrar = self.make_user("notice-registrar", "NOTICE-REG", "DMS Records/Registry Officer")
        self.outsider = self.make_user("notice-outsider", "NOTICE-OUT", "DMS Standard User")
        self.grant(self.manager, "issue_management_directives")
        self.grant(self.manager, "assign_workflow_tasks")
        self.grant(self.manager, "reassign_workflow_tasks")
        self.grant(self.registrar, "register_incoming_correspondence")
        self.office = self.manager.employee_profile.current_assignment.department
        self.office.head = self.manager.employee_profile
        self.office.save(update_fields=["head"])
        self.advise = WorkflowActionType.objects.get(code="advise", is_active=True)
        self.incoming = register_incoming_correspondence(actor=self.registrar, values={
            "sender": "External sender", "external_reference": "NOTICE-EXT-1",
            "document_date": date(2026, 10, 1), "received_on": date(2026, 10, 2),
            "subject": "Management notification test", "correspondence_type": "OTHER",
            "other_correspondence_type": "Test", "receiving_office": self.office, "priority": "MEDIUM",
            "classification": Document.Classification.CONFIDENTIAL,
            "original_document": SimpleUploadedFile("notice.pdf", b"notice", content_type="application/pdf"),
            "supporting_documents": [],
        })

    def tearDown(self):
        self.settings_override.disable()
        self.media_dir.cleanup()

    def make_user(self, username, staff_id, group, *, supervisor=None):
        User = get_user_model()
        user = User.objects.create_user(username=username, password="pw")
        profile = EmployeeProfile.objects.create(
            user=user, staff_id=staff_id, account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        department = Department.objects.get_or_create(code="NOTICE-UNIT", defaults={"name": "Notice Unit"})[0]
        position = Position.objects.create(code=f"{staff_id}-POS", title=f"{username} position", department=department)
        create_assignment(employee=profile, department=department, position=position,
                          supervisor=supervisor, actor=user)
        user.groups.add(Group.objects.get(name=group))
        return user

    @staticmethod
    def grant(user, codename):
        user.user_permissions.add(Permission.objects.get(
            content_type__app_label="documents", codename=codename,
        ))

    def test_incoming_registration_creates_receipt_only_for_designated_manager(self):
        receipt = NotificationReceipt.objects.get(recipient=self.manager)
        self.assertEqual(receipt.event.action, AuditEvent.Action.INCOMING_REGISTERED)
        self.assertFalse(NotificationReceipt.objects.filter(recipient=self.outsider).exists())
        self.assertEqual(notification_menu_data(self.manager)["notification_unread_count"], 1)

    def test_task_directive_generates_actionable_receipt_and_shows_due_date(self):
        directive = issue_management_directive(
            target_document=self.incoming, issuer=self.manager, action_type=self.advise,
            instruction="Review and respond.", assignee=self.assignee, due_date=date(2026, 10, 15),
        )
        receipt = NotificationReceipt.objects.get(recipient=self.assignee, workflow_task=directive.workflow_task)
        self.assertEqual(receipt.event.action, AuditEvent.Action.DIRECTIVE_ISSUED)
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 1)
        client = Client()
        client.force_login(self.assignee)
        response = client.get(reverse("dashboard"))
        self.assertContains(response, "Advise")
        self.assertContains(response, "Due Oct 15, 2026")
        self.assertContains(response, self.incoming.reference)

    def test_seen_state_changes_badge_without_changing_pending_task(self):
        directive = issue_management_directive(
            target_document=self.incoming, issuer=self.manager, action_type=self.advise,
            instruction="Review and respond.", assignee=self.assignee,
        )
        receipt = NotificationReceipt.objects.get(recipient=self.assignee, workflow_task=directive.workflow_task)
        client = Client()
        client.force_login(self.assignee)
        response = client.post(reverse("notifications:open", args=[receipt.pk]))
        self.assertRedirects(response, reverse("documents:incoming-detail", args=[self.incoming.pk]), fetch_redirect_response=False)
        receipt.refresh_from_db()
        directive.workflow_task.refresh_from_db()
        self.assertIsNotNone(receipt.seen_at)
        self.assertEqual(directive.workflow_task.status, WorkflowTask.Status.PENDING)
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 0)

    def test_notification_list_and_open_enforce_recipient_and_document_visibility(self):
        receipt = NotificationReceipt.objects.get(recipient=self.manager)
        client = Client()
        client.force_login(self.outsider)
        denied = client.post(reverse("notifications:open", args=[receipt.pk]))
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(client.get(reverse("notifications:list")).status_code, 200)

        client.force_login(self.manager)
        self.assertContains(client.get(reverse("notifications:list")), self.incoming.reference)
        DocumentAccess.objects.filter(document=self.incoming, user=self.manager).delete()
        hidden = client.get(reverse("notifications:list"))
        self.assertNotContains(hidden, self.incoming.reference)
        self.assertNotContains(hidden, "Management notification test")
        self.assertEqual(client.post(reverse("notifications:open", args=[receipt.pk])).status_code, 404)

    def test_open_notification_navigates_to_existing_record_detail(self):
        receipt = NotificationReceipt.objects.get(recipient=self.manager)
        client = Client()
        client.force_login(self.manager)
        response = client.post(reverse("notifications:open", args=[receipt.pk]))
        self.assertRedirects(response, reverse("documents:incoming-detail", args=[self.incoming.pk]), fetch_redirect_response=False)

    def test_header_badge_displays_zero_one_and_multiple_unseen_counts(self):
        client = Client()
        client.force_login(self.manager)
        receipt = NotificationReceipt.objects.get(recipient=self.manager)
        receipt.seen_at = timezone.now()
        receipt.save(update_fields=["seen_at"])
        response = client.get(reverse("dashboard"))
        self.assertNotContains(response, "dms-notification-count")

        for number in (2, 3):
            document = register_incoming_correspondence(actor=self.registrar, values={
                "sender": "External sender", "external_reference": f"NOTICE-EXT-{number}",
                "document_date": date(2026, 10, 1), "received_on": date(2026, 10, 2),
                "subject": f"Management notification {number}", "correspondence_type": "OTHER",
                "other_correspondence_type": "Test", "receiving_office": self.office, "priority": "MEDIUM",
                "classification": Document.Classification.CONFIDENTIAL,
                "original_document": SimpleUploadedFile(f"notice-{number}.pdf", b"notice", content_type="application/pdf"),
                "supporting_documents": [],
            })
            self.assertIsNotNone(document.pk)
            response = client.get(reverse("dashboard"))
            self.assertContains(response, f'<span class="dms-notification-count" aria-hidden="true">{number - 1}</span>', html=True)

    def test_unread_badge_counts_only_actionable_receipts_not_seen_or_completed_tasks(self):
        directive = issue_management_directive(
            target_document=self.incoming, issuer=self.manager, action_type=self.advise,
            instruction="Review and respond.", assignee=self.assignee,
        )
        receipt = NotificationReceipt.objects.get(recipient=self.assignee, workflow_task=directive.workflow_task)
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 1)
        receipt.seen_at = timezone.now()
        receipt.save(update_fields=["seen_at"])
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 0)
        directive.workflow_task.status = WorkflowTask.Status.COMPLETED
        directive.workflow_task.save(update_fields=["status"])
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 0)

    def make_submitted_memo(self, *, action_code="review", subject="Notification memo"):
        action = WorkflowActionType.objects.get(code=action_code)
        # Memo workflows are configured separately from incoming correspondence.
        from apps.documents.models import DocumentType
        memo_type = DocumentType.objects.get(code="IM")
        step = memo_type.workflows.first().steps.filter(is_review=True).order_by("sequence").first()
        step.action_type = action
        step.save(update_fields=["action_type"])
        return create_memo(
            # Route upward through the manager; subordinate review selection is
            # intentionally rejected by the memo service.
            owner=self.assignee,
            reviewer=self.manager,
            values={
                "to": "Managing Director", "through": "Executive Director", "cc": "Finance",
                "subject": subject, "body": "Please review this memo.",
                "classification": Document.Classification.INTERNAL,
            },
        )

    def test_memo_review_and_approval_assignments_notify_actual_assignee(self):
        review_memo = self.make_submitted_memo(subject="Review me")
        review_task = review_memo.workflow.tasks.get()
        review_receipt = NotificationReceipt.objects.get(recipient=self.manager, workflow_task=review_task)
        self.assertEqual(review_receipt.title, "Memo requires your review")
        self.assertFalse(NotificationReceipt.objects.filter(recipient=self.outsider).exists())

        approval_memo = self.make_submitted_memo(action_code="approve", subject="Approve me")
        approval_task = approval_memo.workflow.tasks.get()
        approval_receipt = NotificationReceipt.objects.get(recipient=self.manager, workflow_task=approval_task)
        self.assertEqual(approval_receipt.title, "Memo requires your approval")
        self.assertEqual(notification_menu_data(self.manager)["notification_unread_count"], 3)

    def test_actionable_receipt_queues_one_workflow_email_without_sending(self):
        self.manager.email = "manager@example.test"
        self.manager.save(update_fields=["email"])
        with override_settings(WORKFLOW_EMAIL_NOTIFICATIONS=True):
            memo = self.make_submitted_memo(subject="Email queue test")
        task = memo.workflow.tasks.get()
        self.assertEqual(
            EmailOutbox.objects.filter(
                message_type=EmailOutbox.MessageType.WORKFLOW_NOTIFICATION,
                recipient=self.manager,
                source_reference=f"workflow:{task.instance.document.audit_events.filter(action=AuditEvent.Action.UNDER_REVIEW).first().pk}:{self.manager.pk}",
            ).count(),
            1,
        )
        # Re-recording the same event is idempotent.
        event = task.instance.document.audit_events.get(action=AuditEvent.Action.UNDER_REVIEW)
        record_notification(event, self.manager, workflow_task=task)
        self.assertEqual(EmailOutbox.objects.filter(message_type=EmailOutbox.MessageType.WORKFLOW_NOTIFICATION).count(), 1)

    def test_workflow_email_notifications_default_off_preserves_receipts(self):
        self.manager.email = "manager@example.test"
        self.manager.save(update_fields=["email"])
        with override_settings(WORKFLOW_EMAIL_NOTIFICATIONS=False):
            memo = self.make_submitted_memo(subject="In-app only notification")
        task = memo.workflow.tasks.get()
        self.assertTrue(NotificationReceipt.objects.filter(
            recipient=self.manager, workflow_task=task,
        ).exists())
        self.assertFalse(EmailOutbox.objects.filter(
            message_type=EmailOutbox.MessageType.WORKFLOW_NOTIFICATION,
        ).exists())

    def test_reassignment_notifies_new_assignee_and_existing_receipt_stops_actionable(self):
        memo = self.make_submitted_memo()
        task = memo.workflow.tasks.get()
        old_receipt = NotificationReceipt.objects.get(recipient=self.manager, workflow_task=task)
        reassign_task(task_id=task.pk, actor=self.manager, new_user=self.registrar, reason="Coverage change")
        task.refresh_from_db()
        self.assertEqual(task.assigned_to, self.registrar)
        self.assertTrue(NotificationReceipt.objects.filter(
            recipient=self.registrar, event__action=AuditEvent.Action.REASSIGNED, workflow_task=task,
        ).exists())
        self.assertEqual(notification_menu_data(self.manager)["notification_unread_count"], 1)
        self.assertEqual(notification_menu_data(self.registrar)["notification_unread_count"], 1)
        self.assertIsNone(old_receipt.seen_at)

    def test_approved_and_returned_memos_notify_original_initiator_and_render(self):
        approved = self.make_submitted_memo(subject="Approval outcome")
        approval_task = approved.workflow.tasks.get()
        decide_review(task_id=approval_task.pk, actor=self.manager, outcome=WorkflowDecision.Outcome.APPROVE)
        approved_receipt = NotificationReceipt.objects.get(recipient=self.assignee, event__action=AuditEvent.Action.APPROVED)
        self.assertEqual(approved_receipt.title, "Your memo has been approved.")
        self.assertEqual(approved_receipt.event.document.created_by, self.assignee)
        record_notification(approved_receipt.event, self.assignee)
        self.assertEqual(NotificationReceipt.objects.filter(
            recipient=self.assignee, event=approved_receipt.event,
        ).count(), 1)

        returned = self.make_submitted_memo(subject="Return outcome")
        return_task = returned.workflow.tasks.get()
        decide_review(task_id=return_task.pk, actor=self.manager, outcome=WorkflowDecision.Outcome.RETURN,
                      comment="Please correct the cost estimate.")
        returned_receipt = NotificationReceipt.objects.get(recipient=self.assignee, event__action=AuditEvent.Action.RETURNED)
        self.assertEqual(returned_receipt.title, "Your memo has been returned for revision.")
        self.assertEqual(returned_receipt.message, "Please correct the cost estimate.")
        self.assertEqual(notification_menu_data(self.assignee)["notification_unread_count"], 2)

        client = Client()
        client.force_login(self.assignee)
        dashboard = client.get(reverse("dashboard"))
        self.assertContains(dashboard, "Your memo has been approved.")
        self.assertContains(dashboard, "Approval outcome")
        self.assertContains(dashboard, "Please correct the cost estimate.")
        recent = client.get(reverse("notifications:list"))
        self.assertContains(recent, "Your memo has been returned for revision.")
        self.assertContains(recent, returned.reference)

        response = client.post(reverse("notifications:open", args=[approved_receipt.pk]))
        self.assertRedirects(response, reverse("documents:memo-detail", args=[approved.pk]), fetch_redirect_response=False)
        approved_receipt.refresh_from_db()
        approval_task.refresh_from_db()
        self.assertIsNotNone(approved_receipt.seen_at)
        self.assertEqual(approval_task.status, WorkflowTask.Status.APPROVED)

    def test_rolled_back_memo_decision_leaves_no_outcome_receipt(self):
        from django.db import transaction

        memo = self.make_submitted_memo()
        task = memo.workflow.tasks.get()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                decide_review(task_id=task.pk, actor=self.manager, outcome=WorkflowDecision.Outcome.APPROVE)
                raise RuntimeError("rollback enclosing business operation")
        memo.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(memo.status, Document.Status.UNDER_REVIEW)
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)
        self.assertFalse(NotificationReceipt.objects.filter(
            recipient=self.assignee, event__action=AuditEvent.Action.APPROVED,
        ).exists())

    def test_failed_memo_decision_does_not_notify_initiator(self):
        memo = self.make_submitted_memo()
        task = memo.workflow.tasks.get()
        with self.assertRaises(ValidationError):
            decide_review(task_id=task.pk, actor=self.manager, outcome=WorkflowDecision.Outcome.RETURN)
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)
        self.assertFalse(NotificationReceipt.objects.filter(
            recipient=self.assignee, event__action=AuditEvent.Action.RETURNED,
        ).exists())
