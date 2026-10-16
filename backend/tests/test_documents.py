import tempfile
import base64
import re
import zlib
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
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
from apps.documents.pdf import render_memo_pdf
from apps.documents.services import (
    archive_document,
    create_memo,
    decide_review,
    finalize_document,
    save_memo,
)
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


def response_body(response):
    return b"".join(response.streaming_content) if response.streaming else response.content


def decoded_pdf_page_content(pdf_bytes):
    decoded = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        encoded = match.group(1).strip()
        if encoded.endswith(b"~>"):
            encoded = encoded[:-2]
        try:
            decoded.append(zlib.decompress(base64.a85decode(encoded)))
        except (ValueError, zlib.error):
            continue
    return b"".join(decoded)


class InternalMemoLifecycleTests(TestCase):
    databases = {"default"}

    def setUp(self):
        self.media_dir = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_override.enable()
        User = get_user_model()
        self.owner = User.objects.create_user(username="memo-owner", first_name="Iliyas", last_name="Nasir", password="pw")
        self.reviewer = User.objects.create_user(username="memo-reviewer", first_name="Umar", last_name="Mohammed", password="pw")
        self.department_head = User.objects.create_user(username="memo-department-head", first_name="Amaka", last_name="Okafor", password="pw")
        self.outsider = User.objects.create_user(username="memo-outsider", password="pw")
        self.viewer = User.objects.create_user(username="memo-viewer", password="pw")
        self.department = Department.objects.create(code="MEMO-TEST", name="Memo test department")
        self.position = Position.objects.create(code="MEMO-TEST", title="Memo test position", department=self.department)
        role_assignments = (
            (self.owner, "MEMO-OWNER", "DMS Standard User"),
            (self.reviewer, "MEMO-REVIEWER", "DMS Department Manager/HOD"),
            (self.department_head, "MEMO-DEPARTMENT-HEAD", "DMS Department Manager/HOD"),
            (self.outsider, "MEMO-OUTSIDER", "DMS Standard User"),
            (self.viewer, "MEMO-VIEWER", "DMS Viewer"),
        )
        profiles = {}
        for user, staff_id, group_name in role_assignments:
            profile = EmployeeProfile.objects.create(
                user=user,
                staff_id=staff_id,
                account_status=EmployeeProfile.AccountStatus.ACTIVE,
            )
            user.groups.add(Group.objects.get(name=group_name))
            profiles[user.pk] = profile
        self.owner_profile = profiles[self.owner.pk]
        self.reviewer_profile = profiles[self.reviewer.pk]
        self.department_head_profile = profiles[self.department_head.pk]
        ordered_profiles = (
            self.reviewer_profile,
            self.department_head_profile,
            self.owner_profile,
            profiles[self.outsider.pk],
            profiles[self.viewer.pk],
        )
        for employee in ordered_profiles:
            supervisor = self.reviewer_profile if employee == self.owner_profile else None
            create_assignment(
                employee=employee,
                department=self.department,
                position=self.position,
                supervisor=supervisor,
                actor=employee.user,
            )
        self.department.head = self.department_head_profile
        self.department.save(update_fields=["head", "updated_at"])
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

    def directory_form_values(self, subject="Directory-backed memo", **overrides):
        data = {key: value for key, value in self.values(subject).items() if key not in ("to", "through", "cc")}
        data.update({
            "to": [str(self.reviewer_profile.pk)],
            "through": [str(self.department_head_profile.pk)],
            "cc": [str(self.outsider.employee_profile.pk)],
        })
        data.update(overrides)
        return data

    def memo_values_with_recipients(self, subject="Directory-backed memo", **overrides):
        data = self.values(subject, **overrides)
        data.update({
            "to": [self.reviewer_profile],
            "through": [self.department_head_profile],
            "cc": [self.outsider.employee_profile],
        })
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
        self.assertIsNone(document.reference)
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
            **self.directory_form_values(),
            "action": "save",
        })
        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(created_by=self.owner)
        self.assertEqual(response["Location"], reverse("documents:memo-detail", args=[document.pk]))
        self.assertIsNone(document.reference)
        self.assertEqual(document.current_version.content["to_employee_ids"], [self.reviewer_profile.pk])
        self.assertEqual(document.current_version.content["through_employee_ids"], [self.department_head_profile.pk])
        self.assertEqual(document.current_version.content["cc_employee_ids"], [self.outsider.employee_profile.pk])

    def test_http_submission_binds_selected_recipients_and_exact_workflow_version(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("documents:memo-create"), {
            **self.directory_form_values("Submitted directory memo"),
            "reviewer": str(self.reviewer.pk),
            "action": "submit",
        })
        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(created_by=self.owner)
        task = WorkflowTask.objects.get(instance__document=document)
        self.assertEqual(task.submitted_version, document.current_version)
        self.assertEqual(task.submitted_version.content["to_employee_ids"], [self.reviewer_profile.pk])
        self.assertEqual(task.submitted_version.content["through_employee_ids"], [self.department_head_profile.pk])
        self.assertEqual(task.submitted_version.content["cc_employee_ids"], [self.outsider.employee_profile.pk])

    def test_consequential_document_actions_render_accessible_confirmation_dialogs(self):
        self.client.force_login(self.owner)
        create_response = self.client.get(reverse("documents:memo-create"))
        self.assertEqual(create_response.status_code, 200)
        self.assertContains(create_response, "Submit this memo for review?")
        self.assertContains(create_response, "Once submitted, the current version will become the submitted workflow version.")
        self.assertContains(create_response, "requestSubmit(action)")
        self.assertContains(create_response, 'name="action" value="save"')
        self.assertContains(create_response, 'name="action" value="submit"')
        self.assertContains(create_response, "Cancel")
        self.assertContains(create_response, '<dialog', html=False)
        self.assertContains(create_response, 'aria-describedby="memo-submit-confirm-message"')
        self.assertContains(create_response, '$refs.confirmationDialog.close()')
        self.assertContains(create_response, 'name="csrfmiddlewaretoken"')

        document = self.submit(self.make_draft())
        self.client.force_login(self.reviewer)
        review_response = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertContains(review_response, "Approve ${$el.dataset.documentReference}?")
        self.assertContains(review_response, "Return ${$el.dataset.documentReference} for revision?")
        self.assertContains(review_response, "The approval decision will be recorded")
        self.assertContains(review_response, "Return for revision")
        self.assertContains(review_response, "Approve")
        self.assertContains(review_response, 'type="button" autofocus')

        task = WorkflowTask.objects.get(instance__document=document)
        decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        self.client.force_login(self.owner)
        approved_response = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertContains(approved_response, "Finalize this memo?")
        finalize_document(document=document, actor=self.owner)
        archived_ready = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertContains(archived_ready, "Archive this memo?")

    def make_directory_employee(self, *, username, first_name, last_name, staff_id, role="DMS Standard User",
                                account_status=EmployeeProfile.AccountStatus.ACTIVE, supervisor=None, assignment=True):
        user = get_user_model().objects.create_user(
            username=username, first_name=first_name, last_name=last_name, password="pw"
        )
        if role:
            user.groups.add(Group.objects.get(name=role))
        profile = EmployeeProfile.objects.create(
            user=user, staff_id=staff_id, account_status=account_status,
        )
        if assignment:
            create_assignment(
                employee=profile,
                department=self.department,
                position=self.position,
                supervisor=supervisor,
                actor=user,
            )
        return user, profile

    def test_directory_recipient_fields_resolve_stable_employee_ids_and_human_labels(self):
        recipient_user, recipient = self.make_directory_employee(
            username="recipient-login-secret", first_name="Amina", last_name="Recipient", staff_id="RECIP-001"
        )
        second_user, second_recipient = self.make_directory_employee(
            username="second-login-secret", first_name="Amina", last_name="Recipient", staff_id="RECIP-002",
            assignment=False,
        )
        finance = Department.objects.create(code="FIN", name="Finance")
        finance_position = Position.objects.create(code="FIN-MGR", title="Finance Manager", department=finance)
        create_assignment(employee=second_recipient, department=finance, position=finance_position, actor=second_user)
        data = {
            "to": [str(recipient.pk), str(second_recipient.pk)],
            "through": [str(self.reviewer_profile.pk)],
            "cc": [str(second_recipient.pk)],
            "subject": "Directory recipient memo",
            "classification": Document.Classification.INTERNAL,
            "body": "Recipient identity test.",
        }
        form = MemoForm(data=data, user=self.owner, action="save")
        self.assertTrue(form.is_valid(), form.errors)
        rendered = str(form["to"])
        self.assertIn("Amina Recipient — Memo test position (MEMO-TEST)", rendered)
        self.assertIn("Amina Recipient — Finance Manager (FIN)", rendered)
        self.assertIn("data-search=\"amina recipient memo test position memo test department memo-test\"", rendered)
        self.assertIn("recipient", rendered.lower())
        self.assertNotIn(recipient_user.username, rendered)
        self.assertNotIn(second_user.username, rendered)

        document = create_memo(owner=self.owner, values=form.cleaned_data)
        content = document.current_version.content
        self.assertEqual(set(content["to_employee_ids"]), {recipient.pk, second_recipient.pk})
        self.assertEqual(content["through_employee_ids"], [self.reviewer_profile.pk])
        self.assertEqual(content["cc_employee_ids"], [second_recipient.pk])
        self.assertEqual(set(content["to"].split(", ")), {
            "Amina Recipient — Memo test position (MEMO-TEST)",
            "Amina Recipient — Finance Manager (FIN)",
        })
        self.assertTrue(form.fields["to"].required)
        self.assertFalse(form.fields["through"].required)
        self.assertFalse(form.fields["cc"].required)

    def test_directory_recipient_query_excludes_inactive_unassigned_and_non_dms_employees(self):
        inactive_user, inactive_profile = self.make_directory_employee(
            username="inactive-recipient", first_name="Inactive", last_name="Person", staff_id="RECIP-003",
            account_status=EmployeeProfile.AccountStatus.INACTIVE,
        )
        no_assignment_user, no_assignment_profile = self.make_directory_employee(
            username="unassigned-recipient", first_name="Unassigned", last_name="Person", staff_id="RECIP-004",
            assignment=False,
        )
        no_role_user, no_role_profile = self.make_directory_employee(
            username="no-role-recipient", first_name="NoRole", last_name="Person", staff_id="RECIP-005", role=None,
        )
        form = MemoForm(user=self.owner)
        available_ids = set(form.fields["to"].queryset.values_list("pk", flat=True))
        for profile in (inactive_profile, no_assignment_profile, no_role_profile):
            self.assertNotIn(profile.pk, available_ids)
        self.assertIn(self.reviewer_profile.pk, available_ids)
        self.assertTrue(inactive_user)
        self.assertTrue(no_assignment_user)
        self.assertTrue(no_role_user)

    def test_legacy_text_recipients_remain_readable_and_are_not_treated_as_directory_ids(self):
        document = self.make_draft()
        legacy_version = document.current_version
        self.assertEqual(legacy_version.content["to"], "Managing Director")
        self.assertNotIn("to_employee_ids", legacy_version.content)

        form = MemoForm(user=self.owner, document=document, action="save")

        self.assertEqual(form.fields["to"].initial, [])
        self.assertIn("legacy free-text", form.fields["to"].help_text)
        self.assertEqual(legacy_version.content["to"], "Managing Director")

    def test_recipient_selector_rejects_duplicate_identity_in_one_field(self):
        from django.http import QueryDict

        data = QueryDict("", mutable=True)
        data.setlist("to", [str(self.reviewer_profile.pk), str(self.reviewer_profile.pk)])
        data.update({
            "subject": "Duplicate recipient test",
            "classification": Document.Classification.INTERNAL,
            "body": "Duplicate recipient test.",
        })
        form = MemoForm(data=data, user=self.owner)
        self.assertFalse(form.is_valid())
        self.assertIn("once", str(form.errors["to"]))

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

    def test_draft_edit_carries_attachments_to_new_version_without_copying_storage(self):
        uploaded = SimpleUploadedFile("scope.pdf", b"%PDF-1.4 scope", content_type="application/pdf")
        document = self.make_draft(uploads=[uploaded])
        first_version = document.current_version
        original = first_version.attachments.get()
        storage_name = original.file.name

        document = save_memo(document=document, actor=self.owner, values=self.values("Revised scope"))

        second_version = document.current_version
        carried = second_version.attachments.get()
        self.assertNotEqual(first_version.pk, second_version.pk)
        self.assertTrue(first_version.attachments.filter(pk=original.pk).exists())
        self.assertEqual(carried.original_filename, original.original_filename)
        self.assertEqual(carried.file.name, storage_name)
        self.assertEqual(carried.uploaded_by, original.uploaded_by)
        self.assertEqual(carried.uploaded_at, original.uploaded_at)
        self.assertEqual(
            document.audit_events.get(action=AuditEvent.Action.DRAFT_SAVED, version=second_version).context,
            {"carried_forward_attachment_ids": [original.pk], "removed_attachment_ids": []},
        )

    def test_explicit_attachment_removal_applies_to_new_version_only(self):
        uploaded = SimpleUploadedFile("remove-me.pdf", b"%PDF-1.4 old", content_type="application/pdf")
        document = self.make_draft(uploads=[uploaded])
        first_version = document.current_version
        original = first_version.attachments.get()

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("documents:memo-edit", args=[document.pk]),
            {
                **self.directory_form_values("Without old attachment"),
                "action": "save",
                "remove_attachments": [str(original.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        document.refresh_from_db()

        self.assertTrue(first_version.attachments.filter(pk=original.pk).exists())
        self.assertFalse(document.current_version.attachments.exists())
        event = document.audit_events.get(action=AuditEvent.Action.DRAFT_SAVED, version=document.current_version)
        self.assertEqual(event.context, {
            "carried_forward_attachment_ids": [],
            "removed_attachment_ids": [original.pk],
        })

    def test_new_attachment_is_added_to_new_version_alongside_carried_attachment(self):
        original_upload = SimpleUploadedFile("original.pdf", b"%PDF-1.4 first", content_type="application/pdf")
        document = self.make_draft(uploads=[original_upload])
        first_version = document.current_version
        original = first_version.attachments.get()
        additional_upload = SimpleUploadedFile("additional.pdf", b"%PDF-1.4 second", content_type="application/pdf")

        document = save_memo(
            document=document,
            actor=self.owner,
            values=self.values("Two supporting files"),
            uploads=[additional_upload],
        )

        second_version = document.current_version
        self.assertEqual(set(second_version.attachments.values_list("original_filename", flat=True)), {
            "original.pdf", "additional.pdf",
        })
        self.assertEqual(list(first_version.attachments.values_list("original_filename", flat=True)), ["original.pdf"])
        self.assertFalse(first_version.attachments.filter(original_filename="additional.pdf").exists())
        additional = second_version.attachments.get(original_filename="additional.pdf")
        self.assertNotEqual(additional.file.name, original.file.name)

    def test_submission_binds_exact_new_version_and_its_attachment_set(self):
        original_upload = SimpleUploadedFile("basis.pdf", b"%PDF-1.4 basis", content_type="application/pdf")
        document = self.make_draft(uploads=[original_upload])
        first_version = document.current_version
        additional_upload = SimpleUploadedFile("revision.pdf", b"%PDF-1.4 revision", content_type="application/pdf")

        document = save_memo(
            document=document,
            actor=self.owner,
            values=self.values("Submitted revised memo"),
            uploads=[additional_upload],
            reviewer=self.reviewer,
        )

        submitted_version = document.current_version
        task = document.workflow.tasks.get()
        self.assertEqual(task.submitted_version, submitted_version)
        self.assertNotEqual(task.submitted_version, first_version)
        self.assertEqual(set(task.submitted_version.attachments.values_list("original_filename", flat=True)), {
            "basis.pdf", "revision.pdf",
        })
        self.assertEqual(list(first_version.attachments.values_list("original_filename", flat=True)), ["basis.pdf"])
        self.assertEqual(
            document.audit_events.get(action=AuditEvent.Action.SUBMITTED).version,
            submitted_version,
        )

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
        self.assertContains(response, 'x-ref="confirmationDialog"')
        self.assertContains(response, "Return for revision")
        self.assertContains(response, "Approve")

    def test_memo_list_and_detail_render_complete_lifecycle_status_labels(self):
        document = self.submit(self.make_draft())
        self.client.force_login(self.owner)

        under_review_list = self.client.get(reverse("documents:memo-list"))
        under_review_detail = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        under_review_badge = '<span class="badge status-indicator status-pending">Under review</span>'
        self.assertEqual(Document.Status.UNDER_REVIEW.label, "Under review")
        self.assertContains(under_review_list, under_review_badge, html=True)
        self.assertContains(under_review_detail, under_review_badge, html=True)

        task = WorkflowTask.objects.get(instance__document=document)
        decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        approved_list = self.client.get(reverse("documents:memo-list"))
        approved_detail = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        approved_badge = '<span class="badge status-indicator status-approved">Approved</span>'
        self.assertEqual(Document.Status.APPROVED.label, "Approved")
        self.assertContains(approved_list, approved_badge, html=True)
        self.assertContains(approved_detail, approved_badge, html=True)

        finalize_document(document=document, actor=self.owner)
        archive_document(document=document, actor=self.owner)
        archived_list = self.client.get(reverse("documents:memo-list"))
        archived_detail = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        archived_badge = '<span class="badge status-indicator status-approved">Archived</span>'
        self.assertEqual(Document.Status.ARCHIVED.label, "Archived")
        self.assertContains(archived_list, archived_badge, html=True)
        self.assertContains(archived_detail, archived_badge, html=True)

    def test_classification_filter_uses_document_classification_and_renders_accessible_badges(self):
        internal = self.make_draft()
        confidential = create_memo(
            owner=self.owner,
            values=self.values(classification=Document.Classification.CONFIDENTIAL),
        )
        restricted = create_memo(
            owner=self.owner,
            values=self.values(classification=Document.Classification.RESTRICTED),
        )
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("documents:memo-list"), {"classification": Document.Classification.CONFIDENTIAL}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, confidential.title)
        self.assertNotContains(response, 'class="classification-badge classification-internal"')
        self.assertNotContains(response, 'class="classification-badge classification-restricted"')
        self.assertContains(response, 'class="classification-badge classification-confidential"')
        self.assertContains(response, "Confidential")
        self.assertContains(response, 'name="classification"')

    def test_dms_viewer_sees_read_only_shell_without_create_action(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("documents:memo-list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="dms-sidebar"')
        self.assertContains(response, "Internal Memos")
        self.assertNotContains(response, "New Internal Memo")
        self.assertNotContains(response, "Create memo")

    def test_review_form_csrf_token_survives_login_and_protects_review_post(self):
        document = self.submit(self.make_draft())
        client = Client(enforce_csrf_checks=True)

        login_page = client.get(reverse("dms-login"))
        self.assertEqual(login_page.status_code, 200)
        login_csrf = login_page.cookies["csrftoken"].value
        login_response = client.post(
            reverse("dms-login"),
            {"username": self.reviewer.username, "password": "pw"},
            HTTP_X_CSRFTOKEN=login_csrf,
        )
        self.assertEqual(login_response.status_code, 302)

        detail_url = reverse("documents:memo-detail", args=[document.pk])
        detail = client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        review_url = reverse("documents:memo-review", args=[document.pk])
        self.assertContains(detail, f'action="{review_url}"')
        self.assertContains(detail, 'name="csrfmiddlewaretoken"')
        review_csrf = detail.cookies["csrftoken"].value
        self.assertNotEqual(login_csrf, review_csrf)

        missing_token_response = client.post(review_url, {"decision": "APPROVE"})
        self.assertEqual(missing_token_response.status_code, 403)
        invalid_token_response = client.post(
            review_url, {"decision": "APPROVE"}, HTTP_X_CSRFTOKEN="invalid-token"
        )
        self.assertEqual(invalid_token_response.status_code, 403)

        valid_response = client.post(
            review_url, {"decision": "APPROVE"}, HTTP_X_CSRFTOKEN=review_csrf
        )

        self.assertEqual(valid_response.status_code, 302)
        self.assertEqual(document.workflow.tasks.get().status, WorkflowTask.Status.APPROVED)

    def test_reviewer_choices_prioritize_supervisor_and_department_head(self):
        document = self.make_draft()
        form = MemoForm(user=self.owner, document=document, action="submit")
        reviewer_ids = list(form.fields["reviewer"].queryset.values_list("pk", flat=True))

        self.assertEqual(reviewer_ids[:2], [self.reviewer.pk, self.department_head.pk])
        self.assertIn("current supervisor", form.fields["reviewer"].help_text)
        self.assertIn("department manager/HOD", form.fields["reviewer"].help_text)
        self.assertIn("current assignment", form.fields["reviewer"].help_text)
        self.assertIn("you and your direct reports are excluded", form.fields["reviewer"].help_text)
        self.assertNotIn("Other active employees", form.fields["reviewer"].help_text)
        reviewer_label = form.fields["reviewer"].label_from_instance(self.reviewer)
        self.assertEqual(reviewer_label, "Umar Mohammed — Memo test position (MEMO-TEST)")
        self.assertNotIn(self.reviewer.username, reviewer_label)

        subordinate, _ = self.make_directory_employee(
            username="owner-subordinate", first_name="Sub", last_name="Ordinate",
            staff_id="MEMO-SUBORDINATE", supervisor=self.owner_profile,
        )
        subordinate_form = MemoForm(user=self.owner, document=document, action="submit")
        subordinate_ids = set(subordinate_form.fields["reviewer"].queryset.values_list("pk", flat=True))
        self.assertNotIn(subordinate.pk, subordinate_ids)
        self.assertNotIn(self.owner.pk, subordinate_ids)

        inactive_user, _ = self.make_directory_employee(
            username="inactive-reviewer-account", first_name="Inactive", last_name="Reviewer",
            staff_id="MEMO-INACTIVE-REVIEWER", account_status=EmployeeProfile.AccountStatus.INACTIVE,
        )
        no_assignment_user, _ = self.make_directory_employee(
            username="reviewer-no-current-assignment", first_name="No", last_name="Assignment",
            staff_id="MEMO-NO-CURRENT-ASSIGNMENT", assignment=False,
        )
        refreshed_reviewer_ids = set(
            MemoForm(user=self.owner, document=document, action="submit").fields[
                "reviewer"
            ].queryset.values_list("pk", flat=True)
        )
        self.assertNotIn(inactive_user.pk, refreshed_reviewer_ids)
        self.assertNotIn(no_assignment_user.pk, refreshed_reviewer_ids)

        unassigned_user = get_user_model().objects.create_user(username="memo-no-dms-role", password="pw")
        unassigned_profile = EmployeeProfile.objects.create(
            user=unassigned_user,
            staff_id="MEMO-NO-DMS-ROLE",
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        create_assignment(
            employee=unassigned_profile,
            department=self.department,
            position=self.position,
            actor=unassigned_user,
        )
        refreshed_form = MemoForm(user=self.owner, document=document, action="submit")
        available_ids = set(refreshed_form.fields["reviewer"].queryset.values_list("pk", flat=True))
        self.assertNotIn(unassigned_user.pk, available_ids)
        self.assertNotIn(self.viewer.pk, available_ids)

    def test_reviewer_selector_only_appears_when_selected_user_is_configured(self):
        document = self.make_draft()
        step = document.document_type.workflows.first().steps.filter(is_review=True).first()
        step.actor_type = step.ActorType.CREATOR_SUPERVISOR
        step.save(update_fields=["actor_type"])

        form = MemoForm(user=self.owner, document=document, action="submit")

        self.assertFalse(form.show_reviewer_selector)
        self.assertFalse(form.fields["reviewer"].required)
        self.assertFalse(form.fields["reviewer"].queryset.exists())

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

    def test_bound_workflow_task_can_be_approved_with_task_only_row_lock(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        submitted_version = task.submitted_version
        original_select_for_update = WorkflowTask.objects.select_for_update
        observed = {}

        def observe_lock(*args, **kwargs):
            observed["in_atomic_block"] = connection.in_atomic_block
            observed["kwargs"] = kwargs
            return original_select_for_update(*args, **kwargs)

        # Django's manager method remains wrapped so the real queryset and decision path execute.
        with patch.object(WorkflowTask.objects, "select_for_update", side_effect=observe_lock):
            decision = decide_review(
                task_id=task.pk,
                actor=self.reviewer,
                outcome=WorkflowDecision.Outcome.APPROVE,
            )

        self.assertTrue(observed["in_atomic_block"])
        self.assertEqual(observed["kwargs"], {"of": ("self",)})
        self.assertEqual(decision.task.submitted_version, submitted_version)
        self.assertEqual(decision.submitted_version, submitted_version)
        self.assertEqual(decision.outcome, WorkflowDecision.Outcome.APPROVE)
        self.assertEqual(document.audit_events.get(action=AuditEvent.Action.APPROVED).version, submitted_version)

    @skipUnlessDBFeature("has_select_for_update")
    def test_database_lock_clause_targets_only_workflow_task(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        with CaptureQueriesContext(connection) as captured:
            decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)

        lock_queries = [query["sql"] for query in captured if "FOR UPDATE" in query["sql"].upper()]
        task_lock = next(sql for sql in lock_queries if "documents_workflowtask" in sql)
        self.assertIn('FOR UPDATE OF "documents_workflowtask"', task_lock)

    def test_invalid_or_already_decided_workflow_task_remains_rejected(self):
        with self.assertRaises(WorkflowTask.DoesNotExist):
            decide_review(task_id=0, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)

        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        with self.assertRaisesMessage(PermissionDenied, "You do not have an active review assignment"):
            decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)

    def test_approval_and_finalization_remain_bound_when_current_version_changes(self):
        document = self.submit(self.make_draft())
        task = WorkflowTask.objects.get(instance__document=document)
        submitted_version = task.submitted_version
        later_version = DocumentVersion.objects.create(
            document=document,
            number=submitted_version.number + 1,
            content={**submitted_version.content, "subject": "Later unsubmitted version marker"},
            created_by=self.owner,
        )
        document.current_version_number = later_version.number
        document.save(update_fields=["current_version_number"])

        decision = decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        self.assertEqual(decision.task.submitted_version, submitted_version)
        self.assertEqual(decision.submitted_version, submitted_version)
        approved_event = document.audit_events.get(action=AuditEvent.Action.APPROVED)
        self.assertEqual(approved_event.version, submitted_version)

        document = finalize_document(document=document, actor=self.owner)
        finalized_event = document.audit_events.get(action=AuditEvent.Action.FINALIZED)
        self.assertEqual(finalized_event.version, submitted_version)
        self.assertEqual(document.current_version_number, later_version.number)

        self.client.force_login(self.owner)
        with patch("apps.documents.views.render_memo_pdf", wraps=render_memo_pdf) as pdf_renderer:
            response = self.client.get(reverse("documents:memo-pdf-view", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        pdf_bytes = response_body(response)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertTrue(pdf_bytes.rstrip().endswith(b"%%EOF"))
        self.assertRegex(pdf_bytes, rb"/Type\s*/Page\b")
        page_content = decoded_pdf_page_content(pdf_bytes)
        self.assertIn(b"Generator maintenance plan", page_content)
        self.assertNotIn(b"Later unsubmitted version marker", page_content)
        labels = (
            b"Memo Reference", b"Date", b"From", b"Originating department",
            b"Originating position", b"To", b"Through", b"CC", b"Classification", b"Subject",
        )
        label_positions = [page_content.find(b"(" + label + b") Tj") for label in labels]
        self.assertTrue(all(position >= 0 for position in label_positions))
        self.assertEqual(label_positions, sorted(label_positions))
        self.assertIn(document.reference.encode(), page_content)
        self.assertIn(b"Memo test department", page_content)
        self.assertIn(b"Memo test position", page_content)
        self.assertIn(b"Managing Director", page_content)
        self.assertIn(b"Director, Operations", page_content)
        self.assertIn(b"Finance Department", page_content)
        self.assertIn(b"Internal", page_content)
        self.assertIn(b"Please review the attached plan.", page_content)
        self.assertIn(b"No attachments", page_content)
        self.assertIn(b"Approved by Umar Mohammed", page_content)
        self.assertIn(b"Finalized by Iliyas Nasir", page_content)
        self.assertEqual(response["Content-Length"], str(len(pdf_bytes)))
        self.assertTrue(response["Content-Disposition"].startswith("inline; filename=\""))
        rendered_version = pdf_renderer.call_args.kwargs["version"]
        self.assertEqual(rendered_version.pk, submitted_version.pk)
        self.assertNotEqual(rendered_version.pk, document.current_version.pk)
        viewed_event = document.audit_events.get(action=AuditEvent.Action.PDF_VIEWED)
        self.assertEqual(viewed_event.version, submitted_version)
        download_response = self.client.get(reverse("documents:memo-pdf-download", args=[document.pk]))
        self.assertEqual(download_response.status_code, 200)
        download_bytes = response_body(download_response)
        self.assertEqual(download_response["Content-Type"], "application/pdf")
        self.assertTrue(download_bytes.startswith(b"%PDF-"))
        self.assertTrue(download_bytes.rstrip().endswith(b"%%EOF"))
        self.assertEqual(download_response["Content-Length"], str(len(download_bytes)))
        self.assertTrue(download_response["Content-Disposition"].startswith("attachment; filename=\""))
        download_content = decoded_pdf_page_content(download_bytes)
        self.assertIn(b"Generator maintenance plan", download_content)
        self.assertNotIn(b"Later unsubmitted version marker", download_content)
        downloaded_event = document.audit_events.get(action=AuditEvent.Action.DOWNLOADED, context__kind="pdf")
        self.assertEqual(downloaded_event.version, submitted_version)

        archive_document(document=document, actor=self.owner)
        archived_event = document.audit_events.get(action=AuditEvent.Action.ARCHIVED)
        self.assertEqual(archived_event.version, submitted_version)
        archived_pdf = self.client.get(reverse("documents:memo-pdf-view", args=[document.pk]))
        self.assertEqual(archived_pdf.status_code, 200)
        self.assertTrue(response_body(archived_pdf).startswith(b"%PDF-"))

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
        pdf_bytes = response_body(pdf_response)
        self.assertGreater(len(pdf_bytes), 1000)
        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        self.assertTrue(pdf_bytes.rstrip().endswith(b"%%EOF"))
        self.assertRegex(pdf_bytes, rb"/Type\s*/Page\b")
        self.assertIn("inline", pdf_response["Content-Disposition"])
        self.assertEqual(pdf_response["Content-Length"], str(len(pdf_bytes)))
        download_response = self.client.get(reverse("documents:memo-pdf-download", args=[document.pk]))
        self.assertEqual(download_response.status_code, 200)
        download_bytes = response_body(download_response)
        self.assertGreater(len(download_bytes), 1000)
        self.assertTrue(download_bytes.startswith(b"%PDF-"))
        self.assertTrue(download_bytes.rstrip().endswith(b"%%EOF"))
        self.assertRegex(download_bytes, rb"/Type\s*/Page\b")
        self.assertIn("attachment", download_response["Content-Disposition"])
        self.assertEqual(download_response["Content-Length"], str(len(download_bytes)))

        archive_document(document=document, actor=self.owner)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.ARCHIVED)
        self.assertTrue(document.audit_events.filter(action=AuditEvent.Action.ARCHIVED).exists())

    def test_access_is_object_scoped_and_view_does_not_imply_download(self):
        document = self.submit(self.make_draft())
        self.approve_and_finalize(document)
        self.assertFalse(can_view_document(self.outsider, document))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-view", args=[document.pk])).status_code, 404)

        DocumentAccess.objects.create(document=document, user=self.viewer, can_view=True,
                                      can_download=False, granted_by=self.owner)
        self.assertTrue(can_view_document(self.viewer, document))
        self.assertFalse(can_download_document(self.viewer, document))
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-download", args=[document.pk])).status_code, 403)

    def test_internal_recipients_can_view_but_recipient_status_grants_no_actions(self):
        document = create_memo(owner=self.owner, values=self.memo_values_with_recipients())

        for recipient in (self.reviewer, self.department_head, self.outsider):
            with self.subTest(recipient=recipient.username):
                self.assertTrue(can_view_document(recipient, document))
                self.client.force_login(recipient)
                listing = self.client.get(reverse("documents:memo-list"))
                self.assertContains(listing, document.title)
                self.assertEqual(
                    self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code,
                    200,
                )
                self.assertFalse(can_download_document(recipient, document))
                self.assertEqual(
                    self.client.get(reverse("documents:memo-edit", args=[document.pk])).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(reverse("documents:memo-review", args=[document.pk]),
                                     {"decision": "APPROVE"}).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(reverse("documents:memo-finalize", args=[document.pk])).status_code,
                    403,
                )
                self.assertEqual(
                    self.client.post(reverse("documents:memo-archive", args=[document.pk])).status_code,
                    403,
                )

    def test_confidential_recipients_can_view_without_organizational_membership_grants(self):
        document = create_memo(
            owner=self.owner,
            values=self.memo_values_with_recipients(classification=Document.Classification.CONFIDENTIAL),
        )

        for recipient in (self.reviewer, self.department_head, self.outsider):
            self.assertTrue(can_view_document(recipient, document))

        self.assertFalse(can_view_document(self.viewer, document))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 200)
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 404)

    def test_restricted_recipient_is_denied_until_explicitly_authorized(self):
        uploaded = SimpleUploadedFile("restricted-support.pdf", b"%PDF-1.4 restricted", content_type="application/pdf")
        document = create_memo(
            owner=self.owner,
            values=self.memo_values_with_recipients(classification=Document.Classification.RESTRICTED),
            uploads=[uploaded],
            reviewer=self.reviewer,
        )
        self.approve_and_finalize(document)
        attachment = document.current_version.attachments.get()

        self.assertFalse(can_view_document(self.outsider, document))
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("documents:memo-list")).status_code, 200)
        self.assertNotContains(self.client.get(reverse("documents:memo-list")), document.reference)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-view", args=[document.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk])).status_code, 404)

        DocumentAccess.objects.create(
            document=document, user=self.outsider, can_view=True, can_download=False, granted_by=self.owner,
        )
        self.assertTrue(can_view_document(self.outsider, document))
        self.assertFalse(can_download_document(self.outsider, document))
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-view", args=[document.pk])).status_code, 200)
        attachment_response = self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk]))
        self.assertEqual(attachment_response.status_code, 200)
        self.assertEqual(b"".join(attachment_response.streaming_content), b"%PDF-1.4 restricted")
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-download", args=[document.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("documents:attachment-download", args=[document.pk, attachment.pk])).status_code, 403)

    def test_restricted_workflow_assignee_can_view_independently_of_recipients(self):
        values = self.values(classification=Document.Classification.RESTRICTED)
        document = create_memo(owner=self.owner, values=values, reviewer=self.reviewer)

        self.assertFalse(can_view_document(self.outsider, document))
        self.assertTrue(can_view_document(self.reviewer, document))
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("documents:memo-detail", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Review this memo")

    def test_recipient_identity_comes_from_current_or_exact_approved_version(self):
        draft = create_memo(owner=self.owner, values=self.memo_values_with_recipients())
        save_memo(
            document=draft,
            actor=self.owner,
            values=self.values("Recipient removed from current version"),
        )
        self.assertFalse(can_view_document(self.outsider, draft))
        self.assertTrue(can_view_document(self.owner, draft))

        approved = create_memo(
            owner=self.owner,
            values=self.memo_values_with_recipients("Approved recipient snapshot"),
            reviewer=self.reviewer,
        )
        self.approve_and_finalize(approved)
        approved_version = approved.workflow.tasks.get(status=WorkflowTask.Status.APPROVED).submitted_version
        later_version = DocumentVersion.objects.create(
            document=approved,
            number=approved.current_version_number + 1,
            content={
                "subject": "Unapproved version",
                "classification": Document.Classification.INTERNAL,
                "to_employee_ids": [self.viewer.employee_profile.pk],
                "through_employee_ids": [],
                "cc_employee_ids": [],
            },
            created_by=self.owner,
        )
        approved.current_version_number = later_version.number
        approved.save(update_fields=["current_version_number"])

        self.assertTrue(can_view_document(self.outsider, approved))
        self.assertFalse(can_view_document(self.viewer, approved))
        self.assertNotEqual(approved_version, later_version)

    def test_recipient_view_policy_applies_to_final_pdf_and_attachment_routes(self):
        uploaded = SimpleUploadedFile("internal-support.pdf", b"%PDF-1.4 internal", content_type="application/pdf")
        document = create_memo(
            owner=self.owner,
            values=self.memo_values_with_recipients(),
            uploads=[uploaded],
            reviewer=self.reviewer,
        )
        self.approve_and_finalize(document)
        attachment = document.workflow.tasks.get(status=WorkflowTask.Status.APPROVED).submitted_version.attachments.get()

        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(reverse("documents:memo-detail", args=[document.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-view", args=[document.pk])).status_code, 200)
        attachment_response = self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk]))
        self.assertEqual(attachment_response.status_code, 200)
        self.assertEqual(b"".join(attachment_response.streaming_content), b"%PDF-1.4 internal")
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-download", args=[document.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("documents:attachment-download", args=[document.pk, attachment.pk])).status_code, 403)

        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(reverse("documents:memo-pdf-view", args=[document.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("documents:attachment-view", args=[document.pk, attachment.pk])).status_code, 404)

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
        no_role_user, _ = self.make_directory_employee(
            username="active-no-role-reviewer", first_name="No", last_name="Role", staff_id="NO-ROLE-REVIEWER",
            role=None,
        )
        viewer_user, _ = self.make_directory_employee(
            username="viewer-reviewer", first_name="View", last_name="Only", staff_id="VIEWER-REVIEWER",
            role="DMS Viewer",
        )
        for reviewer in (self.owner, inactive_staff, regular_user, no_role_user, viewer_user):
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
        view_content = decoded_pdf_page_content(response_body(view_response))
        self.assertIn(b"supporting-note.pdf", view_content)
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

