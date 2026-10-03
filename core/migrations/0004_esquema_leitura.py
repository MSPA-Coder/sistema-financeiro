"""Esquema `leitura`: views versionadas para quem lê o banco sem passar pela aplicação.

Quem lê as tabelas do CB direto (hoje, o FinancasMCP) acopla o próprio SQL ao
schema e reescreve as regras do domínio: status efetivo, saldo, o que é gasto
estimado de fatura. Esta migração publica essas regras uma vez, em views, no
esquema `leitura`. O leitor ganha SELECT só nelas e não vê as tabelas.

O PostgreSQL recusa `DROP COLUMN` (e `ALTER ... TYPE`) de coluna que uma view
usa. A quebra de schema, que antes aparecia em produção no cliente, passa a
reprovar a migração de quem mexeu, na suíte, que aplica todas as migrações num
banco vazio a cada execução. Quem mudar uma coluna lida aqui deve depender desta
migração e recriar a view com `CREATE OR REPLACE VIEW` (ou `DROP` e `CREATE`
quando as colunas mudarem).
"""

from django.db import migrations

SQL = """
CREATE SCHEMA leitura;

COMMENT ON SCHEMA leitura IS
    'Contrato de leitura do CB: views versionadas pelas migrações. Quem lê o banco de fora usa só este esquema.';

-- A data de hoje que vale para o CB: a do fuso de São Paulo, e não a do servidor.
CREATE FUNCTION leitura.hoje() RETURNS date
LANGUAGE sql STABLE AS $$
    SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date
$$;

-- Um lançamento por linha, com as regras do domínio já aplicadas:
--   data     realizado -> data de realização; em aberto -> vencimento.
--   status   o EFETIVO: realizado; em aberto vencido antes de hoje -> 'vencidos';
--            o resto -> 'a_vencer'. O gravado (status_gravado) só é normalizado
--            quando alguém grava o lançamento.
--   valor    realizado -> valor realizado (ou o previsto, se faltar); em aberto -> previsto.
--   sinal    +1 receita, -1 despesa. Valores são sempre positivos.
CREATE VIEW leitura.lancamento AS
SELECT
    e.id,
    e.account_id AS conta_id,
    a.account_name AS conta,
    ow.owner_name AS titular,
    a.currency AS moeda,
    e.category_id AS categoria_id,
    c.category_name AS categoria,
    c.kind AS natureza,
    g.group_name AS grupo_da_categoria,
    CASE WHEN e.status = 'realizado' THEN e.realized_date ELSE e.due_date END AS data,
    CASE
        WHEN e.status = 'realizado' THEN 'realizado'
        WHEN e.due_date < leitura.hoje() THEN 'vencidos'
        ELSE 'a_vencer'
    END AS status,
    e.status AS status_gravado,
    e.entry_type AS tipo,
    CASE WHEN e.entry_type = 'receita' THEN 1 ELSE -1 END AS sinal,
    CASE
        WHEN e.status = 'realizado' THEN COALESCE(e.realized_amount, e.entry_amount)
        ELSE e.entry_amount
    END AS valor,
    e.entry_amount AS valor_previsto,
    e.realized_amount AS valor_realizado,
    e.description AS descricao,
    CASE WHEN e.installments > 1 THEN e.current_installment || '/' || e.installments END AS parcela,
    e.is_recurring AS recorrente,
    e.operation_type AS tipo_operacao,
    COALESCE(o.operation_key, '') ~ '^cartao-(estimativa|pagamento):' AS estimativa_de_fatura,
    COALESCE(o.operation_key, '') LIKE 'cartao-estimativa:%' AS gasto_estimado_do_cartao,
    e.due_date AS vencimento,
    e.realized_date AS realizado_em,
    e.bank_operation_id AS operacao_id,
    e.source_entry_id AS lancamento_de_origem_id
FROM cash_flow_entry e
JOIN financial_account a ON a.id = e.account_id
JOIN account_owner ow ON ow.id = a.owner_id
JOIN cash_flow_category c ON c.id = e.category_id
LEFT JOIN cash_flow_category_group g ON g.id = c.group_id
LEFT JOIN bank_operation o ON o.id = e.bank_operation_id;

COMMENT ON VIEW leitura.lancamento IS
    'Lançamentos com status efetivo, data e valor que valem e sinal. Gasto/renda: natureza = gerencial. Transferência entre contas próprias não é gasto. Não some moedas diferentes.';

-- Uma conta por linha, com os três saldos que o CB mostra.
--   saldo_realizado_hoje     inicial + realizados até hoje.
--   vencido_em_aberto        soma assinada do que está em aberto e já venceu.
--   saldo_com_todo_previsto  inicial + todos os lançamentos, realizados ou não.
-- Cartão de crédito: saldo negativo = fatura em aberto.
CREATE VIEW leitura.conta AS
SELECT
    a.id,
    a.account_name AS conta,
    ow.owner_name AS titular,
    i.institution_name AS instituicao,
    i.institution_type AS tipo_instituicao,
    a.currency AS moeda,
    a.account_kind AS tipo,
    a.purpose AS finalidade,
    a.card_closing_day AS dia_fechamento_cartao,
    a.card_due_day AS dia_vencimento_cartao,
    a.initial_balance AS saldo_inicial,
    a.initial_balance_date AS data_saldo_inicial,
    a.initial_balance + COALESCE(
        SUM(l.sinal * l.valor) FILTER (WHERE l.status = 'realizado' AND l.realizado_em <= leitura.hoje()), 0
    ) AS saldo_realizado_hoje,
    COALESCE(
        SUM(l.sinal * l.valor_previsto) FILTER (WHERE l.status <> 'realizado' AND l.vencimento < leitura.hoje()), 0
    ) AS vencido_em_aberto,
    a.initial_balance + COALESCE(SUM(l.sinal * l.valor), 0) AS saldo_com_todo_previsto,
    MAX(l.vencimento) FILTER (WHERE l.status <> 'realizado') AS ultimo_vencimento_previsto
FROM financial_account a
JOIN account_owner ow ON ow.id = a.owner_id
JOIN financial_institution i ON i.id = a.institution_id
LEFT JOIN leitura.lancamento l ON l.conta_id = a.id
GROUP BY a.id, ow.owner_name, i.institution_name, i.institution_type;

COMMENT ON VIEW leitura.conta IS
    'Contas com saldo realizado hoje, vencido em aberto e saldo com todo o previsto. Tipos: conta, cartao_credito, aplicacao.';

CREATE VIEW leitura.categoria AS
SELECT
    c.id,
    c.category_name AS categoria,
    c.kind AS natureza,
    g.group_name AS grupo,
    COUNT(e.id) AS lancamentos,
    MIN(COALESCE(e.realized_date, e.due_date)) AS primeiro,
    MAX(COALESCE(e.realized_date, e.due_date)) AS ultimo
FROM cash_flow_category c
LEFT JOIN cash_flow_category_group g ON g.id = c.group_id
LEFT JOIN cash_flow_entry e ON e.category_id = c.id
GROUP BY c.id, g.group_name;

COMMENT ON VIEW leitura.categoria IS
    'Categorias. natureza: gerencial (receita/despesa de verdade), transferencia (entre contas próprias) ou movimentacao (aporte/resgate).';

CREATE VIEW leitura.orcamento_mensal AS
SELECT
    b.id,
    ow.owner_name AS titular,
    b.category_id AS categoria_id,
    c.category_name AS categoria,
    b.year AS ano,
    b.month AS mes,
    b.planned_amount AS planejado,
    b.active AS ativo
FROM monthly_budget b
JOIN cash_flow_category c ON c.id = b.category_id
JOIN account_owner ow ON ow.id = b.owner_id;

COMMENT ON VIEW leitura.orcamento_mensal IS
    'Orçamento (meta) por categoria, ano e mês, em moeda base (BRL). Só os registros com ativo = true valem.';

CREATE VIEW leitura.operacao AS
SELECT
    o.id,
    o.operation_key AS chave,
    o.operation_type AS tipo,
    o.description AS descricao,
    o.status,
    o.installment_total AS total_de_parcelas,
    o.recurrence_ended_on AS recorrencia_encerrada_em,
    o.created_at AS criada_em
FROM bank_operation o;

COMMENT ON VIEW leitura.operacao IS
    'Operações que agrupam lançamentos: parcelas, recorrências e transferências.';

CREATE VIEW leitura.extrato_importacao AS
SELECT
    b.id,
    b.account_id AS conta_id,
    a.account_name AS conta,
    a.account_kind AS tipo_conta,
    b.source_filename AS arquivo,
    b.row_count AS linhas,
    b.status,
    b.created_at AS importada_em,
    b.statement_balance AS saldo_do_extrato,
    b.statement_balance_date AS data_do_saldo_do_extrato
FROM bank_statement_import b
JOIN financial_account a ON a.id = b.account_id;

COMMENT ON VIEW leitura.extrato_importacao IS
    'Importações de extrato ou fatura. Em cartão de crédito, cada importação é uma fatura.';

CREATE VIEW leitura.extrato_linha AS
SELECT
    l.id,
    l.import_id AS importacao_id,
    l.account_id AS conta_id,
    l.matched_entry_id AS lancamento_conciliado_id,
    l.statement_date AS data,
    l.description AS descricao,
    l.amount AS valor,
    l.status,
    l.bank_category AS categoria_do_banco,
    l.card_holder AS portador,
    l.installment_current AS parcela_atual,
    l.installment_total AS parcela_total,
    l.purchase_date AS data_da_compra
FROM bank_statement_line l;

COMMENT ON VIEW leitura.extrato_linha IS
    'Linhas importadas de extratos e faturas. valor tem sinal: negativo é saída. lancamento_conciliado_id liga ao lançamento.';

CREATE VIEW leitura.fechamento_mes AS
SELECT
    m.id,
    m.account_id AS conta_id,
    a.account_name AS conta,
    m.year AS ano,
    m.month AS mes,
    m.closing_balance AS saldo_final,
    m.closed_at AS fechado_em,
    m.reopened_at AS reaberto_em,
    m.active AS ativo
FROM account_month_close m
JOIN financial_account a ON a.id = m.account_id;

COMMENT ON VIEW leitura.fechamento_mes IS
    'Meses fechados por conta. Um mês fechado (ativo) não muda sem reabertura.';

CREATE VIEW leitura.lancamento_etiqueta AS
SELECT et.entry_id AS lancamento_id, t.tag_name AS etiqueta
FROM cash_flow_entry_tag et
JOIN management_tag t ON t.id = et.tag_id;

CREATE VIEW leitura.lancamento_projeto AS
SELECT ep.entry_id AS lancamento_id, p.project_name AS projeto
FROM cash_flow_entry_project ep
JOIN management_project p ON p.id = ep.project_id;
"""

REVERSE_SQL = "DROP SCHEMA IF EXISTS leitura CASCADE;"


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_patrimonio_v4_outbox"),
        ("accounts", "0010_remover_contas_em_analises"),
        ("banking", "0011_xp_investimentos_homologada"),
        ("transactions", "0009_grupos_de_categoria"),
        ("bank_statements", "0005_regra_com_instituicao_de_destino"),
        ("management", "0002_managementtag_active_monthlybudget_active_and_more"),
    ]

    operations = [migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL)]
