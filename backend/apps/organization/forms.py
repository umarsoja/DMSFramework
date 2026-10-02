from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Department, EmployeeProfile, Position


class EmployeeOnboardingForm(forms.Form):
    existing_user = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="Link an existing account", widget=forms.Select(attrs={"class": "form-select"}))
    username = forms.CharField(max_length=150, required=False, widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    first_name = forms.CharField(max_length=150, required=False, label="First name", widget=forms.TextInput(attrs={"class": "form-control"}))
    last_name = forms.CharField(max_length=150, required=False, label="Last name", widget=forms.TextInput(attrs={"class": "form-control"}))
    email = forms.EmailField(required=False, label="Official email", widget=forms.EmailInput(attrs={"class": "form-control", "autocomplete": "off"}))
    staff_id = forms.CharField(max_length=64, label="Staff ID", widget=forms.TextInput(attrs={"class": "form-control"}))
    employment_status = forms.ChoiceField(choices=EmployeeProfile.EmploymentStatus.choices, widget=forms.Select(attrs={"class": "form-select"}))
    account_status = forms.ChoiceField(choices=EmployeeProfile.AccountStatus.choices, initial=EmployeeProfile.AccountStatus.INACTIVE, widget=forms.Select(attrs={"class": "form-select"}))
    department = forms.ModelChoiceField(queryset=Department.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    position = forms.ModelChoiceField(queryset=Position.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    supervisor = forms.ModelChoiceField(queryset=EmployeeProfile.objects.none(), required=False, widget=forms.Select(attrs={"class": "form-select"}))
    start_date = forms.DateField(widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}))
    roles = forms.ModelMultipleChoiceField(queryset=Group.objects.none(), required=False, widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 5}))

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        User = get_user_model()
        self.fields["existing_user"].queryset = User.objects.filter(employee_profile__isnull=True).order_by("last_name", "first_name", "username")
        self.fields["department"].queryset = Department.objects.filter(is_active=True)
        self.fields["position"].queryset = Position.objects.filter(is_active=True).select_related("department")
        self.fields["supervisor"].queryset = EmployeeProfile.objects.filter(
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            assignments__end_date__isnull=True,
            assignments__start_date__lte=timezone.localdate(),
            assignments__department__is_active=True,
            assignments__position__is_active=True,
        ).select_related("user")
        self.fields["roles"].queryset = Group.objects.filter(name__startswith="DMS ").order_by("name")

    def clean(self):
        cleaned = super().clean()
        existing = cleaned.get("existing_user")
        if not existing:
            for field in ("username", "first_name", "last_name"):
                if not cleaned.get(field):
                    self.add_error(field, "Enter this value when creating a new account.")
            if cleaned.get("username") and get_user_model().objects.filter(username=cleaned["username"]).exists():
                self.add_error("username", "That username is already in use.")
        if cleaned.get("position") and cleaned.get("department"):
            position = cleaned["position"]
            if position.department_id and position.department_id != cleaned["department"].pk:
                self.add_error("position", "This position belongs to another department.")
        if existing and existing == self.actor:
            self.add_error("existing_user", "You cannot create an employee profile for yourself.")
        return cleaned


class EmployeeEditForm(forms.Form):
    staff_id = forms.CharField(max_length=64, label="Staff ID", widget=forms.TextInput(attrs={"class": "form-control"}))
    employment_status = forms.ChoiceField(choices=EmployeeProfile.EmploymentStatus.choices, widget=forms.Select(attrs={"class": "form-select"}))
    account_status = forms.ChoiceField(choices=EmployeeProfile.AccountStatus.choices, widget=forms.Select(attrs={"class": "form-select"}))
    department = forms.ModelChoiceField(queryset=Department.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    position = forms.ModelChoiceField(queryset=Position.objects.none(), widget=forms.Select(attrs={"class": "form-select"}))
    supervisor = forms.ModelChoiceField(queryset=EmployeeProfile.objects.none(), required=False, widget=forms.Select(attrs={"class": "form-select"}))
    start_date = forms.DateField(required=False, label="Assignment / separation effective date", widget=forms.DateInput(attrs={"class": "form-control", "type": "date"}), help_text="For an assignment change, this is the new start date. For separation, this is the final day of the current assignment.")
    roles = forms.ModelMultipleChoiceField(queryset=Group.objects.none(), required=False, widget=forms.SelectMultiple(attrs={"class": "form-select", "size": 5}))

    def __init__(self, *args, profile, **kwargs):
        super().__init__(*args, **kwargs)
        current = profile.current_assignment
        departments = Department.objects.filter(is_active=True)
        positions = Position.objects.filter(is_active=True)
        if current and not current.department.is_active:
            departments = departments | Department.objects.filter(pk=current.department_id)
        if current and not current.position.is_active:
            positions = positions | Position.objects.filter(pk=current.position_id)
        self.fields["department"].queryset = departments
        self.fields["position"].queryset = positions.select_related("department")
        self.fields["supervisor"].queryset = EmployeeProfile.objects.filter(
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            assignments__end_date__isnull=True,
            assignments__start_date__lte=timezone.localdate(),
            assignments__department__is_active=True,
            assignments__position__is_active=True,
        ).exclude(pk=profile.pk).select_related("user")
        self.fields["roles"].queryset = Group.objects.filter(name__startswith="DMS ").order_by("name")
        if not self.is_bound:
            self.initial.update({
                "staff_id": profile.staff_id,
                "employment_status": profile.employment_status,
                "account_status": profile.account_status,
                "department": current.department_id if current else None,
                "position": current.position_id if current else None,
                "supervisor": current.supervisor_id if current else None,
                "roles": profile.user.groups.filter(name__startswith="DMS "),
            })

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("position") and cleaned.get("department"):
            position = cleaned["position"]
            if position.department_id and position.department_id != cleaned["department"].pk:
                self.add_error("position", "This position belongs to another department.")
        return cleaned


class DepartmentForm(forms.ModelForm):
    class Meta:
        model = Department
        fields = ("code", "name", "description", "parent", "head", "is_active")
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "parent": forms.Select(attrs={"class": "form-select"}),
            "head": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        parents = Department.objects.filter(is_active=True).exclude(pk=self.instance.pk)
        if self.instance.pk and self.instance.parent_id and not self.instance.parent.is_active:
            parents = parents | Department.objects.filter(pk=self.instance.parent_id)
        self.fields["parent"].queryset = parents
        self.fields["head"].queryset = EmployeeProfile.objects.filter(
            account_status=EmployeeProfile.AccountStatus.ACTIVE,
            employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
        ).select_related("user")
        if self.instance.pk and self.instance.head_id:
            self.fields["head"].queryset = self.fields["head"].queryset | EmployeeProfile.objects.filter(pk=self.instance.head_id).select_related("user")
        if not self.instance.pk:
            self.fields["head"].queryset = EmployeeProfile.objects.none()
            self.fields["head"].help_text = "Create the department and assign its lead there before selecting a department head."

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        node = parent
        while node:
            if self.instance.pk and node.pk == self.instance.pk:
                raise ValidationError("A department cannot be nested below itself.")
            node = node.parent
        return parent


class PositionForm(forms.ModelForm):
    class Meta:
        model = Position
        fields = ("code", "title", "description", "department", "is_active")
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "department": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        departments = Department.objects.filter(is_active=True)
        if self.instance.pk and self.instance.department_id and not self.instance.department.is_active:
            departments = departments | Department.objects.filter(pk=self.instance.department_id)
        self.fields["department"].queryset = departments
