from pathlib import Path

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import (
    Attachment,
    AuditEvent,
    Document,
    DocumentAccess,
    DocumentType,
    DocumentVersion,
    WorkflowDecision,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowTask,
)


def _content_from_values(values):
    return {
        "to": values["to"].strip(),
        "through": values.get("through", "").strip(),
        "cc": values.get("cc", "").strip(),
        "subject": values["subject"].strip(),
        "body": values["body"].strip(),
        "classification": values["classification"],
    }


def _record(document, actor, action, *, version=None, context=None):
    from apps.organization.services import current_assignment

    return AuditEvent.objects.create(
        document=document,
        actor=actor,
        actor_assignment=current_assignment(actor),
        action=action,
        version=version,
        context=context or {},
    )


def _store_attachments(version, actor, uploads):
    for uploaded_file in uploads:
        filename = Path(uploaded_file.name).name[:255]
        media_type = getattr(uploaded_file, "content_type", "") or "application/octet-stream"
        attachment = Attachment(
            version=version,
            original_filename=filename,
            media_type=media_type[:120],
            size=uploaded_file.size,
            uploaded_by=actor,
        )
        attachment.file.save(filename, uploaded_file, save=True)
    return len(uploads)


def _new_version(document, actor, content, revision_note="", uploads=()):
    version_number = document.current_version_number + 1 if document.pk and document.versions.exists() else 1
    version = DocumentVersion.objects.create(
        document=document,
        number=version_number,
        content=content,
        revision_note=revision_note[:500],
        created_by=actor,
    )
    _store_attachments(version, actor, uploads)
    document.title = content["subject"]
    document.classification = content["classification"]
    document.current_version_number = version_number
    document.save(update_fields=["title", "classification", "current_version_number", "modified_at"])
    return version


def _submit_locked(document, actor, reviewer, *, resubmitted=False):
    if document.status not in (Document.Status.DRAFT, Document.Status.RETURNED):
        raise ValidationError("Only a draft or returned document can be submitted.")

    if document.status == Document.Status.RETURNED and not resubmitted:
        raise ValidationError("Save a revision before resubmitting a returned document.")

    workflow = WorkflowInstance.objects.filter(document=document).first()
    if workflow:
        definition = workflow.definition
    else:
        definition = WorkflowDefinition.objects.filter(
            document_type=document.document_type,
            is_active=True,
        ).prefetch_related("steps").first()
        if not definition or not definition.steps.filter(is_review=True).exists():
            raise ValidationError("No active review workflow is configured for this document type.")
        workflow = WorkflowInstance.objects.create(document=document, definition=definition)

    step = definition.steps.filter(is_review=True).order_by("sequence").first()
    from apps.organization.actors import resolve_workflow_actor

    reviewer, reviewer_assignment = resolve_workflow_actor(step=step, document=document, selected_user=reviewer)
    if reviewer.pk == actor.pk:
        raise ValidationError("A document creator cannot review their own memo.")
    cycle = workflow.tasks.aggregate(models_max=Max("cycle"))["models_max"] or 0
    cycle += 1
    WorkflowTask.objects.create(instance=workflow, step=step, assigned_to=reviewer,
                                assigned_assignment=reviewer_assignment, cycle=cycle)
    workflow.status = WorkflowInstance.Status.IN_PROGRESS
    workflow.completed_at = None
    workflow.save(update_fields=["status", "completed_at"])
    document.status = Document.Status.UNDER_REVIEW
    document.save(update_fields=["status", "modified_at"])
    _record(document, actor, AuditEvent.Action.RESUBMITTED if resubmitted else AuditEvent.Action.SUBMITTED,
            version=document.current_version, context={"reviewer_id": reviewer.pk, "cycle": cycle})
    _record(document, actor, AuditEvent.Action.UNDER_REVIEW,
            version=document.current_version, context={"reviewer_id": reviewer.pk, "cycle": cycle})
    return workflow


@transaction.atomic
def create_memo(*, owner, values, uploads=(), reviewer=None):
    from apps.organization.services import current_assignment

    content = _content_from_values(values)
    document_type = DocumentType.objects.get(code="IM", is_active=True)
    document = Document.objects.create(
        document_type=document_type,
        title=content["subject"],
        classification=content["classification"],
        created_by=owner,
        originating_assignment=current_assignment(owner),
    )
    memo_date = timezone.localtime(document.created_at).date()
    document.reference = f"APGC/IM/{memo_date.year}/{document.pk:06d}"
    document.save(update_fields=["reference"])
    version = _new_version(document, owner, content, uploads=uploads)
    DocumentAccess.objects.create(
        document=document,
        user=owner,
        can_view=True,
        can_download=True,
        granted_by=owner,
    )
    _record(document, owner, AuditEvent.Action.CREATED, version=version)
    if reviewer:
        _submit_locked(document, owner, reviewer)
    else:
        _record(document, owner, AuditEvent.Action.DRAFT_SAVED, version=version)
    return document


@transaction.atomic
def save_memo(*, document, actor, values, uploads=(), revision_note="", reviewer=None):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the document owner can edit this memo.")
    if document.status not in (Document.Status.DRAFT, Document.Status.RETURNED):
        raise ValidationError("Only a draft or returned memo can be edited.")
    if document.status == Document.Status.RETURNED and not revision_note.strip():
        raise ValidationError("Add a revision note before saving a returned memo.")

    content = _content_from_values(values)
    version = _new_version(document, actor, content, revision_note=revision_note, uploads=uploads)
    if document.status == Document.Status.RETURNED:
        _record(document, actor, AuditEvent.Action.REVISION_SAVED, version=version,
                context={"revision_note": revision_note.strip()})
    else:
        _record(document, actor, AuditEvent.Action.DRAFT_SAVED, version=version)
    if reviewer:
        _submit_locked(document, actor, reviewer, resubmitted=document.status == Document.Status.RETURNED)
    return document


