import binascii
import hashlib
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login, get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from apps.documents.models import EmailOutbox, WorkflowTask

from .authentication import (BootstrapAuthenticationForm, bootstrap_available,
                             dms_access_required, dms_write_required)
from .authentication import is_dms_viewer
from .activation_email import request_activation_email, resend_activation_email
from .directory import current_directory_profiles, current_direct_report_profiles, employee_directory_label
from .forms import DepartmentForm, EmployeeActivationForm, EmployeeEditForm, EmployeeOnboardingForm, PositionForm, RecoveryEmailForm, RecoveryPasswordForm
from .models import (Department, EmployeeActivationToken, EmployeeAssignment, EmployeeBootstrapState,
                     EmployeeProfile, OrganizationAuditEvent, Position)
from .services import (allow_recovery_request, complete_password_reset, complete_employee_activation, onboard_employee, onboard_first_dms_administrator,
                       activation_eligibility, valid_password_reset_token,
                       save_department, save_position, update_employee)
from .activation_email import queue_password_reset_email
from apps.documents.zoho_mail import authorization_url, complete_authorization


def _shell(request, title, crumbs):
    candidates = [
        ("organization.manage_employees", "Employees", "organization:employee-list", request.resolver_match.url_name.startswith("employee-")),
        ("organization.manage_departments", "Departments", "organization:department-list", request.resolver_match.url_name.startswith("department-")),
        ("organization.manage_positions", "Positions", "organization:position-list", request.resolver_match.url_name.startswith("position-")),
        ("organization.manage_employees", "Organisation", "organization:structure", request.resolver_match.url_name == "structure"),
        ("organization.manage_roles", "Roles & permissions", "organization:roles", request.resolver_match.url_name == "roles"),
    ]
    nav = [(label, name, active) for perm, label, name, active in candidates if request.user.has_perm(perm)]
    return {
        "page_title": title,
        "breadcrumbs": crumbs,
        "can_create_memo": not is_dms_viewer(request.user),
        "sidebar_items": [{"label": label, "url": reverse(name), "active": active} for label, name, active in nav],
    }


def _add_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, values in error.message_dict.items():
            for value in values:
                form.add_error(field if field in form.fields else None, value)
    else:
        form.add_error(None, " ".join(error.messages))


@dms_access_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_list(request):
    employees = EmployeeProfile.objects.select_related("user").prefetch_related("assignments__department", "assignments__position", "assignments__supervisor__user")
    query = request.GET.get("q", "").strip()
    if query:
        employees = employees.filter(Q(staff_id__icontains=query) | Q(user__first_name__icontains=query) |
                                     Q(user__last_name__icontains=query) | Q(user__email__icontains=query) |
                                     Q(assignments__department__name__icontains=query) | Q(assignments__position__title__icontains=query)).distinct()
    account_status = request.GET.get("account_status", "")
    if account_status in EmployeeProfile.AccountStatus.values:
        employees = employees.filter(account_status=account_status)
    employment_status = request.GET.get("employment_status", "")
    if employment_status in EmployeeProfile.EmploymentStatus.values:
        employees = employees.filter(employment_status=employment_status)
    page = Paginator(employees, 20).get_page(request.GET.get("page"))
    for profile in page.object_list:
        profile._current_assignment_cache = next((assignment for assignment in profile.assignments.all() if assignment.end_date is None), None)
    context = _shell(request, "Employees", [{"label": "Organisation"}, {"label": "Employees"}])
    context.update({"page": page, "query": query, "account_filter": account_status,
                    "employment_filter": employment_status,
                    "account_statuses": EmployeeProfile.AccountStatus.choices,
                    "employment_statuses": EmployeeProfile.EmploymentStatus.choices})
    return render(request, "organization/employee_list.html", context)


