import tempfile
from types import SimpleNamespace
from datetime import date
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.documents.access import can_view_document
from apps.documents.forms import IncomingCorrespondenceForm, validate_upload_batch
from apps.documents.handlers import (
    INCOMING_CORRESPONDENCE_TYPE_CODE,
    get_document_type_handler,
)
from apps.documents.models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    RelatedDocument,
)
from apps.documents.services import (
    create_memo,
    create_related_document,
    register_incoming_correspondence,
    register_incoming_revision,
)
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


@override_settings(MEDIA_ROOT=tempfile.gettempdir())
class IncomingCorrespondenceTests(TestCase):
    def test_upload_batch_limit_allows_five_maximum_files_and_rejects_over_limit(self):
        files = [SimpleNamespace(name=f"file-{index}.pdf", size=20 * 1024 * 1024) for index in range(5)]
        validate_upload_batch(files)
        with self.assertRaises(ValidationError):
            validate_upload_batch([*files, SimpleNamespace(name="sixth.pdf", size=1)])

    def test_upload_batch_preserves_per_file_size_limit(self):
        oversized = SimpleNamespace(name="oversized.pdf", size=20 * 1024 * 1024 + 1)
        with self.assertRaisesMessage(ValidationError, "each attachment must be 20 MB or smaller"):
            validate_upload_batch([oversized])

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("registry-user", password="pw")
        self.outsider = User.objects.create_user("unrelated-user", password="pw")
        self.office = Department.objects.create(code="REG", name="Registry Office")
        self.position = Position.objects.create(code="REG-STAFF", title="Registry staff", department=self.office)
        profile = EmployeeProfile.objects.create(
            user=self.user, staff_id="REG-001", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        create_assignment(employee=profile, department=self.office, position=self.position, actor=self.user)
        self.user.groups.add(Group.objects.get(name="DMS Standard User"))
        outsider_profile = EmployeeProfile.objects.create(
            user=self.outsider, staff_id="REG-002", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        outsider_position = Position.objects.create(
            code="REG-OUTSIDER", title="Registry observer", department=self.office,
        )
        create_assignment(
            employee=outsider_profile, department=self.office, position=outsider_position, actor=self.user,
        )
        self.outsider.groups.add(Group.objects.get(name="DMS Standard User"))

        self.manager = User.objects.create_user(username="registry-manager", password="pw")
        self.manager_profile = EmployeeProfile.objects.create(
            user=self.manager, staff_id="REG-MANAGER", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        manager_position = Position.objects.create(
            code="REG-MANAGER", title="Office manager", department=self.office,
        )
        create_assignment(
            employee=self.manager_profile, department=self.office, position=manager_position, actor=self.user,
        )
        self.manager.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))
        self.office.head = self.manager_profile
        self.office.full_clean()
        self.office.save(update_fields=["head"])

        self.other_office = Department.objects.create(code="OTHER", name="Other Office")
        self.other_manager = User.objects.create_user(username="other-office-manager", password="pw")
        self.other_manager_profile = EmployeeProfile.objects.create(
            user=self.other_manager, staff_id="OTHER-MANAGER", account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        other_manager_position = Position.objects.create(
            code="OTHER-MANAGER", title="Other office manager", department=self.other_office,
        )
        create_assignment(
            employee=self.other_manager_profile, department=self.other_office,
            position=other_manager_position, actor=self.user,
        )
        self.other_manager.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))
        self.other_office.head = self.other_manager_profile
        self.other_office.full_clean()
        self.other_office.save(update_fields=["head"])
        permission = Permission.objects.get(
            content_type__app_label="documents", codename="register_incoming_correspondence",
        )
        self.user.user_permissions.add(permission)
        self.document_type = DocumentType.objects.get(code=INCOMING_CORRESPONDENCE_TYPE_CODE)
        self.media_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.client = Client()
        self.client.force_login(self.user)

    def tearDown(self):
        self.settings_override.disable()
        self.media_dir.cleanup()

    def values(self, *, office=None, subject="Grid connection request", external_reference="EXT-77",
               classification=Document.Classification.CONFIDENTIAL, **overrides):
        data = {
            "sender": "  North River Energy Ltd  ",
            "external_reference": external_reference,
            "document_date": date(2026, 9, 28),
            "received_on": date(2026, 10, 7),
            "subject": subject,
            "correspondence_type": "CONTRACT_AGREEMENT",
            "receiving_office": office or self.office,
            "priority": "HIGH",
            "classification": classification,
            "original_document": SimpleUploadedFile("received.pdf", b"original pdf", content_type="application/pdf"),
            "supporting_documents": [SimpleUploadedFile("schedule.pdf", b"support pdf", content_type="application/pdf")],
        }
        data.update(overrides)
        return data

    def register(self, *, classification=Document.Classification.CONFIDENTIAL, **kwargs):
        return register_incoming_correspondence(
            actor=self.user, values=self.values(classification=classification, **kwargs),
        )

    def test_registration_grants_view_only_access_to_designated_office_head_and_audits_reason(self):
        document = self.register()

        self.assertTrue(can_view_document(self.user, document))
        self.assertTrue(can_view_document(self.manager, document))
        manager_grant = DocumentAccess.objects.get(document=document, user=self.manager)
        self.assertTrue(manager_grant.can_view)
        self.assertFalse(manager_grant.can_download)
        self.assertEqual(manager_grant.granted_by, self.user)
        self.client.force_login(self.manager)
        self.assertEqual(
            self.client.get(reverse("documents:incoming-detail", args=[document.pk])).status_code,
            200,
        )
        self.assertContains(self.client.get(reverse("documents:incoming-list")), document.reference)
        event = document.audit_events.get(action=AuditEvent.Action.INCOMING_REGISTERED)
        self.assertIn({
            "user_id": self.manager.pk,
            "employee_profile_id": self.manager_profile.pk,
            "assignment_id": self.manager_profile.current_assignment.pk,
            "reason": "receiving_office_designated_head",
            "grant_created": True,
            "can_view": True,
            "can_download": False,
        }, event.context["visibility_grants"])

    def test_office_membership_and_other_office_manager_role_do_not_grant_visibility(self):
        document = self.register()

        self.assertFalse(can_view_document(self.outsider, document))
        self.assertFalse(can_view_document(self.other_manager, document))

    def test_changing_designated_head_does_not_revoke_existing_correspondence_grant(self):
        document = self.register()
        replacement_manager = get_user_model().objects.create_user(
            username="replacement-office-manager", password="pw",
        )
        replacement_profile = EmployeeProfile.objects.create(
            user=replacement_manager,
            staff_id="REG-MANAGER-2",
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        replacement_position = Position.objects.create(
            code="REG-MANAGER-2", title="Replacement office manager", department=self.office,
        )
        create_assignment(
            employee=replacement_profile, department=self.office,
            position=replacement_position, actor=self.user,
        )
        replacement_manager.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))
        self.office.head = replacement_profile
        self.office.full_clean()
        self.office.save(update_fields=["head"])

        self.assertTrue(can_view_document(self.manager, document))
        self.assertFalse(can_view_document(replacement_manager, document))

    def test_registration_requires_a_valid_designated_head_and_rolls_back(self):
        office_without_head = Department.objects.create(code="NOHEAD", name="Office without a head")
        before_count = Document.objects.count()

        with self.assertRaises(ValidationError):
            self.register(office=office_without_head)

        self.assertEqual(Document.objects.count(), before_count)

        self.manager_profile.account_status = EmployeeProfile.AccountStatus.INACTIVE
        self.manager_profile.save(update_fields=["account_status", "updated_at"])
        before_count = Document.objects.count()
        with self.assertRaises(ValidationError):
            self.register()
        self.assertEqual(Document.objects.count(), before_count)

    def test_explicit_receiving_office_head_grant_obeys_existing_classification_relationship_rules(self):
        for classification in Document.Classification.values:
            with self.subTest(classification=classification):
                document = self.register(
                    classification=classification,
                    subject=f"{classification} incoming record",
                    external_reference=f"EXT-{classification}",
                )
                self.assertTrue(can_view_document(self.manager, document))
                self.assertFalse(can_view_document(self.outsider, document))
                self.assertFalse(can_view_document(self.other_manager, document))

    def test_memo_cc_recipient_visibility_remains_unchanged(self):
        memo = create_memo(owner=self.user, values={
            "subject": "Memo recipient visibility check",
            "body": "The designated receiving-office manager is a structured CC recipient.",
            "classification": Document.Classification.INTERNAL,
            "to": [],
            "through": [],
            "cc": [self.manager_profile],
        })

        self.assertTrue(can_view_document(self.manager, memo))
        self.assertFalse(can_view_document(self.other_manager, memo))

    def test_register_creates_typed_registered_document_version_and_audit(self):
        document = self.register()
        version = document.current_version
        self.assertEqual(document.document_type, self.document_type)
        self.assertEqual(document.status, Document.Status.REGISTERED)
        self.assertRegex(document.reference, r"^APGC/REG/\d{4}/000001$")
        self.assertEqual(document.classification, Document.Classification.CONFIDENTIAL)
        self.assertEqual(version.number, 1)
        self.assertEqual(version.content["sender"], "North River Energy Ltd")
        self.assertEqual(version.content["external_reference"], "EXT-77")
        self.assertEqual(version.content["correspondence_type"], "Contract / Agreement")
        self.assertEqual(version.content["correspondence_type_code"], "CONTRACT_AGREEMENT")
        self.assertEqual(version.content["priority"], "High")
        self.assertEqual(version.content["priority_code"], "HIGH")
        self.assertEqual(version.content["receiving_office_id"], self.office.pk)
        self.assertEqual(version.attachments.count(), 2)
        self.assertTrue(document.audit_events.filter(
            actor=self.user, action=AuditEvent.Action.INCOMING_REGISTERED, version=version,
        ).exists())

    def test_handler_is_registered_and_keeps_metadata_structured_in_version_payload(self):
        self.assertEqual(get_document_type_handler(INCOMING_CORRESPONDENCE_TYPE_CODE).__class__.__name__, "IncomingCorrespondenceHandler")
        document = self.register()
        payload = document.current_version.content
        for key in ("sender", "external_reference", "document_date", "received_on", "correspondence_type", "correspondence_type_code", "priority", "priority_code", "classification"):
            self.assertIn(key, payload)

    def test_required_registration_metadata_and_original_file_are_validated(self):
        data = {
            "sender": "",
            "external_reference": "",
            "document_date": "",
            "received_on": "2026-10-07",
            "subject": "",
            "correspondence_type": "",
            "other_correspondence_type": "",
            "receiving_office": str(self.office.pk),
            "priority": "",
            "classification": "CONFIDENTIAL",
        }
        form = IncomingCorrespondenceForm(data=data)
        self.assertFalse(form.is_valid())
        for field in ("sender", "subject", "correspondence_type", "priority", "original_document"):
            self.assertIn(field, form.errors)

    def test_sequential_registrations_receive_distinct_office_scoped_numbers(self):
        first = self.register(subject="First incoming record")
        second = self.register(subject="Second incoming record")
        self.assertEqual(first.number_sequence, 1)
        self.assertEqual(second.number_sequence, 2)
        self.assertNotEqual(first.reference, second.reference)

    def test_registration_requires_capability_permission(self):
        self.user.user_permissions.clear()
        with self.assertRaises(PermissionDenied):
            self.register()
        response = self.client.get(reverse("documents:incoming-register"))
        self.assertEqual(response.status_code, 403)

    def test_registration_form_uses_sections_and_preserves_free_text_metadata(self):
        response = self.client.get(reverse("documents:incoming-register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Correspondence Details")
        self.assertContains(response, "Routing &amp; Classification")
        self.assertContains(response, "Documents")
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'x-ref="confirmationDialog"')
        self.assertContains(response, "Register Correspondence")
        self.assertContains(response, '<option value="MEDIUM" selected>Medium</option>', html=True)
        for field in (
            "sender", "external_reference", "subject", "correspondence_type", "other_correspondence_type", "document_date",
            "received_on", "priority", "receiving_office", "classification", "original_document",
            "supporting_documents",
        ):
            self.assertContains(response, f'name="{field}"')
        form = IncomingCorrespondenceForm()
        self.assertEqual(form.fields["correspondence_type"].widget.__class__.__name__, "Select")
        self.assertEqual(form.fields["priority"].widget.__class__.__name__, "Select")
        self.assertEqual(form.fields["priority"].initial, "MEDIUM")
        self.assertEqual([code for code, _ in form.fields["priority"].choices], ["URGENT", "HIGH", "MEDIUM", "LOW"])
        for priority_code in ("URGENT", "HIGH", "MEDIUM", "LOW"):
            self.assertEqual(form.fields["priority"].clean(priority_code), priority_code)
        self.assertEqual([label for _, label in form.fields["correspondence_type"].choices], [
            "Contract / Agreement", "Invitation", "Invoice", "Letter", "Notice", "Petition",
            "Policy / Guideline", "Proposal", "Quotation", "Report", "Others",
        ])

    def test_detail_access_uses_existing_document_visibility_policy(self):
        document = self.register()
        self.assertFalse(can_view_document(self.outsider, document))
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("documents:incoming-detail", args=[document.pk]))
        self.assertEqual(response.status_code, 404)
        attachment = document.current_version.attachments.first()
        self.assertEqual(self.client.get(reverse(
            "documents:incoming-attachment-view", args=[document.pk, attachment.pk],
        )).status_code, 404)
        DocumentAccess.objects.create(
            document=document, user=self.outsider, can_view=True, can_download=False, granted_by=self.user,
        )
        self.assertEqual(self.client.get(reverse("documents:incoming-detail", args=[document.pk])).status_code, 200)

    def test_registration_post_stores_original_supporting_files_and_redirects_to_detail(self):
        response = self.client.post(reverse("documents:incoming-register"), {
            "sender": "North River Energy Ltd",
            "external_reference": "EXT-UI-4",
            "document_date": "2026-09-28",
            "received_on": "2026-10-07",
            "subject": "UI registered subject",
            "correspondence_type": "LETTER",
            "other_correspondence_type": "",
            "receiving_office": str(self.office.pk),
            "priority": "MEDIUM",
            "classification": "INTERNAL",
            "original_document": SimpleUploadedFile("ui-original.pdf", b"ui original", content_type="application/pdf"),
            "supporting_documents": [SimpleUploadedFile("ui-support.pdf", b"ui supporting", content_type="application/pdf")],
        })
        self.assertEqual(response.status_code, 302)
        document = Document.objects.get(title="UI registered subject")
        self.assertEqual(response.url, reverse("documents:incoming-detail", args=[document.pk]))
        self.assertEqual(document.current_version.attachments.count(), 2)
        self.assertEqual(document.current_version.content["priority_code"], "MEDIUM")
        self.assertEqual(document.current_version.content["priority"], "Medium")

    def test_register_interface_and_search_filter_by_reference_sender_external_reference_and_metadata(self):
        document = self.register()
        list_url = reverse("documents:incoming-list")
        self.assertEqual(self.client.get(reverse("documents:incoming-register")).status_code, 200)
        for params in (
            {"reference": document.reference},
            {"sender": "North River"},
            {"external_reference": "EXT-77"},
            {"subject": "Grid connection"},
            {"received_on": "2026-10-07"},
            {"document_date": "2026-09-28"},
            {"receiving_office": str(self.office.pk)},
            {"correspondence_type": "Contract"},
            {"correspondence_type": "CONTRACT_AGREEMENT"},
            {"priority": "High"},
            {"priority": "HIGH"},
            {"classification": "CONFIDENTIAL"},
        ):
            response = self.client.get(list_url, params)
            self.assertEqual(response.status_code, 200, params)
            self.assertContains(response, document.reference)
        self.assertEqual(self.client.get(list_url, {"sender": "No such sender"}).context["page"].paginator.count, 0)

    def test_controlled_categories_and_priorities_reject_manipulated_values(self):
        form = IncomingCorrespondenceForm(data={
            "sender": "Sender", "subject": "Subject", "correspondence_type": "MADE_UP",
            "other_correspondence_type": "", "received_on": "2026-10-07", "priority": "CRITICAL",
            "receiving_office": str(self.office.pk), "classification": "INTERNAL",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("correspondence_type", form.errors)
        self.assertIn("priority", form.errors)

    def test_other_category_requires_and_stores_separate_description(self):
        data = {
            "sender": "Sender", "subject": "Other category", "correspondence_type": "OTHER",
            "other_correspondence_type": "", "document_date": "", "received_on": "2026-10-07",
            "priority": "MEDIUM", "receiving_office": str(self.office.pk), "classification": "INTERNAL",
        }
        file_data = {"original_document": SimpleUploadedFile("other.pdf", b"other", content_type="application/pdf")}
        form = IncomingCorrespondenceForm(data=data, files=file_data)
        self.assertFalse(form.is_valid())
        self.assertIn("other_correspondence_type", form.errors)
        data["other_correspondence_type"] = "  Engineering drawing  "
        form = IncomingCorrespondenceForm(data=data, files=file_data)
        self.assertTrue(form.is_valid(), form.errors)
        payload = get_document_type_handler(INCOMING_CORRESPONDENCE_TYPE_CODE).prepare_payload({
            **form.cleaned_data,
            "original_document": file_data["original_document"],
            "supporting_documents": [],
        })
        self.assertEqual(payload["correspondence_type"], "Others")
        self.assertEqual(payload["correspondence_type_code"], "OTHER")
        self.assertEqual(payload["correspondence_type_description"], "Engineering drawing")
        document = self.register(
            subject="Other type registration", correspondence_type="OTHER",
            other_correspondence_type="Engineering drawing",
        )
        self.assertEqual(document.current_version.content["correspondence_type"], "Others")
        self.assertEqual(document.current_version.content["correspondence_type_description"], "Engineering drawing")

    def test_historical_metadata_is_normalized_for_display_and_search_without_rewriting(self):
        legacy_content = {
            "subject": "Legacy proposal", "sender": "Legacy sender", "received_on": "2026-10-07",
            "document_date": "", "external_reference": "LEGACY-1", "correspondence_type": "proposals",
            "priority": "Critical", "receiving_office_id": self.office.pk,
            "receiving_office_code": self.office.code, "receiving_office_name": self.office.name,
            "classification": Document.Classification.CONFIDENTIAL,
        }
        document = Document.objects.create(
            document_type=self.document_type, title="Legacy proposal",
            classification=Document.Classification.CONFIDENTIAL, status=Document.Status.REGISTERED,
            created_by=self.user, issuing_office=self.office, issuing_office_code=self.office.code,
            reference="LEGACY/2026/000001",
        )
        version = DocumentVersion.objects.create(
            document=document, number=1, content=legacy_content, created_by=self.user,
        )

        list_response = self.client.get(reverse("documents:incoming-list"), {
            "correspondence_type": "Proposal", "priority": "Urgent",
        })
        self.assertContains(list_response, document.reference)
        self.assertContains(list_response, "Urgent")
        detail_response = self.client.get(reverse("documents:incoming-detail", args=[document.pk]))
        self.assertContains(detail_response, "Proposal")
        self.assertContains(detail_response, "Urgent")
        version.refresh_from_db()
        self.assertEqual(version.content["correspondence_type"], "proposals")
        self.assertEqual(version.content["priority"], "Critical")

    def test_detail_shows_attachments_and_related_document_without_granting_access(self):
        document = self.register()
        memo = Document.objects.create(
            document_type=DocumentType.objects.get(code="IM"), title="Related memo",
            classification=Document.Classification.INTERNAL, created_by=self.user,
        )
        relationship = create_related_document(
            source_document=document, target_document=memo,
            relationship_type=RelatedDocument.RelationshipType.REFERENCES, actor=self.user,
        )
        response = self.client.get(reverse("documents:incoming-detail", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Original received document")
        self.assertContains(response, "schedule.pdf")
        original = document.current_version.attachments.get(original_filename="received.pdf")
        view_url = reverse("documents:incoming-attachment-view", args=[document.pk, original.pk])
        self.assertContains(
            response,
            f'href="{view_url}" target="_blank" rel="noopener noreferrer">View</a>',
        )
        self.assertContains(response, "Related memo")
        self.assertEqual(relationship.target_document_id, memo.pk)
        self.assertFalse(can_view_document(self.outsider, memo))

    def test_revised_received_document_creates_new_version_and_preserves_original(self):
        document = self.register()
        first_version = document.current_version
        old_original = first_version.attachments.get(original_filename="received.pdf")
        revised_values = {
            "original_document": SimpleUploadedFile("received-revised.pdf", b"revised pdf", content_type="application/pdf"),
            "supporting_documents": [SimpleUploadedFile("appendix.pdf", b"new appendix", content_type="application/pdf")],
            "revision_note": "Corrected external schedule",
        }
        updated = register_incoming_revision(document=document, actor=self.user, values=revised_values)
        self.assertEqual(updated.current_version_number, 2)
        self.assertEqual(DocumentVersion.objects.filter(document=document).count(), 2)
        self.assertTrue(old_original.file.storage.exists(old_original.file.name))
        first_version.refresh_from_db()
        self.assertEqual(first_version.content["original_received_filename"], "received.pdf")
        second = updated.current_version
        self.assertEqual(second.content["original_received_filename"], "received-revised.pdf")
        self.assertEqual(second.revision_note, "Corrected external schedule")
        self.assertTrue(second.attachments.filter(original_filename="schedule.pdf").exists())
        self.assertTrue(second.attachments.filter(original_filename="received-revised.pdf").exists())
        self.assertTrue(document.audit_events.filter(action=AuditEvent.Action.INCOMING_REVISION_REGISTERED).exists())

    def test_memo_creation_and_numbering_behavior_remains_unchanged(self):
        memo = create_memo(owner=self.user, values={
            "to": "Managing Director", "through": "", "cc": "", "subject": "Existing memo",
            "body": "Memo body", "classification": Document.Classification.INTERNAL,
        })
        self.assertEqual(memo.document_type.code, "IM")
        self.assertEqual(memo.status, Document.Status.DRAFT)
        self.assertIsNone(memo.reference)
