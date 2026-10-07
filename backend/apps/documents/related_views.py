from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.organization.authentication import dms_access_required, dms_write_required
from .access import visible_documents
from .models import RelatedDocument
from .related_forms import RelatedDocumentForm
from .services import create_related_document, deactivate_related_document, related_documents


def _accessible_document(request, pk):
    return get_object_or_404(visible_documents(request.user), pk=pk)


@dms_access_required
def related_documents_panel(request, pk):
    document = _accessible_document(request, pk)
    return render(request, "documents/related_documents_panel.html", {
        "document": document,
        "related_documents": related_documents(document=document, user=request.user),
        "related_document_form": RelatedDocumentForm(source_document=document, user=request.user),
    })


@dms_write_required
@require_POST
def relate_document(request, pk):
    source = _accessible_document(request, pk)
    form = RelatedDocumentForm(request.POST, source_document=source, user=request.user)
    if not form.is_valid():
        messages.error(request, "Choose a visible document and relationship type.")
        return redirect("documents:memo-detail", pk=source.pk)
    try:
        create_related_document(
            source_document=source,
            target_document=form.cleaned_data["target_document"],
            relationship_type=form.cleaned_data["relationship_type"],
            actor=request.user,
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Related document added.")
    return redirect("documents:memo-detail", pk=source.pk)


@dms_write_required
@require_POST
def unrelate_document(request, pk, relationship_id):
    source = _accessible_document(request, pk)
    relationship = get_object_or_404(
        RelatedDocument, pk=relationship_id, source_document=source, is_active=True,
    )
    deactivate_related_document(relationship_id=relationship.pk, actor=request.user)
    messages.success(request, "Related document removed.")
    return redirect("documents:memo-detail", pk=source.pk)