@transaction.atomic
def decide_review(*, task_id, actor, outcome, comment=""):
    from apps.organization.services import current_assignment

    task = WorkflowTask.objects.select_for_update().select_related(
        "instance__document", "instance__definition", "step"
    ).get(pk=task_id)
    document = Document.objects.select_for_update().get(pk=task.instance.document_id)
    if task.assigned_to_id != actor.pk or task.status != WorkflowTask.Status.PENDING:
        raise PermissionDenied("You do not have an active review assignment for this memo.")
    if document.status != Document.Status.UNDER_REVIEW:
        raise ValidationError("This memo is no longer awaiting review.")
    if outcome not in (WorkflowDecision.Outcome.APPROVE, WorkflowDecision.Outcome.RETURN):
        raise ValidationError("Choose approve or return for revision.")
    if outcome == WorkflowDecision.Outcome.RETURN and not comment.strip():
        raise ValidationError({"comment": "A reason is required when returning a memo."})

    decision = WorkflowDecision.objects.create(
        task=task,
        actor=actor,
        actor_assignment=current_assignment(actor),
        outcome=outcome,
        comment=comment.strip(),
    )
    task.status = WorkflowTask.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else WorkflowTask.Status.RETURNED
    task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at"])
    action = AuditEvent.Action.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else AuditEvent.Action.RETURNED
    document.status = Document.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else Document.Status.RETURNED
    document.save(update_fields=["status", "modified_at"])
    workflow = task.instance
    workflow.status = WorkflowInstance.Status.APPROVED if outcome == WorkflowDecision.Outcome.APPROVE else WorkflowInstance.Status.RETURNED
    workflow.save(update_fields=["status"])
    _record(document, actor, action, version=document.current_version,
            context={"decision_id": decision.pk, "comment": decision.comment})
    return decision


@transaction.atomic
def finalize_document(*, document, actor):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the memo owner can finalize it.")
    if document.status != Document.Status.APPROVED:
        raise ValidationError("Only an approved memo can be finalized.")
    document.status = Document.Status.FINALIZED
    document.finalized_by = actor
    document.finalized_at = timezone.now()
    document.save(update_fields=["status", "finalized_by", "finalized_at", "modified_at"])
    workflow = document.workflow
    workflow.status = WorkflowInstance.Status.COMPLETED
    workflow.completed_at = document.finalized_at
    workflow.save(update_fields=["status", "completed_at"])
    _record(document, actor, AuditEvent.Action.FINALIZED, version=document.current_version)
    return document


@transaction.atomic
def archive_document(*, document, actor):
    document = Document.objects.select_for_update().get(pk=document.pk)
    if document.created_by_id != actor.pk:
        raise PermissionDenied("Only the memo owner can archive it.")
    if document.status != Document.Status.FINALIZED:
        raise ValidationError("Only a finalized memo can be archived.")
    document.status = Document.Status.ARCHIVED
    document.archived_by = actor
    document.archived_at = timezone.now()
    document.save(update_fields=["status", "archived_by", "archived_at", "modified_at"])
    _record(document, actor, AuditEvent.Action.ARCHIVED, version=document.current_version)
    return document


@transaction.atomic
def record_access(*, document, actor, action, context=None):
    return _record(document, actor, action, version=document.current_version, context=context)


@transaction.atomic
def reassign_task(*, task_id, actor, new_user, reason):
    from apps.organization.models import EmployeeProfile
    from apps.organization.services import current_assignment
    from .models import WorkflowTaskReassignment

    if not actor.has_perm("documents.reassign_workflow_tasks"):
        raise PermissionDenied("You are not authorized to reassign workflow tasks.")
    task = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document", "step", "assigned_to", "assigned_assignment"
    ).get(pk=task_id)
    document = Document.objects.select_for_update().get(pk=task.instance.document_id)
    if task.status != WorkflowTask.Status.PENDING or document.status != Document.Status.UNDER_REVIEW:
        raise ValidationError("Only a pending review task can be reassigned.")
    if not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError({"reason": "Provide a reassignment reason of up to 500 characters."})
    previous_user = task.assigned_to
    previous_assignment = task.assigned_assignment
    try:
        profile = new_user.employee_profile
    except EmployeeProfile.DoesNotExist:
        profile = None
    if profile:
        assignment = profile.current_assignment
        if (profile.account_status != EmployeeProfile.AccountStatus.ACTIVE
                or profile.employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED
                or not new_user.is_active
                or not assignment
                or not assignment.department.is_active
                or not assignment.position.is_active):
            raise ValidationError("A new task must be assigned to an active employee account.")
    elif not new_user.is_active or not new_user.is_staff:
        raise ValidationError("A new task must be assigned to an active staff reviewer.")
    resolved_user = new_user
    resolved_assignment = current_assignment(new_user)
    if resolved_user.pk == previous_user.pk:
        raise ValidationError("Choose someone other than the current assignee.")
    WorkflowTaskReassignment.objects.create(
        task=task,
        previous_user=previous_user,
        previous_assignment=previous_assignment,
        new_user=resolved_user,
        new_assignment=resolved_assignment,
        actor=actor,
        reason=reason.strip(),
    )
    task.assigned_to = resolved_user
    task.assigned_assignment = resolved_assignment
    task.save(update_fields=["assigned_to", "assigned_assignment"])
    _record(document, actor, AuditEvent.Action.REASSIGNED, version=document.current_version,
            context={"task_id": task.pk, "from_user_id": previous_user.pk,
                     "to_user_id": resolved_user.pk, "reason": reason.strip()})
    return task
