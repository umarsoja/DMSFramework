from django.utils import timezone
from django.conf import settings

from .access import can_view_document, visible_documents
from .handlers import INCOMING_CORRESPONDENCE_TYPE_CODE
from .models import AuditEvent, Document, NotificationReceipt, WorkflowTask


MENU_LIMIT = 5
ALL_LIMIT = 50


def record_notification(event, recipient, workflow_task=None):
    """Persist only per-user receipt/seen state for an existing DMS event."""
    if recipient is None or (event.action == AuditEvent.Action.INCOMING_REGISTERED and event.actor_id == recipient.pk):
        return None
    receipt, _ = NotificationReceipt.objects.get_or_create(
        recipient=recipient,
        event=event,
        defaults={"workflow_task": workflow_task},
    )
    if receipt and recipient.email and getattr(settings, "WORKFLOW_EMAIL_NOTIFICATIONS", True):
        from .email_service import queue_workflow_notification
        queue_workflow_notification(event=event, recipient=recipient, workflow_task=workflow_task)
    return receipt


def supporting_task_delegator(task):
    """Resolve the immutable actor who created a supporting-task assignment."""
    if not task.parent_task_id:
        return None
    assignment_event = AuditEvent.objects.filter(
        document_id=task.instance.document_id,
        action=AuditEvent.Action.TASK_ASSIGNED,
        context__task_id=task.pk,
    ).select_related("actor").order_by("occurred_at", "pk").first()
    return assignment_event.actor if assignment_event else task.parent_task.assigned_to


def actionable_notifications(user):
    visible = visible_documents(user)
    task_pending = NotificationReceipt.objects.filter(
        recipient=user,
        workflow_task__isnull=False,
        workflow_task__assigned_to=user,
        workflow_task__status=WorkflowTask.Status.PENDING,
        event__document__in=visible,
    )
    incoming_pending = NotificationReceipt.objects.filter(
        recipient=user,
        workflow_task__isnull=True,
        event__action=AuditEvent.Action.INCOMING_REGISTERED,
        event__document__in=visible,
        event__document__document_type__code=INCOMING_CORRESPONDENCE_TYPE_CODE,
        event__document__status=Document.Status.REGISTERED,
    ).exclude(event__document__management_directives__isnull=False)
    task_responses = NotificationReceipt.objects.filter(
        recipient=user,
        workflow_task__isnull=False,
        event__action=AuditEvent.Action.TASK_COMPLETED,
        event__document__in=visible,
    )
    memo_outcomes = NotificationReceipt.objects.filter(
        recipient=user,
        workflow_task__isnull=True,
        event__action__in=(AuditEvent.Action.APPROVED, AuditEvent.Action.RETURNED),
        event__document__in=visible,
        event__document__document_type__code="IM",
        event__document__created_by=user,
    )
    return (task_pending | incoming_pending | task_responses | memo_outcomes).select_related(
        "event__document__document_type", "workflow_task__action_type", "workflow_task__step",
    ).order_by("-event__occurred_at", "-pk")


def notification_menu_data(user):
    all_actionable = actionable_notifications(user)
    qs = all_actionable[:MENU_LIMIT]
    return {
        "notification_unread_count": all_actionable.filter(seen_at__isnull=True).count(),
        "notification_items": list(qs),
    }


def notification_target(receipt):
    document = receipt.event.document
    if document.document_type.code == "IM":
        return "documents:memo-detail", document.pk
    if document.document_type.code == INCOMING_CORRESPONDENCE_TYPE_CODE:
        return "documents:incoming-detail", document.pk
    return None, None


def mark_notification_seen(receipt):
    if receipt.seen_at is None:
        receipt.seen_at = timezone.now()
        receipt.save(update_fields=["seen_at"])


def visible_notification_receipts(user):
    return NotificationReceipt.objects.filter(
        recipient=user,
        event__document__in=visible_documents(user),
    ).select_related(
        "event__document__document_type", "workflow_task__action_type", "workflow_task__step",
    ).order_by("-event__occurred_at", "-pk")[:ALL_LIMIT]


def can_open_notification(user, receipt):
    return can_view_document(user, receipt.event.document)
