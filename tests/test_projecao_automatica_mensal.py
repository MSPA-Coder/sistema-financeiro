"""A execucao automatica existe, roda uma vez por mes, e nao a cada requisicao.

Ate 2026-08-22 o campo "Dia de execucao automatica" da tela de Parametros era
salvo, exibido e confirmado por mensagem ("execucao no dia N") -- e nao havia
execucao automatica alguma: nem agendador no codigo, nem comando de management,
nem cron ou timer no servidor. A projecao so andava por clique.

Estes testes cobrem as tres coisas que podem dar errado numa funcionalidade
dessas, e que nao dao sintoma quando dao:

- nao executar nunca (o caso do dia 31, abaixo);
- executar mais de uma vez no mes;
- executar a cada requisicao, martelando o banco.

Sem banco: as duas dependencias que tocam o Postgres sao substituidas, e o que
se mede e a DECISAO -- que e onde mora a regra.
"""

from __future__ import annotations

from datetime import date

import pytest

from transactions import middleware as mw
from transactions import recurring_projection as rp

# ---------------------------------------------------------------------------
# dia efetivo -- o caso que quebraria com o valor PADRAO
# ---------------------------------------------------------------------------


def test_dia_31_vira_o_ultimo_dia_nos_meses_curtos():
    # `DEFAULT_PROJECTION_RUN_DAY` e 31. Com `hoje.day >= run_day` cru, a
    # execucao automatica nunca aconteceria em fevereiro, abril, junho,
    # setembro e novembro -- para quem nunca mexeu na configuracao.
    assert rp.dia_efetivo_de_execucao(31, date(2026, 2, 10)) == 28
    assert rp.dia_efetivo_de_execucao(31, date(2024, 2, 10)) == 29, "ano bissexto"
    assert rp.dia_efetivo_de_execucao(31, date(2026, 4, 10)) == 30
    assert rp.dia_efetivo_de_execucao(31, date(2026, 3, 10)) == 31


def test_dia_normal_nao_e_alterado():
    assert rp.dia_efetivo_de_execucao(10, date(2026, 2, 10)) == 10
    assert rp.dia_efetivo_de_execucao(1, date(2026, 12, 31)) == 1


# ---------------------------------------------------------------------------
# ja rodou neste mes?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gravado,esperado",
    [
        ("2026-01-20T10:30:00", True),
        ("2026-01-01", True),
        ("2025-12-31T23:59:59", False),
        ("2026-02-01", False),
        (None, False),
        ("", False),
        ("nao e data", False),
    ],
)
def test_ja_executada_no_mes(gravado, esperado):
    assert rp.was_projection_run_in_month(gravado, date(2026, 1, 20)) is esperado


# ---------------------------------------------------------------------------
# a decisao
# ---------------------------------------------------------------------------


class _Config:
    def __init__(self, run_day=31, last=None):
        self.run_day = run_day
        self.horizon_months = 6
        self.last_projection_run = last


@pytest.fixture
def projecao(monkeypatch):
    """Substitui a projecao de verdade e registra cada execucao."""
    chamadas: list[date] = []

    def _falsa(*, today=None, horizon_months=None, update_last_run=True):
        chamadas.append(today)
        return rp.RecurringProjectionResult(
            horizon_end=date(2026, 7, 31), generated_count=3, processed_operations=1
        )

    monkeypatch.setattr(rp, "ensure_recurring_projection_horizon", _falsa)
    return chamadas


def _configurar(monkeypatch, config):
    monkeypatch.setattr(rp, "get_recurring_projection_settings", lambda: config)


def test_executa_no_dia_marcado(monkeypatch, projecao):
    _configurar(monkeypatch, _Config(run_day=10))

    decisao = rp.executar_projecao_mensal_se_devido(date(2026, 1, 10))

    assert decisao.executou is True
    assert decisao.mes_resolvido is True
    assert projecao == [date(2026, 1, 10)]


def test_nao_executa_antes_do_dia_marcado(monkeypatch, projecao):
    _configurar(monkeypatch, _Config(run_day=10))

    decisao = rp.executar_projecao_mensal_se_devido(date(2026, 1, 9))

    assert decisao.executou is False
    # Nao resolvido: precisa voltar a olhar quando o dia chegar.
    assert decisao.mes_resolvido is False
    assert projecao == []


def test_nao_executa_duas_vezes_no_mesmo_mes(monkeypatch, projecao):
    _configurar(monkeypatch, _Config(run_day=10, last="2026-01-10T08:00:00"))

    decisao = rp.executar_projecao_mensal_se_devido(date(2026, 1, 25))

    assert decisao.executou is False
    assert decisao.mes_resolvido is True
    assert projecao == []


def test_executa_de_novo_no_mes_seguinte(monkeypatch, projecao):
    _configurar(monkeypatch, _Config(run_day=10, last="2026-01-10T08:00:00"))

    decisao = rp.executar_projecao_mensal_se_devido(date(2026, 2, 10))

    assert decisao.executou is True
    assert projecao == [date(2026, 2, 10)]


