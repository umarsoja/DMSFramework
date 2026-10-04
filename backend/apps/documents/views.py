from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from apps.organization.authentication import is_dms_viewer

from .access import can_download_document, can_edit_document, pending_review_task, visible_documents
from .forms import MemoForm
from .models import Attachment, AuditEvent, Document, WorkflowDecision, WorkflowInstance
from .pdf import render_memo_pdf
from .services import (
    archive_document,
    approved_workflow_version,
    create_memo,
    decide_review,
    finalize_document,
    record_access,
    save_memo,
)


def _workspace_context(request, page_title, breadcrumbs):
    return {
        "page_title": page_title,
        "breadcrumbs": breadcrumbs,
        "can_create_memo": not is_dms_viewer(request.user),
        "sidebar_items": [
            {"label": "Internal memos", "url": reverse("documents:memo-list"), "active": True},
            {"label": "Create memo", "url": reverse("documents:memo-create"), "active": False},
        ],
    }


def _add_validation_error(form, error):
    if hasattr(error, "message_dict"):
        for field, field_errors in error.message_dict.items():
            for field_error in field_errors:
                form.add_error(field if field in form.fields else None, field_error)
    else:
        for message in error.messages:
            form.add_error(None, message)


def _accessible_memo(request, pk):
    return get_object_or_404(
        visible_documents(request.user).filter(document_type__code="IM").select_related(
            "originating_assignment__department", "originating_assignment__position",
        ),
        pk=pk,
    )


@login_required
def memo_list(request):
    memos = visible_documents(request.user).filter(document_type__code="IM").select_related("created_by")
    query = request.GET.get("q", "").strip()
    if query:
        memos = memos.filter(Q(reference__icontains=query) | Q(title__icontains=query))
    status_filter = request.GET.get("status", "")
    if status_filter in Document.Status.values:
        memos = memos.filter(status=status_filter)
    page = Paginator(memos, 20).get_page(request.GET.get("page"))
    context = _workspace_context(request, "Internal memos", [
        {"label": "Workspace", "url": reverse("documents:memo-list")},
        {"label": "Internal memos"},
    ])
    context.update({"page": page, "query": query, "status_filter": status_filter, "statuses": Document.Status.choices})
    return render(request, "documents/memo_list.html", context)


@login_required
def memo_create(request):
    action = request.POST.get("action", "save") if request.method == "POST" else "save"
    form = MemoForm(request.POST or None, request.FILES or None, user=request.user, action=action)
    if request.method == "POST":
        if action not in ("save", "submit"):
            return HttpResponseBadRequest("Invalid memo action.")
        if form.is_valid():
            try:
                document = create_memo(
                    owner=request.user,
                    values=form.cleaned_data,
                    uploads=form.cleaned_data["attachments"],
                    reviewer=form.cleaned_data.get("reviewer") if action == "submit" else None,
                )
            except (ValidationError, PermissionDenied) as error:
                if isinstance(error, PermissionDenied):
                    raise
                _add_validation_error(form, error)
            else:
                messages.success(request, "Memo submitted for review." if action == "submit" else "Draft saved.")
                return redirect("documents:memo-detail", pk=document.pk)
    context = _workspace_context(request, "Create internal memo", [
        {"label": "Internal memos", "url": reverse("documents:memo-list")},
        {"label": "Create memo"},
    ])
    context["form"] = form
    return render(request, "documents/memo_form.html", context)


@login_required
def memo_edit(request, pk):
    document = _accessible_memo(request, pk)
    if not can_edit_document(request.user, document):
        raise PermissionDenied("You cannot edit this memo.")
    action = request.POST.get("action", "save") if request.method == "POST" else "save"
    form = MemoForm(
        request.POST or None,
        request.FILES or None,
        user=request.user,
        document=document,
        action=action,
    )
    if request.method == "POST":
        if action not in ("save", "submit"):
            return HttpResponseBadRequest("Invalid memo action.")
        if form.is_valid():
            try:
                document = save_memo(
                    document=document,
                    actor=request.user,
                    values=form.cleaned_data,
                    uploads=form.cleaned_data["attachments"],
                    revision_note=form.cleaned_data.get("revision_note", ""),
                    reviewer=form.cleaned_data.get("reviewer") if action == "submit" else None,
                )
            except (ValidationError, PermissionDenied) as error:
                if isinstance(error, PermissionDenied):
                    raise
                _add_validation_error(form, error)
            else:
                messages.success(request, "Memo resubmitted for review." if action == "submit" and document.status == Document.Status.UNDER_REVIEW else "Memo changes saved.")
                return redirect("documents:memo-detail", pk=document.pk)
    context = _workspace_context(request, "Revise internal memo" if document.status == Document.Status.RETURNED else "Edit draft memo", [
        {"label": "Internal memos", "url": reverse("documents:memo-list")},
        {"label": document.reference, "url": reverse("documents:memo-detail", kwargs={"pk": document.pk})},
        {"label": "Edit"},
    ])
    context.update({"form": form, "document": document})
    return render(request, "documents/memo_form.html", context)


