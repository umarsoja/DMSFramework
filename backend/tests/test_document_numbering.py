from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as datetime_timezone
from unittest import skipIf
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase

from apps.documents.models import (
    Document,
    DocumentNumberSequence,
    WorkflowDecision,
    WorkflowTask,
)
from apps.documents.services import create_memo, decide_review, finalize_document
from apps.organization.models import Department, EmployeeProfile, Position
from apps.organization.services import create_assignment


class DocumentNumberingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.office = self.make_office("MD")
        self.owner = self.make_employee("number-owner", "NUMBER-OWNER", self.office)
        self.reviewer = self.make_employee("number-reviewer", "NUMBER-REVIEWER", self.office)
        self.reviewer.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))

    def make_office(self, code):
        department = Department.objects.create(code=code, name=f"{code} Office")
        position = Position.objects.create(code=f"{code}-POS", title=f"{code} Staff", department=department)
        return department, position

    def make_employee(self, username, staff_id, office):
        User = get_user_model()
        user = User.objects.create_user(username=username, password="pw")
        profile = EmployeeProfile.objects.create(
            user=user,
            staff_id=staff_id,
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
        )
        create_assignment(
            employee=profile,
            department=office[0],
            position=office[1],
            actor=user,
        )
        return user

    def make_approved_document(self, *, owner=None, reviewer=None, subject="Numbering memo"):
        owner = owner or self.owner
        reviewer = reviewer or self.reviewer
        document = create_memo(
            owner=owner,
            reviewer=reviewer,
            values={
                "to": "Managing Director",
                "through": "",
                "cc": "",
                "subject": subject,
                "body": "Please review.",
                "classification": Document.Classification.INTERNAL,
            },
        )
        task = document.workflow.tasks.get(status=WorkflowTask.Status.PENDING)
        decide_review(task_id=task.pk, actor=reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
        document.refresh_from_db()
        return document

    def finalize(self, document, *, at=None):
        if at is None:
            return finalize_document(document=document, actor=document.created_by)
        with patch("apps.documents.services.timezone.now", return_value=at):
            return finalize_document(document=document, actor=document.created_by)

    def test_sequential_documents_in_same_office_get_distinct_numbers(self):
        first = self.make_approved_document(subject="First")
        second = self.make_approved_document(subject="Second")

        self.finalize(first)
        self.finalize(second)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertRegex(first.reference, r"^APGC/MD/\d{4}/000001$")
        self.assertRegex(second.reference, r"^APGC/MD/\d{4}/000002$")
        self.assertNotEqual(first.pk, first.reference)
        self.assertEqual(first.issuing_office_id, self.office[0].pk)
        self.assertEqual(first.issuing_office_code, "MD")
        self.assertEqual(first.numbering_year, second.numbering_year)
        self.assertEqual((first.number_sequence, second.number_sequence), (1, 2))

    def test_different_offices_have_independent_sequences(self):
        other_office = self.make_office("FIN")
        other_owner = self.make_employee("finance-owner", "FIN-OWNER", other_office)
        first = self.make_approved_document(subject="MD document")
        second = self.make_approved_document(owner=other_owner, subject="Finance document")

        self.finalize(first)
        self.finalize(second)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.reference.endswith("/000001"))
        self.assertTrue(second.reference.endswith("/000001"))
        self.assertTrue(first.reference.startswith("APGC/MD/"))
        self.assertTrue(second.reference.startswith("APGC/FIN/"))
        self.assertEqual(DocumentNumberSequence.objects.count(), 2)

    def test_year_change_starts_new_office_sequence(self):
        first = self.make_approved_document(subject="Prior year")
        second = self.make_approved_document(subject="New year")
        self.finalize(first, at=datetime(2026, 12, 31, 10, 0, tzinfo=datetime_timezone.utc))
        self.finalize(second, at=datetime(2027, 1, 1, 10, 0, tzinfo=datetime_timezone.utc))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.numbering_year, 2026)
        self.assertEqual(second.numbering_year, 2027)
        self.assertEqual(first.number_sequence, 1)
        self.assertEqual(second.number_sequence, 1)

    def test_draft_does_not_consume_number_and_existing_reference_is_preserved(self):
        draft = create_memo(
            owner=self.owner,
            values={
                "to": "Managing Director",
                "through": "",
                "cc": "",
                "subject": "Abandoned draft",
                "body": "Not issued.",
                "classification": Document.Classification.INTERNAL,
            },
        )
        self.assertIsNone(draft.reference)
        self.assertEqual(DocumentNumberSequence.objects.count(), 0)

        first = self.make_approved_document(subject="First official document")
        legacy = self.make_approved_document(subject="Legacy reference")
        legacy.reference = "APGC/IM/2025/000321"
        legacy.save(update_fields=["reference"])
        self.finalize(first)
        self.finalize(legacy)

        draft.refresh_from_db()
        first.refresh_from_db()
        legacy.refresh_from_db()
        self.assertIsNone(draft.reference)
        self.assertTrue(first.reference.endswith("/000001"))
        self.assertEqual(legacy.reference, "APGC/IM/2025/000321")
        self.assertIsNone(legacy.numbering_year)
        self.assertIsNone(legacy.number_sequence)

    def test_finalization_requires_an_issuing_department(self):
        document = self.make_approved_document()
        document.issuing_office = None
        document.issuing_office_code = ""
        document.save(update_fields=["issuing_office", "issuing_office_code"])

        with self.assertRaisesMessage(ValidationError, "An issuing department is required"):
            self.finalize(document)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.APPROVED)
        self.assertIsNone(document.reference)


