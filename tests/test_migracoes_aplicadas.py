"""As migrações aplicam de verdade, produzem o schema esperado e são reversíveis.

O QUE ESTE ARQUIVO ACRESCENTA A `test_schema_bootstrap.py`

Aquele arquivo lê os arquivos de migração e confere o grafo -- uma base, uma
cabeça, elos íntegros. É barato e pega uma classe real de erro. Mas ele não
aplica nada, e o próprio docstring diz isso: "verificacao manual obrigatoria".

O buraco que sobrava é o encadeamento descrito na §1 do
`LEVANTAMENTO_2026-09.md`: a CI não conseguia reprovar uma migração que falha
ao ser executada, o `deploy.sh` a aplicaria em produção, e o rollback
automático reverte código e imagem mas **não reverte migração**. Uma migração
com SQL inválido, coluna `NOT NULL` acrescentada a tabela com linhas ou
dependência de extensão ausente passava verde daqui até o servidor.

Com a suíte falando com PostgreSQL, o simples ato de existir um teste marcado
com `django_db` faz o pytest-django construir o banco de teste aplicando TODAS
as migrações a partir do zero. Este arquivo torna isso explícito e acrescenta
as duas perguntas que a aplicação sozinha não responde.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader

pytestmark = pytest.mark.django_db


# Constraints que são contrato de domínio, não detalhe de implementação. Se uma
# migração futura derrubar qualquer uma delas, o `AGENTS.md` está mentindo e
# este teste avisa antes do deploy.
CONSTRAINTS_ESPERADAS = {
    "ck_cash_flow_entry_amount_positive",
    "ck_cash_flow_entry_status_valid",
    "ck_cash_flow_entry_type_valid",
    "ck_cash_flow_entry_realized_amount_positive",
    "ck_account_month_close_month_range",
    "ck_account_month_close_year_range",
    "uq_account_month_close_account_period",
    "ck_financial_institution_type_valid",
}


def test_migracoes_aplicaram_e_criaram_as_tabelas():
    """O banco de teste nasceu de `migrate` num PostgreSQL vazio.

    Se qualquer migração falhasse ao ser executada, nem este teste chegaria a
    rodar -- o pytest-django aborta na construção do banco. A asserção abaixo
    fecha o outro lado: as migrações rodaram *e* produziram o schema esperado,
    não apenas terminaram sem erro.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        tabelas = {linha[0] for linha in cursor.fetchall()}

    esperadas = {
        "cash_flow_entry",
        "account_month_close",
        "financial_account",
        "financial_institution",
        "bank_operation",
        "cash_flow_category",
    }
    faltando = esperadas - tabelas
    assert not faltando, f"migrações não criaram: {sorted(faltando)}"


def test_constraints_de_dominio_existem_no_banco():
    """As garantias declaradas nos modelos chegaram ao PostgreSQL.

    `CheckConstraint` em `Meta` só vale se a migração correspondente tiver sido
    gerada e aplicada. Declarar no modelo e esquecer de migrar deixa o código
    parecendo protegido e o banco aceitando qualquer coisa.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT conname FROM pg_constraint "
            "WHERE connamespace = 'public'::regnamespace"
        )
        existentes = {linha[0] for linha in cursor.fetchall()}

    faltando = CONSTRAINTS_ESPERADAS - existentes
    assert not faltando, (
        f"declaradas nos modelos mas ausentes no banco: {sorted(faltando)}. "
        "Provavelmente falta gerar ou aplicar uma migração."
    )


def test_modelos_e_migracoes_estao_em_dia():
    """Nenhuma mudança de modelo sem migração correspondente.

    Este é o defeito mais comum e mais silencioso do Django: alguém edita um
    campo, os testes passam (o banco de teste é construído a partir dos
    modelos... não -- a partir das MIGRAÇÕES), e a divergência só aparece no
    `migrate` do deploy, com o schema real fora de sincronia com o código.

    `makemigrations --check` responde exatamente isso e sai diferente de zero
    quando há mudança pendente.
    """
    try:
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)
    except SystemExit as exc:  # pragma: no cover - só ocorre quando há pendência
        pytest.fail(
            "Há mudança de modelo sem migração gerada. "
            "Rode `manage.py makemigrations` e revise o arquivo produzido. "
            f"(código de saída {exc.code})"
        )


def test_toda_migracao_e_reversivel():
    """Um rollback de schema é possível, mesmo que ninguém o execute aqui.

    O `deploy.sh` avisa, com todas as letras, que o rollback automático não
    desfaz migração. A consequência prática é que reverter schema é um
    procedimento MANUAL -- e um procedimento manual só existe se as migrações
    forem reversíveis.

    Uma `RunPython` sem função de reversão, ou uma `RunSQL` sem `reverse_sql`,
    torna o caminho de volta impossível e ninguém percebe até precisar dele, às
    duas da manhã. Este teste não executa o `downgrade`: ele confere que ele
    seria possível.
    """
    loader = MigrationLoader(connection, ignore_no_migrations=True)

    irreversiveis: list[str] = []
    for (app, nome), migracao in loader.disk_migrations.items():
        # Só os aplicativos deste repositório: migração de biblioteca de
        # terceiro não é responsabilidade nossa nem está sob nosso controle.
        if app not in {
            "accounts",
            "banking",
            "bank_statements",
            "core",
            "dashboard",
            "management",
            "reports",
            "transactions",
        }:
            continue
        for operacao in migracao.operations:
            if not operacao.reversible:
                irreversiveis.append(f"{app}/{nome}: {operacao.__class__.__name__}")

    assert not irreversiveis, (
        "Migrações sem caminho de volta:\n  "
        + "\n  ".join(irreversiveis)
        + "\nDê a `RunPython` uma função de reversão (ou "
        "`migrations.RunPython.noop`, se a reversão for mesmo um no-op) e a "
        "`RunSQL` um `reverse_sql`."
    )