@dms_access_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_detail(request, pk):
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    assignments = profile.assignments.select_related("department", "position", "supervisor__user").all()
    current = next((item for item in assignments if item.end_date is None), None)
    permissions = sorted(profile.user.get_all_permissions())
    context = _shell(request, profile.user.get_full_name() or profile.staff_id, [
        {"label": "Employees", "url": reverse("organization:employee-list")}, {"label": profile.staff_id},
    ])
    context.update({"profile": profile, "current": current, "assignments": assignments,
                    "roles": profile.user.groups.order_by("name"), "permissions": permissions,
                    "audit_events": profile.user.organization_events_about.select_related("actor", "department", "position", "assignment")[:30],
                    "tasks": WorkflowTask.objects.filter(assigned_to=profile.user).select_related("instance__document", "step").order_by("-assigned_at")[:20],
                    "latest_activation_delivery": EmailOutbox.objects.filter(
                        recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
                    ).first(),
                    "can_resend_activation": activation_eligibility(profile.user)})
    return render(request, "organization/employee_detail.html", context)


@dms_write_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_create(request):
    form = EmployeeOnboardingForm(request.POST or None, actor=request.user)
    for field_name, field in form.fields.items():
        if field_name == "account_mode":
            continue
        field.widget.attrs["x-model"] = f"formValues.{field_name}"
    form.fields["existing_user"].widget.attrs["x-bind:disabled"] = "accountMode !== 'link'"
    for field_name in ("first_name", "last_name"):
        form.fields[field_name].widget.attrs["x-bind:disabled"] = "accountMode !== 'create'"
        form.fields[field_name].widget.attrs["@input"] = "syncSuggestedUsername($event)"
    form.fields["username"].widget.attrs["x-bind:disabled"] = "accountMode !== 'create'"
    form.fields["username"].widget.attrs["@input"] = "markUsernameEdited()"
    if form.is_bound:
        for field_name, field in form.fields.items():
            if field_name in form.errors:
                field.widget.attrs["aria-invalid"] = "true"
                field.widget.attrs["aria-describedby"] = f"onboarding-error-{field_name}"
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["roles"] and not request.user.has_perm("organization.manage_roles"):
            form.add_error("roles", "You do not have permission to assign DMS roles.")
        else:
            try:
                profile = onboard_employee(
                    actor=request.user,
                    user=data["existing_user"],
                    first_name=data["first_name"],
                    last_name=data["last_name"], email=data["email"],
                    username=data.get("username"),
                    staff_id=data["staff_id"], employment_status=data["employment_status"],
                    account_status=data["account_status"], department=data["department"],
                    position=data["position"], supervisor=data["supervisor"],
                    start_date=data["start_date"], roles=data["roles"],
                )
            except ValidationError as error:
                _add_errors(form, error)
            else:
                activation_token = getattr(profile, "_activation_token", None)
                delivery = EmailOutbox.objects.filter(
                    recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
                ).first()
                profile.user.refresh_from_db(fields=["is_active", "password"])
                return render(request, "organization/employee_created.html", {
                    "profile": profile, "delivery": delivery,
                    "activation_eligible": activation_eligibility(profile.user),
                    "account_linked": data["existing_user"] is not None,
                    "activation_queued": activation_token is not None,
                })
    context = _shell(request, "Onboard employee", [{"label": "Employees", "url": reverse("organization:employee-list")}, {"label": "Onboard"}])
    context["form"] = form
    identity_fields = {"account_mode", "existing_user", "first_name", "last_name", "email"}
    context["wizard_initial_step"] = (
        1 if form.is_bound and form.errors and any(name in identity_fields for name in form.errors)
        else 2 if form.is_bound and form.errors else 1
    )
    context["wizard_mode"] = form["account_mode"].value() if form["account_mode"].value() in {"create", "link"} else "create"
    return render(request, "organization/employee_form.html", context)


def _valid_activation(uidb64, raw_token):
    try:
        user_id = int(force_str(urlsafe_base64_decode(uidb64)))
        activation = EmployeeActivationToken.objects.select_related("user").get(user_id=user_id)
    except (binascii.Error, TypeError, ValueError, OverflowError, EmployeeActivationToken.DoesNotExist):
        return None
    supplied_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    if (activation.used_at or activation.expires_at <= timezone.now()
            or not constant_time_compare(activation.token_hash, supplied_hash)):
        return None
    if not activation_eligibility(activation.user):
        return None
    return activation


