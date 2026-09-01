"""Rotas do módulo Gestão."""
from django.urls import path

from . import views

app_name = "management"

urlpatterns = [
    path("management/", views.management_view, name="management_view"),
    path("management/tag/", views.create_tag_view, name="create_tag"),
    path("management/tag/retire/", views.retire_tag_view, name="retire_tag"),
    path("management/project/", views.create_project_view, name="create_project"),
    path("management/project/retire/", views.retire_project_view, name="retire_project"),
    path("management/budget/", views.save_budget_view, name="save_budget"),
    path("management/budget/retire/", views.retire_budget_view, name="retire_budget"),
    path("management/assign-tag/", views.assign_tag_view, name="assign_tag"),
    path("management/assign-project/", views.assign_project_view, name="assign_project"),
]