def test_dia_31_executa_no_ultimo_dia_de_fevereiro(monkeypatch, projecao):
    # Amarra o caso do valor padrao a um comportamento observavel, e nao so a
    # aritmetica da funcao auxiliar.
    _configurar(monkeypatch, _Config(run_day=31))

    assert rp.executar_projecao_mensal_se_devido(date(2026, 2, 27)).executou is False
    assert rp.executar_projecao_mensal_se_devido(date(2026, 2, 28)).executou is True


# ---------------------------------------------------------------------------
# o middleware -- custo por requisicao
# ---------------------------------------------------------------------------


class _Usuario:
    def __init__(self, autenticado=True):
        self.is_authenticated = autenticado


class _Requisicao:
    def __init__(self, autenticado=True):
        self.user = _Usuario(autenticado)


class _Relogio:
    def __init__(self, hoje):
        self.hoje = hoje

    def today(self):
        return self.hoje


@pytest.fixture
def middleware(monkeypatch):
    """Middleware com a decisao substituida, contando quantas vezes consulta."""
    chamadas: list[date] = []
    resposta = {"mes_resolvido": True}

    class _Decisao:
        def __init__(self, mes_resolvido):
            self.mes_resolvido = mes_resolvido
            self.executou = False
            self.motivo = "teste"

    def _falsa(hoje):
        chamadas.append(hoje)
        return _Decisao(resposta["mes_resolvido"])

    monkeypatch.setattr(mw, "executar_projecao_mensal_se_devido", _falsa)
    instancia = mw.ProjecaoRecorrenteMensalMiddleware(lambda _req: "resposta")
    return instancia, chamadas, resposta


def _relogio(monkeypatch, dia):
    monkeypatch.setattr(mw, "date", _Relogio(dia))


def test_middleware_verifica_uma_vez_e_devolve_a_resposta(monkeypatch, middleware):
    instancia, chamadas, _ = middleware
    _relogio(monkeypatch, date(2026, 1, 15))

    assert instancia(_Requisicao()) == "resposta"
    assert chamadas == [date(2026, 1, 15)]


def test_middleware_nao_consulta_de_novo_no_resto_do_mes(monkeypatch, middleware):
    instancia, chamadas, _ = middleware
    _relogio(monkeypatch, date(2026, 1, 15))
    instancia(_Requisicao())

    for dia in (16, 17, 31):
        _relogio(monkeypatch, date(2026, 1, dia))
        instancia(_Requisicao())

    assert chamadas == [date(2026, 1, 15)], "mes resolvido nao se reconsulta"


def test_middleware_volta_a_verificar_no_mes_seguinte(monkeypatch, middleware):
    instancia, chamadas, _ = middleware
    _relogio(monkeypatch, date(2026, 1, 15))
    instancia(_Requisicao())

    _relogio(monkeypatch, date(2026, 2, 1))
    instancia(_Requisicao())

    assert chamadas == [date(2026, 1, 15), date(2026, 2, 1)]


def test_middleware_verifica_no_maximo_uma_vez_por_dia(monkeypatch, middleware):
    instancia, chamadas, resposta = middleware
    resposta["mes_resolvido"] = False  # antes do dia marcado

    _relogio(monkeypatch, date(2026, 1, 5))
    for _ in range(20):
        instancia(_Requisicao())
    assert chamadas == [date(2026, 1, 5)], "uma consulta por dia, nao por requisicao"

    _relogio(monkeypatch, date(2026, 1, 6))
    instancia(_Requisicao())
    assert len(chamadas) == 2, "no dia seguinte volta a olhar"


def test_middleware_ignora_requisicao_sem_sessao(monkeypatch, middleware):
    # A sonda de saude e a tela de login batem aqui o tempo todo. Amarrar
    # `/health` a uma escrita no banco seria trocar um sinal por um risco.
    instancia, chamadas, _ = middleware
    _relogio(monkeypatch, date(2026, 1, 15))

    assert instancia(_Requisicao(autenticado=False)) == "resposta"
    assert chamadas == []


def test_middleware_nao_derruba_a_pagina_se_a_projecao_falhar(monkeypatch, middleware):
    instancia, _, _ = middleware
    _relogio(monkeypatch, date(2026, 1, 15))

    def _explode(_hoje):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(mw, "executar_projecao_mensal_se_devido", _explode)

    # A projecao e trabalho de fundo: falhar nela nao pode custar a pagina que
    # o usuario pediu. Sumir do log e que nao pode.
    assert instancia(_Requisicao()) == "resposta"


def test_middleware_tenta_de_novo_depois_de_falhar(monkeypatch, middleware):
    instancia, chamadas, _ = middleware

    def _explode(_hoje):
        raise RuntimeError("banco fora")

    monkeypatch.setattr(mw, "executar_projecao_mensal_se_devido", _explode)
    _relogio(monkeypatch, date(2026, 1, 15))
    instancia(_Requisicao())

    # Nada foi marcado como resolvido, entao no dia seguinte tenta de novo --
    # uma falha transitoria de banco nao pode custar o mes inteiro.
    def _funciona(hoje):
        chamadas.append(hoje)
        return rp.DecisaoProjecaoMensal(True, True, "executada")

    monkeypatch.setattr(mw, "executar_projecao_mensal_se_devido", _funciona)
    _relogio(monkeypatch, date(2026, 1, 16))
    instancia(_Requisicao())

    assert chamadas == [date(2026, 1, 16)]
