"""URLs para módulo de transações."""
from django.urls import path

from . import views

app_name = 'transactions'

# Somente as rotas abaixo compoem o contrato deste modulo. Fechar e reabrir
# mes pertencem a `core:settings_close_month` e
# `core:settings_reopen_month`, que calculam o saldo a partir do razao. Nao ha
# rota de cancelamento de lancamento. Os limites sao protegidos por
# `tests/test_rotas_orfas_removidas.py`.

urlpatterns = [
    # Lista de transações
    path('transactions/', views.transactions_view, name='transactions_view'),

    # Movimentação n+1 (operações compostas)
    path('operations/', views.operations_view, name='operations_view'),
    
    # Realizar lançamento
    path('mark_realized/<int:tx_id>/', views.mark_realized, name='mark_realized'),

    # Criar/editar/excluir lançamento (único, parcelado, recorrente ou
    # transferência interna, conforme a categoria escolhida)
    path('transaction/', views.transaction_new, name='transaction_new'),
    path('transaction/<int:tx_id>/', views.transaction_edit, name='transaction_edit'),
    path('transaction/delete/<int:tx_id>/', views.transaction_delete, name='transaction_delete'),

    # Cadastros: categorias
    path('tables/categories/', views.categories_view, name='categories_view'),
    path('tables/categories/create/', views.create_category_view, name='create_category'),
    path('tables/categories/<int:category_id>/update/', views.update_category_view, name='update_category'),
    path('tables/categories/<int:category_id>/delete/', views.delete_category_view, name='delete_category'),
]
