"""Rotas do shell Django: Permissões e Configurações."""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("permissions/", views.permissions_view, name="permissions"),

    path("settings/", views.settings_home_view, name="settings_home"),
    path("settings/password-policy/", views.settings_update_password_policy_view, name="settings_update_password_policy"),
    path("settings/login-lockout-policy/", views.settings_update_login_lockout_view, name="settings_update_login_lockout"),
    path("settings/recurring-projection/", views.settings_update_recurring_projection_view, name="settings_update_recurring_projection"),
    path("settings/recurring-projection/run/", views.settings_run_recurring_projection_view, name="settings_run_recurring_projection"),

    path("settings/profile/", views.settings_profile_view, name="settings_profile"),
    path("settings/theme/", views.settings_update_theme_view, name="settings_update_theme"),
    path("settings/table-scroll/", views.settings_update_table_scroll_view, name="settings_update_table_scroll"),

    path("settings/account-visibility/", views.settings_visibility_view, name="settings_visibility"),

    path("settings/monthly-close/", views.settings_monthly_close_view, name="settings_monthly_close"),
    path("settings/month-close/close/", views.settings_close_month_view, name="settings_close_month"),
    path("settings/month-close/reopen/", views.settings_reopen_month_view, name="settings_reopen_month"),

    path("settings/database/", views.settings_database_view, name="settings_database"),
    path("settings/database/health-check/", views.settings_health_check_view, name="settings_health_check"),
    path("settings/database/optimize/", views.settings_optimize_view, name="settings_optimize"),

    path("settings/audit-log/", views.settings_audit_log_view, name="settings_audit_log"),
]
