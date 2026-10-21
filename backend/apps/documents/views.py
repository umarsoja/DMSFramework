from io import BytesIO
from pathlib import Path

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import FileResponse, HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.organization.authentication import dms_access_required, dms_write_required
from apps.organization.authentication import is_dms_viewer
from .access import can_download_attachment, can_download_document, can_edit_document, pending_review_task, visible_documents
from .forms import (
    IncomingCorrespondenceForm,
    IncomingCorrespondenceRevisionForm,
    ManagementDirectiveForm,
    MemoForm,
    SupportingWorkflowTaskForm,
    WorkflowTaskCompletionForm,
)
from .handlers import (
    INCOMING_CORRESPONDENCE_TYPE_CODE,
    correspondence_metadata_display,
    correspondence_type_filter_code,
    correspondence_type_search_terms,
    priority_search_terms,
)
from .models import Attachment, AuditEvent, Document, ManagementDirective, WorkflowDecision, WorkflowInstance, WorkflowTask
from .pdf import render_memo_pdf
from .services import (
    archive_document,
    approved_workflow_version,
    create_memo,
    create_supporting_workflow_task,
    decide_review,
    finalize_document,
    issue_management_directive,
    record_access,
    register_incoming_correspondence,
    register_incoming_revision,
    related_documents,
    save_memo,
)
from .workflow_tasks import complete_workflow_task


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


def _accessible_incoming(request, pk):
    return get_object_or_404(
        visible_documents(request.user).filter(
            document_type__code=INCOMING_CORRESPONDENCE_TYPE_CODE,
        ).select_related("created_by", "issuing_office"),
        pk=pk,
    )


def _require_incoming_registration_capability(user):
    if not user.has_perm("documents.register_incoming_correspondence"):
        raise PermissionDenied("You are not authorized to register incoming correspondence.")


@dms_access_required
def incoming_list(request):
    from apps.organization.models import Department

    records = visible_documents(request.user).filter(
        document_type__code=INCOMING_CORRESPONDENCE_TYPE_CODE,
        versions__number=F("current_version_number"),
    ).select_related("created_by", "issuing_office")
    if request.GET.get("awaiting_management") == "1":
        records = records.filter(status=Document.Status.REGISTERED).exclude(
            management_directives__isnull=False,
        )
    filters = {
        "reference": "reference",
        "sender": "versions__content__sender__icontains",
        "external_reference": "versions__content__external_reference__icontains",
        "subject": "title__icontains",
        "received_on": "versions__content__received_on",
        "document_date": "versions__content__document_date",
    }
    for parameter, field in filters.items():
        value = request.GET.get(parameter, "").strip()
        if value:
            lookup = field + "__icontains" if parameter == "reference" else field
            records = records.filter(**{lookup: value})
    type_value = request.GET.get("correspondence_type", "").strip()
    if type_value:
        type_query = Q()
        type_code = correspondence_type_filter_code(type_value)
        for term in correspondence_type_search_terms(type_value):
            type_query |= Q(versions__content__correspondence_type__icontains=term)
            if type_code:
                type_query |= Q(versions__content__correspondence_type_code__iexact=term)
            else:
                type_query |= Q(versions__content__correspondence_type_description__icontains=term)
        records = records.filter(type_query)
    priority_value = request.GET.get("priority", "").strip()
    if priority_value:
        priority_query = Q()
        for term in priority_search_terms(priority_value):
            priority_query |= (
                Q(versions__content__priority__icontains=term)
                | Q(versions__content__priority_code__iexact=term)
            )
        records = records.filter(priority_query)
    office_id = request.GET.get("receiving_office", "").strip()
    if office_id.isdigit():
        records = records.filter(issuing_office_id=int(office_id))
    classification = request.GET.get("classification", "")
    if classification in Document.Classification.values:
        records = records.filter(classification=classification)
    page = Paginator(records.distinct(), 20).get_page(request.GET.get("page"))
    for document in page.object_list:
        metadata = correspondence_metadata_display(document.current_version.content)
        document.correspondence_type_display = metadata["correspondence_type"]
        document.priority_display = metadata["priority"]
    return render(request, "documents/incoming_list.html", {
        "page": page,
        "filters": {
            **{key: request.GET.get(key, "") for key in filters},
            "correspondence_type": type_value,
            "priority": priority_value,
        },
        "selected_office": office_id,
        "selected_classification": classification,
        "offices": Department.objects.filter(is_active=True).order_by("name"),
        "classifications": Document.Classification.choices,
        "can_register": request.user.has_perm("documents.register_incoming_correspondence"),
    })


