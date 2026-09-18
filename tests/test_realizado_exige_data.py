"""Lançamento realizado precisa de data de realização.

POR QUE ESTE ARQUIVO EXISTE

O saldo realizado é filtrado por `realized_date` (`_balance_date_expr` em
`reports/services.py`). Um lançamento com status `realizado` e sem data não
cai em nenhum intervalo, então some de todas as telas de saldo realizado -- e
nada avisa. O ensaio de 17/09/2026 com uma cópia do banco de produção achou um
assim, o #1236, criado pela tela em 15/08, antes de o extrato daquela conta ser
importado.

A tela não exigia a data e o serviço copiava `req.realized_date` sem conferir.
O formulário agora marca o campo como obrigatório, mas isso é conveniência
de quem digita: um POST direto não passa pelo JavaScript. A recusa mora no serviço,
porque ele é a porta única: a tela e a criação a partir do extrato passam por
`create_transaction_batch`, e a edição passa por `update_transaction_operation`.
Os testes por HTTP provam que a tela chega a essa recusa, e não a contorna.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection

from accounts.models import AccountOwner
from accounts.services import save_transfer_destination_accesses
from banking.models import FinancialAccount, FinancialInstitution
from core.domain.finance import (
    CALC_DIVIDE,
    CATEGORY_KIND_TRANSFER,
    CURRENCY_USD,
    ENTRY_TYPE_EXPENSE,
    OPERATION_SCOPE_CURRENT_FUTURE,
    OPERATION_SCOPE_SINGLE,
    STATUS_PENDING,
    STATUS_REALIZED,
)
from core.domain.identity import USER_TYPE_ADMINISTRATOR
from transactions import services
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry

pytestmark = pytest.mark.django_db

RECUSA = "data de realização"
DIA = date(2026, 6, 3)


@pytest.fixture
def cenario():
    # `administrator` enxerga todos os titulares, mas a conta destino de uma
    # transferência pede concessão própria mesmo assim.
    usuario = get_user_model().objects.create_user(
        username="teste-realizado-sem-data",
        password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )
    titular = AccountOwner.objects.create(name="Titular de teste")
    corretora = FinancialInstitution.objects.create(
        institution_name="Corretora de teste", institution_type="Corretora"
    )
    conta = FinancialAccount.objects.create(
        owner=titular, institution=corretora, account_name="Conta de teste"
    )
    destino = FinancialAccount.objects.create(
        owner=titular, institution=corretora, account_name="Conta destino"
    )
    em_dolar = FinancialAccount.objects.create(
        owner=titular, institution=corretora, account_name="Conta em dólar", currency=CURRENCY_USD
    )
    save_transfer_destination_accesses(usuario, {destino.id, em_dolar.id})
    return SimpleNamespace(
        usuario=usuario,
        conta=conta,
        destino=destino,
        em_dolar=em_dolar,
        despesa=CashFlowCategory.objects.create(category_name="Corretagem"),
        transferencia=CashFlowCategory.objects.create(
            category_name="Transferência", kind=CATEGORY_KIND_TRANSFER
        ),
    )


def _requisicao(cenario, **campos):
    """O que a tela manda para um realizado cujo campo de data ficou vazio."""
    padrao = {
        "account_id": cenario.conta.id,
        "category_id": cenario.despesa.id,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Corretagem Executor - Btc",
        "entry_amount": Decimal("100.35"),
        "installments": 1,
        "due_date": DIA,
        "status": STATUS_REALIZED,
    }
    padrao.update(campos)
    return services.TransactionRequest(**padrao)


def _transferencia(cenario, **campos):
    campos.setdefault("counterparty_account_id", cenario.destino.id)
    return _requisicao(cenario, category_id=cenario.transferencia.id, **campos)


def _criar(cenario, req):
    return services.create_transaction_batch(req, user=cenario.usuario)


# --- Criação -----------------------------------------------------------------


@pytest.mark.parametrize(
    "extras",
    [
        pytest.param({}, id="unico"),
        pytest.param({"installments": 3}, id="parcelado"),
        pytest.param({"is_recurring": True}, id="recorrente"),
    ],
)
def test_criar_realizado_sem_data_e_recusado(cenario, extras):
    with pytest.raises(ValueError, match=RECUSA):
        _criar(cenario, _requisicao(cenario, **extras))

    assert CashFlowEntry.objects.count() == 0
    assert BankOperation.objects.count() == 0


def test_criar_transferencia_realizada_sem_data_recusa_as_duas_pontas(cenario):
    with pytest.raises(ValueError, match=RECUSA):
        _criar(cenario, _transferencia(cenario))

    assert CashFlowEntry.objects.count() == 0
    assert BankOperation.objects.count() == 0


def test_valor_realizado_sozinho_nao_substitui_a_data(cenario):
    """O valor preenchido não torna o lançamento visível: quem filtra é a data."""
    with pytest.raises(ValueError, match=RECUSA):
        _criar(cenario, _requisicao(cenario, realized_amount=Decimal("100.35")))

    assert CashFlowEntry.objects.count() == 0


def test_criar_realizado_com_data_continua_funcionando(cenario):
    """Controle: a recusa não pode ter alcançado o caminho que sempre esteve certo."""
    [entry] = _criar(cenario, _requisicao(cenario, realized_date=DIA))

    entry.refresh_from_db()
    assert entry.status == STATUS_REALIZED
    assert entry.realized_date == DIA


# --- Edição ------------------------------------------------------------------


def test_editar_para_realizado_sem_data_e_recusado(cenario):
    [entry] = _criar(cenario, _requisicao(cenario, status=STATUS_PENDING))

    with pytest.raises(ValueError, match=RECUSA):
        services.update_transaction_operation(
            entry, _requisicao(cenario), OPERATION_SCOPE_SINGLE, user=cenario.usuario
        )

    entry.refresh_from_db()
    assert entry.status == STATUS_PENDING
    assert entry.realized_date is None


def test_apagar_a_data_de_um_realizado_e_recusado(cenario):
    """A tela reapresenta a data gravada; esvaziar o campo não pode apagá-la."""
    [entry] = _criar(cenario, _requisicao(cenario, realized_date=DIA))

    with pytest.raises(ValueError, match=RECUSA):
        services.update_transaction_operation(
            entry, _requisicao(cenario), OPERATION_SCOPE_SINGLE, user=cenario.usuario
        )

    entry.refresh_from_db()
    assert entry.status == STATUS_REALIZED
    assert entry.realized_date == DIA


def test_editar_transferencia_para_realizada_sem_data_e_recusado(cenario):
    origem, destino = _criar(cenario, _transferencia(cenario, status=STATUS_PENDING))

    with pytest.raises(ValueError, match=RECUSA):
        services.update_transaction_operation(
            origem, _transferencia(cenario), OPERATION_SCOPE_SINGLE, user=cenario.usuario
        )

    for ponta in (origem, destino):
        ponta.refresh_from_db()
        assert ponta.status == STATUS_PENDING
        assert ponta.realized_date is None


def test_editar_parcelas_para_realizado_sem_data_nao_recria_o_bloco(cenario):
    """O escopo "este registro e os próximos" apaga e recria o bloco.

    É o caminho em que uma gravação sem data custaria mais caro: o bloco
    inteiro renasceria realizado e invisível. Mede-se o resultado -- os mesmos
    ids de antes e nenhum realizado.
    """
    parcelas = _criar(cenario, _requisicao(cenario, status=STATUS_PENDING, installments=3))
    primeira = parcelas[0]
    token = services.current_future_confirmation_token(primeira.id)

    with pytest.raises(ValueError, match=RECUSA):
        services.update_transaction_operation(
            primeira,
            _requisicao(cenario, installments=3),
            OPERATION_SCOPE_CURRENT_FUTURE,
            token,
            user=cenario.usuario,
        )

    assert sorted(CashFlowEntry.objects.values_list("id", flat=True)) == sorted(
        p.id for p in parcelas
    )
    assert not CashFlowEntry.objects.filter(status=STATUS_REALIZED).exists()


# --- Valor realizado vazio ----------------------------------------------------
#
# Decisão do mantenedor (17/09/2026): valor realizado vazio grava o previsto,
# como o botão "Realizar" já fazia. O vazio era lido de dois jeitos -- o saldo
# usava o previsto, o planejamento anual usava zero --, e guardar o número
# acaba com a divergência na origem.


def _realizados(entries):
    for entry in entries:
        entry.refresh_from_db()
    return [(e.status, e.realized_date, e.realized_amount) for e in entries]


def test_valor_realizado_vazio_grava_o_previsto(cenario):
    entries = _criar(cenario, _requisicao(cenario, realized_date=DIA))

    assert _realizados(entries) == [(STATUS_REALIZED, DIA, Decimal("100.35"))]


def test_valor_realizado_informado_e_preservado(cenario):
    entries = _criar(
        cenario, _requisicao(cenario, realized_date=DIA, realized_amount=Decimal("99.90"))
    )

    assert _realizados(entries) == [(STATUS_REALIZED, DIA, Decimal("99.90"))]


def test_parcela_dividida_grava_o_previsto_dela_e_nao_o_total(cenario):
    """100,00 em três vira 33,33 + 33,33 + 33,34: a parcela realiza pelo seu.

    A última de propósito: é a única diferente das outras, então só ela separa
    "o previsto da parcela" de "um valor qualquer do grupo". E sozinha, no
    escopo "somente este", porque realizar é fato de uma parcela, não do
    parcelado.
    """
    dividido = {"entry_amount": Decimal("100.00"), "installments": 3, "calc_mode": CALC_DIVIDE}
    parcelas = _criar(cenario, _requisicao(cenario, status=STATUS_PENDING, **dividido))
    ultima = parcelas[-1]

    services.update_transaction_operation(
        ultima,
        _requisicao(
            cenario, due_date=ultima.due_date, realized_date=ultima.due_date, **dividido
        ),
        OPERATION_SCOPE_SINGLE,
        user=cenario.usuario,
    )

    assert _realizados([ultima]) == [(STATUS_REALIZED, ultima.due_date, Decimal("33.34"))]


def test_transferencia_entre_moedas_grava_o_previsto_de_cada_ponta(cenario):
    """Espelhar o valor da origem gravaria reais na conta em dólar."""
    origem, destino = _criar(
        cenario,
        _transferencia(
            cenario,
            counterparty_account_id=cenario.em_dolar.id,
            counterparty_amount=Decimal("18.50"),
            realized_date=DIA,
        ),
    )

    assert _realizados([origem, destino]) == [
        (STATUS_REALIZED, DIA, Decimal("100.35")),
        (STATUS_REALIZED, DIA, Decimal("18.50")),
    ]


def test_editar_para_realizado_sem_valor_grava_o_previsto(cenario):
    [entry] = _criar(cenario, _requisicao(cenario, status=STATUS_PENDING))

    services.update_transaction_operation(
        entry,
        _requisicao(cenario, realized_date=DIA),
        OPERATION_SCOPE_SINGLE,
        user=cenario.usuario,
    )

    assert _realizados([entry]) == [(STATUS_REALIZED, DIA, Decimal("100.35"))]


def test_editar_transferencia_para_realizada_sem_valor_grava_o_previsto_nas_duas_pontas(cenario):
    origem, destino = _criar(cenario, _transferencia(cenario, status=STATUS_PENDING))

    services.update_transaction_operation(
        origem,
        _transferencia(cenario, realized_date=DIA),
        OPERATION_SCOPE_SINGLE,
        user=cenario.usuario,
    )

    assert _realizados([origem, destino]) == [
        (STATUS_REALIZED, DIA, Decimal("100.35")),
        (STATUS_REALIZED, DIA, Decimal("100.35")),
    ]


def test_voltar_para_aberto_limpa_data_e_valor(cenario):
    """Controle do outro lado: aberto não guarda realização, mesmo que a requisição traga."""
    [entry] = _criar(cenario, _requisicao(cenario, realized_date=DIA))

    services.update_transaction_operation(
        entry,
        _requisicao(
            cenario, status=STATUS_PENDING, realized_date=DIA, realized_amount=Decimal("100.35")
        ),
        OPERATION_SCOPE_SINGLE,
        user=cenario.usuario,
    )

    assert _realizados([entry]) == [(STATUS_PENDING, None, None)]


# --- Pela tela ---------------------------------------------------------------


def _formulario(cenario, **campos):
    """O POST do formulário de lançamento com o status realizado escolhido."""
    dados = {
        "account_id": str(cenario.conta.id),
        "category_id": str(cenario.despesa.id),
        "entry_type": ENTRY_TYPE_EXPENSE,
        "description": "Corretagem Executor - Btc",
        "entry_amount": "100.35",
        "installments": "1",
        "calc_mode": "repeat",
        "due_date": DIA.isoformat(),
        "status": STATUS_REALIZED,
        "realized_date": "",
        "realized_amount": "",
    }
    dados.update(campos)
    return dados


def _mensagens(resposta):
    return [str(m) for m in get_messages(resposta.wsgi_request)]


# "03/06/2026" não é o formato que o `<input type="date">` envia, e a view o
# lê como ausente. O resultado precisa ser o mesmo do campo vazio: recusa.
@pytest.mark.parametrize("data_enviada", ["", "03/06/2026"])
def test_tela_nao_cria_realizado_sem_data(client, cenario, data_enviada):
    client.force_login(cenario.usuario)

    resposta = client.post("/transaction/", _formulario(cenario, realized_date=data_enviada))

    assert resposta.status_code == 302
    assert CashFlowEntry.objects.count() == 0
    assert any(RECUSA in m for m in _mensagens(resposta))


def test_tela_nao_edita_para_realizado_sem_data(client, cenario):
    [entry] = _criar(cenario, _requisicao(cenario, status=STATUS_PENDING))
    client.force_login(cenario.usuario)

    resposta = client.post(f"/transaction/{entry.id}/", _formulario(cenario))

    assert resposta.status_code == 302
    entry.refresh_from_db()
    assert entry.status == STATUS_PENDING
    assert entry.realized_date is None
    assert any(RECUSA in m for m in _mensagens(resposta))


# --- A trava da migration 0005 -------------------------------------------------


def test_migration_para_e_lista_os_realizados_incompletos(cenario, monkeypatch):
    """A migration não corrige dados: ela para e diz quais são.

    A constraint já existe no banco de teste, então ela sai durante o teste
    para reproduzir o banco de produção antes da correção. O PostgreSQL desfaz
    a remoção junto com o resto da transação do teste.

    A remoção vem antes de qualquer lançamento: depois de um INSERT, as FKs
    adiadas deixam gatilhos pendentes na tabela, e o PostgreSQL recusa o
    ALTER TABLE na mesma transação.
    """
    migracao = import_module("transactions.migrations.0005_realizado_exige_data")
    constraint = next(
        c for c in CashFlowEntry._meta.constraints
        if c.name == "ck_cash_flow_entry_realized_has_date_and_amount"
    )
    with connection.schema_editor() as editor:
        editor.remove_constraint(CashFlowEntry, constraint)

    # Com tudo completo, a conferência deixa passar.
    [completo] = _criar(cenario, _requisicao(cenario, realized_date=DIA))
    migracao.conferir_realizados(apps, None)

    base = {
        "account": cenario.conta,
        "category": cenario.despesa,
        "entry_type": ENTRY_TYPE_EXPENSE,
        "entry_amount": Decimal("100.35"),
        "due_date": DIA,
        "status": STATUS_REALIZED,
    }
    sem_nada = CashFlowEntry.objects.create(**base)
    sem_valor = CashFlowEntry.objects.create(**base, realized_date=DIA)
    sem_data = CashFlowEntry.objects.create(**base, realized_amount=Decimal("100.35"))

    with pytest.raises(RuntimeError) as erro:
        migracao.conferir_realizados(apps, None)

    mensagem = str(erro.value)
    assert mensagem.startswith("3 lançamento(s) realizado(s)")
    assert f"#{sem_nada.id}: sem data e sem valor\n" in mensagem
    assert f"#{sem_valor.id}: sem valor\n" in mensagem
    assert f"#{sem_data.id}: sem data\n" in mensagem
    assert f"#{completo.id}:" not in mensagem

    # Lista longa é cortada, mas a contagem continua inteira.
    monkeypatch.setattr(migracao, "LIMITE_DA_LISTA", 2)
    with pytest.raises(RuntimeError) as erro:
        migracao.conferir_realizados(apps, None)

    mensagem = str(erro.value)
    assert mensagem.startswith("3 lançamento(s) realizado(s)")
    assert f"#{sem_data.id}:" not in mensagem
    assert "  ... e mais 1\n" in mensagem
