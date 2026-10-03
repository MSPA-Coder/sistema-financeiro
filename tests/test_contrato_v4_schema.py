"""O que o CB publica em /patrimonio/v4 cumpre o JSON Schema do contrato.

O contrato v4 era descrito em três lugares (o doc do Wealthfolio, este repositório
e o CRV), e nenhum publicador testava a própria saída contra uma definição comum:
um campo renomeado ou um valor que virasse número passava na suíte de quem mudou e
quebrava o consumidor. O schema (`tests/contrato/patrimonio-v4.schema.json`) é uma
cópia da canônica em `manutencao/docs/contratos/`; a suíte do CRV e a do
Wealthfolio validam contra a mesma. Para mudar o contrato, mude a canônica e copie
para todos na mesma mudança.

Aqui o schema é aplicado à saída real das três rotas, num cenário com cada tipo de
recurso que o CB publica. E há mutações: sem elas, um schema frouxo demais passaria
sem ninguém notar.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.db import transaction
from django.test import Client
from django.utils import timezone
from jsonschema import Draft202012Validator

from accounts.models import AccountOwner
from banking.models import FinancialAccount, FinancialInstitution
from core import patrimonio
from core.domain.finance import (
    ACCOUNT_KIND_CREDIT_CARD,
    ACCOUNT_KIND_INVESTMENT,
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from transactions.models import (
    BankOperation,
    CashFlowCategory,
    CashFlowCategoryGroup,
    CashFlowEntry,
)

pytestmark = pytest.mark.django_db(transaction=True)

SCHEMA = json.loads((Path(__file__).parent / "contrato" / "patrimonio-v4.schema.json").read_text(encoding="utf-8"))
TOKEN_V4 = "token-de-integracao-v4-com-mais-de-trinta-e-dois-caracteres"


def validar(nome: str, instancia) -> None:
    validador = Draft202012Validator({"$ref": f"#/$defs/{nome}", "$defs": SCHEMA["$defs"]})
    erros = sorted(validador.iter_errors(instancia), key=lambda e: [str(p) for p in e.absolute_path])
    assert not erros, "\n".join(f"{'/'.join(map(str, e.absolute_path)) or '(raiz)'}: {e.message}" for e in erros[:8])


def invalido(nome: str, instancia) -> bool:
    validador = Draft202012Validator({"$ref": f"#/$defs/{nome}", "$defs": SCHEMA["$defs"]})
    return any(True for _ in validador.iter_errors(instancia))


@pytest.fixture
def com_token(monkeypatch, tmp_path):
    arquivo = tmp_path / "patrimonio_integration_token"
    arquivo.write_text(TOKEN_V4, encoding="utf-8")
    monkeypatch.delenv(patrimonio.NOME_DO_SEGREDO_V4, raising=False)
    monkeypatch.setenv(f"{patrimonio.NOME_DO_SEGREDO_V4}_FILE", str(arquivo))


@pytest.fixture
def cenario(com_token):
    hoje = timezone.localdate()
    titular = AccountOwner.objects.create(name="Mariano")
    banco = FinancialInstitution.objects.create(institution_name="Itaú", institution_type="Banco")
    corretora = FinancialInstitution.objects.create(institution_name="XP", institution_type="Corretora")
    corrente = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta corrente", initial_balance=Decimal("1000.00"),
        currency="BRL", initial_balance_date=date(2025, 12, 31),
    )
    cartao = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Cartão", initial_balance=Decimal("-100.00"),
        currency="BRL", initial_balance_date=date(2025, 12, 31), account_kind=ACCOUNT_KIND_CREDIT_CARD,
        card_closing_day=10, card_due_day=17,
    )
    aplicacao = FinancialAccount.objects.create(
        owner=titular, institution=corretora, account_name="Caixinha", initial_balance=Decimal("0.00"),
        currency="BRL", initial_balance_date=date(2025, 12, 31), account_kind=ACCOUNT_KIND_INVESTMENT,
    )
    dolar = FinancialAccount.objects.create(
        owner=titular, institution=corretora, account_name="Conta em dólar", initial_balance=Decimal("250.50"),
        currency="USD", initial_balance_date=date(2025, 12, 31),
    )
    futura = FinancialAccount.objects.create(
        owner=titular, institution=banco, account_name="Conta que ainda não existe", initial_balance=Decimal("5.00"),
        currency="BRL", initial_balance_date=hoje + timedelta(days=30),
    )
    grupo = CashFlowCategoryGroup.objects.create(group_name="Moradia")
    gerencial = CashFlowCategory.objects.create(category_name="Aluguel", kind=CATEGORY_KIND_MANAGERIAL, group=grupo)
    sem_grupo = CashFlowCategory.objects.create(category_name="Salário", kind=CATEGORY_KIND_MANAGERIAL)
    transferencia = CashFlowCategory.objects.create(category_name="Transferência", kind=CATEGORY_KIND_TRANSFER)
    movimentacao = CashFlowCategory.objects.create(category_name="Aporte", kind=CATEGORY_KIND_MOVEMENT)

    def lancar(conta, categoria, tipo, descricao, valor, vencimento, realizado=None, **extras):
        return CashFlowEntry.objects.create(
            account=conta, category=categoria, entry_type=tipo, description=descricao,
            entry_amount=Decimal(valor), due_date=vencimento,
            status=STATUS_REALIZED if realizado else STATUS_PROJECTED,
            realized_date=vencimento if realizado else None,
            realized_amount=Decimal(realizado) if realizado else None, **extras,
        )

    lancar(corrente, sem_grupo, ENTRY_TYPE_INCOME, "Salário", "5000.00", date(2026, 1, 5), realizado="5000.00")
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "Aluguel pago", "1500.00", date(2026, 2, 5), realizado="1480.50")
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "Vencido gravado como a vencer", "99.90", hoje - timedelta(days=4))
    lancar(corrente, gerencial, ENTRY_TYPE_EXPENSE, "Por vir", "10.00", hoje + timedelta(days=9), is_recurring=True)
    lancar(corrente, movimentacao, ENTRY_TYPE_EXPENSE, "Aporte em ações", "300.00", date(2026, 3, 1), realizado="300.00")
    lancar(dolar, sem_grupo, ENTRY_TYPE_INCOME, "Dividendo no exterior", "12.34", date(2026, 4, 2), realizado="12.34")

    operacao = BankOperation.objects.create(
        operation_key="transferencia-contrato-v4", operation_type=OPERATION_INTERNAL_TRANSFER
    )
    with transaction.atomic():
        origem = CashFlowEntry.objects.create(
            account=corrente, category=transferencia, entry_type=ENTRY_TYPE_EXPENSE, description="Para a caixinha",
            entry_amount=Decimal("200.00"), due_date=date(2026, 5, 6), realized_date=date(2026, 5, 6),
            realized_amount=Decimal("200.00"), status=STATUS_REALIZED, operation_type=OPERATION_INTERNAL_TRANSFER,
            bank_operation=operacao,
        )
        CashFlowEntry.objects.create(
            account=aplicacao, category=transferencia, entry_type=ENTRY_TYPE_INCOME, description="Da conta corrente",
            entry_amount=Decimal("200.00"), due_date=date(2026, 5, 6), realized_date=date(2026, 5, 6),
            realized_amount=Decimal("200.00"), status=STATUS_REALIZED, operation_type=OPERATION_INTERNAL_TRANSFER,
            bank_operation=operacao, source_entry=origem,
        )
    return {"corrente": corrente, "cartao": cartao, "futura": futura, "operacao": operacao}


def pedir(caminho: str, **parametros):
    resposta = Client().get(caminho, parametros, HTTP_AUTHORIZATION=f"Bearer {TOKEN_V4}")
    assert resposta.status_code == 200, resposta.content[:300]
    return resposta.json()


@pytest.fixture
def snapshot(cenario):
    return pedir("/patrimonio/v4/snapshot")


def test_o_proprio_schema_e_um_schema_valido():
    Draft202012Validator.check_schema(SCHEMA)


def test_metadata_cumpre_o_contrato(cenario):
    validar("cb_metadata", pedir("/patrimonio/v4/metadata"))


def test_snapshot_cumpre_o_contrato_com_todos_os_recursos(snapshot):
    validar("cb_snapshot", snapshot)
    recursos = {item["resource"] for item in snapshot["items"]}
    assert recursos == {"account", "category", "cash_entry", "transfer"}


def test_conta_que_ainda_nao_existe_leva_saldo_nulo_e_nao_zero(snapshot):
    contas = {i["payload"]["name"]: i["payload"] for i in snapshot["items"] if i["resource"] == "account"}
    futura = contas["Conta que ainda não existe"]
    assert futura["balance"] is None and futura["balance_as_of"] is None
    validar("cb_snapshot", snapshot)


def test_changes_cumpre_o_contrato(cenario):
    corpo = pedir("/patrimonio/v4/changes", limit=500)
    validar("cb_changes", corpo)
    assert corpo["items"], "o cenário grava pela aplicação, então a outbox tem itens"
    assert all(item["payload"] == {"mode": "snapshot_required"} for item in corpo["items"])


def test_changes_pagina_com_has_more_e_cursor_opaco(cenario):
    primeira = pedir("/patrimonio/v4/changes", limit=1)
    validar("cb_changes", primeira)
    assert primeira["has_more"] is True
    segunda = pedir("/patrimonio/v4/changes", limit=1, after=primeira["next_cursor"])
    validar("cb_changes", segunda)
    assert segunda["items"][0]["cursor"] != primeira["items"][0]["cursor"]


def test_changes_de_exclusao_e_um_tombstone_sem_payload(cenario):
    marco = pedir("/patrimonio/v4/changes", limit=500)["next_cursor"]
    CashFlowEntry.objects.filter(description="Por vir").delete()

    corpo = pedir("/patrimonio/v4/changes", after=marco)

    validar("cb_changes", corpo)
    exclusoes = [i for i in corpo["items"] if i["operation"] == "delete"]
    assert exclusoes and all(i["payload"] is None for i in exclusoes)


def _primeiro(snapshot, recurso):
    return next(i for i in snapshot["items"] if i["resource"] == recurso)


def _sem_effective_status(s):
    del _primeiro(s, "cash_entry")["payload"]["effective_status"]


def _valor_vira_numero(s):
    _primeiro(s, "cash_entry")["payload"]["planned_amount"] = 10.5


def _status_fora_do_vocabulario(s):
    _primeiro(s, "cash_entry")["payload"]["status"] = "pending"


def _moeda_em_minuscula(s):
    _primeiro(s, "account")["payload"]["currency"] = "brl"


def _id_sem_prefixo_da_fonte(s):
    _primeiro(s, "cash_entry")["payload"]["account_id"] = "17"


def _campo_renomeado(s):
    payload = _primeiro(s, "account")["payload"]
    payload["nome"] = payload.pop("name")


def _sistema_errado(s):
    s["sistema"] = "controle-renda-variavel"


def _sem_high_watermark(s):
    del s["high_watermark"]


def _data_fora_do_iso(s):
    _primeiro(s, "cash_entry")["payload"]["due_date"] = "05/02/2026"


def _recurso_desconhecido(s):
    s["items"].append({"resource": "orcamento", "source_id": "controle-bancario:orcamento:1", "payload": {}})


def _saldo_como_zero_de_texto_vazio(s):
    _primeiro(s, "account")["payload"]["balance"] = ""


@pytest.mark.parametrize(
    "mutacao",
    [
        _sem_effective_status, _valor_vira_numero, _status_fora_do_vocabulario, _moeda_em_minuscula,
        _id_sem_prefixo_da_fonte, _campo_renomeado, _sistema_errado, _sem_high_watermark, _data_fora_do_iso,
        _recurso_desconhecido, _saldo_como_zero_de_texto_vazio,
    ],
    ids=lambda f: f.__name__.strip("_"),
)
def test_o_schema_reprova_cada_mutacao_do_snapshot(snapshot, mutacao):
    """Sem isto, um schema frouxo demais passaria sem ninguém notar."""
    quebrado = copy.deepcopy(snapshot)
    mutacao(quebrado)

    assert invalido("cb_snapshot", quebrado), mutacao.__name__


def test_o_schema_aceita_um_acrescimo_opcional(snapshot):
    """Campo a mais não muda a versão do contrato: quem não o conhece o ignora."""
    novo = copy.deepcopy(snapshot)
    _primeiro(novo, "account")["payload"]["campo_novo_e_opcional"] = "x"
    novo["extra_na_raiz"] = True

    validar("cb_snapshot", novo)
