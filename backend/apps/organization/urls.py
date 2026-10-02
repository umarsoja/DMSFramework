from django.urls import path

from . import views

app_name = "organization"

urlpatterns = [
    path("employees/", views.employee_list, name="employee-list"),
    path("employees/new/", views.employee_create, name="employee-create"),
    path("employees/<int:pk>/", views.employee_detail, name="employee-detail"),
    path("employees/<int:pk>/manage/", views.employee_edit, name="employee-edit"),
    path("departments/", views.department_list, name="department-list"),
    path("departments/new/", views.department_edit, name="department-create"),
    path("departments/<int:pk>/edit/", views.department_edit, name="department-edit"),
    path("positions/", views.position_list, name="position-list"),
    path("positions/new/", views.position_edit, name="position-create"),
    path("positions/<int:pk>/edit/", views.position_edit, name="position-edit"),
    path("structure/", views.structure, name="structure"),
    path("roles/", views.roles, name="roles"),
    path("workflow-tasks/<int:pk>/reassign/", views.task_reassign, name="task-reassign"),
]
