from django import forms

from .access import visible_documents
from .models import Document, RelatedDocument


class RelatedDocumentForm(forms.Form):
    target_document = forms.ModelChoiceField(queryset=Document.objects.none(), label="Related document")
    relationship_type = forms.ChoiceField(choices=RelatedDocument.RelationshipType.choices, label="Relationship")

    def __init__(self, *args, source_document, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["target_document"].queryset = visible_documents(user).exclude(pk=source_document.pk).order_by(
            "reference", "title"
        )