@skipIf(connection.vendor != "postgresql", "PostgreSQL row locking is required for this concurrency test")
class ConcurrentDocumentNumberingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.office = Department.objects.create(code="IT", name="IT Office")
        self.position = Position.objects.create(code="IT-POS", title="IT Staff", department=self.office)
        User = get_user_model()
        users = []
        for username, staff_id in (("concurrent-owner-a", "CONCURRENT-A"),
                                   ("concurrent-owner-b", "CONCURRENT-B"),
                                   ("concurrent-reviewer", "CONCURRENT-R")):
            user = User.objects.create_user(username=username, password="pw")
            profile = EmployeeProfile.objects.create(
                user=user,
                staff_id=staff_id,
                account_status=EmployeeProfile.AccountStatus.ACTIVE,
            )
            create_assignment(employee=profile, department=self.office, position=self.position, actor=user)
            users.append(user)
        self.owner_a, self.owner_b, self.reviewer = users
        self.reviewer.groups.add(Group.objects.get(name="DMS Department Manager/HOD"))
        self.document_ids = []
        for owner in (self.owner_a, self.owner_b):
            document = create_memo(
                owner=owner,
                reviewer=self.reviewer,
                values={
                    "to": "Managing Director",
                    "through": "",
                    "cc": "",
                    "subject": "Concurrent numbering",
                    "body": "Please review.",
                    "classification": Document.Classification.INTERNAL,
                },
            )
            task = document.workflow.tasks.get(status=WorkflowTask.Status.PENDING)
            decide_review(task_id=task.pk, actor=self.reviewer, outcome=WorkflowDecision.Outcome.APPROVE)
            self.document_ids.append((document.pk, owner.pk))

    def test_concurrent_finalization_allocates_unique_references(self):
        def finalize_one(item):
            close_old_connections()
            document_id, owner_id = item
            try:
                document = Document.objects.get(pk=document_id)
                owner = get_user_model().objects.get(pk=owner_id)
                return finalize_document(document=document, actor=owner).reference
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            references = list(executor.map(finalize_one, self.document_ids))

        self.assertEqual(len(set(references)), 2)
        self.assertEqual(set(references), {
            "APGC/IT/{}/000001".format(Document.objects.get(pk=self.document_ids[0][0]).numbering_year),
            "APGC/IT/{}/000002".format(Document.objects.get(pk=self.document_ids[0][0]).numbering_year),
        })
        self.assertEqual(DocumentNumberSequence.objects.get(office_code="IT").last_sequence, 2)
