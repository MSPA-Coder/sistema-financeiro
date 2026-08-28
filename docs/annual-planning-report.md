# Relatório de planejamento anual

`reports/annual_planning.html` é a casca da tela e
`reports/partials/annual_planning_content.html` é o fragmento usado em
atualizações HTMX. A view pode renderizar a página inteira ou somente o
fragmento, como nas demais telas de relatórios.

## Contrato de contexto

Além das opções de filtro (`reference_month`, `default_reference_month`,
`layout`, `view_mode`, `status_options`, `owners`, `accounts`,
`selected_owner_ids`, `selected_account_ids`, `show_descriptions` e
`system_start_date`), o template espera `report` com:

- `owner_columns`: lista de `{id, name}` para as colunas individuais do mês
  de referência;
- `months`: lista ordenada de `{key, label, is_current, url?}`. A lista tem
  os meses do ano calendário ou os 13 meses da janela móvel;
- `rows`: lista achatada da árvore de categorias. Cada item tem
  `{kind, label, description?, category_path?, level, owner_values,
  months}`. `months` é uma lista alinhada aos meses do relatório e cada item
  tem `{value, is_current}`. `owner_values` é alinhada a `owner_columns`;
- `totals` (opcional), com `owner_values` e `months` no mesmo formato das
  linhas;

## Regras de cálculo

- As colunas de titulares usam apenas os lançamentos do mês de referência
  com status `vencidos` ou `a_vencer`; lançamentos realizados ficam fora
  dessas colunas.
- Os meses consolidados usam a visão completa: realizado pela data de
  realização e lançamentos abertos pelo vencimento.
- O layout padrão mostra janeiro a dezembro e destaca o mês de referência.
  A janela móvel mostra seis meses antes, o mês de referência e seis meses
  depois.
- Despesas e receitas são divididas por `is_recurring`; parcelas sem
  recorrência pertencem às seções não recorrentes.
- A linha **Movimentações Internas** do resumo mostra as transferências que
  afetam os saldos, sem misturá-las à geração de caixa. No mês de referência,
  ela inclui também transferências já realizadas; nas colunas mensais, mostra
  o volume de cada transferência uma única vez.

Os filtros múltiplos usam `owner_ids` e `account_ids` repetidos na query
string (por exemplo, `?owner_ids=1&owner_ids=3`). A autorização e a
normalização desses IDs continuam sendo responsabilidade do servidor.
