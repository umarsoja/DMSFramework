from django.test import SimpleTestCase

from apps.documents.handlers import (
    INTERNAL_MEMO_TYPE_CODE,
    InternalMemoHandler,
    UnknownDocumentTypeHandler,
    get_document_type_handler,
)


class DocumentTypeHandlerTests(SimpleTestCase):
    def test_internal_memo_code_resolves_registered_handler(self):
        self.assertIsInstance(get_document_type_handler(INTERNAL_MEMO_TYPE_CODE), InternalMemoHandler)

    def test_unknown_type_has_clear_lookup_error(self):
        with self.assertRaisesRegex(UnknownDocumentTypeHandler, "No document type handler.*'UNKNOWN'"):
            get_document_type_handler("UNKNOWN")

    def test_internal_memo_prepares_normalized_copy(self):
        values = {
            "to": "  Managing Director  ",
            "through": " Director, Operations ",
            "cc": " Finance Department ",
            "subject": " Generator maintenance plan ",
            "body": " Please review the plan. ",
            "classification": "INTERNAL",
        }

        result = get_document_type_handler(INTERNAL_MEMO_TYPE_CODE).prepare_payload(values)

        self.assertEqual(result, {
            "to": "Managing Director",
            "through": "Director, Operations",
            "cc": "Finance Department",
            "subject": "Generator maintenance plan",
            "body": "Please review the plan.",
            "classification": "INTERNAL",
        })
        self.assertEqual(values["to"], "  Managing Director  ")
        self.assertIsNot(result, values)

    def test_internal_memo_optional_fields_keep_existing_empty_defaults(self):
        result = get_document_type_handler(INTERNAL_MEMO_TYPE_CODE).prepare_payload({
            "to": " Recipient ",
            "subject": " Subject ",
            "body": " Body ",
            "classification": "INTERNAL",
        })

        self.assertEqual(result["through"], "")
        self.assertEqual(result["cc"], "")

    def test_missing_required_payload_field_preserves_existing_key_error(self):
        with self.assertRaises(KeyError):
            get_document_type_handler(INTERNAL_MEMO_TYPE_CODE).prepare_payload({})
