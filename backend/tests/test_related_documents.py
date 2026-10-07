from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from apps.documents.access import can_view_document
from apps.documents.models import (
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    RelatedDocument,
)
from apps.documents.services import (
    create_related_document,
    deactivate_related_document,
    related_documents,
)


class RelatedDocumentTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="relationship-owner", password="pw")
        self.other = User.objects.create_user(username="relationship-other", password="pw")
        self.outsider = User.objects.create_user(username="relationship-outsider", password="pw")
        self.document_type = DocumentType.objects.get(code="IM")
        self.source = self.make_document("Source document", self.owner)
        self.target = self.make_document("Target document", self.owner)
        self.grant(self.source, self.other, can_download=False)
        self.grant(self.target, self.other, can_download=False)

    def make_document(self, title, owner, classification=Document.Classification.INTERNAL):
        return Document.objects.create(
            document_type=self.document_type,
            title=title,
            classification=classification,
            created_by=owner,
        )

    def grant(self, document, user, *, can_download=False):
        DocumentAccess.objects.create(
            document=document,
            user=user,
            can_view=True,
            can_download=can_download,
            granted_by=self.owner,
        )

    def create(self, *, actor=None, source=None, target=None, relationship_type=None):
        return create_related_document(
            source_document=source or self.source,
            target_document=target or self.target,
            relationship_type=relationship_type or RelatedDocument.RelationshipType.SUPPORTS,
            actor=actor or self.owner,
        )

    def test_create_retrieve_and_audit_relationship(self):
        relationship = self.create()
        self.assertEqual(relationship.source_document, self.source)
        self.assertEqual(relationship.target_document, self.target)
        self.assertEqual(relationship.created_by, self.owner)
        self.assertTrue(self.source.audit_events.filter(
            action=AuditEvent.Action.RELATED_DOCUMENT_ADDED,
            context__relationship_id=relationship.pk,
        ).exists())
        listed = related_documents(document=self.source, user=self.owner)
        self.assertEqual(list(listed["outgoing"]), [relationship])
        self.assertFalse(list(listed["incoming"]))

    def test_duplicate_relationship_rejected(self):
        self.create()
        with self.assertRaises(ValidationError):
            self.create()

    def test_self_reference_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(target=self.source)

    def test_invalid_relationship_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.create(relationship_type="ATTACHMENT_OF")

    def test_source_and_target_authorization_are_both_required(self):
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.outsider)
        private_target = self.make_document("Private target", self.owner)
        with self.assertRaises(PermissionDenied):
            self.create(actor=self.other, target=private_target)

    def test_relationship_does_not_grant_access_and_listing_hides_target(self):
        restricted = self.make_document("Restricted target", self.owner, Document.Classification.RESTRICTED)
        self.create(target=restricted)
        self.assertFalse(can_view_document(self.outsider, self.source))
        self.assertFalse(can_view_document(self.outsider, restricted))
        listed = related_documents(document=self.source, user=self.other)
        self.assertFalse(list(listed["outgoing"]))
        self.assertFalse(can_view_document(self.other, restricted))

    def test_incoming_and_outgoing_are_directional(self):
        relationship = self.create()
        reverse = related_documents(document=self.target, user=self.owner)
        self.assertEqual(list(reverse["incoming"]), [relationship])
        self.assertFalse(list(reverse["outgoing"]))

    def test_deactivate_hides_and_audits_then_reactivate(self):
        relationship = self.create()
        deactivated = deactivate_related_document(relationship_id=relationship.pk, actor=self.owner)
        self.assertFalse(deactivated.is_active)
        self.assertFalse(list(related_documents(document=self.source, user=self.owner)["outgoing"]))
        self.assertTrue(self.source.audit_events.filter(
            action=AuditEvent.Action.RELATED_DOCUMENT_REMOVED,
            context__relationship_id=relationship.pk,
        ).exists())
        reactivated = self.create()
        self.assertEqual(reactivated.pk, relationship.pk)
        self.assertTrue(reactivated.is_active)
