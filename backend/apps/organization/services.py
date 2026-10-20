import hashlib
import re
import secrets
import unicodedata
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .activation_email import queue_activation_email

from .models import (AuthRecoveryThrottle, Department, EmployeeActivationToken, EmployeeAssignment, EmployeeBootstrapState,
                     EmployeeProfile, OrganizationAuditEvent, Position)


EMPLOYEE_ACTIVATION_TIMEOUT = timedelta(hours=24)
USERNAME_COLLISION_RETRIES = 1000


def _generated_username_base(first_name, last_name, *, max_length):
    parts = []
    # The onboarding form stores the complete official last-name field. Use
    # its first name component for the concise login identifier while keeping
    # the original field unchanged on the User record.
    values = (first_name, (last_name or "").split()[0] if (last_name or "").split() else "")
    for value in values:
        ascii_name = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
        normalized = re.sub(r"[^a-z0-9]+", ".", ascii_name.casefold()).strip(".")
        if normalized:
            parts.append(normalized)
    base = ".".join(parts) or "employee"
    return base[:max_length].rstrip(".") or "employee"[:max_length]


def _normalize_username(value, *, max_length):
    normalized = re.sub(r"[^a-z0-9._-]+", ".", (value or "").strip().casefold())
    normalized = re.sub(r"[.]{2,}", ".", normalized).strip(".-_")
    return normalized[:max_length].rstrip(".-_")


def _create_employee_user(*, first_name, last_name, email):
    """Create an employee user with a unique, name-derived username.

    Database uniqueness is the final authority. Each insert runs in a savepoint
    so a concurrent collision can be retried without breaking the surrounding
    atomic onboarding transaction.
    """
    User = get_user_model()
    username_field = User._meta.get_field("username")
    max_length = max(1, username_field.max_length or 150)
    base = _generated_username_base(first_name, last_name, max_length=max_length)
    for suffix in range(1, USERNAME_COLLISION_RETRIES + 1):
        suffix_text = "" if suffix == 1 else str(suffix)
        candidate = f"{base[:max_length - len(suffix_text)]}{suffix_text}"
        try:
            with transaction.atomic():
                user = User(
                    username=candidate, first_name=first_name, last_name=last_name,
                    email=email, is_active=False,
                )
                user.set_unusable_password()
                user.save(force_insert=True)
            return user
        except IntegrityError:
            if User.objects.filter(username=candidate).exists():
                continue
            raise
    raise ValidationError("A unique username could not be generated. Please retry onboarding.")


def activation_eligibility(user):
    from .authentication import DMS_ROLE_GROUPS
    try:
        profile = user.employee_profile
    except EmployeeProfile.DoesNotExist:
        return False
    assignment = profile.current_assignment
    # INACTIVE is the normal invited-but-not-yet-activated state. Some
    # employees are already enabled in DMS while their initial credential
    # setup is still pending; allow that state too, but reject status/login
    # mismatches and all suspended accounts.
    awaiting_initial_credentials = (
        profile.account_status == EmployeeProfile.AccountStatus.INACTIVE
        and not user.is_active
    ) or (
        profile.account_status == EmployeeProfile.AccountStatus.ACTIVE
        and user.is_active
    )
    return bool(
        user.email
        and awaiting_initial_credentials
        and profile.employment_status == EmployeeProfile.EmploymentStatus.EMPLOYED
        and not user.has_usable_password() and assignment
        and assignment.department.is_active and assignment.position.is_active
        and user.groups.filter(name__in=DMS_ROLE_GROUPS).exists()
    )


def password_reset_eligibility(user):
    from .authentication import DMS_ROLE_GROUPS
    try:
        profile = user.employee_profile
    except EmployeeProfile.DoesNotExist:
        return False
    assignment = profile.current_assignment
    return bool(
        user.is_active and user.has_usable_password() and user.email
        and profile.account_status == EmployeeProfile.AccountStatus.ACTIVE
        and profile.employment_status == EmployeeProfile.EmploymentStatus.EMPLOYED
        and user.groups.filter(name__in=DMS_ROLE_GROUPS).exists()
        and assignment and assignment.department.is_active and assignment.position.is_active
    )