def bootstrap_login(request):
    if not bootstrap_available():
        return redirect("dms-login")
    form = BootstrapAuthenticationForm(request, data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        login(request, form.get_user())
        return redirect("bootstrap-onboard")
    return render(request, "accounts/bootstrap_login.html", {"form": form})


@login_required
def bootstrap_employee_create(request):
    if not request.user.is_superuser:
        raise PermissionDenied("The initial setup route is restricted to the configured bootstrap administrator.")
    if not bootstrap_available():
        raise Http404
    form = EmployeeOnboardingForm(request.POST or None, actor=request.user)
    form.fields["account_mode"].choices = (("create", "Create a new employee account"),)
    form.fields["existing_user"].queryset = get_user_model().objects.none()
    form.fields["employment_status"].required = False
    form.fields["account_status"].required = False
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            profile = onboard_first_dms_administrator(
                actor=request.user,
                first_name=data["first_name"],
                last_name=data["last_name"], email=data["email"],
                staff_id=data["staff_id"],
                department=data["department"], position=data["position"],
                supervisor=data["supervisor"], start_date=data["start_date"],
            )
        except (ValidationError, PermissionDenied) as error:
            if isinstance(error, PermissionDenied):
                raise
            _add_errors(form, error)
        else:
            delivery = EmailOutbox.objects.filter(
                recipient=profile.user, message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
            ).first()
            return render(request, "accounts/bootstrap_created.html", {
                "profile": profile, "delivery": delivery, "bootstrap_created": True,
            })
    context = {
        "form": form,
        "page_title": "Create the first DMS Administrator",
        "bootstrap": True,
    }
    return render(request, "accounts/bootstrap_onboarding.html", context)


def employee_activate(request, uidb64, token):
    def activation_response(template, context=None, *, status=200):
        response = render(request, template, context or {}, status=status)
        response["Referrer-Policy"] = "same-origin"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    activation = _valid_activation(uidb64, token)
    if not activation:
        return activation_response("accounts/activation_invalid.html", status=400)
    form = EmployeeActivationForm(activation.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            complete_employee_activation(
                user_id=activation.user_id, raw_token=token,
                password=form.cleaned_data["new_password1"],
            )
        except ValidationError:
            return activation_response("accounts/activation_invalid.html", status=400)
        activation.user.employee_profile.refresh_from_db()
        return activation_response("accounts/activation_complete.html", {
            "account_active": activation.user.employee_profile.account_status == EmployeeProfile.AccountStatus.ACTIVE,
        })
    return activation_response("accounts/activate_employee.html", {"form": form})


def _recovery_response(request, template, context=None, *, status=200):
    response = render(request, template, context or {}, status=status)
    # Keep same-origin form navigations compatible with Django's Origin check.
    # Cross-origin requests still receive no Referer, protecting token URLs.
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


def _recovery_user(uidb64):
    try:
        user_id = int(force_str(urlsafe_base64_decode(uidb64)))
        return get_user_model().objects.get(pk=user_id)
    except (binascii.Error, TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
        return None


def _employee_user_for_recovery(email):
    """Return the unique employee-backed account for an email, if unambiguous."""
    normalized_email = email.strip().casefold()
    profiles = list(
        EmployeeProfile.objects.select_related("user")
        .filter(user__email__iexact=normalized_email)[:2]
    )
    if len(profiles) != 1:
        return None
    return profiles[0].user


def _public_recovery_request(request, *, kind):
    form = RecoveryEmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].strip().casefold()
        allowed = allow_recovery_request(email=email, source=request.META.get("REMOTE_ADDR", ""))
        if allowed:
            user = _employee_user_for_recovery(email)
            if user:
                if kind == "activation":
                    request_activation_email(user=user)
                else:
                    queue_password_reset_email(user=user)
        return _recovery_response(request, "accounts/recovery_requested.html", {"kind": kind})
    return _recovery_response(request, "accounts/recovery_request.html", {"form": form, "kind": kind})


def request_activation_link(request):
    return _public_recovery_request(request, kind="activation")


def request_password_reset(request):
    return _public_recovery_request(request, kind="password")


def confirm_password_reset(request, uidb64, token):
    user = _recovery_user(uidb64)
    if not user or not valid_password_reset_token(user, token):
        return _recovery_response(request, "accounts/recovery_invalid.html", status=400)
    form = RecoveryPasswordForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        try:
            complete_password_reset(user_id=user.pk, token=token,
                                    password=form.cleaned_data["new_password1"])
        except (ValidationError, get_user_model().DoesNotExist):
            return _recovery_response(request, "accounts/recovery_invalid.html", status=400)
        return _recovery_response(request, "accounts/recovery_complete.html")
    return _recovery_response(request, "accounts/recovery_confirm.html", {"form": form})


@dms_write_required
@permission_required("organization.manage_roles", raise_exception=True)
def zoho_connect(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    return redirect(authorization_url(request))


@dms_write_required
@permission_required("organization.manage_roles", raise_exception=True)
def zoho_callback(request):
    if request.method != "GET":
        return HttpResponse(status=405)
    try:
        complete_authorization(request, code=request.GET.get("code"), state=request.GET.get("state"))
    except Exception:
        return HttpResponse("Zoho authorization could not be completed.", status=400)
    return HttpResponse("Zoho Mail API authorization completed.", status=200)


@dms_write_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_resend_activation(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    if not activation_eligibility(profile.user):
        raise PermissionDenied("This account is not awaiting activation.")
    try:
        resend_activation_email(user=profile.user, actor=request.user)
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("organization:employee-detail", pk=profile.pk)
    messages.success(request, "A fresh activation email has been queued. The previous activation link has been invalidated.")
    return redirect("organization:employee-detail", pk=profile.pk)


@dms_write_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_edit(request, pk):
    profile = get_object_or_404(EmployeeProfile.objects.select_related("user"), pk=pk)
    form = EmployeeEditForm(request.POST or None, profile=profile)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        existing_roles = set(profile.user.groups.filter(name__startswith="DMS ").values_list("pk", flat=True))
        submitted_roles = {role.pk for role in data["roles"]}
        if existing_roles != submitted_roles and not request.user.has_perm("organization.manage_roles"):
            form.add_error("roles", "You do not have permission to change DMS roles.")
        else:
            try:
                update_employee(actor=request.user, profile=profile, **{
                    key: data[key] for key in ("staff_id", "employment_status", "account_status", "department", "position", "supervisor", "start_date", "roles")
                })
            except (ValidationError, PermissionDenied) as error:
                if isinstance(error, PermissionDenied):
                    raise
                _add_errors(form, error)
            else:
                messages.success(request, "Employee record updated. Changes to department, position or supervisor were recorded as a new dated assignment.")
                return redirect("organization:employee-detail", pk=profile.pk)
    context = _shell(request, "Manage employee", [{"label": "Employees", "url": reverse("organization:employee-list")},
                                                   {"label": profile.staff_id, "url": reverse("organization:employee-detail", args=[pk])},
                                                   {"label": "Manage"}])
    context.update({"form": form, "profile": profile,
                    "can_resend_activation": activation_eligibility(profile.user),
                    "latest_activation_delivery": EmailOutbox.objects.filter(
                        recipient=profile.user,
                        message_type=EmailOutbox.MessageType.ACCOUNT_ACTIVATION,
                    ).first()})
    return render(request, "organization/employee_form.html", context)


@dms_access_required
@permission_required("organization.manage_departments", raise_exception=True)
def department_list(request):
    departments = Department.objects.select_related("parent", "head__user").annotate(
        current_employee_count=Count("assignments", filter=Q(assignments__end_date__isnull=True, assignments__start_date__lte=timezone.localdate()), distinct=True)
    ).order_by("name", "pk")
    query = request.GET.get("q", "").strip()
    if query:
        departments = departments.filter(Q(code__icontains=query) | Q(name__icontains=query))
    status = request.GET.get("status", "")
    if status in ("active", "inactive"):
        departments = departments.filter(is_active=(status == "active"))
    page = Paginator(departments, 20).get_page(request.GET.get("page"))
    context = _shell(request, "Departments", [{"label": "Organisation"}, {"label": "Departments"}])
    context.update({"page": page, "query": query, "status_filter": status})
    return render(request, "organization/department_list.html", context)


@dms_write_required
@permission_required("organization.manage_departments", raise_exception=True)
def department_edit(request, pk=None):
    department = get_object_or_404(Department, pk=pk) if pk else Department()
    form = DepartmentForm(request.POST or None, instance=department)
    if request.method == "POST" and form.is_valid():
        try:
            save_department(actor=request.user, department=department, values=form.cleaned_data)
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(request, "Department saved.")
            return redirect("organization:department-list")
    context = _shell(request, "Edit department" if pk else "Create department", [{"label": "Departments", "url": reverse("organization:department-list")}, {"label": "Edit" if pk else "Create"}])
    context.update({
        "form": form,
        "record_type": "department",
        "record_mode": "update" if pk else "create",
        "record_name": department.name,
        "record_initial_active": department.is_active,
    })
    context["confirm_deactivation"] = bool(department.pk and department.is_active)
    return render(request, "organization/entity_form.html", context)


@dms_access_required
@permission_required("organization.manage_positions", raise_exception=True)
def position_list(request):
    positions = Position.objects.select_related("department").annotate(
        current_employee_count=Count("assignments", filter=Q(assignments__end_date__isnull=True, assignments__start_date__lte=timezone.localdate()), distinct=True)
    ).order_by("title", "pk")
    query = request.GET.get("q", "").strip()
    if query:
        positions = positions.filter(Q(code__icontains=query) | Q(title__icontains=query) | Q(department__name__icontains=query))
    status = request.GET.get("status", "")
    if status in ("active", "inactive"):
        positions = positions.filter(is_active=(status == "active"))
    page = Paginator(positions, 20).get_page(request.GET.get("page"))
    context = _shell(request, "Positions", [{"label": "Organisation"}, {"label": "Positions"}])
    context.update({"page": page, "query": query, "status_filter": status})
    return render(request, "organization/position_list.html", context)


@dms_write_required
@permission_required("organization.manage_positions", raise_exception=True)
def position_edit(request, pk=None):
    position = get_object_or_404(Position, pk=pk) if pk else Position()
    form = PositionForm(request.POST or None, instance=position)
    if request.method == "POST" and form.is_valid():
        try:
            save_position(actor=request.user, position=position, values=form.cleaned_data)
        except ValidationError as error:
            _add_errors(form, error)
        else:
            messages.success(request, "Position saved.")
            return redirect("organization:position-list")
    context = _shell(request, "Edit position" if pk else "Create position", [{"label": "Positions", "url": reverse("organization:position-list")}, {"label": "Edit" if pk else "Create"}])
    context.update({
        "form": form,
        "record_type": "position",
        "record_mode": "update" if pk else "create",
        "record_name": position.title,
        "record_initial_active": position.is_active,
    })
    context["confirm_deactivation"] = bool(position.pk and position.is_active)
    return render(request, "organization/entity_form.html", context)


@dms_access_required
@permission_required("organization.manage_employees", raise_exception=True)
def structure(request):
    today = timezone.localdate()
    assignments = list(EmployeeAssignment.objects.filter(
        end_date__isnull=True, start_date__lte=today,
    ).select_related(
        "employee__user", "department__head", "position", "supervisor__user",
    ).prefetch_related("employee__user__groups").order_by(
        "department__name", "employee__user__last_name", "employee__user__first_name", "employee__staff_id",
    ))

    # The bootstrap completion marker is global, so correlate it with the
    # immutable onboarding audit trail to identify only the account created by
    # that bootstrap transaction. Administrator role membership alone is not
    # sufficient to exclude an employee from the organogram.
    bootstrap_state = EmployeeBootstrapState.objects.filter(pk=1).only("completed_at").first()
    bootstrap_user_ids = set()
    bootstrap_assignment_ids = set()
    if bootstrap_state and bootstrap_state.completed_at:
        window_start = bootstrap_state.completed_at - timedelta(seconds=5)
        bootstrap_user_ids = set(OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.USER_CREATED,
            occurred_at__gte=window_start, occurred_at__lte=bootstrap_state.completed_at,
        ).values_list("subject_user_id", flat=True))
        role_change_user_ids = set(OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.ROLE_CHANGED,
            occurred_at__gte=window_start, occurred_at__lte=bootstrap_state.completed_at,
        ).values_list("subject_user_id", flat=True))
        assignment_user_ids = set(OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.ASSIGNMENT_CREATED,
            occurred_at__gte=window_start, occurred_at__lte=bootstrap_state.completed_at,
        ).values_list("subject_user_id", flat=True))
        bootstrap_user_ids &= role_change_user_ids & assignment_user_ids
        bootstrap_assignment_ids = set(OrganizationAuditEvent.objects.filter(
            action=OrganizationAuditEvent.Action.ASSIGNMENT_CREATED,
            subject_user_id__in=bootstrap_user_ids,
            occurred_at__gte=window_start, occurred_at__lte=bootstrap_state.completed_at,
        ).values_list("assignment_id", flat=True))

    # Use department.parent for visual unit nesting, while retaining supervisor
    # assignments as the source of employee-to-employee reporting relationships.
    nodes = {}
    for assignment in assignments:
        profile = assignment.employee
        groups = {group.name for group in profile.user.groups.all()}
        if (
            "DMS Administrator" in groups
            and profile.user_id in bootstrap_user_ids
            and assignment.pk in bootstrap_assignment_ids
            and window_start <= profile.created_at <= bootstrap_state.completed_at
        ):
            continue
        nodes[profile.pk] = {
            "profile": profile,
            "assignment": assignment,
            "assignment_id": assignment.pk,
            "children": [],
            "role": "employee",
            "url": reverse("organization:employee-detail", args=[profile.pk]),
            "name": profile.user.get_full_name().strip() or profile.user.get_username(),
            "position": assignment.position.title,
            "department": assignment.department.name,
            "reports_to": (
                assignment.supervisor.user.get_full_name().strip()
                or assignment.supervisor.user.get_username()
            ) if assignment.supervisor_id else "",
            "status": profile.get_account_status_display(),
            "is_active": profile.account_status == EmployeeProfile.AccountStatus.ACTIVE
            and profile.employment_status == EmployeeProfile.EmploymentStatus.EMPLOYED,
        }

    for node in nodes.values():
        title = node["position"].casefold()
        if "managing director" in title or title.strip() == "md":
            node["role"] = "md"
        elif "executive director" in title or title.strip() == "ed":
            node["role"] = "ed"
        elif node["assignment"].department.head_id == node["profile"].pk:
            node["role"] = "head"

    departments_by_id = {
        department.pk: department
        for department in Department.objects.select_related("parent", "head__user").all()
    }
    # Include active saved units even when they have no current employees, so
    # empty departments remain visible in the management-facing structure.
    needed_department_ids = {node["assignment"].department_id for node in nodes.values()}
    needed_department_ids.update(
        department.pk for department in departments_by_id.values() if department.is_active
    )
    for department_id in tuple(needed_department_ids):
        department = departments_by_id.get(department_id)
        while department and department.parent_id:
            needed_department_ids.add(department.parent_id)
            department = departments_by_id.get(department.parent_id)
    department_nodes = {
        department_id: {
            "department": departments_by_id[department_id],
            "children": [],
            "employees": [],
            "employee_count": 0,
        }
        for department_id in needed_department_ids
        if department_id in departments_by_id
    }
    department_roots = []
    for department_id, department_node in department_nodes.items():
        parent_id = department_node["department"].parent_id
        if parent_id in department_nodes and parent_id != department_id:
            department_nodes[parent_id]["children"].append(department_node)
        else:
            department_roots.append(department_node)
    for department_node in department_nodes.values():
        department_node["children"].sort(key=lambda item: (item["department"].name, item["department"].pk))
    department_roots.sort(key=lambda item: (item["department"].name, item["department"].pk))

    # Keep employee connectors only where both assignments share a department.
    # A supervisor in another department remains visible in the card's explicit
    # “Reports to” line while department.parent controls the outer unit grouping.
    for node in nodes.values():
        department_nodes[node["assignment"].department_id]["employee_count"] += 1
        supervisor = nodes.get(node["assignment"].supervisor_id)
        if supervisor and supervisor["assignment"].department_id == node["assignment"].department_id:
            supervisor["children"].append(node)
        else:
            department_nodes[node["assignment"].department_id]["employees"].append(node)

    for department_node in department_nodes.values():
        department_node["employees"].sort(key=lambda item: (item["name"].casefold(), item["assignment_id"]))

    # Titles identify the executive card treatment only. Saved assignments still
    # determine whether any employee reports directly to an executive.
    md_nodes = [node for node in nodes.values() if node["role"] == "md"]

    # Visually group direct MD reports from the same department as the MD as
    # the executive office. This only changes presentation, not parentage.
    for md in md_nodes:
        office_reports = [
            child for child in md["children"]
            if child["role"] != "ed"
            and child["assignment"].department_id == md["assignment"].department_id
        ]
        for child in office_reports:
            child["role"] = "executive-office"
        md["executive_office_reports"] = office_reports
        md["children"] = [child for child in md["children"] if child not in office_reports]

    # Protect rendering from malformed/cyclic legacy data, while keeping every
    # assigned employee visible exactly once.
    def guard_tree(node, ancestors=()):
        profile_id = node["profile"].pk
        if profile_id in ancestors:
            node["children"] = []
            return
        for child in node["children"]:
            guard_tree(child, ancestors + (profile_id,))

    attached_assignment_ids = set()
    def gather(node):
        attached_assignment_ids.add(node["assignment_id"])
        for office_report in node.get("executive_office_reports", ()):
            gather(office_report)
        for child in node["children"]:
            gather(child)
    def gather_department(department_node):
        for employee in department_node["employees"]:
            gather(employee)
        for child_department in department_node["children"]:
            gather_department(child_department)

    for department_root in department_roots:
        gather_department(department_root)
    # Any cyclic employee component has no natural root; keep every assignment
    # visible in its department's reporting-data notice.
    unlinked = [node for node in nodes.values() if node["assignment_id"] not in attached_assignment_ids]
    for node in unlinked:
        node["children"] = []

    for node in nodes.values():
        guard_tree(node)

    context = _shell(request, "Organisation structure", [{"label": "Organisation"}, {"label": "Structure"}])
    context.update({
        "department_roots": department_roots,
        "unlinked": unlinked,
        "employee_count": len(nodes),
    })
    return render(request, "organization/structure.html", context)


@dms_access_required
@permission_required("organization.manage_roles", raise_exception=True)
def roles(request):
    from django.contrib.auth.models import Group, Permission
    groups = Group.objects.filter(name__startswith="DMS ").prefetch_related("permissions", "user_set")
    context = _shell(request, "Roles & permissions", [{"label": "Organisation"}, {"label": "Roles & permissions"}])
    context.update({"groups": groups, "permissions": Permission.objects.filter(content_type__app_label__in=("organization", "documents")).select_related("content_type").order_by("content_type__app_label", "codename")})
    return render(request, "organization/roles.html", context)


@dms_write_required
@permission_required("documents.reassign_workflow_tasks", raise_exception=True)
def task_reassign(request, pk):
    from apps.documents.services import reassign_task
    task = get_object_or_404(WorkflowTask.objects.select_related(
        "instance__document", "assigned_to", "management_directive__issued_by",
    ), pk=pk, status=WorkflowTask.Status.PENDING)
    directive = getattr(task, "management_directive", None)
    employees = (
        current_direct_report_profiles(directive.issued_by, include_viewers=False)
        if directive else current_directory_profiles(include_viewers=False)
    )
    if request.method == "POST":
        new_profile = get_object_or_404(employees, pk=request.POST.get("employee"))
        reason = request.POST.get("reason", "").strip()
        if not reason or len(reason) > 500:
            messages.error(request, "Provide a reassignment reason of up to 500 characters.")
        else:
            try:
                reassign_task(task_id=task.pk, actor=request.user, new_user=new_profile.user, reason=reason)
            except ValidationError as error:
                messages.error(request, " ".join(error.messages))
            else:
                messages.success(request, "Pending workflow task reassigned; its previous assignment was preserved in history.")
                return redirect("documents:memo-detail", pk=task.instance.document_id)
    context = _shell(request, "Reassign workflow task", [{"label": "Employees", "url": reverse("organization:employee-list")}, {"label": "Reassign task"}])
    available_employees = employees.exclude(user=task.assigned_to)
    context.update({
        "task": task,
        "employees": [(profile, employee_directory_label(profile)) for profile in available_employees],
    })
    return render(request, "organization/task_reassign.html", context)
