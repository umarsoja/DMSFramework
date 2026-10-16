"""Visibility-safe query composition for the authenticated DMS dashboard."""

from datetime import timedelta

from django.db.models import Q, Subquery
from django.utils import timezone

from apps.documents.access import visible_documents
from apps.documents.handlers import INCOMING_CORRESPONDENCE_TYPE_CODE, INTERNAL_MEMO_TYPE_CODE
from apps.documents.models import AuditEvent, Document, WorkflowTask
from apps.organization.models import EmployeeAssignment, EmployeeProfile


RECENT_WINDOW = timedelta(days=7)
LIST_LIMIT = 6


def dashboard_data(user):
    """Return dashboard counts and bounded records from existing DMS sources."""
    now = timezone.now()
    recent_since = now - RECENT_WINDOW
    visible = visible_documents(user)
    task_base = WorkflowTask.objects.filter(
        status=WorkflowTask.Status.PENDING,
        instance__document__in=visible,
    ).select_related(
        "instance__document__document_type", "action_type", "step", "assigned_to",
    )
    mine = task_base.filter(assigned_to=user)
    pending_reviews = mine.filter(step__is_review=True).filter(
        Q(action_type__isnull=True) | ~Q(action_type__code="approve"),
    )
    pending_approvals = mine.filter(action_type__code="approve")
    pending_actions = mine.filter(step__is_review=False).filter(
        Q(action_type__isnull=True) | ~Q(action_type__code__in=("review", "approve")),
    )
    overdue = mine.filter(due_at__lt=now)

    try:
        employee = user.employee_profile
    except EmployeeProfile.DoesNotExist:
        employee = None
    direct_report_user_ids = EmployeeAssignment.objects.none().values("employee__user_id")
    if employee:
        direct_report_user_ids = EmployeeAssignment.objects.filter(
            supervisor=employee,
            end_date__isnull=True,
            start_date__lte=timezone.localdate(),
        ).values("employee__user_id")
    team_pending = task_base.filter(assigned_to_id__in=Subquery(direct_report_user_ids))
    team_overdue = team_pending.filter(due_at__lt=now)

    incoming = visible.filter(
        document_type__code=INCOMING_CORRESPONDENCE_TYPE_CODE,
        status=Document.Status.REGISTERED,
    ).distinct()
    awaiting_management = incoming.exclude(management_directives__isnull=False)
    recent_correspondence = AuditEvent.objects.filter(
        document__in=visible,
        action=AuditEvent.Action.INCOMING_REGISTERED,
        occurred_at__gte=recent_since,
    ).select_related("document", "actor").order_by("-occurred_at", "-pk")

    pending_memos = visible.filter(
        document_type__code=INTERNAL_MEMO_TYPE_CODE,
        status__in=(Document.Status.DRAFT, Document.Status.UNDER_REVIEW, Document.Status.RETURNED),
    ).select_related("created_by", "document_type").order_by("-modified_at", "-pk")
    recent_activity = AuditEvent.objects.filter(
        document__in=visible,
        occurred_at__gte=recent_since,
    ).exclude(action__in=(AuditEvent.Action.VIEWED, AuditEvent.Action.PDF_VIEWED,
                         AuditEvent.Action.ATTACHMENT_VIEWED)).select_related(
        "document__document_type", "actor",
    ).order_by("-occurred_at", "-pk")

    return {
        "pending_actions": pending_actions[:LIST_LIMIT],
        "pending_actions_count": pending_actions.count(),
        "pending_reviews": pending_reviews[:LIST_LIMIT],
        "pending_reviews_count": pending_reviews.count(),
        "pending_approvals": pending_approvals[:LIST_LIMIT],
        "pending_approvals_count": pending_approvals.count(),
        "overdue_tasks": overdue[:LIST_LIMIT],
        "overdue_count": overdue.count(),
        "awaiting_management": awaiting_management.select_related(
            "document_type", "created_by", "issuing_office",
        ).order_by("-modified_at", "-pk")[:LIST_LIMIT],
        "awaiting_management_count": awaiting_management.count(),
        "team_pending_tasks": team_pending[:LIST_LIMIT],
        "team_pending_count": team_pending.count(),
        "team_overdue_count": team_overdue.count(),
        "has_direct_reports": direct_report_user_ids.exists(),
        "recent_correspondence": recent_correspondence[:LIST_LIMIT],
        "recent_correspondence_count": recent_correspondence.count(),
        "pending_memos": pending_memos[:LIST_LIMIT],
        "pending_memos_count": pending_memos.count(),
        "recent_activity": recent_activity[:LIST_LIMIT],
        "recent_activity_count": recent_activity.count(),
    }
