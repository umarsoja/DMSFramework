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
            "action_type": task.action_type.code,
            "evidence_attachment_ids": sorted(evidence_ids),
        },
    )


@transaction.atomic
def complete_workflow_task(*, task_id, actor, response="", evidence_ids=()):
    """Complete a non-decision workflow action with DMS response and evidence."""
    task = WorkflowTask.objects.select_for_update(of=("self",)).select_related(
        "instance__document", "submitted_version", "action_type"
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
    evidence = Attachment.objects.filter(pk__in=requested_ids)
    if set(evidence.values_list("pk", flat=True)) != requested_ids:
        raise ValidationError({"evidence": "Choose existing DMS attachments as task evidence."})

    task.response = response.strip()
    task.status = WorkflowTask.Status.COMPLETED
    task.completed_at = timezone.now()
    task.save(update_fields=["response", "status", "completed_at"])
    task.evidence.set(evidence)
    _record_task_completion(document, actor, task, requested_ids)
    return task
