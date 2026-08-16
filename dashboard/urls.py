"""URLs do dashboard."""

from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("dashboard/content/", views.dashboard_content, name="dashboard_content"),
]