@dms_access_required
def incoming_register(request):
    _require_incoming_registration_capability(request.user)
    form = IncomingCorrespondenceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            document = register_incoming_correspondence(actor=request.user, values=form.cleaned_data)
        except (ValidationError, PermissionDenied) as error:
            if isinstance(error, PermissionDenied):
                raise
            _add_validation_error(form, error)
        else:
            messages.success(request, "Incoming correspondence registered.")
            return redirect("documents:incoming-detail", pk=document.pk)
    return render(request, "documents/incoming_form.html", {
        "form": form,
        "page_title": "Register incoming correspondence",
    })


@dms_access_required
def incoming_detail(request, pk):
    document = _accessible_incoming(request, pk)
    version_number = request.GET.get("version", "")
    version = get_object_or_404(document.versions, number=version_number) if version_number.isdigit() else document.current_version
    metadata_display = correspondence_metadata_display(version.content)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.VIEWED, version=version)
    directives = document.management_directives.select_related(
        "issued_by", "action_type", "workflow_task__assigned_to",
        "workflow_task__assigned_assignment__department", "workflow_task__assigned_assignment__position",
        "workflow_task__action_type",
    ).prefetch_related("workflow_task__evidence__version", "workflow_task__supporting_tasks__assigned_to",
                       "workflow_task__supporting_tasks__action_type",
                       "workflow_task__supporting_tasks__step",
                       "workflow_task__supporting_tasks__evidence__version")
    directive_cards = []
    for directive in directives:
        task = directive.workflow_task
        supporting_cards = []
        if task:
            children = list(task.supporting_tasks.all())
            if task.assigned_to_id == request.user.pk:
                authorized_children = children
            else:
                delegated_ids = {
                    event.context.get("task_id")
                    for event in document.audit_events.filter(
                        action=AuditEvent.Action.TASK_ASSIGNED,
                        actor=request.user,
                    )
                    if event.context.get("parent_task_id") == task.pk
                }
                authorized_children = [child for child in children if child.pk in delegated_ids]
            supporting_cards = [
                {
                    "task": supporting_task,
                    "evidence_attachments": list(supporting_task.evidence.filter(version__document=document)),
                }
                for supporting_task in authorized_children
            ]
        directive_cards.append({
            "directive": directive,
            "task": task,
            "evidence_attachments": list(task.evidence.filter(version__document=document)) if task else [],
            "supporting_tasks": supporting_cards,
            "supporting_form": SupportingWorkflowTaskForm(issuer=request.user)
            if task and task.assigned_to_id == request.user.pk
            and task.status == WorkflowTask.Status.PENDING
            and request.user.has_perm("documents.assign_workflow_tasks") else None,
            "completion_form": WorkflowTaskCompletionForm(document=document)
            if task and task.assigned_to_id == request.user.pk and task.status == WorkflowTask.Status.PENDING
            else None,
        })
    can_issue = request.user.has_perm("documents.issue_management_directives")
    can_assign = request.user.has_perm("documents.assign_workflow_tasks")
    return render(request, "documents/incoming_detail.html", {
        "document": document,
        "version": version,
        "metadata_display": metadata_display,
        "versions": document.versions.all(),
        "attachments": version.attachments.all(),
        "downloadable_attachment_ids": [a.pk for a in version.attachments.all() if can_download_attachment(request.user, a)],
        "audit_events": document.audit_events.select_related("actor", "version"),
        "related_documents": related_documents(document=document, user=request.user),
        "can_download": can_download_document(request.user, document),
        "can_add_revision": request.user.has_perm("documents.register_incoming_correspondence"),
        "revision_form": IncomingCorrespondenceRevisionForm(),
        "directives": directive_cards,
        "can_issue_directive": can_issue,
        "directive_form": ManagementDirectiveForm(can_assign=can_assign, issuer=request.user) if can_issue else None,
        "can_assign_workflow_tasks": can_assign,
    })


