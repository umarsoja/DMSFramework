from django.contrib import admin
from django.urls import path

from core.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", health_check, name="health"),
]