"""URLs de Relatórios: Projeções, Movimentos futuros e Posição por conta."""

from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("reports/projections/", views.projections_view, name="projections_view"),
    path("reports/upcoming-movements/", views.upcoming_movements_view, name="upcoming_movements_view"),
    path("reports/account-position/", views.account_position_view, name="account_position_view"),
]
