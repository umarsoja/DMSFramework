"""Type-specific document payload preparation."""

from types import MappingProxyType
from pathlib import Path
from typing import Any, Mapping, Protocol

from django.core.exceptions import ValidationError


INTERNAL_MEMO_TYPE_CODE = "IM"
INCOMING_CORRESPONDENCE_TYPE_CODE = "IC"

PRIORITY_CHOICES = (
    ("URGENT", "Urgent"),
    ("HIGH", "High"),
    ("MEDIUM", "Medium"),
    ("LOW", "Low"),
)

CORRESPONDENCE_TYPE_CHOICES = (
    ("CONTRACT_AGREEMENT", "Contract / Agreement"),
    ("INVITATION", "Invitation"),
    ("INVOICE", "Invoice"),
    ("LETTER", "Letter"),
    ("NOTICE", "Notice"),
    ("PETITION", "Petition"),
    ("POLICY_GUIDELINE", "Policy / Guideline"),
    ("PROPOSAL", "Proposal"),
    ("QUOTATION", "Quotation"),
    ("REPORT", "Report"),
    ("OTHER", "Others"),
)

_TYPE_LABELS = dict(CORRESPONDENCE_TYPE_CHOICES)
_PRIORITY_LABELS = dict(PRIORITY_CHOICES)
_LEGACY_TYPE_LABELS = {
    "contract": "Contract / Agreement",
    "contract correspondence": "Contract / Agreement",
    "contract agreement": "Contract / Agreement",
    "contract / agreement": "Contract / Agreement",
    "agreement": "Contract / Agreement",
    "invitation": "Invitation",
    "invoice": "Invoice",
    "letter": "Letter",
    "notice": "Notice",
    "petition": "Petition",
    "policy": "Policy / Guideline",
    "guideline": "Policy / Guideline",
    "policy / guideline": "Policy / Guideline",
    "proposal": "Proposal",
    "proposals": "Proposal",
    "quotation": "Quotation",
    "quotations": "Quotation",
    "report": "Report",
    "reports": "Report",
}
_TYPE_ALIASES = {
    "CONTRACT_AGREEMENT": ("Contract", "Agreement", "Contract correspondence"),
    "INVITATION": ("Invitation",),
    "INVOICE": ("Invoice",),
    "LETTER": ("Letter",),
    "NOTICE": ("Notice",),
    "PETITION": ("Petition",),
    "POLICY_GUIDELINE": ("Policy", "Guideline"),
    "PROPOSAL": ("Proposals",),
    "QUOTATION": ("Quotations",),
    "REPORT": ("Reports",),
    "OTHER": ("Other",),
}


def correspondence_metadata_display(content):
    """Render new controlled metadata and clearly equivalent legacy values consistently."""
    type_code = content.get("correspondence_type_code")
    if type_code in _TYPE_LABELS:
        type_label = _TYPE_LABELS[type_code]
        if type_code == "OTHER" and content.get("correspondence_type_description"):
            type_label = f"{type_label}: {content['correspondence_type_description']}"
    else:
        legacy_type = str(content.get("correspondence_type", ""))
        type_label = _LEGACY_TYPE_LABELS.get(legacy_type.strip().casefold(), legacy_type)

    priority_code = content.get("priority_code")
    if priority_code in _PRIORITY_LABELS:
        priority_label = _PRIORITY_LABELS[priority_code]
    else:
        legacy_priority = str(content.get("priority", ""))
        folded_priority = legacy_priority.strip().casefold()
        priority_label = "Urgent" if folded_priority in {"critical", "urgent"} else legacy_priority
    return {"correspondence_type": type_label, "priority": priority_label}


def correspondence_type_search_terms(value):
    """Include stable codes, canonical labels and unambiguous historical spellings."""
    query = value.strip()
    if not query:
        return ()
    folded = query.casefold()
    for code, label in CORRESPONDENCE_TYPE_CHOICES:
        if folded in {code.casefold(), label.casefold()}:
            return (code, label, *_TYPE_ALIASES[code])
    legacy_label = _LEGACY_TYPE_LABELS.get(folded)
    if legacy_label:
        code = next(code for code, label in CORRESPONDENCE_TYPE_CHOICES if label == legacy_label)
        return (code, legacy_label, *_TYPE_ALIASES[code])
    return (query,)


def correspondence_type_filter_code(value):
    folded = value.strip().casefold()
    for code, label in CORRESPONDENCE_TYPE_CHOICES:
        if folded in {code.casefold(), label.casefold()}:
            return code
    legacy_label = _LEGACY_TYPE_LABELS.get(folded)
    if legacy_label:
        return next(code for code, label in CORRESPONDENCE_TYPE_CHOICES if label == legacy_label)
    return None


