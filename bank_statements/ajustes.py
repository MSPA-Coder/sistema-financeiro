"""Ajustes pontuais de dados a partir de um plano declarativo (JSON).

Serve às correções que não cabem numa regra geral: o lançamento que foi gravado
na conta errada, a conta que ganhou número de extrato, o lançamento agregado que
o extrato passou a mostrar em várias linhas. O plano **não** mora no repositório
(descreve dinheiro real); só estas operações moram, cada uma conferindo o que
espera achar e recusando tudo se não achar.

Operações:

- `contas`: define o identificador no extrato e/ou o saldo inicial de uma conta;
- `mover_pontas`: troca a conta de um lançamento (uma ponta de transferência
  ou um avulso), refazendo o texto das duas pontas;
- `excluir`: exclui um lançamento (a transferência inteira, se for ponta), com a
  linha de extrato ligada a ele voltando para "novo".

Um lançamento é descrito por `conta`, `data`, `tipo`, `valor` e, se preciso,
`descricao_contem`; precisa casar com **um** só. Uma conta é descrita por
`titular`, `instituicao` (um nome ou uma lista de nomes) e, se preciso, `nome`.

Tudo ou nada. Mover e excluir exigem os meses abertos: mudar de conta mexe no
saldo das duas, e isso não cabe no contrato "o saldo de fechamento não muda".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction as db_transaction

from accounts.services import can_use_transfer_destination
from banking.models import FinancialAccount
from banking.services import can_access_account, update_account
from core.domain.finance import OPERATION_INTERNAL_TRANSFER
from transactions.models import CashFlowEntry
from transactions.services import (
    _account_label,
    assert_entry_period_open,
    delete_transaction_or_operation,
    is_month_closed,
    transfer_counterparty,
)


@dataclass
class Relatorio:
    linhas: list[str] = field(default_factory=list)
    erros: list[str] = field(default_factory=list)

    def ok(self, texto: str) -> None:
        self.linhas.append(texto)

    def erro(self, texto: str) -> None:
        self.erros.append(texto)


def _decimal(valor, rotulo: str) -> Decimal:
    try:
        return Decimal(str(valor))
    except InvalidOperation as exc:
        raise ValueError(f"{rotulo} inválido: {valor!r}.") from exc


def achar_conta(spec: dict) -> FinancialAccount:
    instituicoes = spec.get("instituicao")
    if isinstance(instituicoes, str):
        instituicoes = [instituicoes]
    if not spec.get("titular") or not instituicoes:
        raise ValueError(f"A conta precisa de titular e instituição: {spec!r}.")
    consulta = FinancialAccount.objects.select_related("owner", "institution")
    achadas = [
        conta for conta in consulta.filter(owner__name__iexact=spec["titular"])
        if conta.institution.institution_name.lower() in {nome.lower() for nome in instituicoes}
        and (not spec.get("nome") or conta.account_name.lower() == spec["nome"].lower())
    ]
    if len(achadas) != 1:
        raise ValueError(f"A conta {spec!r} casa com {len(achadas)} contas; esperava exatamente uma.")
    return achadas[0]


def achar_lancamento(spec: dict) -> CashFlowEntry:
    conta = achar_conta(spec["conta"])
    data = date.fromisoformat(spec["data"])
    valor = _decimal(spec["valor"], "Valor")
    consulta = CashFlowEntry.objects.select_related("account__owner", "account__institution", "category").filter(
        account=conta, entry_type=spec["tipo"], entry_amount=valor
    )
    achados = [
        e for e in consulta
        if (e.realized_date or e.due_date) == data
        and (not spec.get("descricao_contem") or spec["descricao_contem"].lower() in e.description.lower())
    ]
    if len(achados) != 1:
        raise ValueError(f"O lançamento {spec!r} casa com {len(achados)} lançamentos; esperava exatamente um.")
    return achados[0]


def _definir_conta(user, spec: dict, relatorio: Relatorio) -> None:
    conta = achar_conta(spec["conta"])
    novo_identificador = spec.get("identificador", conta.statement_identifier)
    novo_saldo = spec.get("saldo_inicial", conta.initial_balance)
    nova_data = spec.get("data_do_saldo_inicial", conta.initial_balance_date.isoformat())
    update_account(
        user, conta,
        owner_id=str(conta.owner_id), institution_id=str(conta.institution_id), account_name=conta.account_name,
        initial_balance=str(novo_saldo), currency=conta.currency, initial_balance_date=str(nova_data),
        purpose=conta.purpose, account_kind=conta.account_kind,
        card_closing_day=str(conta.card_closing_day or ""), card_due_day=str(conta.card_due_day or ""),
        card_payment_account_id=str(conta.card_payment_account_id or ""),
        card_estimated_spend=str(conta.card_estimated_spend or ""),
        statement_identifier=str(novo_identificador),
    )
    relatorio.ok(
        f"Conta {_account_label(conta)}: identificador \"{novo_identificador}\", "
        f"saldo inicial {novo_saldo} em {nova_data}."
    )


def _refazer_textos(entrada: CashFlowEntry) -> None:
    contraparte = transfer_counterparty(entrada)
    if contraparte is None:
        return
    origem, destino = (entrada, contraparte) if entrada.source_entry_id is None else (contraparte, entrada)
    origem.description = f"Conta Destino: {_account_label(destino.account)}"
    destino.description = f"Conta Origem: {_account_label(origem.account)}"
    origem.save(update_fields=["description", "updated_at"])
    destino.save(update_fields=["description", "updated_at"])


def _mover_ponta(user, spec: dict, relatorio: Relatorio, audit_context) -> None:
    from bank_statements.models import BankStatementLine
    from core.services import log_audit_event

    entrada = achar_lancamento(spec["lancamento"])
    destino = achar_conta(spec["para_conta"])
    if destino.currency != entrada.account.currency:
        raise ValueError("Não dá para mover lançamento entre contas de moedas diferentes.")
    if destino.id == entrada.account_id:
        raise ValueError(f"O lançamento #{entrada.id} já está na conta de destino.")
    if not (can_access_account(user, entrada.account_id, "update") and can_access_account(user, destino.id, "update")):
        raise ValueError("Acesso negado para mover este lançamento.")
    if entrada.operation_type == OPERATION_INTERNAL_TRANSFER:
        contraparte = transfer_counterparty(entrada)
        if contraparte is not None and contraparte.account_id == destino.id:
            raise ValueError("A conta de destino é a outra ponta da própria transferência.")
        if not can_use_transfer_destination(user, destino.id):
            raise ValueError("Sem permissão de destino de transferência para a conta.")
    assert_entry_period_open(entrada, action_label="mover")
    for dia in {entrada.due_date, entrada.realized_date} - {None}:
        if is_month_closed(destino, dia.year, dia.month):
            raise ValueError(f"O mês {dia.month:02d}/{dia.year} está fechado na conta de destino.")
    if BankStatementLine.objects.filter(matched_entry=entrada).exists():
        raise ValueError(
            f"O lançamento #{entrada.id} está conciliado com uma linha de extrato da conta de origem: "
            "desfaça a conciliação antes de mover."
        )
    antes = _account_label(entrada.account)
    entrada.account = destino
    entrada.save(update_fields=["account", "updated_at"])
    entrada.refresh_from_db()
    _refazer_textos(entrada)
    log_audit_event(
        "cash_flow_entry", entrada.id, "update",
        old_values={"account": antes}, new_values={"account": _account_label(destino)},
        user=user, request_context=audit_context, summary="Lançamento movido de conta por ajuste de dados.",
    )
    relatorio.ok(f"Lançamento #{entrada.id} ({entrada.entry_amount}) movido de {antes} para {_account_label(destino)}.")


def _excluir(user, spec: dict, relatorio: Relatorio, audit_context) -> None:
    entrada = achar_lancamento(spec["lancamento"])
    descricao = f"#{entrada.id} {entrada.entry_amount} ({_account_label(entrada.account)})"
    apagados = delete_transaction_or_operation(entrada, audit_context=audit_context, user=user)
    relatorio.ok(f"Excluído o lançamento {descricao}: {apagados} lançamento(s) removido(s).")


def executar(user, plano: dict, *, aplicar: bool = False, audit_context=None) -> Relatorio:
    """Valida e, com `aplicar`, grava o plano inteiro. Sem `aplicar`, tudo é
    desfeito no fim: a simulação roda as operações de verdade dentro de uma
    transação revertida, então ela recusa exatamente o que a aplicação recusaria."""
    relatorio = Relatorio()
    ordem = (
        ("contas", lambda item: _definir_conta(user, item, relatorio)),
        ("mover_pontas", lambda item: _mover_ponta(user, item, relatorio, audit_context)),
        ("excluir", lambda item: _excluir(user, item, relatorio, audit_context)),
    )
    desconhecidas = set(plano) - {nome for nome, _ in ordem}
    if desconhecidas:
        raise ValueError(f"Operações desconhecidas no plano: {sorted(desconhecidas)}.")
    try:
        with db_transaction.atomic():
            for nome, operacao in ordem:
                for item in plano.get(nome, []):
                    try:
                        operacao(item)
                    except ValueError as exc:
                        relatorio.erro(f"[{nome}] {exc}")
            if relatorio.erros or not aplicar:
                raise _ReversaoError
    except _ReversaoError:
        pass
    return relatorio


class _ReversaoError(Exception):
    """Desfaz a transação: houve erro, ou é só simulação."""
