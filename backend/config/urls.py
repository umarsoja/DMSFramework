from django.contrib import admin
from django.urls import include, path


from core.views import (
    DMSLoginView,
    DMSLogoutView,
    DMSPasswordChangeView,
    account_profile,
    account_settings,
    health_check,
    landing,
)

admin.site.site_header = "APGC - DMS Admin Login"
admin.site.site_title = "APGC - Document Management System"
admin.site.index_title = "Welcome to APGC - Document Management System Admin"

urlpatterns = [
    path("accounts/login/", DMSLoginView.as_view(), name="dms-login"),
    path("accounts/logout/", DMSLogoutView.as_view(), name="dms-logout"),
    path("accounts/profile/", account_profile, name="account-profile"),
    path("accounts/settings/", account_settings, name="account-settings"),
    path("accounts/password-change/", DMSPasswordChangeView.as_view(), name="account-password-change"),
    path("admin/", admin.site.urls),
    path("documents/", include("apps.documents.urls")),
    path("organisation/", include("apps.organization.urls")),
    path("", landing, name="home"),
    path("health/", health_check, name="health"),
]
