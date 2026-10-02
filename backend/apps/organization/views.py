from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.documents.models import WorkflowTask

from .forms import DepartmentForm, EmployeeEditForm, EmployeeOnboardingForm, PositionForm
from .models import Department, EmployeeProfile, OrganizationAuditEvent, Position
from .services import onboard_employee, save_department, save_position, update_employee


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
        "sidebar_items": [{"label": label, "url": reverse(name), "active": active} for label, name, active in nav],
    }


def _add_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, values in error.message_dict.items():
            for value in values:
                form.add_error(field if field in form.fields else None, value)
    else:
        form.add_error(None, " ".join(error.messages))


@login_required
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


@login_required
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
                    "tasks": WorkflowTask.objects.filter(assigned_to=profile.user).select_related("instance__document", "step").order_by("-assigned_at")[:20]})
    return render(request, "organization/employee_detail.html", context)


@login_required
@permission_required("organization.manage_employees", raise_exception=True)
def employee_create(request):
    form = EmployeeOnboardingForm(request.POST or None, actor=request.user)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        if data["roles"] and not request.user.has_perm("organization.manage_roles"):
            form.add_error("roles", "You do not have permission to assign DMS roles.")
        else:
            try:
                profile = onboard_employee(
                    actor=request.user,
                    user=data["existing_user"],
                    username=data["username"], first_name=data["first_name"],
                    last_name=data["last_name"], email=data["email"],
                    staff_id=data["staff_id"], employment_status=data["employment_status"],
                    account_status=data["account_status"], department=data["department"],
                    position=data["position"], supervisor=data["supervisor"],
                    start_date=data["start_date"], roles=data["roles"],
                )
            except ValidationError as error:
                _add_errors(form, error)
            else:
                messages.success(request, "Employee profile created. New accounts have no password set; arrange the account credential setup separately.")
                return redirect("organization:employee-detail", pk=profile.pk)
    context = _shell(request, "Onboard employee", [{"label": "Employees", "url": reverse("organization:employee-list")}, {"label": "Onboard"}])
    context["form"] = form
    return render(request, "organization/employee_form.html", context)


@login_required
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
    context.update({"form": form, "profile": profile})
    return render(request, "organization/employee_form.html", context)


@login_required
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


@login_required
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
    context["form"] = form
    return render(request, "organization/entity_form.html", context)


@login_required
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


@login_required
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
    context["form"] = form
    return render(request, "organization/entity_form.html", context)


@login_required
@permission_required("organization.manage_employees", raise_exception=True)
def structure(request):
    departments = Department.objects.filter(is_active=True, parent__isnull=True).prefetch_related(
        "children__children__children__children__assignments__employee__user",
        "children__children__children__assignments__employee__user",
        "children__children__assignments__employee__user",
        "children__assignments__employee__user",
        "assignments__employee__user", "assignments__position", "assignments__supervisor__user",
        "children__assignments__position", "children__assignments__supervisor__user",
        "head__user", "children__head__user",
    )
    context = _shell(request, "Organisation structure", [{"label": "Organisation"}, {"label": "Structure"}])
    context["departments"] = departments
    return render(request, "organization/structure.html", context)


@login_required
@permission_required("organization.manage_roles", raise_exception=True)
def roles(request):
    from django.contrib.auth.models import Group, Permission
    groups = Group.objects.filter(name__startswith="DMS ").prefetch_related("permissions", "user_set")
    context = _shell(request, "Roles & permissions", [{"label": "Organisation"}, {"label": "Roles & permissions"}])
    context.update({"groups": groups, "permissions": Permission.objects.filter(content_type__app_label__in=("organization", "documents")).select_related("content_type").order_by("content_type__app_label", "codename")})
    return render(request, "organization/roles.html", context)


@login_required
@permission_required("documents.reassign_workflow_tasks", raise_exception=True)
def task_reassign(request, pk):
    from apps.documents.services import reassign_task
    task = get_object_or_404(WorkflowTask.objects.select_related("instance__document", "assigned_to"), pk=pk, status=WorkflowTask.Status.PENDING)
    employees = EmployeeProfile.objects.filter(account_status=EmployeeProfile.AccountStatus.ACTIVE,
                                                employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
                                                assignments__end_date__isnull=True,
                                                assignments__start_date__lte=timezone.localdate(),
                                                assignments__department__is_active=True,
                                                assignments__position__is_active=True).select_related("user").distinct()
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
    context.update({"task": task, "employees": employees.exclude(user=task.assigned_to)})
    return render(request, "organization/task_reassign.html", context)
