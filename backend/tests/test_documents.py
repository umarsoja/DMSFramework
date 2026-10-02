import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test.utils import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.documents.access import can_download_document, can_view_document
from apps.documents.forms import MemoForm
from apps.documents.models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    WorkflowDecision,
    WorkflowTask,
)
from apps.documents.services import (
    archive_document,
    create_memo,
    decide_review,
    finalize_document,
    save_memo,
)


class InternalMemoLifecycleTests(TestCase):
    databases = {"default"}

    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        User = get_user_model()
        self.owner = User.objects.create_user(username="memo-owner", password="pw")
        self.reviewer = User.objects.create_user(username="memo-reviewer", password="pw", is_staff=True)
        self.outsider = User.objects.create_user(username="memo-outsider", password="pw", is_staff=True)
        self.viewer = User.objects.create_user(username="memo-viewer", password="pw")
        self.document_type = DocumentType.objects.get(code="IM")

    def tearDown(self):
        self.media_override.disable()
        self.media_dir.cleanup()

    @staticmethod
    def values(subject="Generator maintenance plan", **overrides):
        data = {
            "to": "Managing Director",
            "through": "Director, Operations",
            "cc": "Finance Department",
            "subject": subject,
            "body": "Please review the attached plan.\n\nFurther details follow.",
            "classification": Document.Classification.INTERNAL,
        }
        data.update(overrides)
        return data

    def make_draft(self, *, uploads=()):
        return create_memo(owner=self.owner, values=self.values(), uploads=uploads)

    def submit(self, document):
        return save_memo(
            document=document,
            actor=self.owner,
            values=self.values(),
            reviewer=self.reviewer,
        )

    def approve_and_finalize(self, document):
        task = WorkflowTask.objects.get(instance__document=document, status=WorkflowTask.Status.PENDING)
        decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        return finalize_document(document=document, actor=self.owner)

    def test_create_and_save_draft_assigns_system_fields_and_audit(self):
        document = self.make_draft()
        self.assertRegex(document.reference, r"^APGC/IM/\d{4}/\d{6,}$")
        self.assertEqual(document.status, Document.Status.DRAFT)
        self.assertEqual(document.created_by, self.owner)
        self.assertEqual(document.versions.count(), 1)
        self.assertEqual(document.current_version.content["to"], "Managing Director")
        self.assertTrue(can_view_document(self.owner, document))
        self.assertTrue(can_download_document(self.owner, document))
        self.assertSetEqual(set(document.audit_events.values_list("action", flat=True)), {
            AuditEvent.Action.CREATED, AuditEvent.Action.DRAFT_SAVED,
        })

    def test_http_create_saves_draft_without_accepting_system_fields(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("documents:memo-create"), {
            **self.values(),
            "action": "save",
        })
        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(created_by=self.owner)
        self.assertEqual(response["Location"], reverse("documents:memo-detail", args=[document.pk]))
        self.assertNotEqual(document.reference, "")

    def test_draft_edit_creates_an_immutable_prior_version(self):
        document = self.make_draft()
        original_version = document.current_version
        save_memo(document=document, actor=self.owner, values=self.values("Updated subject"))
        document.refresh_from_db()
        self.assertEqual(document.current_version_number, 2)
        self.assertEqual(document.versions.get(number=1).content["subject"], "Generator maintenance plan")
        self.assertEqual(document.current_version.content["subject"], "Updated subject")
        self.assertNotEqual(original_version.pk, document.current_version.pk)
        original_version.content = {"subject": "tampered"}
        with self.assertRaises(ValidationError):
            original_version.save()
        with self.assertRaises(ValidationError):
            original_version.delete()

    def test_submit_assigns_reviewer_and_reviewer_can_open_memo(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        self.assertEqual(document.status, Document.Status.UNDER_REVIEW)
        self.assertEqual(task.submitted_version, document.current_version)
        self.assertEqual(task.assigned_to, self.reviewer)
        submitted_event = document.audit_events.get(action=AuditEvent.Action.SUBMITTED)
        self.assertEqual(submitted_event.version, task.submitted_version)
        self.assertEqual(submitted_event.context["workflow_id"], task.instance_id)
        self.assertTrue(can_view_document(self.reviewer, document))
        self.assertFalse(can_download_document(self.reviewer, document))
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review this memo")

    def test_return_requires_reason_and_preserves_decision_before_resubmission(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        first_version = task.submitted_version
        with self.assertRaises(ValidationError):
            decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.RETURN)
        decision = decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.RETURN,
                                 comment="Please add the maintenance date.")
        self.assertEqual(decision.task.submitted_version, first_version)
        self.assertEqual(decision.submitted_version, first_version)
        returned_event = document.audit_events.get(action=AuditEvent.Action.RETURNED)
        self.assertEqual(returned_event.version, first_version)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.RETURNED)
        save_memo(document=document, actor=self.owner, values=self.values("Updated maintenance plan"),
                  revision_note="Added the requested maintenance date.", reviewer=self.reviewer)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.UNDER_REVIEW)
        self.assertEqual(document.versions.count(), 3)
        self.assertEqual(document.workflow.tasks.count(), 2)
        self.assertEqual(decision.comment, "Please add the maintenance date.")
        self.assertEqual(document.workflow.tasks.get(cycle=2).assigned_to, self.reviewer)
        second_task = document.workflow.tasks.get(cycle=2)
        self.assertEqual(second_task.submitted_version, document.current_version)
        self.assertEqual(decision.task.submitted_version, first_version)
        resubmitted_event = document.audit_events.get(action=AuditEvent.Action.RESUBMITTED)
        self.assertEqual(resubmitted_event.version, second_task.submitted_version)
        self.assertTrue(document.audit_events.filter(action=AuditEvent.Action.RESUBMITTED).exists())

    def test_approval_and_finalization_remain_bound_when_current_version_changes(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        submitted_version = task.submitted_version
        later_version = DocumentVersion.objects.create(
            document=document,
            number=submitted_version.number + 1,
            content={**submitted_version.content, "subject": "Later, unsubmitted version"},
            created_by=self.owner,
        )
        document.current_version_number = later_version.number
        document.save(update_fields=["current_version_number"])

        decision = decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        self.assertEqual(decision.task.submitted_version, submitted_version)
        self.assertEqual(decision.submitted_version, submitted_version)
        approved_event = document.audit_events.get(action=AuditEvent.Action.APPROVED)
        self.assertEqual(approved_event.version, submitted_version)

        finalize_document(document=document, actor=self.owner)
        finalized_event = document.audit_events.get(action=AuditEvent.Action.FINALIZED)
        self.assertEqual(finalized_event.version, submitted_version)
        self.assertEqual(document.current_version_number, later_version.number)

    def test_workflow_task_rejects_missing_or_cross_document_version_binding(self):
        document = self.submit(self.make_draft())
        other_document = self.make_draft()
        task = document.workflow.tasks.get()
        step = task.step

        with self.assertRaisesMessage(ValidationError, "A new workflow task must identify its submitted document version."):
            WorkflowTask.objects.create(
                instance=task.instance, step=step, assigned_to=self.reviewer, cycle=task.cycle + 1,
            )
        with self.assertRaises(ValidationError):
            WorkflowTask.objects.create(
                instance=task.instance,
                submitted_version=other_document.current_version,
                step=step,
                assigned_to=self.reviewer,
                cycle=task.cycle + 1,
            )

        task.submitted_version = other_document.current_version
        with self.assertRaisesMessage(ValidationError, "A workflow task's submitted document version cannot be changed."):
            task.save(update_fields=["submitted_version"])

    def test_unbound_historical_task_cannot_decide_using_current_version(self):
        document = self.submit(self.make_draft())
        task = document.workflow.tasks.get()
        WorkflowTask.objects.filter(pk=task.pk).update(submitted_version=None)

        with self.assertRaisesMessage(
            ValidationError,
            "This workflow task has no safely identified submitted version and cannot be decided.",
        ):
            decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)

        task.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(task.status, WorkflowTask.Status.PENDING)
        self.assertEqual(document.status, Document.Status.UNDER_REVIEW)

    def test_approve_finalize_archive_and_pdf_lifecycle(self):
        document = self.submit(self.make_draft())
        self.approve_and_finalize(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.FINALIZED)
        self.assertEqual(document.finalized_by, self.owner)

        self.client.force_login(self.owner)
        pdf_response = self.client.get(reverse("documents:memo-pdf-view", args=[document.pk]))
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response["Content-Type"], "application/pdf")
        self.assertTrue(pdf_response.content.startswith(b"%PDF-"))
        self.assertIn("inline", pdf_response["Content-Disposition"])
        download_response = self.client.get(reverse("documents:memo-pdf-download", args=[document.pk]))
        self.assertEqual(download_response.status_code, 200)
        self.assertIn("attachment", download_response["Content-Disposition"])

        archive_document(document=document, actor=self.owner)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.ARCHIVED)
        self.assertTrue(document.audit_events.filter(action=AuditEvent.Action.ARCHIVED).exists())

    def test_access_is_object_scoped_and_view_does_not_imply_download(self):
        document = self.submit(self.make_draft())
        self.assertFalse(can_view_document(self.outsider, document))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 404)

        DocumentAccess.objects.create(document=document, user=self.viewer, can_view=True,
                                      can_download=False, granted_by=self.owner)
        self.assertTrue(can_view_document(self.viewer, document))
        self.assertFalse(can_download_document(self.viewer, document))
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-download", args=[document.pk])).status_code, 403)

    def test_assigned_reviewer_cannot_decide_for_someone_elses_task(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        with self.assertRaises(PermissionDenied):
            decide_review(task_id=task.pk, actor=self.outsider, outcome=WorkflowDecision.Outcome.APPROVE)
        self.assertEqual(document.workflow.tasks.get().status, WorkflowTask.Status.PENDING)

    def test_submission_rejects_inactive_nonstaff_or_self_reviewer(self):
        document = self.make_draft()
        inactive_staff = get_user_model().objects.create_user(
            username="inactive-reviewer", password="pw", is_staff=True, is_active=False
        )
        regular_user = get_user_model().objects.create_user(username="regular-reviewer", password="pw")
        for reviewer in (self.owner, inactive_staff, regular_user):
            with self.subTest(reviewer=reviewer.username), self.assertRaises(ValidationError):
                save_memo(document=document, actor=self.owner, values=self.values(), reviewer=reviewer)

    def test_invalid_transitions_and_non_owner_mutations_are_rejected(self):
        document = self.make_draft()
        with self.assertRaises(ValidationError):
            finalize_document(document=document, actor=self.owner)
        with self.assertRaises(PermissionDenied):
            save_memo(document=document, actor=self.outsider, values=self.values("Unauthorized"))
        with self.assertRaises(ValidationError):
            archive_document(document=document, actor=self.owner)

    def test_read_only_grant_is_enforced_for_pdf_and_attachment_downloads(self):
        pdf = SimpleUploadedFile("supporting-note.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        document = create_memo(owner=self.owner, values=self.values(), uploads=[pdf], reviewer=self.reviewer)
        self.approve_and_finalize(document)
        attachment = Attachment.objects.get(version=document.current_version)
        DocumentAccess.objects.create(document=document, user=self.viewer, can_view=True,
                                      can_download=False, granted_by=self.owner)
        self.client.force_login(self.viewer)
        view_response = self.client.get(reverse("documents:memo-pdf-view", args=[document.pk]))
        self.assertEqual(view_response.status_code, 200)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-download", args=[document.pk])).status_code, 403)
        attachment_view = self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk]))
        self.assertEqual(attachment_view.status_code, 200)
        self.assertIn("inline", attachment_view["Content-Disposition"])
        self.assertEqual(b"".join(attachment_view.streaming_content), b"%PDF-1.4 test")
        self.assertEqual(self.client.get(reverse("documents:attachment-download", args=[document.pk, attachment.pk])).status_code, 403)
        stored_path = Path(attachment.file.name)
        self.assertTrue(stored_path.parts[0] == "private_documents")

    def test_attachment_is_private_and_owner_can_download_through_authorized_route(self):
        uploaded = SimpleUploadedFile("plan.pdf", b"%PDF-1.4 plan", content_type="application/pdf")
        document = self.make_draft(uploads=[uploaded])
        attachment = Attachment.objects.get(version=document.current_version)
        self.assertTrue(attachment.file.name.startswith("private_documents/"))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("documents:attachment-download", args=[document.pk, attachment.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertTrue(document.audit_events.filter(action=AuditEvent.Action.DOWNLOADED).exists())
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4 plan")

    def test_attachment_routes_require_document_access_and_uploads_are_validated(self):
        invalid = SimpleUploadedFile("payload.exe", b"not allowed", content_type="application/octet-stream")
        form = MemoForm(data=self.values(), files={"attachments": invalid}, user=self.owner)
        self.assertFalse(form.is_valid())
        self.assertIn("attachments", form.errors)
        document = self.make_draft()
        attachment = Attachment.objects.create(
            version=document.current_version,
            original_filename="private.pdf",
            media_type="application/pdf",
            size=4,
            uploaded_by=self.owner,
        )
        attachment.file.save("private.pdf", SimpleUploadedFile("private.pdf", b"%PDF"), save=True)
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk]))
        self.assertEqual(response.status_code, 404)

    def test_audit_records_cannot_be_updated_or_deleted(self):
        document = self.make_draft()
        event = document.audit_events.first()
        event.context = {"changed": True}
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_document_and_audit_changes_roll_back_together(self):
        with patch("apps.documents.services._record", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.make_draft()
        self.assertEqual(Document.objects.count(), 0)

    def test_access_grant_cannot_download_without_view_permission(self):
        document = self.make_draft()
        grant = DocumentAccess(document=document, user=self.viewer, can_view=False,
                              can_download=True, granted_by=self.owner)
        with self.assertRaises(ValidationError):
            grant.full_clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentAccess.objects.create(document=document, user=self.viewer, can_view=False,
                                          can_download=True, granted_by=self.owner)

