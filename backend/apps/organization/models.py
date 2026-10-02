from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Department(models.Model):
    code = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    head = models.ForeignKey("EmployeeProfile", null=True, blank=True, on_delete=models.PROTECT, related_name="headed_departments")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        permissions = [("manage_departments", "Can manage APGC departments")]

    def clean(self):
        parent = self.parent
        while parent:
            if self.pk and parent.pk == self.pk:
                raise ValidationError({"parent": "A department cannot be nested below itself."})
            parent = parent.parent
        if self.parent_id and self.pk == self.parent_id:
            raise ValidationError({"parent": "A department cannot be its own parent."})
        if self.head_id:
            if (self.head.account_status != EmployeeProfile.AccountStatus.ACTIVE
                    or self.head.employment_status != EmployeeProfile.EmploymentStatus.EMPLOYED):
                raise ValidationError({"head": "Choose an active employee as department head."})
            assignment = self.head.current_assignment
            if not assignment or not self.pk or assignment.department_id != self.pk:
                raise ValidationError({"head": "The department head must have a current assignment in this department."})

    def __str__(self):
        return f"{self.code} — {self.name}"


class Position(models.Model):
    code = models.CharField(max_length=48, unique=True)
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name="positions")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        permissions = [("manage_positions", "Can manage APGC positions")]

    def __str__(self):
        return f"{self.title} ({self.code})"


class EmployeeProfile(models.Model):
    class EmploymentStatus(models.TextChoices):
        EMPLOYED = "EMPLOYED", "Employed"
        ON_LEAVE = "ON_LEAVE", "On leave"
        SEPARATED = "SEPARATED", "Separated"

    class AccountStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        INACTIVE = "INACTIVE", "Inactive"
        SUSPENDED = "SUSPENDED", "Suspended"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="employee_profile")
    staff_id = models.CharField(max_length=64, unique=True)
    employment_status = models.CharField(max_length=16, choices=EmploymentStatus.choices, default=EmploymentStatus.EMPLOYED)
    account_status = models.CharField(max_length=16, choices=AccountStatus.choices, default=AccountStatus.INACTIVE)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name", "staff_id"]
        constraints = [models.CheckConstraint(
            condition=~Q(employment_status="SEPARATED") | ~Q(account_status="ACTIVE"),
            name="separated_employee_account_not_active",
        )]
        permissions = [
            ("manage_employees", "Can onboard and manage APGC employees"),
            ("manage_roles", "Can manage DMS role and group assignments"),
        ]

    @property
    def current_assignment(self):
        if hasattr(self, "_current_assignment_cache"):
            return self._current_assignment_cache
        return self.assignments.filter(end_date__isnull=True, start_date__lte=timezone.localdate()).select_related(
            "department", "position", "supervisor__user"
        ).first()

    def clean(self):
        if self.employment_status == self.EmploymentStatus.SEPARATED and self.account_status == self.AccountStatus.ACTIVE:
            raise ValidationError({"account_status": "A separated employee cannot have an active DMS account."})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        target_active = self.account_status == self.AccountStatus.ACTIVE
        if self.user.is_active != target_active:
            type(self.user).objects.filter(pk=self.user_id).update(is_active=target_active)

    def __str__(self):
        return f"{self.staff_id} — {self.user.get_full_name() or self.user.get_username()}"


