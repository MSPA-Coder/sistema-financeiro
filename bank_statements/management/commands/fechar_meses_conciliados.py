"""Fecha os meses conciliados de todas as contas.

    python manage.py fechar_meses_conciliados --usuario NOME
    python manage.py fechar_meses_conciliados --usuario NOME --aplicar

"Conciliado" é o estado da Situação das Contas (`bank_statements.situacao`): o
mês tem linhas de extrato ou fatura e nenhuma pendente; numa aplicação com conta
de movimento, essa conta está conciliada e o saldo foi informado no último dia
do mês. Mês sem nada importado não entra, nem o mês corrente, nem mês já
fechado, nem mês anterior à data inicial do sistema. Mês cujo saldo de extrato não bate com o do CB na mesma data também
fica de fora, listado: fechar congelaria a diferença.

Sem `--aplicar` só lista. Com ele, fecha tudo numa transação: o saldo de
fechamento é o realizado no fim do mês, como no botão da tela.
"""
from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import AppUser
from bank_statements import situacao
from bank_statements.models import BankStatementImport, BankStatementLine
from bank_statements.services import conferencia_de_saldo
from banking.models import FinancialAccount
from core.services import system_start_date
from transactions.models import AccountMonthClose
from transactions.services import close_month


def _meses(desde: date, ate_exclusive: date) -> list[date]:
    meses, mes = [], desde.replace(day=1)
    while mes < ate_exclusive:
        meses.append(mes)
        mes = situacao.primeiro_dia_do_mes_seguinte(mes)
    return meses


class Command(BaseCommand):
    help = "Lista ou fecha os meses conciliados (extrato/fatura importado e conciliado)."

    def add_arguments(self, parser):
        parser.add_argument("--usuario", required=True)
        parser.add_argument("--aplicar", action="store_true", help="Fecha; sem isto só lista.")

    def handle(self, *args, usuario, aplicar, **options):
        try:
            user = AppUser.objects.get(username=usuario)
        except AppUser.DoesNotExist as exc:
            raise CommandError(f"Usuário \"{usuario}\" não existe.") from exc

        primeira = BankStatementLine.objects.order_by("statement_date").values_list("statement_date", flat=True).first()
        if primeira is None:
            self.stdout.write("Nenhuma linha de extrato importada.")
            return
        piso = system_start_date()
        if piso is not None:
            primeira = max(primeira, piso)  # antes da data inicial do sistema não se fecha nada
        meses = _meses(primeira, date.today().replace(day=1))
        contas = list(
            FinancialAccount.objects.select_related("owner", "institution", "movement_account__institution")
            .order_by("owner__name", "institution__institution_name", "account_name")
        )
        celulas = situacao.estados(contas, meses)
        fechados = set(AccountMonthClose.objects.filter(active=True).values_list("account_id", "year", "month"))
        divergencias: dict[tuple[int, int, int], object] = {}
        for lote in BankStatementImport.objects.exclude(statement_balance_date=None):
            conferencia = conferencia_de_saldo(lote)
            if conferencia.saldo_do_extrato != conferencia.saldo_no_cb:
                divergencias[(lote.account_id, conferencia.data.year, conferencia.data.month)] = conferencia

        fechar = []
        for conta in contas:
            rotulo = f"{conta.owner.name} / {conta.institution.institution_name} / {conta.account_name}"
            for mes in meses:
                chave = (conta.id, mes.year, mes.month)
                celula = celulas[(conta.id, mes)]
                if celula.estado != situacao.CONCILIADO or chave in fechados:
                    continue
                divergente = divergencias.get(chave)
                if divergente is not None:
                    self.stdout.write(
                        f"PULADO {mes:%m/%Y} {rotulo}: extrato {divergente.saldo_do_extrato} x CB "
                        f"{divergente.saldo_no_cb} em {divergente.data:%d/%m}"
                    )
                    continue
                fechar.append((conta, mes, rotulo, celula.descricao))

        for _conta, mes, rotulo, descricao in fechar:
            self.stdout.write(f"FECHAR {mes:%m/%Y} {rotulo} ({descricao})")
        if not aplicar:
            self.stdout.write(f"Simulação: {len(fechar)} mês(es) a fechar; nada foi gravado.")
            return
        with transaction.atomic():
            for conta, mes, _rotulo, _descricao in fechar:
                close_month(conta, mes.year, mes.month, None, user)
        self.stdout.write(f"{len(fechar)} mês(es) fechado(s).")
