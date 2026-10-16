from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.organization.authentication import dms_access_required
from .models import NotificationReceipt
from .notifications import (
    can_open_notification,
    mark_notification_seen,
    notification_target,
    visible_notification_receipts,
)


@login_required
@dms_access_required
def notification_list(request):
    return render(request, "notifications/list.html", {
        "notification_items": visible_notification_receipts(request.user),
    })


@login_required
@dms_access_required
@require_POST
def notification_open(request, pk):
    receipt = get_object_or_404(
        NotificationReceipt.objects.select_related("event__document__document_type"),
        pk=pk,
        recipient=request.user,
    )
    if not can_open_notification(request.user, receipt):
        raise Http404
    target_name, target_pk = notification_target(receipt)
    if target_name is None:
        raise Http404
    mark_notification_seen(receipt)
    return redirect(reverse(target_name, kwargs={"pk": target_pk}))