def allow_recovery_request(*, email, source):
    """Enforce hourly persistent account and source buckets using opaque hashes."""
    import hashlib
    import hmac
    from django.conf import settings

    now = timezone.now()
    keys = [f"account:{email.strip().casefold()}", f"source:{source or 'unknown'}"]
    hashes = [hmac.new(settings.SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest() for key in keys]
    limits = (3, 12)
    allowed = True
    with transaction.atomic():
        # Periodic bounded-retention cleanup prevents arbitrary email/IP values accumulating forever.
        AuthRecoveryThrottle.objects.filter(updated_at__lt=now - timedelta(hours=48)).delete()
        for digest, limit in zip(hashes, limits):
            bucket, _ = AuthRecoveryThrottle.objects.get_or_create(
                key_hash=digest, defaults={"window_started_at": now, "request_count": 0},
            )
            bucket = AuthRecoveryThrottle.objects.select_for_update().get(pk=bucket.pk)
            if bucket.window_started_at <= now - timedelta(hours=1):
                bucket.window_started_at, bucket.request_count = now, 0
            bucket.request_count += 1
            bucket.save(update_fields=["window_started_at", "request_count", "updated_at"])
            allowed = allowed and bucket.request_count <= limit
    return allowed


@transaction.atomic
def complete_password_reset(*, user_id, token, password):
    user = get_user_model().objects.select_for_update().get(pk=user_id)
    if not valid_password_reset_token(user, token):
        raise ValidationError("This password reset link is invalid or has expired.")
    user.set_password(password)
    user.save(update_fields=["password"])
    profile = user.employee_profile
    _event(user, OrganizationAuditEvent.Action.PASSWORD_RESET_COMPLETED, profile=profile,
           context={"source": "self_service_recovery"})
    return user


def valid_password_reset_token(user, token):
    from apps.documents.models import EmailOutbox
    from .activation_email import _password_reset_token
    if not password_reset_eligibility(user):
        return False
    latest = EmailOutbox.objects.filter(
        recipient=user, message_type=EmailOutbox.MessageType.PASSWORD_RESET,
    ).order_by("-queued_at", "-pk").first()
    from django.contrib.auth.tokens import default_token_generator
    return bool(latest and default_token_generator.check_token(user, token)
                and _password_reset_token(user, latest.queued_at) == token)


@transaction.atomic
def issue_employee_activation(*, user):
    user = get_user_model().objects.select_for_update().get(pk=user.pk)
    raw_token = secrets.token_urlsafe(32)
    EmployeeActivationToken.objects.update_or_create(
        user=user,
        defaults={
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "created_at": timezone.now(),
            "expires_at": timezone.now() + EMPLOYEE_ACTIVATION_TIMEOUT,
            "used_at": None,
        },
    )
    return raw_token


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
                      supervisor=None, start_date=None, user=None, username=None, first_name="", last_name="", email="", roles=()):
    User = get_user_model()
    if roles and not actor.has_perm("organization.manage_roles"):
        raise PermissionDenied("You are not authorized to assign DMS roles.")
    created_user = user is None
    if created_user:
        username_field = User._meta.get_field("username")
        max_length = max(1, username_field.max_length or 150)
        requested = _normalize_username(username, max_length=max_length) if username else ""
        if requested:
            if User.objects.filter(username__iexact=requested).exists():
                raise ValidationError({"username": "This username is already in use."})
            user = User(username=requested, first_name=first_name, last_name=last_name, email=email, is_active=False)
            user.set_unusable_password()
            user.save(force_insert=True)
        else:
            user = _create_employee_user(first_name=first_name, last_name=last_name, email=email)
    else:
        if account_status == EmployeeProfile.AccountStatus.ACTIVE and not user.is_active:
            raise ValidationError({"account_status": "A disabled existing account cannot be enabled by linking it to an employee profile."})
        if not user.email and email:
            # The submitted official address can fill a missing value, but never
            # overrides an address already authoritative on the linked User.
            user.email = email
            user.save(update_fields=["email"])
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
    user.refresh_from_db(fields=["is_active"])
    needs_activation = (created_user or not user.has_usable_password()) and activation_eligibility(user)
    if needs_activation:
        profile._activation_token = issue_employee_activation(user=user)
        queue_activation_email(user=user, raw_token=profile._activation_token)
    return profile


@transaction.atomic
def onboard_first_dms_administrator(*, actor, first_name, last_name, email,
                                     staff_id, department, position, supervisor=None, start_date=None):
    state, _ = EmployeeBootstrapState.objects.get_or_create(pk=1)
    state = EmployeeBootstrapState.objects.select_for_update().get(pk=1)
    if (state.completed_at is not None
            or EmployeeProfile.objects.filter(user__groups__name="DMS Administrator").exists()):
        raise PermissionDenied("Initial DMS Administrator onboarding has already been completed.")
    admin_group = Group.objects.get(name="DMS Administrator")
    profile = onboard_employee(
        actor=actor,
        first_name=first_name, last_name=last_name, email=email,
        staff_id=staff_id,
        employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
        account_status=EmployeeProfile.AccountStatus.INACTIVE,
        department=department, position=position, supervisor=supervisor,
        start_date=start_date, roles=[admin_group],
    )
    state.completed_at = timezone.now()
    state.save(update_fields=["completed_at"])
    return profile


@transaction.atomic
def complete_employee_activation(*, user_id, raw_token, password):
    import hashlib
    import hmac

    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.select_for_update().get(pk=user_id)
    except User.DoesNotExist:
        raise ValidationError("This activation link is invalid or has expired.")
    try:
        activation = EmployeeActivationToken.objects.select_for_update().get(user_id=user_id)
    except EmployeeActivationToken.DoesNotExist:
        raise ValidationError("This activation link is invalid or has expired.")
    now = timezone.now()
    supplied_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    if (activation.used_at or activation.expires_at <= now
            or not hmac.compare_digest(activation.token_hash, supplied_hash)):
        raise ValidationError("This activation link is invalid or has expired.")
    try:
        profile = EmployeeProfile.objects.select_for_update().get(user=user)
    except EmployeeProfile.DoesNotExist:
        raise ValidationError("This activation link is invalid or has expired.")
    if not activation_eligibility(user):
        raise ValidationError("This activation link is invalid or has expired.")

    user.set_password(password)
    user.save(update_fields=["password"])
    activation.used_at = now
    activation.save(update_fields=["used_at"])
    _event(user, OrganizationAuditEvent.Action.CREDENTIALS_ESTABLISHED, profile=profile,
           context={"activation_token_id": activation.pk})

    if profile.account_status == EmployeeProfile.AccountStatus.INACTIVE:
        profile.account_status = EmployeeProfile.AccountStatus.ACTIVE
        profile.save(update_fields=["account_status", "updated_at"])
        _event(user, OrganizationAuditEvent.Action.ACCOUNT_STATUS_CHANGED, profile=profile,
               context={"from": EmployeeProfile.AccountStatus.INACTIVE,
                        "to": EmployeeProfile.AccountStatus.ACTIVE,
                        "source": "employee_activation"})
    return user


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
