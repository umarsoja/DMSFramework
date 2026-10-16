from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Attachment, AuditEvent, Document, WorkflowTask


def _record_task_completion(document, actor, task, evidence_ids):
    from apps.organization.services import current_assignment

    assignment = current_assignment(actor)
    return AuditEvent.objects.create(
        document=document,
        actor=actor,
        actor_assignment=assignment,
        action=AuditEvent.Action.TASK_COMPLETED,
        version=task.submitted_version,
        context={
            "task_id": task.pk,
            "parent_task_id": task.parent_task_id,
            "action_type": task.action_type.code,
            "response": task.response,
            "evidence_attachment_ids": sorted(evidence_ids),
        },
    )


@transaction.atomic
def complete_workflow_task(*, task_id, actor, response="", evidence_ids=(), uploads=()):
    """Complete a non-decision workflow action with DMS response and evidence."""
    task = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document", "submitted_version", "action_type",
        "parent_task__assigned_to", "management_directive__issued_by",
    ).get(pk=task_id)
    document = Document.objects.select_for_update().get(pk=task.instance.document_id)
    if task.assigned_to_id != actor.pk or task.status != WorkflowTask.Status.PENDING:
        raise PermissionDenied("You do not have an active assignment for this task.")
    if task.action_type_id and task.action_type.code in {"review", "approve"}:
        raise ValidationError("Review and approval tasks must be completed through a recorded decision.")

    try:
        requested_ids = {int(value) for value in evidence_ids}
    except (TypeError, ValueError):
        raise ValidationError({"evidence": "Choose existing DMS attachments as task evidence."})
    evidence = Attachment.objects.filter(pk__in=requested_ids, version__document=document)
    if set(evidence.values_list("pk", flat=True)) != requested_ids:
        raise ValidationError({"evidence": "Choose existing attachments from this document as task evidence."})

    response = response.strip()
    if not response and not requested_ids and not uploads:
        raise ValidationError("Provide a response or at least one evidence attachment.")

    from .services import _store_attachments

    uploaded_evidence = _store_attachments(task.submitted_version, actor, uploads)
    evidence_ids_to_record = requested_ids | {attachment.pk for attachment in uploaded_evidence}

    task.response = response
    task.status = WorkflowTask.Status.COMPLETED
    task.completed_at = timezone.now()
    task.save(update_fields=["response", "status", "completed_at"])
    task.evidence.set([*evidence, *uploaded_evidence])
    event = _record_task_completion(document, actor, task, evidence_ids_to_record)
    if task.parent_task_id:
        from .notifications import supporting_task_delegator

        recipient = supporting_task_delegator(task)
    else:
        directive = getattr(task, "management_directive", None)
        recipient = directive.issued_by if directive else None
    if recipient:
        from .access import can_view_document
        from .notifications import record_notification

        if can_view_document(recipient, document):
            record_notification(event, recipient, workflow_task=task)
    return task
