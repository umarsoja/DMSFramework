from django.contrib import admin
from django.urls import include, path


from core.views import (
    DMSLoginView,
    DMSLogoutView,
    DMSPasswordChangeView,
    account_profile,
    account_settings,
    dashboard,
    health_check,
    landing,
)
from apps.organization import views as organization_views

admin.site.site_header = "APGC - DMS Admin Login"
admin.site.site_title = "APGC - Document Management System"
admin.site.index_title = "Welcome to APGC - Document Management System Admin"

urlpatterns = [
    path("accounts/login/", DMSLoginView.as_view(), name="dms-login"),
    path("accounts/bootstrap/", organization_views.bootstrap_login, name="bootstrap-login"),
    path("accounts/bootstrap/onboard/", organization_views.bootstrap_employee_create, name="bootstrap-onboard"),
    path("accounts/activate/<str:uidb64>/<str:token>/", organization_views.employee_activate, name="employee-activate"),
    path("accounts/request-activation/", organization_views.request_activation_link, name="request-activation-link"),
    path("accounts/password-reset/", organization_views.request_password_reset, name="password-reset-request"),
    path("accounts/password-reset/<str:uidb64>/<str:token>/", organization_views.confirm_password_reset, name="password-reset-confirm"),
    path("integrations/zoho/connect/", organization_views.zoho_connect, name="zoho-connect"),
    path("integrations/zoho/callback/", organization_views.zoho_callback, name="zoho-callback"),
    path("accounts/logout/", DMSLogoutView.as_view(), name="dms-logout"),
    path("accounts/profile/", account_profile, name="account-profile"),
    path("accounts/settings/", account_settings, name="account-settings"),
    path("accounts/password-change/", DMSPasswordChangeView.as_view(), name="account-password-change"),
    path("dashboard/", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("documents/", include("apps.documents.urls")),
    path("notifications/", include("apps.documents.notification_urls")),
    path("organisation/", include("apps.organization.urls")),
    path("", landing, name="home"),
    path("health/", health_check, name="health"),
]
