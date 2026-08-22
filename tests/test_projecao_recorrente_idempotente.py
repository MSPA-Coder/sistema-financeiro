"""A projecao pode ser reexecutada, e e por isso que nao pede confirmacao.

Estes testes protegem os invariantes da execucao manual: reexecutar nao duplica
ocorrencias nem ressuscita lacunas apagadas. Uma mudanca nesses invariantes
exige rever o contrato de repeticao sem confirmacao.

Sem banco, como o resto desta suite (ver `conftest.py`). Da para exercitar as
duas propriedades que importam porque `_extend_operation` decide pelas datas
que recebe, e o caminho que grava so e alcancado quando ha data faltando
dentro do horizonte -- que e justamente o caso que estes testes provam nao
ocorrer numa reexecucao.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from transactions.recurring_projection import (
    _extend_operation,
    recurring_projection_horizon_end,
)


@dataclass
class _Ocorrencia:
    """So o que `_extend_operation` le para decidir se ha algo a fazer."""

    due_date: date


def _serie(*meses: int, dia: int = 10, ano: int = 2026) -> list[_Ocorrencia]:
    return [_Ocorrencia(date(ano, mes, dia)) for mes in meses]


def test_horizonte_nao_se_move_dentro_do_mes():
    # A razao de fundo da idempotencia: o horizonte e ancorado no PRIMEIRO dia
    # do mes corrente, nao no dia de hoje. Dois cliques no mesmo mes miram
    # exatamente a mesma data final.
    assert recurring_projection_horizon_end(
        date(2026, 1, 5), horizon_months=3
    ) == recurring_projection_horizon_end(date(2026, 1, 20), horizon_months=3)


def test_horizonte_termina_no_ultimo_dia_do_mes_alvo():
    assert recurring_projection_horizon_end(date(2026, 1, 5), horizon_months=3) == date(
        2026, 4, 30
    )


def test_reexecutar_no_mesmo_mes_nao_gera_nada():
    horizonte = recurring_projection_horizon_end(date(2026, 1, 20), horizon_months=3)
    ja_projetado = _serie(1, 2, 3, 4)

    assert _extend_operation(ja_projetado, horizonte, date(2026, 1, 20)) == 0


def test_ocorrencia_apagada_a_mao_no_meio_nao_ressuscita():
    # Se o preenchimento voltasse atras, reexecutar desfaria uma exclusao
    # deliberada do usuario -- e a operacao deixaria de ser segura de repetir.
    # Marco falta de proposito.
    horizonte = recurring_projection_horizon_end(date(2026, 1, 20), horizon_months=3)
    com_lacuna = _serie(1, 2, 4)

    assert _extend_operation(com_lacuna, horizonte, date(2026, 1, 20)) == 0


def test_no_mes_seguinte_o_horizonte_avanca():
    # Controle positivo: sem isto, os testes acima passariam tambem se a
    # projecao nunca fizesse nada. O horizonte precisa andar, senao a
    # funcionalidade nao existe.
    janeiro = recurring_projection_horizon_end(date(2026, 1, 20), horizon_months=3)
    fevereiro = recurring_projection_horizon_end(date(2026, 2, 1), horizon_months=3)

    assert fevereiro > janeiro


def test_serie_curta_dentro_do_horizonte_tem_o_que_gerar():
    # O complemento do anterior, do lado de `_extend_operation`: com a serie
    # parando antes do horizonte existe trabalho pendente. Aqui so se confirma
    # que a decisao e "ha o que fazer" -- gerar de fato toca o banco, que esta
    # fora do orcamento desta suite.
    horizonte = recurring_projection_horizon_end(date(2026, 1, 20), horizon_months=3)
    curta = _serie(1)

    assert max(o.due_date for o in curta) < horizonte
