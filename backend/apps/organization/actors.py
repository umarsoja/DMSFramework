from django.core.exceptions import ValidationError

from .models import Department, EmployeeProfile
from .services import current_assignment


def _eligible(profile):
    if not (
        profile
        and profile.account_status == EmployeeProfile.AccountStatus.ACTIVE
        and profile.employment_status == EmployeeProfile.EmploymentStatus.EMPLOYED
        and profile.user.is_active
    ):
        return False
    assignment = profile.current_assignment
    return bool(assignment and assignment.department.is_active and assignment.position.is_active)


def resolve_workflow_actor(*, step, document, selected_user=None):
    """Resolve a configured workflow actor while keeping position and access separate."""
    assignment = document.originating_assignment
    actor_type = step.actor_type
    if actor_type == step.ActorType.SELECTED_USER:
        user = selected_user
        if user is None:
            raise ValidationError("A reviewer must be selected for this workflow step.")
        try:
            profile = user.employee_profile
        except EmployeeProfile.DoesNotExist:
            profile = None
        if profile and not _eligible(profile):
            raise ValidationError("The selected employee does not have an active DMS account.")
        if not profile and (not user.is_active or not user.is_staff):
            raise ValidationError("Choose an active staff reviewer.")
    elif actor_type == step.ActorType.DOCUMENT_CREATOR:
        user = document.created_by
        try:
            profile = user.employee_profile
        except EmployeeProfile.DoesNotExist:
            profile = None
        if profile and not _eligible(profile):
            raise ValidationError("The document creator's DMS account is inactive.")
    elif actor_type == step.ActorType.CREATOR_SUPERVISOR:
        profile = assignment.supervisor if assignment else None
        if not _eligible(profile):
            raise ValidationError("The document creator has no active supervisor to resolve for this step.")
        user = profile.user
    elif actor_type == step.ActorType.DEPARTMENT_HEAD:
        department = Department.objects.select_related("head__user").filter(pk=assignment.department_id).first() if assignment else None
        profile = department.head if department else None
        if not _eligible(profile):
            raise ValidationError("No active department head is configured for the document's originating department.")
        user = profile.user
    else:
        raise ValidationError("Unsupported workflow actor type.")
    if user.pk == document.created_by_id and actor_type != step.ActorType.DOCUMENT_CREATOR:
        raise ValidationError("The workflow reviewer must be different from the document creator.")
    resolved_assignment = current_assignment(user)
    return user, resolved_assignment
