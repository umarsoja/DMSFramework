from django.contrib import admin
from django.urls import path

from core.views import health_check, landing

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", landing, name="home"),
    path("health/", health_check, name="health"),
]
