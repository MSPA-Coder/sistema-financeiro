"""URLs para módulo de transações."""
from django.urls import path

from . import views

app_name = 'transactions'

# Tres rotas POST sairam daqui em 2026-08-22: `cancel_entry`, `close_month` e
# `reopen_month`. Mudavam estado e NENHUM template as acionava -- conferido por
# `{% url %}`, por `reverse()`, por caminho literal e no JS, que so conhece as
# acoes `delete` e `realize`. Eram superficie alcancavel por requisicao direta,
# sem tela correspondente.
#
# Fechar e reabrir mes continuam existindo, em `core:settings_close_month` e
# `core:settings_reopen_month`, que sao os caminhos que a tela de Fechamento
# Mensal usa. A versao que saiu era a mais fraca das duas: recebia
# `closing_balance` do proprio POST, enquanto a que ficou calcula o saldo do
# razao (`decimal_balance_before`). Um cliente direto podia fechar o mes com
# qualquer saldo.
#
# `tests/test_rotas_orfas_removidas.py` impede a volta.

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
