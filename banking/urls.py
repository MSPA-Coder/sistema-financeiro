"""URLs para os cadastros de Instituições e Contas financeiras."""
from django.urls import path

from . import views

app_name = 'banking'

urlpatterns = [
    path('tables/banks/', views.institutions_view, name='institutions_view'),
    path('tables/banks/create/', views.create_institution_view, name='create_institution'),
    path('tables/banks/<int:institution_id>/update/', views.update_institution_view, name='update_institution'),
    path('tables/banks/<int:institution_id>/delete/', views.delete_institution_view, name='delete_institution'),

    path('tables/accounts/', views.accounts_view, name='accounts_view'),
    path('tables/accounts/create/', views.create_account_view, name='create_account'),
    path('tables/accounts/<int:account_id>/update/', views.update_account_view, name='update_account'),
    path('tables/accounts/<int:account_id>/delete/', views.delete_account_view, name='delete_account'),
]
