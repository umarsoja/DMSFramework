from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.documents.models import (
    Attachment,
    Document,
    DocumentType,
    WorkflowActionType,
    WorkflowDecision,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
    WorkflowTask,
)
from apps.documents.services import create_memo, decide_review, finalize_document
from apps.documents.workflow_tasks import complete_workflow_task
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


class WorkflowActionFoundationTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="action-owner", password="pw")
        self.assignee = User.objects.create_user(username="action-assignee", password="pw")
        self.other = User.objects.create_user(username="action-other", password="pw")
        self.department = Department.objects.create(code="ACT-TEST", name="Action test")
        self.position = Position.objects.create(code="ACT-TEST", title="Action tester", department=self.department)
        self.owner_profile = EmployeeProfile.objects.create(
            user=self.owner, staff_id="ACT-OWNER", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.assignee_profile = EmployeeProfile.objects.create(
            user=self.assignee, staff_id="ACT-ASSIGNEE", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        self.assignee.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))
        create_assignment(employee=self.owner_profile, department=self.department, position=self.position, actor=self.owner)
        self.assignment = create_assignment(
            employee=self.assignee_profile, department=self.department, position=self.position, actor=self.assignee,
        )
        self.review_action = WorkflowActionType.objects.get(code="review")
        self.execute_action = WorkflowActionType.objects.get(code="execute")
        self.document_type = DocumentType.objects.get(code="IM")
        self.definition = WorkflowDefinition.objects.create(
            document_type=self.document_type, name="Action test", version=99, is_active=False,
        )
        self.step = WorkflowStep.objects.create(
            definition=self.definition, sequence=1, name="Execution", is_review=False,
            action_type=self.execute_action,
        )

    def values(self, **overrides):
        return {
            "to": "Correspondent",
            "through": "Routing office",
            "cc": "Copy recipient",
            "subject": "Action foundation",
            "body": "Please complete an action.",
            "classification": Document.Classification.INTERNAL,
            **overrides,
        }

    def test_action_catalog_and_task_fields_support_action_accountability(self):
        self.assertEqual(
            set(WorkflowActionType.objects.values_list("code", flat=True)),
            {"review", "approve", "acknowledge", "assign", "execute", "respond", "endorse", "issue", "close", "advise"},
        )
        document = create_memo(owner=self.owner, values=self.values())
        version = document.current_version
        workflow = WorkflowInstance.objects.create(document=document, definition=self.definition)
        task = WorkflowTask.objects.create(
            instance=workflow,
            step=self.step,
            action_type=self.execute_action,
            submitted_version=version,
            assigned_to=self.assignee,
            assigned_assignment=self.assignment,
            instruction="Prepare the requested commercial quotation.",
            due_at=version.created_at,
            cycle=1,
        )
        self.assertEqual(task.action_type.code, "execute")
        self.assertEqual(task.assigned_assignment.department, self.department)
        self.assertEqual(task.instruction, "Prepare the requested commercial quotation.")
        self.assertIsNotNone(task.assigned_at)
        self.assertEqual(task.due_at, version.created_at)
        self.assertIsNone(task.completed_at)

    def test_completion_records_response_timestamp_evidence_and_audit(self):
        target = create_memo(owner=self.owner, values=self.values(subject="Task target"))
        attachment = Attachment.objects.create(
            version=target.current_version,
            file="private_documents/evidence.pdf",
            original_filename="evidence.pdf",
            media_type="application/pdf",
            size=20,
            uploaded_by=self.owner,
        )
        workflow = WorkflowInstance.objects.create(document=target, definition=self.definition)
        task = WorkflowTask.objects.create(
            instance=workflow, step=self.step, action_type=self.execute_action,
            submitted_version=target.current_version,
            assigned_to=self.assignee, assigned_assignment=self.assignment,
            instruction="Provide evidence.", cycle=1,
        )
        completed = complete_workflow_task(
            task_id=task.pk, actor=self.assignee,
            response="Quotation prepared.", evidence_ids=[attachment.pk],
        )
        completed.refresh_from_db()
        self.assertEqual(completed.status, WorkflowTask.Status.COMPLETED)
        self.assertTrue(completed.completed_at)
        self.assertEqual(completed.response, "Quotation prepared.")
        self.assertEqual(list(completed.evidence.all()), [attachment])
        event = target.audit_events.get(action="TASK_COMPLETED", context__task_id=task.pk)
        self.assertEqual(event.context["response"], "Quotation prepared.")
        self.assertEqual(event.context["evidence_attachment_ids"], [attachment.pk])

    def test_task_cannot_link_evidence_from_another_document(self):
        source = create_memo(owner=self.owner, values=self.values(subject="Evidence source"))
        target = create_memo(owner=self.owner, values=self.values(subject="Task target"))
        attachment = Attachment.objects.create(
            version=source.current_version,
            file="private_documents/evidence.pdf",
            original_filename="evidence.pdf",
            media_type="application/pdf",
            size=20,
            uploaded_by=self.owner,
        )
        workflow = WorkflowInstance.objects.create(document=target, definition=self.definition)
        task = WorkflowTask.objects.create(
            instance=workflow, step=self.step, action_type=self.execute_action,
            submitted_version=target.current_version,
            assigned_to=self.assignee, assigned_assignment=self.assignment, cycle=1,
        )

        with self.assertRaises(ValidationError):
            complete_workflow_task(
                task_id=task.pk, actor=self.assignee,
                response="Evidence belongs to a different document.", evidence_ids=[attachment.pk],
            )

        task.refresh_from_db()
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)
        self.assertFalse(task.evidence.exists())

    def test_only_assignee_can_complete_task(self):
        document = create_memo(owner=self.owner, values=self.values())
        workflow = WorkflowInstance.objects.create(document=document, definition=self.definition)
        task = WorkflowTask.objects.create(
            instance=workflow, step=self.step, action_type=self.execute_action,
            submitted_version=document.current_version,
            assigned_to=self.assignee, assigned_assignment=self.assignment, cycle=1,
        )
        with self.assertRaises(PermissionDenied):
            complete_workflow_task(task_id=task.pk, actor=self.other, response="No")
        task.refresh_from_db()
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)

    def test_review_decision_stays_version_bound_and_memo_flow_is_unchanged(self):
        document = create_memo(owner=self.owner, values=self.values(), reviewer=self.assignee)
        task = document.workflow.tasks.get()
        submitted_version = task.submitted_version
        self.assertEqual(task.action_type, self.review_action)
        decision = decide_review(
            task_id=task.pk, actor=self.assignee,
            outcome=WorkflowDecision.Outcome.APPROVE,
        )
        self.assertEqual(decision.submitted_version, submitted_version)
        self.assertEqual(task.submitted_version, submitted_version)
        finalized = finalize_document(document=document, actor=self.owner)
        self.assertEqual(finalized.status, Document.Status.FINALIZED)

    def test_recipients_do_not_create_tasks(self):
        document = create_memo(owner=self.owner, values=self.values())
        self.assertEqual(document.workflow.tasks.count() if hasattr(document, "workflow") else 0, 0)

    def test_review_and_approval_tasks_require_submitted_version(self):
        document = create_memo(owner=self.owner, values=self.values())
        workflow = WorkflowInstance.objects.create(document=document, definition=self.definition)
        review_step = WorkflowStep.objects.create(
            definition=self.definition, sequence=2, name="Review", is_review=True,
            action_type=self.review_action,
        )
        with self.assertRaises(ValidationError):
            WorkflowTask.objects.create(
                instance=workflow, step=review_step, action_type=self.review_action,
                assigned_to=self.assignee, assigned_assignment=self.assignment, cycle=1,
            )
