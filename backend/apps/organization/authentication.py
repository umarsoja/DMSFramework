from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext_lazy as _

from .models import EmployeeProfile


DMS_ROLE_GROUPS = (
    "DMS Administrator",
    "DMS Department Manager/HOD",
    "DMS Document Officer",
    "DMS Records/Registry Officer",
    "DMS Standard User",
    "DMS Viewer",
    "DMS Workflow Administrator",
)
DMS_VIEWER_GROUP = "DMS Viewer"


def has_dms_access(user):
    """Return whether an account is currently eligible to enter the DMS app."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    try:
        profile = user.employee_profile
    except EmployeeProfile.DoesNotExist:
        return False
    if (
        profile.account_status != EmployeeProfile.AccountStatus.ACTIVE
        or profile.employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED
    ):
        return False
    assignment = profile.current_assignment
    if not assignment or not assignment.department.is_active or not assignment.position.is_active:
        return False
    return user.groups.filter(name__in=DMS_ROLE_GROUPS).exists()


def is_dms_viewer(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and user.groups.filter(name=DMS_VIEWER_GROUP).exists()
    )


class DMSAuthenticationForm(AuthenticationForm):
    """Authenticate an active employee authorized for the APGC DMS."""

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)
        if not has_dms_access(user):
            raise ValidationError(
                _("This account is not currently permitted to access APGC DMS."),
                code="dms_access_denied",
            )


def dms_access_required(view_func):
    """Require an authenticated, currently eligible member of an approved DMS role."""

    @login_required
    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if not has_dms_access(request.user):
            raise PermissionDenied("An active APGC employee account and DMS role are required.")
        return view_func(request, *args, **kwargs)

    return wrapped


def dms_write_required(view_func):
    """Apply the DMS access boundary and keep DMS Viewer read-only."""

    @wraps(view_func)
    def wrapped(request, *args, **kwargs):
        if is_dms_viewer(request.user):
            raise PermissionDenied("DMS Viewer access is read-only.")
        return view_func(request, *args, **kwargs)

    return dms_access_required(wrapped)