@dms_write_required
@require_POST
def incoming_create_supporting_task(request, pk, task_id):
    document = _accessible_incoming(request, pk)
    parent_task = get_object_or_404(WorkflowTask, pk=task_id, instance__document=document)
    form = SupportingWorkflowTaskForm(request.POST, issuer=request.user)
    if not form.is_valid():
        messages.error(request, "Correct the supporting task details and try again.")
        return redirect("documents:incoming-detail", pk=document.pk)
    try:
        task = create_supporting_workflow_task(
            parent_task_id=parent_task.pk,
            actor=request.user,
            assignee=form.cleaned_data["assignee"].user,
            action_type=form.cleaned_data["action_type"],
            instruction=form.cleaned_data["instruction"],
            due_date=form.cleaned_data.get("due_date"),
        )
    except (ValidationError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        _add_validation_error(form, error)
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Supporting task assigned.")
    return redirect("documents:incoming-detail", pk=document.pk)


@dms_write_required
@require_POST
def incoming_issue_directive(request, pk):
    if not request.user.has_perm("documents.issue_management_directives"):
        raise PermissionDenied("You are not authorized to issue management directives.")
    document = _accessible_incoming(request, pk)
    form = ManagementDirectiveForm(
        request.POST,
        can_assign=request.user.has_perm("documents.assign_workflow_tasks"),
        issuer=request.user,
    )
    if not form.is_valid():
        messages.error(request, "Correct the directive details and try again.")
        return redirect("documents:incoming-detail", pk=document.pk)
    try:
        directive = issue_management_directive(
            target_document=document,
            issuer=request.user,
            action_type=form.cleaned_data["action_type"],
            instruction=form.cleaned_data["instruction"],
            assignee=form.cleaned_data["assignee"].user if form.cleaned_data.get("assignee") else None,
            due_date=form.cleaned_data.get("due_date"),
        )
    except (ValidationError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        _add_validation_error(form, error)
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(
            request,
            "Directive issued with an assigned task." if directive.workflow_task_id
            else "Directive issued without an assigned task.",
        )
    return redirect("documents:incoming-detail", pk=document.pk)


@dms_write_required
@require_POST
def incoming_complete_directive_task(request, pk, task_id):
    document = _accessible_incoming(request, pk)
    task = get_object_or_404(
        WorkflowTask.objects.select_related("parent_task"),
        pk=task_id,
        instance__document=document,
    )
    form = WorkflowTaskCompletionForm(request.POST, request.FILES, document=document)
    if not form.is_valid():
        messages.error(request, "Provide a response or at least one valid evidence attachment before completing the task.")
        return redirect("documents:incoming-detail", pk=document.pk)
    try:
        complete_workflow_task(
            task_id=task.pk,
            actor=request.user,
            response=form.cleaned_data["response"],
            evidence_ids=form.cleaned_data["evidence"].values_list("pk", flat=True),
            uploads=form.cleaned_data["uploads"],
        )
    except (ValidationError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "Task response recorded in the DMS.")
    directive = ManagementDirective.objects.filter(workflow_task_id__in=[
        task.pk, task.parent_task_id,
    ]).order_by("-issued_at").first() if task.parent_task_id else ManagementDirective.objects.filter(
        workflow_task=task,
    ).first()
    if directive:
        return redirect(f"{reverse('documents:incoming-detail', args=[document.pk])}#directive-{directive.pk}")
    return redirect("documents:incoming-detail", pk=document.pk)


@dms_access_required
@require_POST
def incoming_revision(request, pk):
    _require_incoming_registration_capability(request.user)
    document = _accessible_incoming(request, pk)
    form = IncomingCorrespondenceRevisionForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Provide a valid revised document and revision note.")
        return redirect("documents:incoming-detail", pk=document.pk)
    try:
        register_incoming_revision(document=document, actor=request.user, values=form.cleaned_data)
    except (ValidationError, PermissionDenied) as error:
        if isinstance(error, PermissionDenied):
            raise
        messages.error(request, " ".join(error.messages))
    else:
        messages.success(request, "External document revision registered as a new version.")
    return redirect("documents:incoming-detail", pk=document.pk)


@dms_access_required
def incoming_attachment_view(request, pk, attachment_id):
    document = _accessible_incoming(request, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, version__document=document)
    suffix = Path(attachment.original_filename).suffix.lower()
    inline_types = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
    if suffix not in inline_types:
        return HttpResponse("This attachment type is available as a controlled download.", status=415)
    response = FileResponse(
        attachment.file.open("rb"),
        content_type=inline_types[suffix],
        filename=attachment.original_filename,
    )
    response["Content-Disposition"] = f'inline; filename="{Path(attachment.original_filename).name}"'
    response["X-Content-Type-Options"] = "nosniff"
    record_access(document=document, actor=request.user, action=AuditEvent.Action.ATTACHMENT_VIEWED,
                  context={"attachment_id": attachment.pk, "filename": attachment.original_filename},
                  version=attachment.version)
    return response


@dms_access_required
def incoming_attachment_download(request, pk, attachment_id):
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    document = _accessible_incoming(request, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, version__document=document)
    if not can_download_attachment(request.user, attachment):
        raise PermissionDenied("You have view access but not download access.")
    if request.method == "GET":
        return render(request, "documents/attachment_download_confirm.html", {
            "document": document, "attachment": attachment,
        })
    if request.POST.get("acknowledged") != "1":
        return render(request, "documents/attachment_download_confirm.html", {
            "document": document, "attachment": attachment,
            "download_error": "Please acknowledge the security notice before downloading.",
        }, status=400)
    response = FileResponse(
        attachment.file.open("rb"), as_attachment=True,
        filename=attachment.original_filename, content_type="application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    record_access(document=document, actor=request.user, action=AuditEvent.Action.DOWNLOADED,
                  context={"kind": "attachment", "attachment_id": attachment.pk,
                           "filename": attachment.original_filename}, version=attachment.version)
    return response


@dms_access_required
def memo_list(request):
    memos = visible_documents(request.user).filter(document_type__code="IM").select_related("created_by")
    query = request.GET.get("q", "").strip()
    if query:
        memos = memos.filter(Q(reference__icontains=query) | Q(title__icontains=query))
    status_filter = request.GET.get("status", "")
    if status_filter == "PENDING":
        memos = memos.filter(status__in=(
            Document.Status.DRAFT, Document.Status.UNDER_REVIEW, Document.Status.RETURNED,
        ))
    elif status_filter in Document.Status.values:
        memos = memos.filter(status=status_filter)
    classification_filter = request.GET.get("classification", "")
    if classification_filter in Document.Classification.values:
        memos = memos.filter(classification=classification_filter)
    page = Paginator(memos, 20).get_page(request.GET.get("page"))
    context = _workspace_context(request, "Internal memos", [
        {"label": "Workspace", "url": reverse("documents:memo-list")},
        {"label": "Internal memos"},
    ])
    context.update({
        "page": page,
        "query": query,
        "status_filter": status_filter,
        "statuses": Document.Status.choices,
        "classification_filter": classification_filter,
        "classifications": Document.Classification.choices,
    })
    return render(request, "documents/memo_list.html", context)


@dms_write_required
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


@dms_write_required
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
                    remove_attachment_ids=form.cleaned_data.get("remove_attachments", ()),
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


@dms_access_required
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


@dms_write_required
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


@dms_write_required
@require_POST
def finalize_memo(request, pk):
    document = _accessible_memo(request, pk)
    finalize_document(document=document, actor=request.user)
    messages.success(request, "Memo finalized. Its PDF representation is ready to view.")
    return redirect("documents:memo-detail", pk=document.pk)


@dms_write_required
@require_POST
def archive_memo(request, pk):
    document = _accessible_memo(request, pk)
    archive_document(document=document, actor=request.user)
    messages.success(request, "Memo archived.")
    return redirect("documents:memo-detail", pk=document.pk)


def _pdf_response(document, *, download, version):
    pdf = render_memo_pdf(document, version=version)
    filename = f"{document.reference.replace('/', '-')}.pdf"
    response = FileResponse(
        BytesIO(pdf),
        as_attachment=download,
        filename=filename,
        content_type="application/pdf",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _final_memo_for_user(request, pk):
    document = _accessible_memo(request, pk)
    if document.status not in (Document.Status.FINALIZED, Document.Status.ARCHIVED):
        raise PermissionDenied("A PDF is available only after finalization.")
    return document


@dms_access_required
def memo_pdf_view(request, pk):
    document = _final_memo_for_user(request, pk)
    version = approved_workflow_version(document)
    response = _pdf_response(document, download=False, version=version)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.PDF_VIEWED, version=version)
    return response


@dms_access_required
def memo_pdf_download(request, pk):
    document = _final_memo_for_user(request, pk)
    if not can_download_document(request.user, document):
        raise PermissionDenied("You have view access but not download access.")
    version = approved_workflow_version(document)
    response = _pdf_response(document, download=True, version=version)
    record_access(document=document, actor=request.user, action=AuditEvent.Action.DOWNLOADED,
                  context={"kind": "pdf"}, version=version)
    return response


@dms_access_required
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


@dms_access_required
def attachment_download(request, pk, attachment_id):
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    document = _accessible_memo(request, pk)
    attachment = get_object_or_404(Attachment, pk=attachment_id, version__document=document)
    if not can_download_attachment(request.user, attachment):
        raise PermissionDenied("You have view access but not download access.")
    if request.method == "GET":
        return render(request, "documents/attachment_download_confirm.html", {
            "document": document, "attachment": attachment,
        })
    if request.POST.get("acknowledged") != "1":
        return render(request, "documents/attachment_download_confirm.html", {
            "document": document, "attachment": attachment,
            "download_error": "Please acknowledge the security notice before downloading.",
        }, status=400)
    response = FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.original_filename,
                            content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    record_access(document=document, actor=request.user, action=AuditEvent.Action.DOWNLOADED,
                  context={"kind": "attachment", "attachment_id": attachment.pk, "filename": attachment.original_filename})
    return response
