"""A autenticação das rotas de patrimônio e o contrato v4.

ESTE ARQUIVO PROTEGE UM CONTRATO, NÃO UMA TELA

O que sai destas rotas é lido por outro deployable, escrito em outro momento,
por alguém que não vai ler este código. Contrato quebrado não dá erro: dá número
errado do outro lado.

E as rotas carregam o saldo de todas as contas: os testes de autenticação são
sobre isso, não sobre formalidade. Os contratos v1 a v3 (resumo, fluxos,
atividades, categorias e metadata) foram retirados em 03/10/2026; a projeção
(`/patrimonio/v3/projection`) fica e é a rota do token legado
(`PATRIMONIO_TOKEN`), então é por ela que os testes de configuração do segredo
passam. O corpo da projeção é coberto em `test_projecao_patrimonial.py`.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

TOKEN = "token-de-teste-com-mais-de-trinta-e-dois-caracteres"
TOKEN_V4 = "token-de-integracao-v4-com-mais-de-trinta-e-dois-caracteres"
TOKEN_V4_ROTACIONADO = "novo-token-de-integracao-v4-apos-rotacao-segura"
ROTA = "/patrimonio/v3/projection"


@pytest.fixture
def contas():
    owner = AccountOwner.objects.create(name="Mariano")
    banco = FinancialInstitution.objects.create(institution_name="C6", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(
        institution_name="Avenue", institution_type="Corretora"
    )
    em_reais = FinancialAccount.objects.create(
        owner=owner, institution=banco, account_name="Conta corrente",
        initial_balance=Decimal("100.00"), currency="BRL",
        initial_balance_date=date(2025, 12, 31),
    )
    em_dolar = FinancialAccount.objects.create(
        owner=owner, institution=corretora, account_name="Conta em dólar",
        initial_balance=Decimal("1250.00"), currency="USD",
        initial_balance_date=date(2025, 12, 31),
    )
    categoria = CashFlowCategory.objects.create(category_name="Salário")
    CashFlowEntry.objects.create(
        account=em_reais, category=categoria, entry_type=ENTRY_TYPE_INCOME,
        description="Salário", entry_amount=Decimal("900.00"),
        due_date=date(2026, 1, 10), realized_date=date(2026, 1, 10),
        realized_amount=Decimal("900.00"), status=STATUS_REALIZED,
    )
    CashFlowEntry.objects.create(
        account=em_reais, category=categoria, entry_type=ENTRY_TYPE_EXPENSE,
        description="Conta de luz", entry_amount=Decimal("50.00"),
        due_date=date(2026, 2, 10), realized_date=date(2026, 2, 10),
        realized_amount=Decimal("50.00"), status=STATUS_REALIZED,
    )
    return {"em_reais": em_reais, "em_dolar": em_dolar}


@pytest.fixture
def com_token(monkeypatch, tmp_path):
    """Concede o token pela forma que vale em produção: arquivo montado.

    Sob o Compose, `REQUIRE_FILE_SECRETS=true` recusa a variável direta. Um
    teste que só exercitasse a variável passaria aqui e a rota responderia 503
    no servidor, que é o pior lugar para descobrir isso.
    """
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))
    return TOKEN


def pedir(token: str | None = TOKEN, **parametros):
    cabecalhos = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token is not None else {}
    return Client().get(ROTA, parametros, **cabecalhos)


def configurar_token_v4(monkeypatch, tmp_path, token: str = TOKEN_V4):
    arquivo = tmp_path / "patrimonio_integration_token"
    arquivo.write_text(token, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO_V4, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(arquivo))
    return token


def test_cursor_v4_usa_token_exclusivo_e_invalida_assinaturas_antigas(
    monkeypatch, tmp_path
):
    legado = tmp_path / "patrimonio_token"
    exclusivo = tmp_path / "patrimonio_integration_token"
    rotacionado = tmp_path / "patrimonio_integration_token_rotacionado"
    legado.write_text(TOKEN, encoding="utf-8")
    exclusivo.write_text(TOKEN_V4, encoding="utf-8")
    rotacionado.write_text(TOKEN_V4_ROTACIONADO, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(legado))
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO_V4, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(exclusivo))

    # O HMAC binário pode conter o ponto usado como separador. O parser deve
    # usar o tamanho fixo da assinatura, não procurar o último ponto.
    for position in range(200):
        cursor = patrimonio._cursor_v4(position)
        assert patrimonio._cursor_v4_ler(cursor) == position

    cursor_v4 = patrimonio._cursor_v4(41)
    assert patrimonio._cursor_v4_ler(cursor_v4) == 41

    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(rotacionado))
    with pytest.raises(ValueError, match="cursor inválido"):
        patrimonio._cursor_v4_ler(cursor_v4)

    # Simula um cursor emitido pela implementação anterior, assinada pelo
    # token legado. A chave exclusiva atual não deve aceitá-lo.
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(legado))
    cursor_legado = patrimonio._cursor_v4(42)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(exclusivo))
    with pytest.raises(ValueError, match="cursor inválido"):
        patrimonio._cursor_v4_ler(cursor_legado)


# --- A chave é a permissão -------------------------------------------------


def test_sem_token_nao_ha_resposta(contas, com_token):
    resposta = pedir(token=None)

    assert resposta.status_code == 401
    assert "contas" not in resposta.json()


def test_token_errado_nao_ha_resposta(contas, com_token):
    resposta = pedir(token="token-errado-mas-do-mesmo-tamanho-que-o-certo")

    assert resposta.status_code == 401
    assert "contas" not in resposta.json()


def test_sem_segredo_configurado_a_rota_nao_atende(contas, monkeypatch):
    """"Não configurado" não é "não autorizado".

    Responder 401 aqui mandaria o operador procurar por horas um token errado
    que, na verdade, nunca foi concedido a este servidor.
    """
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.delenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", raising=False)

    resposta = pedir()

    assert resposta.status_code == 503


def test_segredo_curto_demais_e_tratado_como_ausente(contas, monkeypatch, tmp_path):
    """Um token de oito letras não protege saldo de conta nenhuma."""
    arquivo = tmp_path / "patrimonio_token"
    arquivo.write_text("curtinho", encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo))

    resposta = pedir(token="curtinho")

    assert resposta.status_code == 503


def test_sob_o_compose_a_variavel_direta_nao_concede_o_token(contas, monkeypatch):
    """`REQUIRE_FILE_SECRETS=true` é o contrato do Compose, e vale aqui também.

    Sem esta trava, uma sobra de `PATRIMONIO_TOKEN` no ambiente do processo
    substituiria em silêncio o segredo montado como arquivo -- e ninguém
    descobriria qual dos dois está valendo.
    """
    monkeypatch.setenv("REQUIRE_FILE_SECRETS", "true")
    monkeypatch.setenv(patrimonio.NOME_DO_SEGREDO, TOKEN)
    monkeypatch.delenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", raising=False)

    resposta = pedir()

    assert resposta.status_code == 503


def test_a_resposta_nao_pode_ser_guardada_por_intermediario(contas, com_token):
    resposta = pedir()

    assert resposta["Cache-Control"] == "no-store"


def test_a_rota_so_responde_a_get(contas, com_token):
    resposta = Client().post(ROTA, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    assert resposta.status_code == 405


# --- O v4 tem token próprio ------------------------------------------------


def test_v4_nao_usa_o_token_das_rotas_anteriores(contas, monkeypatch, tmp_path):
    arquivo_legado = tmp_path / "patrimonio_token"
    arquivo_legado.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo_legado))
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO_V4, raising=False)
    monkeypatch.delenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", raising=False)

    resposta_v4 = Client().get(
        "/patrimonio/v4/metadata", HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
    )
    resposta_legada = pedir()

    assert resposta_v4.status_code == 503
    assert resposta_legada.status_code == 200


def test_v4_recusa_o_token_das_rotas_anteriores(contas, monkeypatch, tmp_path):
    arquivo_legado = tmp_path / "patrimonio_token"
    arquivo_legado.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo_legado))
    configurar_token_v4(monkeypatch, tmp_path)

    resposta = Client().get(
        "/patrimonio/v4/metadata", HTTP_AUTHORIZATION=f"Bearer {TOKEN}"
    )

    assert resposta.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_v4_aceita_token_de_integracao_sem_mudar_autenticacao_legada(
    contas, monkeypatch, tmp_path
):
    arquivo_legado = tmp_path / "patrimonio_token"
    arquivo_legado.write_text(TOKEN, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO}_FILE", str(arquivo_legado))
    token_v4 = configurar_token_v4(monkeypatch, tmp_path)

    resposta_v4 = Client().get(
        "/patrimonio/v4/snapshot", HTTP_AUTHORIZATION=f"Bearer {token_v4}"
    )

    assert resposta_v4.status_code == 200
    assert resposta_v4.json()["contrato"] == "patrimonio/v4"


# --- O vocabulário ---------------------------------------------------------


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [
        ("Mercado Pago", "mercado-pago"),
        ("mercado  pago", "mercado-pago"),
        ("Itaú", "itau"),
        ("Banco do Brasil", "banco-do-brasil"),
        ("C6", "c6"),
    ],
)
def test_identidade_normaliza_o_nome(nome, esperado):
    assert patrimonio.identidade(nome) == esperado


# --- O status efetivo ------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_v4_publica_o_status_efetivo_derivado_da_data_e_nao_do_gravado(
    contas, monkeypatch, tmp_path
):
    """O status gravado envelhece: em aberto com vencimento passado segue `a_vencer`
    até alguém gravar o lançamento. O contrato publica o que vale hoje, para que
    nenhum consumidor repita a conta (o FinancasMCP a repetia em SQL)."""
    hoje = timezone.localdate()
    categoria = CashFlowCategory.objects.create(category_name="Contas do mês")
    conta = contas["em_reais"]

    def lancar(descricao, dias, status, realizado=False):
        vencimento = hoje + timedelta(days=dias)
        return CashFlowEntry.objects.create(
            account=conta, category=categoria, entry_type=ENTRY_TYPE_EXPENSE,
            description=descricao, entry_amount=Decimal("10.00"), due_date=vencimento,
            status=status,
            realized_date=vencimento if realizado else None,
            realized_amount=Decimal("10.00") if realizado else None,
        )

    lancar("vencido que o banco ainda chama de a vencer", -1, STATUS_PROJECTED)
    lancar("vence hoje", 0, STATUS_PROJECTED)
    lancar("vence amanha", 1, STATUS_PROJECTED)
    lancar("vencido gravado como vencido", -5, patrimonio.STATUS_PENDING)
    lancar("gravado como vencido mas com prazo ainda por vir", 3, patrimonio.STATUS_PENDING)
    lancar("pago ha 30 dias", -30, STATUS_REALIZED, realizado=True)

    token_v4 = configurar_token_v4(monkeypatch, tmp_path)
    resposta = Client().get("/patrimonio/v4/snapshot", HTTP_AUTHORIZATION=f"Bearer {token_v4}")

    assert resposta.status_code == 200
    efetivo = {
        item["payload"]["description"]: (item["payload"]["status"], item["payload"]["effective_status"])
        for item in resposta.json()["items"]
        if item["resource"] == "cash_entry"
    }
    assert efetivo["vencido que o banco ainda chama de a vencer"] == (STATUS_PROJECTED, "vencidos")
    assert efetivo["vence hoje"] == (STATUS_PROJECTED, "a_vencer")
    assert efetivo["vence amanha"] == (STATUS_PROJECTED, "a_vencer")
    assert efetivo["vencido gravado como vencido"] == ("vencidos", "vencidos")
    assert efetivo["gravado como vencido mas com prazo ainda por vir"] == ("vencidos", "a_vencer")
    assert efetivo["pago ha 30 dias"] == (STATUS_REALIZED, "realizado")
    # Os lançamentos da fixture são realizados e continuam realizados.
    assert efetivo["Salário"] == (STATUS_REALIZED, "realizado")
