"""O símbolo da moeda vem da conta, não do código.

Enquanto toda conta era em real, `R$` fixo no filtro dava o mesmo resultado que
a moeda da conta -- e por isso o defeito ficaria invisível até a primeira conta
em dólar existir. Estes testes medem a diferença agora, antes disso.

Sem banco de propósito: o filtro é função pura sobre um valor e uma sigla.
"""

from __future__ import annotations

from decimal import Decimal

from django.template import Context, Template

from core.domain.finance import BASE_CURRENCY, CURRENCY_BRL, CURRENCY_USD
from core.templatetags.money_filters import money, money_signed


def _render(template_text: str, **contexto) -> str:
    return Template("{% load money_filters %}" + template_text).render(Context(contexto))


def test_sem_argumento_vale_a_moeda_base():
    """As telas que ainda não passam a moeda continuam corretas."""
    assert money(Decimal("1234.56")) == "R$ 1.234,56"
    assert BASE_CURRENCY == CURRENCY_BRL


def test_real_e_dolar_saem_com_simbolos_diferentes():
    assert money(Decimal("1234.56"), CURRENCY_BRL) == "R$ 1.234,56"
    assert money(Decimal("1234.56"), CURRENCY_USD) == "US$ 1.234,56"


def test_separador_continua_pt_br_em_qualquer_moeda():
    """Moeda muda o símbolo, não o formato: quem lê a tela é brasileiro."""
    assert money(Decimal("1234567.89"), CURRENCY_USD) == "US$ 1.234.567,89"


def test_valor_com_sinal_tambem_respeita_a_moeda():
    """O sinal vem antes do símbolo, separado -- formato do `SharedAuth`."""
    assert money_signed(Decimal("-10.50"), CURRENCY_USD) == "- US$ 10,50"
    assert money_signed(Decimal("10.50"), CURRENCY_USD) == "+ US$ 10,50"
    assert money_signed(Decimal("-10.50")) == "- R$ 10,50"


def test_moeda_desconhecida_cai_na_base_sem_estourar():
    """A tela não é lugar de descobrir erro de cadastro.

    Quem recusa sigla inválida é a `CheckConstraint` do banco; aqui, o pior
    resultado possível tem que ser um símbolo conservador, nunca um 500 no meio
    de uma tabela.
    """
    assert money(Decimal("1.00"), "EUR") == "R$ 1,00"
    assert money(Decimal("1.00"), None) == "R$ 1,00"
    assert money(Decimal("1.00"), "") == "R$ 1,00"


def test_filtro_aceita_a_moeda_como_argumento_no_template():
    """A forma usada nos templates: `{{ valor|money:conta.currency }}`."""
    saida = _render(
        "{{ valor|money:conta.currency }}",
        valor=Decimal("99.90"),
        conta=type("Conta", (), {"currency": CURRENCY_USD})(),
    )

    assert saida == "US$ 99,90"


def test_simbolo_sozinho_serve_de_rotulo_de_formulario():
    """`<input type="number">` não passa por `money`, mas o rótulo dele precisa
    dizer em que moeda se digita."""
    from core.templatetags.money_filters import currency_symbol

    assert currency_symbol(CURRENCY_BRL) == "R$"
    assert currency_symbol(CURRENCY_USD) == "US$"
    assert currency_symbol(None) == "R$"


def test_o_formulario_de_lancamento_nao_fixa_o_simbolo_no_html():
    """Os rótulos de valor traziam `(R$)` escrito no template. Com conta em
    dólar, isso ofereceria um valor em real para quem digita dólar."""
    from pathlib import Path

    formulario = (
        Path(__file__).resolve().parents[1] / "templates/transactions/_fields.html"
    ).read_text(encoding="utf-8")

    assert "(R$)" not in formulario
    assert 'data-currency-for="account"' in formulario
    assert 'data-currency-for="counterparty"' in formulario
    # A moeda de cada conta viaja na própria opção: é de lá que o JS lê.
    assert 'data-currency-symbol="{{ acc.currency|currency_symbol }}"' in formulario


def test_o_valor_do_destino_nasce_desabilitado_no_formulario():
    """Campo desabilitado não é enviado -- é o que garante que ele só chega ao
    servidor quando o JS o habilita, nas moedas diferentes. O servidor recusa
    de qualquer forma, mas a tela não deve depender disso para estar certa."""
    from pathlib import Path

    formulario = (
        Path(__file__).resolve().parents[1] / "templates/transactions/_fields.html"
    ).read_text(encoding="utf-8")
    script = (
        Path(__file__).resolve().parents[1] / "static/js/transactions.js"
    ).read_text(encoding="utf-8")

    assert 'name="counterparty_amount"' in formulario
    assert 'aria-label="Valor creditado no destino" disabled' in formulario
    assert "input.disabled = !crossCurrency;" in script


def test_telas_por_conta_passam_a_moeda_da_conta():
    """Contrato de tela: onde a linha é de uma conta só, o símbolo é o dela.

    O total que soma várias contas fica de fora de propósito -- somar moedas
    diferentes é assunto da etapa de agregação, e até lá um total só existe
    entre contas da mesma moeda.
    """
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "templates"
    esperado = {
        "tables/_accounts_table.html": "account.initial_balance|money:account.currency",
        "transactions/_table_body.html": "money_signed:t.account.currency",
        "transactions/_operations_table.html": "money_signed:entry.account.currency",
        "reports/partials/account_position_content.html": "money_signed:row.currency",
        "banking/_reconciliation_tables.html": "money_signed:line.account.currency",
        "settings/monthly_close.html": "close.closing_balance|money:close.account.currency",
    }

    faltando = [
        caminho
        for caminho, marca in esperado.items()
        if marca not in (raiz / caminho).read_text(encoding="utf-8")
    ]

    assert not faltando, f"telas por conta sem a moeda da conta: {faltando}"
