"""Type-specific document payload preparation."""

from types import MappingProxyType
from typing import Mapping, Protocol


INTERNAL_MEMO_TYPE_CODE = "IM"


class DocumentTypeHandler(Protocol):
    """Prepare a validated, type-specific payload without mutating its input.

    Callers pass form-cleaned values and receive a new dictionary suitable for
    the existing version content field. Missing required keys raise ``KeyError``;
    validation of form fields remains the responsibility of the memo form.
    """

    def prepare_payload(self, values: Mapping[str, str]) -> dict[str, str]:
        """Return the normalized payload for one document type."""


class InternalMemoHandler:
    """Normalize the existing Internal Memo version payload."""

    def prepare_payload(self, values: Mapping[str, str]) -> dict[str, str]:
        return {
            "to": values["to"].strip(),
            "through": values.get("through", "").strip(),
            "cc": values.get("cc", "").strip(),
            "subject": values["subject"].strip(),
            "body": values["body"].strip(),
            "classification": values["classification"],
        }


class UnknownDocumentTypeHandler(LookupError):
    """Raised when a document type has no explicitly registered handler."""


_HANDLERS: Mapping[str, DocumentTypeHandler] = MappingProxyType({
    INTERNAL_MEMO_TYPE_CODE: InternalMemoHandler(),
})


def get_document_type_handler(type_code: str) -> DocumentTypeHandler:
    """Resolve the explicitly registered handler for a document type code."""
    try:
        return _HANDLERS[type_code]
    except KeyError as exc:
        raise UnknownDocumentTypeHandler(
            f"No document type handler is registered for code {type_code!r}."
        ) from exc
