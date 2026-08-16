"""URLs para o cadastro de Titulares (AccountOwner)."""
from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('tables/owners/', views.owners_view, name='owners_view'),
    path('tables/owners/create/', views.create_owner_view, name='create_owner'),
    path('tables/owners/<int:owner_id>/update/', views.update_owner_view, name='update_owner'),
    path('tables/owners/<int:owner_id>/delete/', views.delete_owner_view, name='delete_owner'),
    path('change-password/', views.change_password_view, name='change_password'),
]
