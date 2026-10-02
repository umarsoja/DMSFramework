from django.contrib import admin
from django.urls import include, path


from core.views import health_check, landing

admin.site.site_header = "APGC - DMS Admin Login"
admin.site.site_title = "APGC - Document Management System"
admin.site.index_title = "Welcome to APGC - Document Management System Admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("documents/", include("apps.documents.urls")),
    path("organisation/", include("apps.organization.urls")),
    path("", landing, name="home"),
    path("health/", health_check, name="health"),
]
