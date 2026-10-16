from django.urls import path

from . import notification_views

app_name = "notifications"

urlpatterns = [
    path("", notification_views.notification_list, name="list"),
    path("<int:pk>/open/", notification_views.notification_open, name="open"),
]
