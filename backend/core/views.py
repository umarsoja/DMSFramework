from django.http import HttpResponse
from django.contrib import messages
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.views import LoginView, LogoutView, PasswordChangeView
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator

from apps.organization.authentication import DMSAuthenticationForm, dms_access_required, is_dms_viewer


def landing(request):
    """Public presentation page; no database or authentication dependency."""
    return render(request, "pages/landing.html")

def health_check(request):
    return HttpResponse("TradeFlow is running successfully.")


class DMSLoginView(LoginView):
    template_name = "accounts/login.html"
    authentication_form = DMSAuthenticationForm


class DMSLogoutView(LogoutView):
    next_page = "dms-login"


@dms_access_required
def account_profile(request):
    profile = request.user.employee_profile
    assignment = profile.current_assignment
    return render(request, "accounts/profile.html", {
        "profile": profile,
        "assignment": assignment,
        "dms_roles": request.user.groups.filter(name__startswith="DMS ").order_by("name"),
        "can_create_memo": not is_dms_viewer(request.user),
    })


@dms_access_required
def account_settings(request):
    return render(request, "accounts/settings.html", {
        "profile": request.user.employee_profile,
        "dms_roles": request.user.groups.filter(name__startswith="DMS ").order_by("name"),
        "can_create_memo": not is_dms_viewer(request.user),
    })


@method_decorator(dms_access_required, name="dispatch")
class DMSPasswordChangeView(PasswordChangeView):
    template_name = "accounts/password_change.html"
    success_url = reverse_lazy("account-settings")
    form_class = PasswordChangeForm

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            field.widget.attrs["class"] = "form-control"
        form.fields["old_password"].label = "Current password"
        form.fields["new_password2"].label = "Confirm new password"
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_create_memo"] = not is_dms_viewer(self.request.user)
        return context

    def form_valid(self, form):
        messages.success(self.request, "Your password has been changed.")
        return super().form_valid(form)
