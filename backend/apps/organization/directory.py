"""Shared queries and labels for current DMS employee directory entries."""

from django.utils import timezone
from django.db.models import Prefetch

from .authentication import DMS_ROLE_GROUPS, DMS_VIEWER_GROUP
from .models import EmployeeAssignment, EmployeeProfile


def current_directory_profiles(*, include_viewers=True):
    """Return active employees with a current active organizational assignment and DMS role."""
    profiles = EmployeeProfile.objects.filter(
        account_status=EmployeeProfile.AccountStatus.ACTIVE,
        employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
        user__is_active=True,
        user__groups__name__in=DMS_ROLE_GROUPS,
        assignments__end_date__isnull=True,
        assignments__start_date__lte=timezone.localdate(),
        assignments__department__is_active=True,
        assignments__position__is_active=True,
    )
    if not include_viewers:
        profiles = profiles.exclude(user__groups__name=DMS_VIEWER_GROUP)
    current_assignments = EmployeeAssignment.objects.filter(
        end_date__isnull=True,
        start_date__lte=timezone.localdate(),
    ).select_related("department", "position", "supervisor__user")
    return profiles.select_related("user").prefetch_related(
        Prefetch("assignments", queryset=current_assignments, to_attr="_directory_current_assignments")
    ).distinct()


def current_direct_report_profiles(supervisor, *, include_viewers=False):
    """Active DMS employees whose current assignment names supervisor directly."""
    try:
        supervisor_profile = supervisor.employee_profile
    except (AttributeError, EmployeeProfile.DoesNotExist):
        return EmployeeProfile.objects.none()
    return current_directory_profiles(include_viewers=include_viewers).filter(
        assignments__supervisor=supervisor_profile,
        assignments__end_date__isnull=True,
        assignments__start_date__lte=timezone.localdate(),
    ).distinct()


def employee_directory_label(profile, assignment=None):
    """Return a human-readable employee label without exposing the Django username."""
    assignment = assignment or next(iter(getattr(profile, "_directory_current_assignments", ())), None) or profile.current_assignment
    full_name = profile.user.get_full_name().strip() or "Name not provided"
    if not assignment:
        return full_name
    department = assignment.department
    position = assignment.position
    department_label = department.code or department.name
    return f"{full_name} — {position.title} ({department_label})"


def employee_directory_search_text(profile, assignment=None):
    """Terms available to the directory selector's client-side search."""
    assignment = assignment or next(iter(getattr(profile, "_directory_current_assignments", ())), None) or profile.current_assignment
    parts = [profile.user.get_full_name()]
    if assignment:
        parts.extend((assignment.position.title, assignment.department.name, assignment.department.code))
    return " ".join(parts).casefold()
