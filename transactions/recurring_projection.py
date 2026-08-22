"""Projeção dinâmica de lançamentos recorrentes (Configurações > Parâmetros).

Estende, até um horizonte configurável, as ocorrências futuras de cada
lançamento recorrente. O agrupamento é por `bank_operation_id`.

A execução é idempotente, e **não há** guarda de "já rodou este mês" -- as
duas coisas juntas, porque a segunda existia e foi removida em 2026-08-22.

Idempotente porque `_extend_operation` só olha para frente: parte da MAIOR
data existente do grupo e preenche até o horizonte, pulando data que já
existe. Três consequências, todas testadas em
`tests/test_projecao_recorrente_idempotente.py`:

- reexecutar no mesmo mês gera zero, porque o horizonte não se move dentro do
  mês e as ocorrências já alcançam o horizonte;
- ocorrência apagada à mão no meio da série NÃO ressuscita, porque o
  preenchimento nunca volta antes da maior data;
- num mês novo o horizonte avança, e aí reexecutar gera o que falta -- que é
  o objetivo.

A guarda removida lia `confirm_current_month` do POST, e o template mandava
esse campo FIXO em `value="on"`: nunca podia reprovar. Não foi "consertada"
porque consertá-la seria pior. Ela obstruiria o caminho legítimo: depois de
aumentar o horizonte na mesma tela, reexecutar é exatamente o que se quer, e
o usuário levaria um "já executada no mês atual" no lugar do resultado. Numa
operação que não duplica nada, pedir confirmação treina a clicar "sim" sem
ler -- ver a regra da Fase 9 em `_manutencao/PLANO_SINAL_E_DEFEITOS.md`.

O disparo é manual, pela tela de Parâmetros. O `generated_count` da mensagem
diz ao usuário quanto foi criado; num clique redundante ele lê "0", que é a
informação certa.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby

from django.db import connection, transaction

from core.domain.finance import (
    OPERATION_INSTALLMENT,
    OPERATION_RECURRING,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
)
from core.services import (
    get_recurring_projection_settings,
    upsert_app_setting,
)
from reports.services import add_months

from .models import CashFlowEntry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecurringProjectionResult:
    horizon_end: date
    generated_count: int
    processed_operations: int


def recurring_projection_horizon_end(
    today: date | None = None, horizon_months: int | None = None
) -> date:
    base_date = today or date.today()
    months = (
        horizon_months
        if horizon_months is not None
        else get_recurring_projection_settings().horizon_months
    )
    current_month_start = date(base_date.year, base_date.month, 1)
    horizon_month_start = add_months(current_month_start, months)
    return add_months(horizon_month_start, 1) - timedelta(days=1)


def was_projection_run_in_month(raw_value: str | None, month_date: date | None = None) -> bool:
    """A projecao ja rodou no mes de `month_date`?

    Esta funcao ja existiu, guardando o botao manual, e foi removida em
    2026-08-22 por nao proteger nada: reexecutar a mao e idempotente, e o
    aviso ainda por cima era inalcancavel (o template mandava o campo de
    confirmacao fixo). Volta agora para o uso em que faz sentido -- segurar a
    execucao AUTOMATICA, que ninguem pediu e que portanto ninguem espera ver
    acontecendo duas vezes.

    A diferenca nao e de implementacao, e de proposito: como confirmacao ela
    obstruia o usuario; como limite de frequencia ela e a razao de a execucao
    automatica ser previsivel.
    """
    if not raw_value:
        return False
    try:
        last_run = datetime.fromisoformat(raw_value).date()
    except ValueError:
        try:
            last_run = date.fromisoformat(raw_value)
        except ValueError:
            # Valor ilegivel conta como "nao rodou": errar para o lado de
            # executar mantem a projecao em dia, e executar de novo nao
            # duplica nada. Errar para o lado de pular deixaria o horizonte
            # parado sem ninguem perceber.
            return False
    base_date = month_date or date.today()
    return last_run.year == base_date.year and last_run.month == base_date.month


def dia_efetivo_de_execucao(run_day: int, hoje: date) -> int:
    """O dia em que a execucao automatica e devida, dentro do mes de `hoje`.

    `run_day` vai de 1 a 31 e o padrao do sistema e **31**. Comparar
    `hoje.day >= run_day` cru faria a execucao automatica nunca acontecer em
    fevereiro, abril, junho, setembro e novembro -- com o valor PADRAO, ou
    seja, para quem nunca mexeu na configuracao. Encurtar para o ultimo dia do
    mes e o que faz "dia 31" significar "fim do mes", que e como a pessoa que
    escolhe 31 le a opcao.
    """
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    return min(run_day, ultimo_dia)


@dataclass(frozen=True)
class DecisaoProjecaoMensal:
    """O que a verificacao automatica decidiu, e por que."""

    executou: bool
    mes_resolvido: bool  # nada mais a fazer neste mes
    motivo: str
    resultado: RecurringProjectionResult | None = None


def executar_projecao_mensal_se_devido(hoje: date | None = None) -> DecisaoProjecaoMensal:
    """Executa a projecao no maximo uma vez por mes, a partir do dia marcado.

    Ate 2026-08-22 o campo "Dia de execucao automatica" era salvo, exibido e
    confirmado por mensagem, e **nao existia execucao automatica alguma** --
    nem agendador no codigo, nem comando de management, nem cron ou timer no
    servidor. A projecao so andava quando alguem clicava no botao.

    Quem chama e o middleware (ver `transactions/middleware.py`), e nao o
    `AppConfig.ready()`. Tres razoes, nesta ordem:

    1. `ready()` roda tambem em `migrate` e `collectstatic`, onde gravar esta
       errado -- inclusive antes de as migracoes existirem no banco;
    2. com varios workers do gunicorn, dispara uma vez por worker no boot;
    3. e sobretudo: so no boot significa que um conteiner de pe ha 40 dias
       nunca executa. Depender de reinicio e a forma silenciosa de a
       funcionalidade parar de existir sem ninguem notar -- exatamente o
       defeito que este projeto passou o mes caçando.

    Concorrencia entre workers e segura sem trava propria: dois que decidam
    executar ao mesmo tempo serializam no `pg_advisory_xact_lock` de
    `ensure_recurring_projection_horizon`, e o segundo gera zero porque a
    operacao e idempotente.
    """
    hoje = hoje or date.today()
    configuracao = get_recurring_projection_settings()

    if was_projection_run_in_month(configuracao.last_projection_run, hoje):
        return DecisaoProjecaoMensal(False, True, "ja executada neste mes")

    dia_devido = dia_efetivo_de_execucao(configuracao.run_day, hoje)
    if hoje.day < dia_devido:
        return DecisaoProjecaoMensal(False, False, f"antes do dia {dia_devido}")

    resultado = ensure_recurring_projection_horizon(today=hoje, update_last_run=True)
    logger.info(
        "Projecao recorrente automatica: %s lancamento(s) ate %s.",
        resultado.generated_count,
        resultado.horizon_end.isoformat(),
    )
    return DecisaoProjecaoMensal(True, True, "executada", resultado)


def _projected_status(template: CashFlowEntry, due_date: date, today: date) -> str:
    if template.status == STATUS_REALIZED:
        return STATUS_PROJECTED
    if template.status == STATUS_PENDING and due_date >= today:
        return STATUS_PROJECTED
    return template.status or STATUS_PROJECTED


def _copy_occurrence(template_rows: list[CashFlowEntry], due_date: date, today: date) -> int:
    created_by_template_id: dict[int, CashFlowEntry] = {}
    for template in template_rows:
        entry = CashFlowEntry.objects.create(
            account_id=template.account_id,
            category_id=template.category_id,
            entry_type=template.entry_type,
            description=template.description,
            entry_amount=template.entry_amount,
            installments=1,
            current_installment=1,
            due_date=due_date,
            is_recurring=True,
            status=_projected_status(template, due_date, today),
            realized_date=None,
            realized_amount=None,
            bank_operation_id=template.bank_operation_id,
            operation_type=template.operation_type or OPERATION_RECURRING,
        )
        created_by_template_id[template.id] = entry

    for template in template_rows:
        if template.source_entry_id:
            created = created_by_template_id[template.id]
            source = created_by_template_id.get(template.source_entry_id)
            if source:
                created.source_entry = source
                created.save(update_fields=["source_entry"])
    return len(created_by_template_id)


def _extend_operation(entries: list[CashFlowEntry], horizon_end: date, today: date) -> int:
    existing_dates = {entry.due_date for entry in entries}
    latest_due_date = max(existing_dates)
    if latest_due_date >= horizon_end:
        return 0

    template_rows = [entry for entry in entries if entry.due_date == latest_due_date]
    next_due_date = add_months(latest_due_date, 1)
    generated_count = 0
    while next_due_date <= horizon_end:
        if next_due_date not in existing_dates:
            generated_count += _copy_occurrence(template_rows, next_due_date, today)
            existing_dates.add(next_due_date)
        next_due_date = add_months(next_due_date, 1)
    return generated_count


@transaction.atomic
def ensure_recurring_projection_horizon(
    *,
    today: date | None = None,
    horizon_months: int | None = None,
    update_last_run: bool = True,
) -> RecurringProjectionResult:
    base_date = today or date.today()
    horizon_end = recurring_projection_horizon_end(base_date, horizon_months)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(81288431)")

    all_entries = list(
        CashFlowEntry.objects.filter(
            is_recurring=True,
            bank_operation__isnull=False,
        )
        .exclude(operation_type=OPERATION_INSTALLMENT)
        .order_by("bank_operation_id", "due_date", "id")
    )

    generated_count = 0
    processed_operations = 0
    for _bank_operation_id, group in groupby(all_entries, key=lambda e: e.bank_operation_id):
        entries = list(group)
        generated_count += _extend_operation(entries, horizon_end, base_date)
        processed_operations += 1

    if update_last_run:
        from core.domain.settings import APP_SETTING_LAST_PROJECTION_RUN

        upsert_app_setting(
            APP_SETTING_LAST_PROJECTION_RUN, datetime.now().isoformat(timespec="seconds")
        )

    return RecurringProjectionResult(
        horizon_end=horizon_end,
        generated_count=generated_count,
        processed_operations=processed_operations,
    )
