from django.db import connection
from django.db.models import F, OuterRef, Q, Subquery

from .models import Document, DocumentVersion, WorkflowTask


def _authoritative_recipient_versions():
    """Versions whose recipients define the current document audience.

    Editable and in-flight records use Document.current_version. Once approved,
    finalized, or archived, the approved workflow task's immutable submission
    is authoritative, matching the version used for official PDF output.
    """
    editable_lifecycle = (
        Document.Status.DRAFT,
        Document.Status.UNDER_REVIEW,
        Document.Status.RETURNED,
    )
    approved_lifecycle = (
        Document.Status.APPROVED,
        Document.Status.FINALIZED,
        Document.Status.ARCHIVED,
    )
    latest_approved_cycle = WorkflowTask.objects.filter(
        instance_id=OuterRef("instance_id"),
        status=WorkflowTask.Status.APPROVED,
    ).order_by("-cycle").values("cycle")[:1]
    latest_approved_tasks = WorkflowTask.objects.filter(
        status=WorkflowTask.Status.APPROVED,
        submitted_version__isnull=False,
        instance__document__status__in=approved_lifecycle,
    ).annotate(_latest_cycle=Subquery(latest_approved_cycle)).filter(
        cycle=F("_latest_cycle"),
    )
    return DocumentVersion.objects.filter(
        Q(
            document__status__in=editable_lifecycle,
            number=F("document__current_version_number"),
        )
        | Q(pk__in=Subquery(latest_approved_tasks.values("submitted_version_id"))),
        document__document_type__code="IM",
        document__classification__in=(
            Document.Classification.INTERNAL,
            Document.Classification.CONFIDENTIAL,
        ),
        content__classification__in=(
            Document.Classification.INTERNAL,
            Document.Classification.CONFIDENTIAL,
        ),
    )


def _recipient_document_ids(user):
    """Return documents addressed to this employee in their authoritative version."""
    if not user.is_authenticated:
        return Document.objects.none().values_list("pk", flat=True)
    from apps.organization.models import EmployeeProfile

    try:
        employee_id = user.employee_profile.pk
    except EmployeeProfile.DoesNotExist:
        return Document.objects.none().values_list("pk", flat=True)

    versions = _authoritative_recipient_versions()
    if connection.features.supports_json_field_contains:
        recipient_match = Q()
        for field in ("to_employee_ids", "through_employee_ids", "cc_employee_ids"):
            recipient_match |= Q(content__contains={field: [employee_id]})
        return versions.filter(recipient_match).values_list("document_id", flat=True)

    # SQLite's JSONField backend lacks array containment. Match the same stored
    # integer identities exactly for testing and local development.
    matching_ids = []
    for document_id, content in versions.values_list("document_id", "content"):
        if any(
            employee_id in content.get(field, ())
            for field in ("to_employee_ids", "through_employee_ids", "cc_employee_ids")
        ):
            matching_ids.append(document_id)
    return versions.filter(document_id__in=matching_ids).values_list("document_id", flat=True)


def visible_documents(user):
    if not user.is_authenticated:
        return Document.objects.none()
    return Document.objects.filter(
        Q(created_by=user)
        | Q(access_grants__user=user, access_grants__can_view=True)
        | Q(workflow__tasks__assigned_to=user)
        | Q(pk__in=Subquery(_recipient_document_ids(user)))
    ).distinct()


def can_view_document(user, document):
    if not user.is_authenticated:
        return False
    return (
        document.created_by_id == user.pk
        or document.access_grants.filter(user=user, can_view=True).exists()
        or WorkflowTask.objects.filter(instance__document=document, assigned_to=user).exists()
        or document.pk in _recipient_document_ids(user)
    )


def can_download_document(user, document):
    if not user.is_authenticated:
        return False
    return document.created_by_id == user.pk or document.access_grants.filter(
        user=user, can_view=True, can_download=True
    ).exists()


def can_edit_document(user, document):
    return bool(
        user.is_authenticated
        and document.created_by_id == user.pk
        and document.status in (Document.Status.DRAFT, Document.Status.RETURNED)
    )


def pending_review_task(user, document):
    if not user.is_authenticated:
        return None
    return WorkflowTask.objects.filter(instance__document=document,
        assigned_to=user,
        status="PENDING",
    ).select_related("step").first()
