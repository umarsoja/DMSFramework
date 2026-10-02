from django.db.models import Q

from .models import Document, WorkflowTask


def visible_documents(user):
    if not user.is_authenticated:
        return Document.objects.none()
    return Document.objects.filter(
        Q(created_by=user)
        | Q(access_grants__user=user, access_grants__can_view=True)
        | Q(workflow__tasks__assigned_to=user)
    ).distinct()


def can_view_document(user, document):
    if not user.is_authenticated:
        return False
    return (
        document.created_by_id == user.pk
        or document.access_grants.filter(user=user, can_view=True).exists()
        or WorkflowTask.objects.filter(instance__document=document, assigned_to=user).exists()
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
