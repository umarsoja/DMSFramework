from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Department, EmployeeAssignment, EmployeeProfile, OrganizationAuditEvent, Position


def current_assignment(user):
    try:
        return user.employee_profile.current_assignment
    except EmployeeProfile.DoesNotExist:
        return None


def _event(actor, action, *, profile=None, department=None, position=None, assignment=None, context=None):
    return OrganizationAuditEvent.objects.create(
        actor=actor,
        actor_assignment=current_assignment(actor),
        action=action,
        subject_user=profile.user if profile else None,
        department=department,
        position=position,
        assignment=assignment,
        context=context or {},
    )


def _validate_assignment_target(*, department, position, supervisor, employee):
    if not department.is_active:
        raise ValidationError({"department": "An inactive department cannot receive a new assignment."})
    if not position.is_active:
        raise ValidationError({"position": "An inactive position cannot receive a new assignment."})
    if position.department_id and position.department_id != department.pk:
        raise ValidationError({"position": "The position belongs to another department."})
    if supervisor:
        if supervisor.pk == employee.pk:
            raise ValidationError({"supervisor": "An employee cannot supervise themselves."})
        supervisor_assignment = supervisor.current_assignment
        if (supervisor.account_status != EmployeeProfile.AccountStatus.ACTIVE
                or supervisor.employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED
                or not supervisor_assignment
                or not supervisor_assignment.department.is_active
                or not supervisor_assignment.position.is_active):
            raise ValidationError({"supervisor": "Choose an active employee as supervisor."})


@transaction.atomic
def create_assignment(*, employee, department, position, supervisor=None, start_date=None, actor):
    start_date = start_date or timezone.localdate()
    employee = EmployeeProfile.objects.select_for_update().get(pk=employee.pk)
    _validate_assignment_target(department=department, position=position, supervisor=supervisor, employee=employee)
    previous = employee.assignments.select_for_update().filter(end_date__isnull=True).first()
    if previous:
        if start_date <= previous.start_date:
            raise ValidationError({"start_date": "A replacement assignment must start after the current assignment."})
        previous.end_date = start_date - timedelta(days=1)
        previous.save(update_fields=["end_date", "updated_at"])
        _event(actor, OrganizationAuditEvent.Action.ASSIGNMENT_ENDED, profile=employee,
               assignment=previous, context={"end_date": previous.end_date.isoformat()})
    conflicts = employee.assignments.filter(Q(end_date__isnull=True) | Q(end_date__gte=start_date))
    if conflicts.exists():
        raise ValidationError({"start_date": "The new assignment overlaps historical assignment dates."})
    assignment = EmployeeAssignment.objects.create(
        employee=employee,
        department=department,
        position=position,
        supervisor=supervisor,
        start_date=start_date,
    )
    action = OrganizationAuditEvent.Action.EMPLOYEE_TRANSFERRED if previous else OrganizationAuditEvent.Action.ASSIGNMENT_CREATED
    _event(actor, action, profile=employee, department=department, position=position,
           assignment=assignment, context={"previous_assignment_id": previous.pk if previous else None})
    return assignment


@transaction.atomic
def onboard_employee(*, actor, staff_id, employment_status, account_status, department, position,
                      supervisor=None, start_date=None, user=None, username="", first_name="", last_name="", email="", roles=()):
    User = get_user_model()
    if roles and not actor.has_perm("organization.manage_roles"):
        raise PermissionDenied("You are not authorized to assign DMS roles.")
    created_user = user is None
    if created_user:
        if User.objects.filter(username=username).exists():
            raise ValidationError({"username": "That username is already in use."})
        user = User(username=username, first_name=first_name, last_name=last_name, email=email, is_active=False)
        user.set_unusable_password()
        user.save()
    if EmployeeProfile.objects.filter(user=user).exists():
        raise ValidationError("This account already has an employee profile.")
    profile = EmployeeProfile(
        user=user, staff_id=staff_id,
        employment_status=employment_status, account_status=account_status,
    )
    profile.full_clean()
    profile.save()
    _event(actor, OrganizationAuditEvent.Action.USER_CREATED if created_user else OrganizationAuditEvent.Action.PROFILE_CREATED,
           profile=profile, context={"account_status": account_status, "employment_status": employment_status})
    create_assignment(employee=profile, department=department, position=position,
                      supervisor=supervisor, start_date=start_date, actor=actor)
    if roles:
        user.groups.add(*roles)
        _event(actor, OrganizationAuditEvent.Action.ROLE_CHANGED, profile=profile,
               context={"added": [role.name for role in roles], "removed": []})
    return profile


