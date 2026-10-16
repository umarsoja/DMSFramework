from apps.organization.authentication import has_dms_access
from .notifications import notification_menu_data


def dms_notifications(request):
    user = request.user
    if not user.is_authenticated or not has_dms_access(user):
        return {"notification_unread_count": 0, "notification_items": []}
    return notification_menu_data(user)