def priority_search_terms(value):
    query = value.strip()
    if not query:
        return ()
    folded = query.casefold()
    if folded in {"urgent", "critical"}:
        return ("URGENT", "Urgent", "Critical")
    for code, label in PRIORITY_CHOICES:
        if folded in {code.casefold(), label.casefold()}:
            return (code, label)
    return (query,)


class DocumentTypeHandler(Protocol):
    """Prepare a validated, type-specific payload without mutating its input.

    Callers pass form-cleaned values and receive a new dictionary suitable for
    the existing version content field. Missing required keys raise ``KeyError``;
    validation of submitted fields remains the responsibility of the document-type form.
    """

    def prepare_payload(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Return the normalized payload for one document type."""


class InternalMemoHandler:
    """Normalize Internal Memo content and snapshot stable directory identities."""

    @staticmethod
    def _recipient_payload(value: Any) -> tuple[str, list[int] | None]:
        # Keep the old text form readable for legacy service callers and stored
        # versions. The DMS form passes EmployeeProfile objects and persists
        # their stable IDs separately from the display snapshot.
        if isinstance(value, str):
            return value.strip(), None
        employees = list(value or ())
        if not employees:
            return "", []
        from apps.organization.directory import employee_directory_label

        return ", ".join(employee_directory_label(employee) for employee in employees), [
            employee.pk for employee in employees
        ]

    def prepare_payload(self, values: Mapping[str, Any]) -> dict[str, Any]:
        content = {
            "subject": values["subject"].strip(),
            "body": values["body"].strip(),
            "classification": values["classification"],
        }
        for field in ("to", "through", "cc"):
            label, employee_ids = self._recipient_payload(values.get(field, ""))
            content[field] = label
            if employee_ids is not None:
                content[f"{field}_employee_ids"] = employee_ids
        return content


class IncomingCorrespondenceHandler:
    """Normalize structured registration metadata for an external record."""

    def prepare_payload(self, values: Mapping[str, Any]) -> dict[str, Any]:
        received_on = values["received_on"]
        document_date = values["document_date"]
        receiving_office = values["receiving_office"]
        type_code = values["correspondence_type"]
        priority_code = values["priority"]
        if type_code not in _TYPE_LABELS:
            raise ValidationError({"correspondence_type": "Choose a valid correspondence category."})
        if priority_code not in _PRIORITY_LABELS:
            raise ValidationError({"priority": "Choose a valid priority."})
        other_description = values.get("other_correspondence_type", "").strip()
        if type_code == "OTHER" and not other_description:
            raise ValidationError({"other_correspondence_type": "Specify the document type when Others is selected."})
        return {
            "sender": values["sender"].strip(),
            "external_reference": values.get("external_reference", "").strip(),
            "document_date": document_date.isoformat() if document_date else "",
            "received_on": received_on.isoformat(),
            "subject": values["subject"].strip(),
            "correspondence_type": _TYPE_LABELS[type_code],
            "correspondence_type_code": type_code,
            "correspondence_type_description": other_description if type_code == "OTHER" else "",
            "receiving_office_id": receiving_office.pk,
            "receiving_office_code": receiving_office.code,
            "receiving_office_name": receiving_office.name,
            "priority": _PRIORITY_LABELS[priority_code],
            "priority_code": priority_code,
            "classification": values["classification"],
            "original_received_filename": Path(values["original_document"].name).name[:255],
            "supporting_filenames": [Path(item.name).name[:255] for item in values.get("supporting_documents", ())],
        }

    def prepare_revision_payload(self, existing: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
        content = dict(existing)
        content["original_received_filename"] = Path(values["original_document"].name).name[:255]
        content["supporting_filenames"] = list(existing.get("supporting_filenames", ())) + [
            Path(item.name).name[:255] for item in values.get("supporting_documents", ())
        ]
        return content


class UnknownDocumentTypeHandler(LookupError):
    """Raised when a document type has no explicitly registered handler."""


_HANDLERS: Mapping[str, DocumentTypeHandler] = MappingProxyType({
    INTERNAL_MEMO_TYPE_CODE: InternalMemoHandler(),
    INCOMING_CORRESPONDENCE_TYPE_CODE: IncomingCorrespondenceHandler(),
})


def get_document_type_handler(type_code: str) -> DocumentTypeHandler:
    """Resolve the explicitly registered handler for a document type code."""
    try:
        return _HANDLERS[type_code]
    except KeyError as exc:
        raise UnknownDocumentTypeHandler(
            f"No document type handler is registered for code {type_code!r}."
        ) from exc