@transaction.atomic
def update_employee(*, actor, profile, staff_id, employment_status, account_status,
                    department, position, supervisor=None, start_date=None, roles=None):
    profile = EmployeeProfile.objects.select_for_update().select_related("user").get(pk=profile.pk)
    if profile.user_id == actor.pk:
        raise PermissionDenied("You cannot change your own employee or organisational record.")
    old_status = profile.account_status
    old_employment = profile.employment_status
    old_assignment = profile.current_assignment
    profile.staff_id = staff_id
    profile.employment_status = employment_status
    profile.account_status = account_status
    profile.full_clean()
    profile.save(update_fields=["staff_id", "employment_status", "account_status", "updated_at"])
    _event(actor, OrganizationAuditEvent.Action.PROFILE_UPDATED, profile=profile,
           context={"employment_status": employment_status, "previous_employment_status": old_employment})
    if old_status != account_status:
        _event(actor, OrganizationAuditEvent.Action.ACCOUNT_STATUS_CHANGED, profile=profile,
               context={"from": old_status, "to": account_status})
    if employment_status == EmployeeProfile.EmploymentStatus.SEPARATED:
        if old_assignment:
            final_date = start_date or timezone.localdate()
            if final_date < old_assignment.start_date:
                raise ValidationError({"start_date": "The separation date cannot precede the current assignment."})
            old_assignment.end_date = final_date
            old_assignment.save(update_fields=["end_date", "updated_at"])
            _event(actor, OrganizationAuditEvent.Action.ASSIGNMENT_ENDED, profile=profile,
                   assignment=old_assignment, context={"end_date": final_date.isoformat(), "reason": "employment separated"})
    if account_status != EmployeeProfile.AccountStatus.ACTIVE or employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED:
        for department in Department.objects.filter(head=profile):
            department.head = None
            department.save(update_fields=["head", "updated_at"])
            _event(actor, OrganizationAuditEvent.Action.DEPARTMENT_UPDATED, department=department,
                   context={"head_removed": profile.staff_id, "reason": "employee unavailable"})

    if employment_status == EmployeeProfile.EmploymentStatus.SEPARATED:
        return profile

    assignment_changed = not old_assignment or any([
        old_assignment.department_id != department.pk,
        old_assignment.position_id != position.pk,
        old_assignment.supervisor_id != (supervisor.pk if supervisor else None),
    ])
    if assignment_changed:
        create_assignment(employee=profile, department=department, position=position,
                          supervisor=supervisor, start_date=start_date, actor=actor)
    if roles is not None:
        old_roles = set(profile.user.groups.filter(name__startswith="DMS ").values_list("name", flat=True))
        new_roles = {role.name for role in roles}
        if old_roles != new_roles:
            if not actor.has_perm("organization.manage_roles"):
                raise PermissionDenied("You are not authorized to change DMS roles.")
            profile.user.groups.remove(*profile.user.groups.filter(name__startswith="DMS "))
            profile.user.groups.add(*roles)
            _event(actor, OrganizationAuditEvent.Action.ROLE_CHANGED, profile=profile,
                   context={"added": sorted(new_roles - old_roles), "removed": sorted(old_roles - new_roles)})
    return profile


@transaction.atomic
def save_department(*, actor, department, values):
    created = department.pk is None
    previous = {
        "code": department.code if not created else None,
        "name": department.name if not created else None,
        "parent_id": department.parent_id if not created else None,
        "head_id": department.head_id if not created else None,
        "is_active": department.is_active if not created else None,
    }
    for key, value in values.items():
        setattr(department, key, value)
    department.full_clean()
    department.save()
    _event(actor, OrganizationAuditEvent.Action.DEPARTMENT_CREATED if created else OrganizationAuditEvent.Action.DEPARTMENT_UPDATED,
           department=department, context={
               "previous": previous,
               "current": {"code": department.code, "name": department.name,
                           "parent_id": department.parent_id, "head_id": department.head_id,
                           "is_active": department.is_active},
           })
    return department


@transaction.atomic
def save_position(*, actor, position, values):
    created = position.pk is None
    previous = {
        "code": position.code if not created else None,
        "title": position.title if not created else None,
        "department_id": position.department_id if not created else None,
        "is_active": position.is_active if not created else None,
    }
    for key, value in values.items():
        setattr(position, key, value)
    position.full_clean()
    position.save()
    _event(actor, OrganizationAuditEvent.Action.POSITION_CREATED if created else OrganizationAuditEvent.Action.POSITION_UPDATED,
           position=position, department=position.department, context={
               "previous": previous,
               "current": {"code": position.code, "title": position.title,
                           "department_id": position.department_id, "is_active": position.is_active},
           })
    return position
