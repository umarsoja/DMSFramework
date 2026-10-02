from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms.widgets import ClearableFileInput
from django.utils import timezone

from .models import Document
from apps.organization.models import EmployeeProfile


MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".txt", ".png", ".jpg", ".jpeg",
}


class MultipleFileInput(ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        return [super(MultipleFileField, self).clean(item, initial) for item in files]


class MemoForm(forms.Form):
    to = forms.CharField(max_length=500, label="To", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    through = forms.CharField(max_length=500, required=False, label="Through", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    cc = forms.CharField(max_length=500, required=False, label="CC", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    subject = forms.CharField(max_length=240, label="Subject", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    classification = forms.ChoiceField(choices=Document.Classification.choices, label="Classification", widget=forms.Select(attrs={"class": "form-select"}))
    body = forms.CharField(label="Memo body", widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}))
    attachments = MultipleFileField(required=False, label="Attachments", widget=MultipleFileInput(attrs={"class": "form-control", "accept": ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.txt,.png,.jpg,.jpeg"}))
    revision_note = forms.CharField(max_length=500, required=False, label="Revision note", widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    reviewer = forms.ModelChoiceField(queryset=get_user_model().objects.none(), required=False, label="Reviewer", widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, user=None, document=None, action="save", **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.document = document
        self.action = action
        User = get_user_model()
        eligible_profiles = User.objects.filter(
            employee_profile__account_status=EmployeeProfile.AccountStatus.ACTIVE,
            employee_profile__employment_status=EmployeeProfile.EmploymentStatus.EMPLOYED,
            employee_profile__assignments__end_date__isnull=True,
            employee_profile__assignments__start_date__lte=timezone.localdate(),
            employee_profile__assignments__department__is_active=True,
            employee_profile__assignments__position__is_active=True,
            is_active=True,
        ).distinct()
        legacy_staff = User.objects.filter(is_active=True, is_staff=True, employee_profile__isnull=True).distinct()
        reviewers = (eligible_profiles | legacy_staff).order_by("last_name", "first_name", "username")
        if user:
            reviewers = reviewers.exclude(pk=user.pk)
        self.fields["reviewer"].queryset = reviewers
        self.fields["reviewer"].required = action == "submit"
        if document:
            version = document.current_version
            for field in ("to", "through", "cc", "subject", "body", "classification"):
                self.fields[field].initial = version.content.get(field, getattr(document, field, ""))
            self.fields["revision_note"].required = document.status == Document.Status.RETURNED

    def clean_attachments(self):
        uploads = self.cleaned_data.get("attachments", [])
        for upload in uploads:
            suffix = Path(upload.name).suffix.lower()
            if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
                raise ValidationError(f"{upload.name}: this file type is not allowed.")
            if upload.size > MAX_ATTACHMENT_SIZE:
                raise ValidationError(f"{upload.name}: each attachment must be 20 MB or smaller.")
        return uploads

