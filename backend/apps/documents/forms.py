from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Case, IntegerField, Prefetch, Value, When
from django.forms.widgets import ClearableFileInput
from django.utils import timezone

from .models import Attachment, Document, WorkflowActionType, WorkflowDefinition
from .handlers import CORRESPONDENCE_TYPE_CHOICES, PRIORITY_CHOICES
from apps.organization.directory import (
    current_directory_profiles,
    employee_directory_label,
    employee_directory_search_text,
)
from apps.organization.models import Department, EmployeeAssignment, EmployeeProfile


MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024
MAX_UPLOAD_BATCH_SIZE = 100 * 1024 * 1024
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


class DirectoryEmployeeSelectorWidget(forms.SelectMultiple):
    template_name = "documents/widgets/directory_employee_selector.html"

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        employee = getattr(value, "instance", None)
        if employee is not None:
            option["directory_search_text"] = employee_directory_search_text(employee)
        return option


class DirectoryEmployeeMultipleChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, profile):
        return employee_directory_label(profile)


class WorkflowActorChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, user):
        try:
            return employee_directory_label(user.employee_profile)
        except EmployeeProfile.DoesNotExist:
            return user.get_full_name().strip() or "Name not provided"


class MemoForm(forms.Form):
    to = DirectoryEmployeeMultipleChoiceField(queryset=EmployeeProfile.objects.none(), label="To", widget=DirectoryEmployeeSelectorWidget)
    through = DirectoryEmployeeMultipleChoiceField(queryset=EmployeeProfile.objects.none(), required=False, label="Through", widget=DirectoryEmployeeSelectorWidget)
    cc = DirectoryEmployeeMultipleChoiceField(queryset=EmployeeProfile.objects.none(), required=False, label="CC", widget=DirectoryEmployeeSelectorWidget)
    subject = forms.CharField(max_length=240, label="Subject", widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    classification = forms.ChoiceField(choices=Document.Classification.choices, label="Classification", widget=forms.Select(attrs={"class": "form-select"}))
    body = forms.CharField(label="Memo body", widget=forms.Textarea(attrs={"class": "form-control", "rows": 10}))
    attachments = MultipleFileField(required=False, label="Attachments", widget=MultipleFileInput(attrs={"class": "form-control", "accept": ".pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.csv,.txt,.png,.jpg,.jpeg"}))
    remove_attachments = forms.MultipleChoiceField(
        required=False,
        label="Remove attachments from the next version",
        widget=forms.CheckboxSelectMultiple,
    )
    revision_note = forms.CharField(max_length=500, required=False, label="Revision note", widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}))
    reviewer = WorkflowActorChoiceField(queryset=get_user_model().objects.none(), required=False, label="Reviewer", widget=forms.Select(attrs={"class": "form-select"}))

    def __init__(self, *args, user=None, document=None, action="save", **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.document = document
        self.action = action
        User = get_user_model()
        directory_profiles = current_directory_profiles()
        self.fields["to"].queryset = directory_profiles
        self.fields["through"].queryset = directory_profiles
        self.fields["cc"].queryset = directory_profiles
        reviewers = User.objects.filter(
            employee_profile__in=current_directory_profiles(include_viewers=False)
        ).exclude(groups__name="DMS Viewer").select_related("employee_profile").prefetch_related(
            Prefetch(
                "employee_profile__assignments",
                queryset=EmployeeAssignment.objects.filter(
                    end_date__isnull=True, start_date__lte=timezone.localdate()
                ).select_related("department", "position"),
                to_attr="_directory_current_assignments",
            )
        ).distinct()
        supervisor_id = None
        department_head_id = None
        recommendation_labels = []
        if user:
            reviewers = reviewers.exclude(pk=user.pk)
            try:
                assignment = user.employee_profile.current_assignment
            except EmployeeProfile.DoesNotExist:
                assignment = None
            if assignment:
                subordinate_ids = assignment.employee.supervisee_assignments.filter(
                    end_date__isnull=True,
                    start_date__lte=timezone.localdate(),
                ).values_list("employee__user_id", flat=True)
                reviewers = reviewers.exclude(pk__in=subordinate_ids)
                supervisor = assignment.supervisor
                department_head = assignment.department.head
                if department_head:
                    head_assignment = department_head.current_assignment
                    if not head_assignment or head_assignment.department_id != assignment.department_id:
                        department_head = None
                eligible_ids = set(reviewers.values_list("pk", flat=True))
                if supervisor and supervisor.user_id in eligible_ids:
                    supervisor_id = supervisor.user_id
                    supervisor_name = employee_directory_label(supervisor)
                if department_head and department_head.user_id in eligible_ids:
                    department_head_id = department_head.user_id
                    department_head_name = employee_directory_label(department_head)
                if supervisor_id and supervisor_id == department_head_id:
                    recommendation_labels.append(
                        f"current supervisor and department manager/HOD: {supervisor_name}"
                    )
                else:
                    if supervisor_id:
                        recommendation_labels.append(f"current supervisor: {supervisor_name}")
                    if department_head_id:
                        recommendation_labels.append(f"department manager/HOD: {department_head_name}")
        reviewers = reviewers.annotate(
            organizational_priority=Case(
                When(pk=supervisor_id, then=Value(0)),
                When(pk=department_head_id, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        ).order_by("organizational_priority", "last_name", "first_name", "username")
        active_workflow = WorkflowDefinition.objects.filter(
            document_type__code="IM", document_type__is_active=True, is_active=True
        ).order_by("version").first()
        review_step = active_workflow.steps.filter(is_review=True).order_by("sequence").first() if active_workflow else None
        self.show_reviewer_selector = bool(review_step and review_step.actor_type == review_step.ActorType.SELECTED_USER)
        self.fields["reviewer"].queryset = reviewers if self.show_reviewer_selector else User.objects.none()
        self.fields["reviewer"].required = action == "submit" and self.show_reviewer_selector
        self.fields["reviewer"].help_text = (
            "Recommended from your current assignment: " + "; ".join(recommendation_labels)
            + ". Candidates must be active employees with a current assignment and a non-viewer DMS role; you and your direct reports are excluded."
            if recommendation_labels else
            "Choose an eligible reviewer. Candidates must be active employees with a current assignment and a non-viewer DMS role; you and your direct reports are excluded."
        )
        if not self.show_reviewer_selector:
            self.fields["reviewer"].help_text = "The active workflow resolves its reviewer from the configured actor rule."
        if document:
            version = document.current_version
            self.fields["remove_attachments"].choices = [
                (str(attachment.pk), attachment.original_filename)
                for attachment in version.attachments.all()
            ]
            for field in ("to", "through", "cc"):
                identity_key = f"{field}_employee_ids"
                self.fields[field].initial = version.content.get(identity_key, [])
                if identity_key not in version.content and version.content.get(field):
                    self.fields[field].help_text = (
                        "This earlier version contains legacy free-text recipient details. "
                        "Select current directory employees for this new version; the earlier version remains unchanged."
                    )
            for field in ("subject", "body", "classification"):
                self.fields[field].initial = version.content.get(field, getattr(document, field, ""))
            self.fields["revision_note"].required = document.status == Document.Status.RETURNED

    def clean(self):
        cleaned = super().clean()
        for field in ("to", "through", "cc"):
            if hasattr(self.data, "getlist"):
                submitted = self.data.getlist(self.add_prefix(field))
                if len(submitted) != len(set(submitted)):
                    self.add_error(field, "An employee can only be selected once in this recipient field.")
        return cleaned

    def clean_attachments(self):
        uploads = self.cleaned_data.get("attachments", [])
        validate_upload_batch(uploads)
        return uploads


class IncomingCorrespondenceForm(forms.Form):
    sender = forms.CharField(max_length=240, label="Sender / originating organisation")
    external_reference = forms.CharField(max_length=120, required=False, label="External reference number")
    subject = forms.CharField(max_length=240, label="Subject")
    correspondence_type = forms.ChoiceField(
        choices=CORRESPONDENCE_TYPE_CHOICES,
        label="Correspondence/document type",
        widget=forms.Select(attrs={"aria-controls": "other-correspondence-type-field"}),
    )
    other_correspondence_type = forms.CharField(
        max_length=100, required=False, label="Specify Document Type",
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )
    document_date = forms.DateField(required=False, label="Date on document", widget=forms.DateInput(attrs={"type": "date"}))
    received_on = forms.DateField(label="Date received by APGC", widget=forms.DateInput(attrs={"type": "date"}))
    priority = forms.ChoiceField(choices=PRIORITY_CHOICES, label="Priority", initial="MEDIUM")
    receiving_office = forms.ModelChoiceField(queryset=Department.objects.none(), label="Receiving office/unit")
    classification = forms.ChoiceField(choices=Document.Classification.choices, label="Classification")
    original_document = forms.FileField(label="Original received document")
    supporting_documents = MultipleFileField(required=False, label="Supporting documents")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css_class = "form-select" if isinstance(field.widget, forms.Select) else "form-control"
            field.widget.attrs["class"] = " ".join(filter(None, (field.widget.attrs.get("class", ""), css_class)))
        self.fields["receiving_office"].queryset = Department.objects.filter(is_active=True).order_by("name")
        self.fields["correspondence_type"].widget.attrs.update({
            "x-model": "correspondenceType",
            "x-init": "correspondenceType = $el.value",
        })
        self.fields["other_correspondence_type"].widget.attrs["x-bind:required"] = "correspondenceType === 'OTHER'"
        self.fields["received_on"].initial = timezone.localdate()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("correspondence_type") == "OTHER" and not cleaned.get("other_correspondence_type", "").strip():
            self.add_error("other_correspondence_type", "Specify the document type when Others is selected.")
        self._check_unique_filenames(cleaned)
        validate_upload_batch([
            cleaned.get("original_document"), *cleaned.get("supporting_documents", [])
        ])
        return cleaned

    def clean_other_correspondence_type(self):
        return self.cleaned_data.get("other_correspondence_type", "").strip()

    def clean_original_document(self):
        upload = self.cleaned_data["original_document"]
        self._validate_uploads([upload])
        return upload

    def clean_supporting_documents(self):
        uploads = self.cleaned_data.get("supporting_documents", [])
        self._validate_uploads(uploads)
        return uploads

    @staticmethod
    def _validate_uploads(uploads):
        validate_upload_batch(uploads)

    def _check_unique_filenames(self, cleaned):
        uploads = [cleaned.get("original_document"), *cleaned.get("supporting_documents", [])]
        names = [Path(upload.name).name[:255] for upload in uploads if upload]
        if len(names) != len(set(names)):
            self.add_error("supporting_documents", "Use a different filename for each file in a version.")


class IncomingCorrespondenceRevisionForm(forms.Form):
    original_document = forms.FileField(label="Revised external document")
    supporting_documents = MultipleFileField(required=False, label="Additional supporting documents")
    revision_note = forms.CharField(max_length=500, label="Revision note")

    def clean(self):
        cleaned = super().clean()
        uploads = [cleaned.get("original_document"), *cleaned.get("supporting_documents", [])]
        names = [Path(upload.name).name[:255] for upload in uploads if upload]
        if len(names) != len(set(names)):
            self.add_error("supporting_documents", "Use a different filename for each file in a version.")
        validate_upload_batch([
            cleaned.get("original_document"), *cleaned.get("supporting_documents", [])
        ])
        return cleaned

    def clean_original_document(self):
        upload = self.cleaned_data["original_document"]
        IncomingCorrespondenceForm._validate_uploads([upload])
        return upload

    def clean_supporting_documents(self):
        uploads = self.cleaned_data.get("supporting_documents", [])
        IncomingCorrespondenceForm._validate_uploads(uploads)
        return uploads


class DirectoryAssigneeChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, profile):
        return employee_directory_label(profile)


class ManagementDirectiveForm(forms.Form):
    action_type = forms.ModelChoiceField(
        queryset=WorkflowActionType.objects.none(), label="Action / directive type",
    )
    instruction = forms.CharField(
        max_length=4000, label="Instruction",
        widget=forms.Textarea(attrs={"rows": 4, "class": "form-control"}),
    )
    assignee = DirectoryAssigneeChoiceField(
        queryset=EmployeeProfile.objects.none(), required=False, label="Assignee",
    )
    due_date = forms.DateField(
        required=False, label="Due date", widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )

    def __init__(self, *args, can_assign=False, issuer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_assign = can_assign
        self.fields["action_type"].queryset = WorkflowActionType.objects.filter(
            is_active=True,
        ).exclude(code__in=("review", "approve")).order_by("label")
        if can_assign:
            from apps.organization.directory import current_direct_report_profiles

            self.fields["assignee"].queryset = current_direct_report_profiles(
                issuer, include_viewers=False,
            )
        else:
            self.fields.pop("assignee")
            self.fields.pop("due_date")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("due_date") and not cleaned.get("assignee"):
            self.add_error("due_date", "A due date requires an assigned workflow task.")
        if cleaned.get("instruction"):
            cleaned["instruction"] = cleaned["instruction"].strip()
            if not cleaned["instruction"]:
                self.add_error("instruction", "Provide the management instruction.")
        return cleaned


class SupportingWorkflowTaskForm(forms.Form):
    action_type = forms.ModelChoiceField(
        queryset=WorkflowActionType.objects.none(), label="Action type",
    )
    instruction = forms.CharField(
        max_length=4000, label="Instruction / context",
        widget=forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
    )
    assignee = DirectoryAssigneeChoiceField(
        queryset=EmployeeProfile.objects.none(), label="Assign to",
    )
    due_date = forms.DateField(
        required=False, label="Due date",
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )

    def __init__(self, *args, issuer, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["action_type"].queryset = WorkflowActionType.objects.filter(
            is_active=True,
        ).exclude(code__in=("review", "approve")).order_by("label")
        from apps.organization.directory import current_direct_report_profiles

        self.fields["assignee"].queryset = current_direct_report_profiles(
            issuer, include_viewers=False,
        )
        for field in self.fields.values():
            if field.widget.__class__ is forms.Select:
                field.widget.attrs["class"] = "form-select"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("instruction"):
            cleaned["instruction"] = cleaned["instruction"].strip()
            if not cleaned["instruction"]:
                self.add_error("instruction", "Provide the task instruction.")
        return cleaned


class WorkflowTaskCompletionForm(forms.Form):
    response = forms.CharField(
        max_length=10000, required=False, label="Response / comment",
        widget=forms.Textarea(attrs={"rows": 5, "class": "form-control"}),
    )
    evidence = forms.ModelMultipleChoiceField(
        queryset=Attachment.objects.none(), required=False, label="Evidence attachments",
        widget=forms.CheckboxSelectMultiple,
    )
    uploads = MultipleFileField(required=False, label="Upload supporting evidence")

    def __init__(self, *args, document, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["evidence"].queryset = Attachment.objects.filter(
            version__document=document,
        ).select_related("version").order_by("-version__number", "original_filename")
        self.fields["evidence"].label_from_instance = lambda attachment: (
            f"v{attachment.version.number} · {attachment.original_filename}"
        )
        self.fields["uploads"].widget.attrs["class"] = "form-control"

    def clean_uploads(self):
        uploads = self.cleaned_data.get("uploads", [])
        IncomingCorrespondenceForm._validate_uploads(uploads)
        return uploads

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("response"):
            cleaned["response"] = cleaned["response"].strip()
        if not cleaned.get("response") and not cleaned.get("evidence") and not cleaned.get("uploads"):
            raise ValidationError("Provide a response or at least one evidence attachment.")
        return cleaned


def validate_upload_batch(uploads):
    """Validate file types, per-file size and total request file payload size."""
    files = [upload for upload in uploads if upload]
    total_size = 0
    for upload in files:
        suffix = Path(upload.name).suffix.lower()
        if suffix not in ALLOWED_ATTACHMENT_EXTENSIONS:
            raise ValidationError(f"{upload.name}: this file type is not allowed.")
        if upload.size > MAX_ATTACHMENT_SIZE:
            raise ValidationError(f"{upload.name}: each attachment must be 20 MB or smaller.")
        total_size += upload.size
    if total_size > MAX_UPLOAD_BATCH_SIZE:
        raise ValidationError("All files in one submission must total 100 MB or less.")

