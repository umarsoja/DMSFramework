"""Atomic allocation of official, office-scoped document references."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from .models import DocumentNumberSequence


def allocate_document_reference(document, *, issued_at=None):
    """Assign a unique APGC/{OFFICE}/{YEAR}/{SEQUENCE} reference once.

    Call from the transaction that performs the document's official transition.
    Existing references are immutable and are returned unchanged.
    """
    if document.reference:
        return document.reference

    office_code = document.issuing_office_code
    if not document.issuing_office_id or not office_code:
        raise ValidationError("An issuing department is required before a document can be finalized.")
    if "/" in office_code or office_code.strip() != office_code:
        raise ValidationError("The issuing department code cannot be used in an official reference.")

    year = timezone.localtime(issued_at or timezone.now()).year
    try:
        sequence = DocumentNumberSequence.objects.select_for_update().get(
            office_code=office_code, year=year,
        )
    except DocumentNumberSequence.DoesNotExist:
        try:
            # A savepoint lets a concurrent first allocator recover from the unique
            # constraint race without poisoning the surrounding finalization.
            with transaction.atomic():
                sequence = DocumentNumberSequence.objects.create(
                    office_code=office_code, year=year, last_sequence=0,
                )
        except IntegrityError:
            sequence = None
        if sequence is None:
            sequence = DocumentNumberSequence.objects.select_for_update().get(
                office_code=office_code, year=year,
            )

    DocumentNumberSequence.objects.filter(pk=sequence.pk).update(
        last_sequence=F("last_sequence") + 1,
    )
    sequence.refresh_from_db(fields=["last_sequence"])

    document.numbering_year = year
    document.number_sequence = sequence.last_sequence
    document.reference = f"APGC/{office_code}/{year}/{sequence.last_sequence:06d}"
    return document.reference
