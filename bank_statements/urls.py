"""URLs de Bancos > Importações."""
from django.urls import path

from . import views

app_name = 'bank_statements'

urlpatterns = [
    path('banking/imports/', views.imports_view, name='imports_view'),
    path('banking/import/', views.create_import_view, name='create_import'),
    path('banking/import/status/<int:batch_id>/', views.import_status_view, name='import_status'),

    path('banking/reconciliation/', views.reconciliation_view, name='reconciliation_view'),
    path('banking/reconciliation/refresh/', views.reconciliation_refresh_view, name='reconciliation_refresh'),
    path('banking/reconcile/', views.reconcile_view, name='reconcile'),
    path('banking/reconcile/create-entry/', views.create_entry_from_line_view, name='create_entry_from_line'),
    path('banking/reconcile/bulk/', views.bulk_action_lines_view, name='bulk_action_lines'),
    path('banking/reconcile/undo/', views.undo_reconciliation_view, name='undo_reconciliation'),
    path('banking/ignore/', views.ignore_line_view, name='ignore_line'),

    path('banking/attachments/', views.attachments_view, name='attachments_view'),
    path('banking/attachment/', views.create_attachment_view, name='create_attachment'),
    path('banking/attachment/<int:attachment_id>/download/', views.attachment_download_view, name='attachment_download'),
]