class EmployeeAssignment(models.Model):
    employee = models.ForeignKey(EmployeeProfile, on_delete=models.PROTECT, related_name="assignments")
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="assignments")
    position = models.ForeignKey(Position, on_delete=models.PROTECT, related_name="assignments")
    supervisor = models.ForeignKey(EmployeeProfile, null=True, blank=True, on_delete=models.PROTECT, related_name="supervisee_assignments")
    department_code_snapshot = models.CharField(max_length=32, blank=True, editable=False)
    department_name_snapshot = models.CharField(max_length=160, blank=True, editable=False)
    position_code_snapshot = models.CharField(max_length=48, blank=True, editable=False)
    position_title_snapshot = models.CharField(max_length=160, blank=True, editable=False)
    supervisor_staff_id_snapshot = models.CharField(max_length=64, blank=True, editable=False)
    supervisor_name_snapshot = models.CharField(max_length=300, blank=True, editable=False)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["employee"], condition=Q(end_date__isnull=True), name="uniq_employee_open_assignment"),
            models.CheckConstraint(condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")), name="assignment_end_after_start"),
            models.CheckConstraint(condition=Q(supervisor__isnull=True) | ~Q(supervisor=models.F("employee")), name="assignment_no_self_supervision"),
        ]

    def clean(self):
        errors = {}
        if self.supervisor_id and self.supervisor_id == self.employee_id:
            errors["supervisor"] = "An employee cannot supervise themselves."
        if self.position_id and self.department_id and self.position.department_id not in (None, self.department_id):
            errors["position"] = "The selected position belongs to another department."
        if self.start_date and (self.end_date is None or self.end_date >= self.start_date) and self.employee_id:
            others = EmployeeAssignment.objects.filter(employee_id=self.employee_id)
            if self.pk:
                others = others.exclude(pk=self.pk)
            overlaps = others.filter(Q(end_date__isnull=True) | Q(end_date__gte=self.start_date))
            if self.end_date:
                overlaps = overlaps.filter(start_date__lte=self.end_date)
            if overlaps.exists():
                errors["start_date"] = "This assignment overlaps an existing assignment."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            frozen = ("employee_id", "department_id", "position_id", "supervisor_id", "start_date")
            if any(getattr(original, field) != getattr(self, field) for field in frozen):
                raise ValidationError("Assignment history is immutable; close it and create a new assignment.")
            if original.end_date is not None and original.end_date != self.end_date:
                raise ValidationError("A closed assignment cannot be reopened or changed.")
        else:
            self.department_code_snapshot = self.department.code
            self.department_name_snapshot = self.department.name
            self.position_code_snapshot = self.position.code
            self.position_title_snapshot = self.position.title
            self.supervisor_staff_id_snapshot = self.supervisor.staff_id if self.supervisor_id else ""
            self.supervisor_name_snapshot = (self.supervisor.user.get_full_name() or self.supervisor.user.get_username()) if self.supervisor_id else ""
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Employee assignments are historical records and cannot be deleted.")

    @property
    def is_current(self):
        return self.end_date is None and self.start_date <= timezone.localdate()

    def __str__(self):
        return f"{self.employee} · {self.department} · {self.position}"


class OrganizationAuditEvent(models.Model):
    class Action(models.TextChoices):
        USER_CREATED = "USER_CREATED", "User onboarded"
        PROFILE_CREATED = "PROFILE_CREATED", "Employee profile created"
        PROFILE_UPDATED = "PROFILE_UPDATED", "Employee profile updated"
        DEPARTMENT_CREATED = "DEPARTMENT_CREATED", "Department created"
        DEPARTMENT_UPDATED = "DEPARTMENT_UPDATED", "Department updated"
        POSITION_CREATED = "POSITION_CREATED", "Position created"
        POSITION_UPDATED = "POSITION_UPDATED", "Position updated"
        ASSIGNMENT_CREATED = "ASSIGNMENT_CREATED", "Assignment created"
        ASSIGNMENT_ENDED = "ASSIGNMENT_ENDED", "Assignment ended"
        EMPLOYEE_TRANSFERRED = "EMPLOYEE_TRANSFERRED", "Employee transferred"
        ACCOUNT_STATUS_CHANGED = "ACCOUNT_STATUS_CHANGED", "Account status changed"
        ROLE_CHANGED = "ROLE_CHANGED", "Role changed"
        USER_PERMISSIONS_CHANGED = "USER_PERMISSIONS_CHANGED", "User permissions changed"
        GROUP_PERMISSIONS_CHANGED = "GROUP_PERMISSIONS_CHANGED", "Group permissions changed"

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="organization_audit_events")
    actor_assignment = models.ForeignKey(EmployeeAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="organization_events_as_actor")
    action = models.CharField(max_length=32, choices=Action.choices)
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="organization_events_about")
    subject_group = models.ForeignKey("auth.Group", null=True, blank=True, on_delete=models.PROTECT, related_name="organization_audit_events")
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    position = models.ForeignKey(Position, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    assignment = models.ForeignKey(EmployeeAssignment, null=True, blank=True, on_delete=models.PROTECT, related_name="audit_events")
    context = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        ordering = ["-occurred_at", "-pk"]
        default_permissions = ("view",)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Organisation audit events are immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Organisation audit events cannot be deleted.")