@login_required
def memo_detail(request, pk):
    document = _accessible_memo(request, pk)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.VIEWED)
    workflow = WorkflowInstance.objects.filter(document=document).prefetch_related(
        "tasks__assigned_to", "tasks__assigned_assignment__department", "tasks__assigned_assignment__position",
        "tasks__reassignments__previous_user", "tasks__reassignments__previous_assignment__department",
        "tasks__reassignments__previous_assignment__position", "tasks__reassignments__new_user",
    ).first()
    tasks = workflow.tasks.all().order_by("cycle", "step__sequence") if workflow else []
    decisions = WorkflowDecision.objects.filter(task__instance=workflow).select_related(
        "actor", "actor_assignment__department", "actor_assignment__position"
    ) if workflow else []
    active_task = pending_review_task(request.user, document)
    pending_task = next((task for task in tasks if task.status == "PENDING"), None)
    responsible_user = pending_task.assigned_to if pending_task else (
        document.created_by if document.status in (Document.Status.DRAFT, Document.Status.RETURNED) else None
    )
    current_version = document.current_version
    context = _workspace_context(request, document.title, [
        {"label": "Internal memos", "url": reverse("documents:memo-list")},
        {"label": document.reference},
    ])
    context.update({
        "document": document,
        "version": current_version,
        "versions": document.versions.select_related("created_by").prefetch_related("attachments").all(),
        "audit_events": document.audit_events.select_related("actor", "version").all(),
        "tasks": tasks,
        "decisions": decisions,
        "review_task": active_task,
        "responsible_user": responsible_user,
        "can_edit": can_edit_document(request.user, document),
        "can_download": can_download_document(request.user, document),
        "can_finalize": document.created_by_id == request.user.pk and document.status == Document.Status.APPROVED,
        "can_archive": document.created_by_id == request.user.pk and document.status == Document.Status.FINALIZED,
        "can_view_pdf": document.status in (Document.Status.FINALIZED, Document.Status.ARCHIVED),
    })
    return render(request, "documents/memo_detail.html", context)


@login_required
@require_POST
def review_memo(request, pk):
    document = _accessible_memo(request, pk)
    task = pending_review_task(request.user, document)
    if not task:
        raise PermissionDenied("You do not have an active review assignment for this memo.")
    try:
        decide_review(
            task_id=task.pk,
            actor=request.user,
            outcome=request.POST.get("decision", ""),
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as error:
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Memo returned to its owner." if request.POST.get("decision") == WorkflowDecision.Outcome.RETURN else "Memo approved.")
    return redirect("documents:memo-detail", pk=document.pk)


@login_required
@require_POST
def finalize_memo(request, pk):
    document = _accessible_memo(request, pk)
    finalize_document(document=document, actor=request.user)
    messages.success(request, "Memo finalized. Its PDF representation is ready to view.")
    return redirect("documents:memo-detail", pk=document.pk)


@login_required
@require_POST
def archive_memo(request, pk):
    document = _accessible_memo(request, pk)
    archive_document(document=document, actor=request.user)
    messages.success(request, "Memo archived.")
    return redirect("documents:memo-detail", pk=document.pk)


def _pdf_response(document, *, download, version):
    pdf = render_memo_pdf(document, version=version)
    filename = f"{document.reference.replace('/', '-')}.pdf"
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'{"attachment" if download else "inline"}; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _final_memo_for_user(request, pk):
    document = _accessible_memo(request, pk)
    if document.status not in (Document.Status.FINALIZED, Document.Status.ARCHIVED):
        raise PermissionDenied("A PDF is available only after finalization.")
    return document


@login_required
def memo_pdf_view(request, pk):
    document = _final_memo_for_user(request, pk)
    version = approved_workflow_version(document)
    response = _pdf_response(document, download=False, version=version)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.PDF_VIEWED, version=version)
    return response


@login_required
def memo_pdf_download(request, pk):
    document = _final_memo_for_user(request, pk)
    if not can_download_document(request.user, document):
        raise PermissionDenied("You have view access but not download access.")
    version = approved_workflow_version(document)
    response = _pdf_response(document, download=True, version=version)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.DOWNLOADED,
                  context={"kind": "pdf"}, version=version)
    return response


@login_required
def attachment_view(request, pk, attachment_id):
    document = _accessible_memo(request, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, version__document=document)
    suffix = Path(attachment.original_filename).suffix.lower()
    inline_types = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    if suffix not in inline_types:
        return HttpResponse("This attachment type is available as a controlled download.", status=415)
    response = FileResponse(attachment.file.open("rb"), content_type=inline_types[suffix], filename=attachment.original_filename)
    response["Content-Disposition"] = f'inline; filename="{Path(attachment.original_filename).name}"'
    response["X-Content-Type-Options"] = "nosniff"
    record_access(document=document, actor=request.user, action=AuditEvent.Action.ATTACHMENT_VIEWED,
                  context={"attachment_id": attachment.pk, "filename": attachment.original_filename})
    return response


@login_required
def attachment_download(request, pk, attachment_id):
    document = _accessible_memo(request, pk)
    if not can_download_document(request.user, document):
        raise PermissionDenied("You have view access but not download access.")
    attachment = get_object_or_404(Attachment, pk=attachment_id, version__document=document)
    response = FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.original_filename,
                            content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    record_access(document=document, actor=request.user, action=AuditEvent.Action.DOWNLOADED,
                  context={"kind": "attachment", "attachment_id": attachment.pk, "filename": attachment.original_filename})
    return response
