"""Atualizar saldo: o usuário informa o saldo real de uma conta numa data e o
sistema lança a diferença com um destino explícito.

Serve ao que o extrato não mostra linha a linha: o rendimento de um CDB, do
cofrinho ou de uma conta remunerada, o IR e o IOF retidos, uma perda. Em vez de
mexer no saldo inicial, a diferença vira um lançamento realizado, com data,
categoria e motivo, que entra nos relatórios e na auditoria.

Destinos da diferença:

- **entrada** (o saldo real é maior que o do CB): *Rendimentos* ou *Ajuste
  assumido*;
- **saída** (o saldo real é menor): *IR/IOF*, *Perda* ou *Ajuste assumido*.

*Ajuste assumido* é o "assumo isto para a movimentação bater": exige motivo, usa
a categoria "Ajustes de Saldo" e aparece na lista de assunções, para nada ficar
escondido e dar para revisar depois.

O saldo comparado é o realizado até a data, inclusive. Mês fechado recusa o
lançamento, como qualquer outro.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from banking.models import FinancialAccount
from banking.services import accessible_account_ids, can_access_account
from core.domain.finance import (
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from reports.services import decimal_balances_before_by_account
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import TransactionRequest, create_transaction_batch

CATEGORIA_DE_AJUSTE = "Ajustes de Saldo"

DESTINO_RENDIMENTOS = "rendimentos"
DESTINO_IR_IOF = "ir_iof"
DESTINO_PERDA = "perda"
DESTINO_AJUSTE = "ajuste"

# destino -> (rótulo, nome da categoria, tipo do lançamento, exige motivo)
DESTINOS = {
    DESTINO_RENDIMENTOS: ("Rendimentos", "Rendimentos", ENTRY_TYPE_INCOME, False),
    DESTINO_IR_IOF: ("IR / IOF", "Impostos e Tributos", ENTRY_TYPE_EXPENSE, False),
    DESTINO_PERDA: ("Perda", CATEGORIA_DE_AJUSTE, ENTRY_TYPE_EXPENSE, True),
    DESTINO_AJUSTE: ("Ajuste assumido", CATEGORIA_DE_AJUSTE, None, True),
}
_QUANTO = Decimal("0.01")


@dataclass(frozen=True)
class Previa:
    conta: FinancialAccount
    data: date
    saldo_informado: Decimal
    saldo_no_cb: Decimal

    @property
    def diferenca(self) -> Decimal:
        return (self.saldo_informado - self.saldo_no_cb).quantize(_QUANTO)

    @property
    def tipo(self) -> str | None:
        if self.diferenca > 0:
            return ENTRY_TYPE_INCOME
        if self.diferenca < 0:
            return ENTRY_TYPE_EXPENSE
        return None

    @property
    def destinos(self) -> list[tuple[str, str]]:
        """Os destinos que servem ao sentido da diferença, como `(chave, rótulo)`."""
        return [
            (chave, rotulo)
            for chave, (rotulo, _, tipo, _) in DESTINOS.items()
            if tipo is None or tipo == self.tipo
        ]


def _conta(user, account_id, acao: str) -> FinancialAccount:
    try:
        conta = FinancialAccount.objects.select_related("owner", "institution").get(id=int(account_id))
    except (FinancialAccount.DoesNotExist, ValueError, TypeError) as exc:
        raise ValueError("Conta não encontrada.") from exc
    if not can_access_account(user, conta.id, acao):
        raise ValueError("Acesso negado para esta conta.")
    if conta.is_credit_card:
        raise ValueError("Cartão de crédito não tem saldo para atualizar: use a fatura.")
    return conta


def _decimal(valor, rotulo: str) -> Decimal:
    texto = str(valor).strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError(f"{rotulo} inválido.") from exc


def previa(user, *, account_id, data: date, saldo_informado) -> Previa:
    conta = _conta(user, account_id, "view")
    informado = _decimal(saldo_informado, "Saldo").quantize(_QUANTO)
    saldos = decimal_balances_before_by_account([conta.id], data + timedelta(days=1), VIEW_REALIZED)
    return Previa(conta=conta, data=data, saldo_informado=informado, saldo_no_cb=saldos.get(conta.id, Decimal("0.00")))


def _categoria(nome: str) -> CashFlowCategory:
    categoria = CashFlowCategory.objects.filter(category_name__iexact=nome).first()
    if categoria is None:
        raise ValueError(f"Cadastre a categoria \"{nome}\" antes de lançar esta diferença.")
    return categoria


def aplicar(
    user, *, account_id, data: date, saldo_informado, diferenca_esperada, destino: str, motivo: str = "",
    audit_context=None,
) -> CashFlowEntry:
    """Lança a diferença como um lançamento realizado na data do saldo.

    `diferenca_esperada` é a que a prévia mostrou: se o saldo do CB mudou desde
    então (outra importação, outro usuário), nada é gravado e a prévia é refeita."""
    if destino not in DESTINOS:
        raise ValueError("Escolha o destino da diferença.")
    rotulo, nome_da_categoria, tipo_fixo, exige_motivo = DESTINOS[destino]
    motivo = (motivo or "").strip()
    if exige_motivo and not motivo:
        raise ValueError(f"\"{rotulo}\" exige um motivo.")

    atual = previa(user, account_id=account_id, data=data, saldo_informado=saldo_informado)
    conta = _conta(user, account_id, "create")
    if atual.diferenca != _decimal(diferenca_esperada, "Diferença").quantize(_QUANTO):
        raise ValueError("O saldo do CB mudou desde a prévia. Confira a nova diferença.")
    if atual.tipo is None:
        raise ValueError("O saldo informado já bate com o do CB: não há o que lançar.")
    if tipo_fixo is not None and tipo_fixo != atual.tipo:
        raise ValueError(f"\"{rotulo}\" não serve ao sentido desta diferença.")

    descricao = f"Atualização de saldo: {rotulo.lower()}"
    if motivo:
        descricao = f"{descricao} ({motivo})"
    valor = abs(atual.diferenca)
    return create_transaction_batch(
        TransactionRequest(
            account_id=conta.id,
            category_id=_categoria(nome_da_categoria).id,
            entry_type=atual.tipo,
            description=descricao,
            entry_amount=valor,
            installments=1,
            due_date=data,
            status=STATUS_REALIZED,
            realized_date=data,
            realized_amount=valor,
        ),
        audit_context=audit_context,
        user=user,
    )[0]


def assuncoes(user, limit: int = 200) -> list[CashFlowEntry]:
    """Todos os lançamentos de "Ajustes de Saldo" das contas do usuário."""
    contas = accessible_account_ids(user, "view")
    if not contas:
        return []
    return list(
        CashFlowEntry.objects.filter(
            account_id__in=contas, category__category_name__iexact=CATEGORIA_DE_AJUSTE
        )
        .select_related("account__owner", "account__institution")
        .order_by("-due_date", "-id")[:limit]
    )
