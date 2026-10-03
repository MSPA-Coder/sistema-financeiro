"""O que este sistema publica para quem consolida o patrimônio.

POR QUE UM CONTRATO PUBLICADO, E NÃO UM BANCO COMPARTILHADO

Quem soma o caixa daqui com os investimentos do Controle de Renda Variável
(hoje, o Wealthfolio) tinha três formas de receber o dado, e duas foram
recusadas na decisão de 16/09/2026: ler o banco alheio acopla os schemas e
quebra a cada migration, e abrir uma API de negócio é uma decisão de
arquitetura que nenhum dos dois sistemas pediu. Sobrou a terceira, a mais
barata e a mais honesta: **cada sistema publica uma foto do que ele sabe**, no
vocabulário comum, e quem consolida só soma.

O QUE EXISTE HOJE

* `patrimonio/v4` (`metadata`, `snapshot`, `changes`): o contrato do
  consolidador. Contas, categorias e lançamentos de caixa lidos sob
  `REPEATABLE READ`, um feed de mudanças com cursor assinado e a cobertura
  declarada. Autentica com `PATRIMONIO_INTEGRATION_TOKEN`.
* `patrimonio/v3/projection`: a projeção de caixa, reservada para a fase de
  projeção consolidada. Autentica com `PATRIMONIO_TOKEN`.

Os contratos `patrimonio/v1` a `v3` (resumo, fluxos, atividades, categorias e
metadata) serviam ao NetWorth, aposentado em 29/09/2026, e foram retirados em
03/10/2026: o nginx não registra acesso a eles desde 28/09, o Wealthfolio só
lê o v4 (o patch dele recusa qualquer outro caminho), e o histórico está no Git.

O VOCABULÁRIO COMUM

Todo valor é **texto**, nunca número JSON: `float` não representa 0,10 e quem
consolida somaria centavos que não existem. Toda data é ISO 8601. Toda moeda é
ISO 4217, explícita, e **nada é somado entre moedas**, porque converter é
decidir, e a decisão é de quem consolida. Os ids de conta e de lançamento são
opacos e estáveis.

A CHAVE É A PERMISSÃO

As rotas publicam **todas as contas**, sem escopo de usuário, e isso é
deliberado: um resumo filtrado por titular produziria um patrimônio consolidado
que esconde contas sem avisar -- o pior tipo de número errado. Quem tem o token
lê o saldo de todas as contas deste sistema. Guarde-o como se guarda uma senha
de banco. A rota não fica na internet: o nginx fecha `/patrimonio/`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import unicodedata
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.db import connection, transaction
from django.db.models import (
    Count,
    Max,
    Min,
    Prefetch,
    Q,
)
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from sharedauth.secrets import SegredoInvalidoError, resolver_segredo

from banking.models import FinancialAccount
from core.domain.finance import (
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_RECURRING,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_ALL,
    VIEW_REALIZED,
)
from core.models import PatrimonioV4ChangeCounter, PatrimonioV4Outbox
from reports.services import decimal_balances_before_by_account
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry

CONTRATO_V3 = "patrimonio/v3"
CONTRATO_V4 = "patrimonio/v4"
SISTEMA = "controle-bancario"

#: O token não tem valor padrão e não é gerado: sem ele configurado, a rota não
#: atende. Um segredo gerado no arranque valeria até o próximo reinício e daria
#: a impressão de que a integração está configurada quando não está.
NOME_DO_SEGREDO = "PATRIMONIO_TOKEN"
NOME_DO_SEGREDO_V4 = "PATRIMONIO_INTEGRATION_TOKEN"
COMPRIMENTO_MINIMO_DO_TOKEN = 32
MONEY_QUANT = Decimal("0.01")
V4_MAX_CHANGE_LIMIT = 500

logger = logging.getLogger(__name__)


def _id_v3(recurso: str, valor: int) -> str:
    """Identificador estável que não revela a chave primária do banco.

    O contrato não precisa resolver esses ids de volta: os links publicados
    pela fonte apontam para a tela local. Assim a API pode manter ids opacos
    sem adicionar uma tabela de mapeamento ou uma segunda verdade persistida.
    """
    material = f"{SISTEMA}:{recurso}:{valor}".encode()
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{SISTEMA}:{recurso}:{digest}"


def _source_id_v4(recurso: str, valor: int) -> str:
    """Identificador estável e opaco para o contrato de integração v4."""
    material = f"{SISTEMA}:{recurso}:{valor}".encode()
    digest = hashlib.sha256(material).hexdigest()[:24]
    return f"{SISTEMA}:{recurso}:{digest}"


def _watermark_v4() -> int:
    return int(
        PatrimonioV4ChangeCounter.objects.filter(id=1).values_list("value", flat=True).first() or 0
    )


def _cursor_v4(cursor: int) -> str:
    material = f"v1:{SISTEMA}:{cursor}".encode()
    secret = (_token_configurado(NOME_DO_SEGREDO_V4) or "").encode()
    signature = hmac.new(secret, b"patrimonio-v4-cursor:" + material, hashlib.sha256).digest()
    return urlsafe_b64encode(material + b"." + signature).decode().rstrip("=")


def _cursor_v4_ler(raw: str | None) -> int:
    if not raw:
        return 0
    if len(raw) > 256:
        raise ValueError("cursor inválido")
    try:
        decoded = urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        if len(decoded) < 34 or decoded[-33:-32] != b".":
            raise ValueError("cursor inválido")
        material, signature = decoded[:-33], decoded[-32:]
        expected = hmac.new(
            (_token_configurado(NOME_DO_SEGREDO_V4) or "").encode(),
            b"patrimonio-v4-cursor:" + material,
            hashlib.sha256,
        ).digest()
        version, source, position = material.decode().split(":")
        cursor = int(position)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise ValueError("cursor inválido") from None
    if not hmac.compare_digest(signature, expected) or (version, source) != ("v1", SISTEMA) or cursor < 0:
        raise ValueError("cursor inválido")
    return cursor


def _change_limit_v4(request) -> int:
    try:
        limit = int(request.GET.get("limit", "100"))
    except (TypeError, ValueError):
        raise ValueError("limit deve ser inteiro entre 1 e 500") from None
    if not 1 <= limit <= V4_MAX_CHANGE_LIMIT:
        raise ValueError("limit deve ser inteiro entre 1 e 500")
    return limit


def _autorizacao_v3(request, versao: str = "v3"):
    """Retorna uma resposta de erro ou ``None`` quando o Bearer é válido."""
    esperado = _token_configurado(NOME_DO_SEGREDO_V4 if versao == "v4" else NOME_DO_SEGREDO)
    if not esperado:
        return JsonResponse(
            {"erro": "integração de patrimônio não configurada neste servidor"},
            status=503,
        )
    recebido = _token_da_requisicao(request)
    if not recebido or not secrets.compare_digest(recebido, esperado):
        logger.warning("Contrato patrimonial %s recusado: token ausente ou inválido.", versao)
        return JsonResponse({"erro": "não autorizado"}, status=401)
    return None



def _resposta_v3(payload: dict) -> JsonResponse:
    resposta = JsonResponse(payload)
    resposta["Cache-Control"] = "no-store"
    return resposta


def identidade(nome: str) -> str:
    """O identificador comum de um titular ou de uma instituição.

    É o nome normalizado, e não o id do banco, porque o id do banco só vale
    dentro de um sistema. "Mercado Pago", "mercado pago" e "Mercado  Pago" são
    a mesma instituição para quem consolida.
    """
    decomposto = unicodedata.normalize("NFKD", nome or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", sem_acento.lower()).strip("-")


def _token_configurado(nome_do_segredo: str = NOME_DO_SEGREDO) -> str | None:
    """O token configurado, ou `None` quando não há um utilizável.

    Um token curto demais ou igual ao do `.env.example` é recusado como se não
    existisse: a integração fica fora do ar, que é a falha visível. O detalhe
    vai para o log e não para a resposta -- quem bate na rota sem credencial
    não precisa saber por que o segredo do servidor foi recusado.
    """
    # Mesmo contrato do `DJANGO_SECRET_KEY` e da senha do banco: sob o Compose
    # (`REQUIRE_FILE_SECRETS=true`) o segredo vem de arquivo montado, e a
    # variável direta só serve a comando local explícito. Sem isso, uma sobra
    # no ambiente do processo substituiria em silêncio o segredo operacional.
    exige_arquivo = os.environ.get("REQUIRE_FILE_SECRETS", "false").lower() == "true"
    try:
        return resolver_segredo(
            nome_do_segredo,
            aceitar_variavel=not exige_arquivo,
            obrigatorio=False,
            comprimento_minimo=COMPRIMENTO_MINIMO_DO_TOKEN,
            valores_recusados=frozenset({"troque-este-token", "changeme"}),
        )
    except SegredoInvalidoError as erro:
        logger.error("%s configurado mas recusado: %s", nome_do_segredo, erro)
        return None


def _token_da_requisicao(request) -> str:
    cabecalho = request.headers.get("Authorization", "")
    prefixo = "Bearer "
    return cabecalho[len(prefixo):] if cabecalho.startswith(prefixo) else ""


def saldo_da_conta(conta: FinancialAccount, referencia: date) -> Decimal:
    """Saldo realizado da conta no fim do dia `referencia`.

    Usa a mesma função que as telas usam. Não há um segundo cálculo de saldo
    aqui de propósito: dois cálculos discordam um dia, e o dia em que
    discordarem ninguém vai estar olhando.
    """
    from reports.services import decimal_balance_before

    return decimal_balance_before([conta.id], referencia + timedelta(days=1), VIEW_REALIZED)


def endereco_da_conta(conta: FinancialAccount, referencia: date) -> str:
    """A página da conta na mesma data pontual da foto publicada."""
    path = reverse("banking:account_detail", kwargs={"account_id": conta.id})
    return f"{path}?{urlencode({'data': referencia.isoformat()})}"



def _data_da_query(valor: str, nome: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{nome} inválida: use AAAA-MM-DD") from None



def _contas_v3() -> list[FinancialAccount]:
    return list(
        FinancialAccount.objects.select_related("owner", "institution").order_by("id")
    )


def _conta_v3(conta: FinancialAccount) -> dict:
    return {
        "id": _id_v3("conta", conta.id),
        "nome": conta.account_name,
        "tipo": conta.account_kind,
        "moeda": conta.currency,
        "titular": identidade(conta.owner.name),
        "instituicao": identidade(conta.institution.institution_name),
        "deep_link": endereco_da_conta(conta, timezone.localdate()),
    }



def _conta_v4(conta: FinancialAccount, referencia: date, saldos: dict[int, Decimal]) -> dict:
    existe_na_data = conta.initial_balance_date <= referencia
    return {
        "source_id": _source_id_v4("account", conta.id),
        "name": conta.account_name,
        "account_type": conta.account_kind,
        # Acréscimo opcional (02/10/2026): "pessoal" ou "administrada". Quem não
        # o conhece ignora, e o contrato continua v4.
        "purpose": conta.purpose,
        "currency": conta.currency,
        "owner": identidade(conta.owner.name),
        "owner_name": conta.owner.name,
        "institution": identidade(conta.institution.institution_name),
        "institution_name": conta.institution.institution_name,
        "institution_type": conta.institution.institution_type,
        "initial_balance": str(conta.initial_balance.quantize(MONEY_QUANT)),
        "initial_balance_date": conta.initial_balance_date.isoformat(),
        # Conta com início futuro não tem saldo disponível nesta fotografia;
        # não publicar zero como se fosse um saldo real.
        "balance": str(saldos.get(conta.id, Decimal("0.00")).quantize(MONEY_QUANT)) if existe_na_data else None,
        "balance_as_of": referencia.isoformat() if existe_na_data else None,
        "deep_link": endereco_da_conta(conta, referencia),
    }


def _categoria_v4(categoria: CashFlowCategory) -> dict:
    return {
        "source_id": _source_id_v4("category", categoria.id),
        "name": categoria.category_name,
        "kind": categoria.kind,
        # Acréscimo opcional (02/10/2026): o nome do grupo da categoria, ou null.
        "group": categoria.group.group_name if categoria.group_id else None,
        "created_at": categoria.created_at.isoformat() if categoria.created_at else None,
        "updated_at": categoria.updated_at.isoformat() if categoria.updated_at else None,
    }


def status_efetivo(entry: CashFlowEntry, referencia: date) -> str:
    """O status que vale em `referencia`, derivado da data e não do gravado.

    O `status` gravado só é normalizado quando alguém grava o lançamento: em
    aberto com vencimento passado continua `a_vencer` até lá. As telas
    compensam na leitura (`_balance_status_q`: projetado vencido conta como
    vencido), e cada consumidor que lê o contrato tinha de repetir a conta. Aqui
    ela sai pronta, pela mesma regra.
    """
    if entry.status == STATUS_REALIZED:
        return STATUS_REALIZED
    return STATUS_PENDING if entry.due_date < referencia else STATUS_PROJECTED


def _lancamento_v4(entry: CashFlowEntry, referencia: date) -> dict:
    return {
        "source_id": _source_id_v4("cash-entry", entry.id),
        "account_id": _source_id_v4("account", entry.account_id),
        "category_id": _source_id_v4("category", entry.category_id),
        "transfer_id": (
            _source_id_v4("transfer", entry.bank_operation_id)
            if entry.operation_type == OPERATION_INTERNAL_TRANSFER and entry.bank_operation_id
            else None
        ),
        "description": entry.description,
        "entry_type": entry.entry_type,
        "status": entry.status,
        # Acréscimo opcional (03/10/2026): o status que vale em `snapshot_as_of`.
        # Muda com o dia sem que o lançamento mude, então quem guarda a foto por
        # `high_watermark` precisa pedir uma nova a cada dia.
        "effective_status": status_efetivo(entry, referencia),
        "category_kind": entry.category.kind,
        "currency": entry.account.currency,
        "planned_amount": str(entry.entry_amount.quantize(MONEY_QUANT)),
        "realized_amount": (
            str(entry.realized_amount.quantize(MONEY_QUANT))
            if entry.realized_amount is not None
            else None
        ),
        "due_date": entry.due_date.isoformat(),
        "realized_date": entry.realized_date.isoformat() if entry.realized_date else None,
        "operation_type": entry.operation_type,
        "installment": entry.current_installment,
        "installments": entry.installments,
        "is_recurring": entry.is_recurring,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def _metadata_v4(referencia: date, watermark: str) -> dict:
    contas = list(FinancialAccount.objects.select_related("owner", "institution").order_by("id"))
    categorias = list(CashFlowCategory.objects.select_related("group").order_by("id"))
    lancamentos = CashFlowEntry.objects.order_by("id")
    periodo = lancamentos.aggregate(inicio=Min("due_date"), fim=Max("due_date"))
    entradas_transferencia = lancamentos.filter(
        Q(operation_type=OPERATION_INTERNAL_TRANSFER) | Q(category__kind=CATEGORY_KIND_TRANSFER)
    )
    sem_grupo = entradas_transferencia.filter(bank_operation_id__isnull=True).count()
    grupos_incompletos = (
        BankOperation.objects.filter(operation_type=OPERATION_INTERNAL_TRANSFER)
        .annotate(quantidade_pernas=Count("entries"))
        .filter(quantidade_pernas__lt=2)
        .count()
    )
    return {
        "contrato": CONTRATO_V4,
        "sistema": SISTEMA,
        "generated_at": timezone.now().isoformat(),
        "high_watermark": watermark,
        "capabilities": {
            "snapshot": True,
            "changes": True,
            "resources": {"account": True, "cash_entry": True, "category": True, "transfer": True},
            "decimal_strings": True,
            "read_only": True,
        },
        "coverage": {
            "account": {"state": "complete", "count": len(contas)},
            "cash_entry": {
                "state": "complete",
                "count": lancamentos.count(),
                "due_date_from": periodo["inicio"].isoformat() if periodo["inicio"] else None,
                "due_date_through": periodo["fim"].isoformat() if periodo["fim"] else None,
                "statuses": [STATUS_REALIZED, STATUS_PENDING, STATUS_PROJECTED],
            },
            "category": {"state": "complete", "count": len(categorias)},
            "transfer": {
                "state": "partial" if sem_grupo or grupos_incompletos else "complete",
                "unlinked_cash_entries": sem_grupo,
                "incomplete_groups": grupos_incompletos,
                "note": "Transferências são publicadas como agrupadores quando há operação bancária associada.",
            },
            "incremental_changes": {
                "state": "available",
                "endpoint": "/patrimonio/v4/changes",
                "mode": "snapshot_invalidation",
                "high_watermark": watermark,
            },
        },
        "snapshot_as_of": referencia.isoformat(),
    }


@require_GET
def metadata_v4_view(request):
    """Capacidades, cobertura e cursor da outbox v4, sob token exclusivo."""
    erro = _autorizacao_v3(request, "v4")
    if erro:
        return erro
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        payload = _metadata_v4(timezone.localdate(), _cursor_v4(_watermark_v4()))
    return _resposta_v3(payload)


@require_GET
def changes_v4_view(request):
    """Invalidações ordenadas; o consumidor reconcilia pelo snapshot v4."""
    erro = _autorizacao_v3(request, "v4")
    if erro:
        return erro
    try:
        after = _cursor_v4_ler(request.GET.get("after"))
        limit = _change_limit_v4(request)
    except ValueError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        watermark = _watermark_v4()
        rows = list(
            PatrimonioV4Outbox.objects.filter(cursor__gt=after, cursor__lte=watermark)
            .order_by("cursor")[: limit + 1]
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_position = rows[-1].cursor if rows else watermark
        source_resource = {"cash_entry": "cash-entry"}
        items = [
            {
                "cursor": _cursor_v4(item.cursor),
                "resource": item.resource,
                "source_id": _source_id_v4(source_resource.get(item.resource, item.resource), item.source_record_id),
                "operation": item.operation,
                "changed_at": item.changed_at.isoformat(),
                "payload": {"mode": "snapshot_required"} if item.operation == "upsert" else None,
            }
            for item in rows
        ]
        payload = {
            "contrato": CONTRATO_V4,
            "recurso": "changes",
            "sistema": SISTEMA,
            "mode": "snapshot_invalidation",
            "high_watermark": _cursor_v4(watermark),
            "next_cursor": _cursor_v4(next_position),
            "has_more": has_more,
            "items": items,
        }
    return _resposta_v3(payload)


@require_GET
def snapshot_v4_view(request):
    """Foto consistente dos recursos bancários já persistidos no CB."""
    erro = _autorizacao_v3(request, "v4")
    if erro:
        return erro
    referencia = timezone.localdate()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        watermark = _cursor_v4(_watermark_v4())
        metadata = _metadata_v4(referencia, watermark)
        contas = list(FinancialAccount.objects.select_related("owner", "institution").order_by("id"))
        contas_ativas = [conta.id for conta in contas if conta.initial_balance_date <= referencia]
        saldos = decimal_balances_before_by_account(
            contas_ativas, referencia + timedelta(days=1), VIEW_REALIZED
        )
        categorias = list(CashFlowCategory.objects.select_related("group").order_by("id"))
        lancamentos = list(
            CashFlowEntry.objects.select_related("account", "category")
            .order_by("id")
        )
        operacoes = list(
            BankOperation.objects.filter(operation_type=OPERATION_INTERNAL_TRANSFER)
            .prefetch_related(Prefetch("entries", queryset=CashFlowEntry.objects.select_related("account")))
            .order_by("id")
        )
        items = [
            {"resource": "account", "source_id": _source_id_v4("account", conta.id), "payload": _conta_v4(conta, referencia, saldos)}
            for conta in contas
        ]
        items.extend(
            {"resource": "category", "source_id": _source_id_v4("category", categoria.id), "payload": _categoria_v4(categoria)}
            for categoria in categorias
        )
        items.extend(
            {"resource": "cash_entry", "source_id": _source_id_v4("cash-entry", entry.id), "payload": _lancamento_v4(entry, referencia)}
            for entry in lancamentos
        )
        for operacao in operacoes:
            pernas = sorted(operacao.entries.all(), key=lambda entry: entry.id)
            items.append({
                "resource": "transfer",
                "source_id": _source_id_v4("transfer", operacao.id),
                "payload": {
                    "source_id": _source_id_v4("transfer", operacao.id),
                    "operation_type": operacao.operation_type,
                    "status": operacao.status,
                    "legs": [
                        {
                            "cash_entry_id": _source_id_v4("cash-entry", entry.id),
                            "account_id": _source_id_v4("account", entry.account_id),
                            "entry_type": entry.entry_type,
                            "currency": entry.account.currency,
                            "planned_amount": str(entry.entry_amount.quantize(MONEY_QUANT)),
                            "realized_amount": str(entry.realized_amount.quantize(MONEY_QUANT)) if entry.realized_amount is not None else None,
                        }
                        for entry in pernas
                    ],
                },
            })
        payload = {
            "contrato": CONTRATO_V4,
            "sistema": SISTEMA,
            "generated_at": metadata["generated_at"],
            "snapshot_id": f"{SISTEMA}:snapshot:{metadata['generated_at']}",
            "high_watermark": watermark,
            "capabilities": metadata["capabilities"],
            "coverage": metadata["coverage"],
            "snapshot_as_of": referencia.isoformat(),
            "items": items,
        }
    return _resposta_v3(payload)


# ---------------------------------------------------------------------------
# Projeção: o caixa que as contas terão se o que está lançado acontecer
# ---------------------------------------------------------------------------
#
# O consolidador desenha "quanto vou ter" somando esta projeção aos
# investimentos. Ele não recalcula saldo: a regra de status (o que é vencido, o
# que é a vencer) e o saldo de partida saem daqui, das mesmas funções que as
# telas de relatório usam.
#
# Três decisões que o consumidor precisa conhecer, porque mudam o número:
#
# - o ponto de partida é o saldo REALIZADO no fim do dia-base (hoje). Tudo o
#   que ainda não aconteceu entra depois, na data de vencimento;
# - lançamento vencido e não realizado não some: ele vai para o bloco
#   `vencidos` e, com `vencidos=incluir` (o padrão), entra no saldo já no
#   dia-base -- é dinheiro que ainda deve entrar ou sair;
# - saída para investimento (categoria de natureza "movimentacao") não é
#   despesa. Ela sai do caixa daqui, mas o patrimônio não diminui: vai para
#   `investimentos_saida`, e o consolidador soma esse valor aos investimentos.
#   Sem isso, cada aporte programado apareceria como perda no patrimônio.
#
# E a projeção termina onde terminam os lançamentos. As recorrências só são
# geradas até o horizonte configurado em Parâmetros; depois dele a curva
# ficaria plana e pareceria que a renda parou. Por isso o contrato publica
# `horizonte`, e o consumidor marca esse limite no gráfico.

PROJECAO_DIAS_PADRAO = 183
PROJECAO_MAX_DIAS = 1100
PROJECAO_MAX_LANCAMENTOS = 10_000
VENCIDOS_OPCOES = {"incluir", "excluir"}


def _dinheiro(valor: Decimal) -> str:
    return str(valor.quantize(MONEY_QUANT))


def _primeiro_dia(dia: date) -> date:
    return date(dia.year, dia.month, 1)


def _proximo_mes(dia: date) -> date:
    return date(dia.year + dia.month // 12, dia.month % 12 + 1, 1)


def _mes_vazio(mes: date, moeda: str) -> dict:
    zero = Decimal("0.00")
    return {
        "mes": mes,
        "moeda": moeda,
        "entradas": zero,
        "saidas": zero,
        "transferencias_entrada": zero,
        "transferencias_saida": zero,
        "investimentos_entrada": zero,
        "investimentos_saida": zero,
        "saldo_final": zero,
    }


def _coluna_do_lancamento(entry: CashFlowEntry) -> str:
    receita = entry.entry_type == ENTRY_TYPE_INCOME
    natureza = entry.category.kind
    if natureza == CATEGORY_KIND_MOVEMENT:
        return "investimentos_entrada" if receita else "investimentos_saida"
    if natureza == CATEGORY_KIND_TRANSFER:
        return "transferencias_entrada" if receita else "transferencias_saida"
    return "entradas" if receita else "saidas"


def _assinado(entry: CashFlowEntry) -> Decimal:
    return entry.entry_amount if entry.entry_type == ENTRY_TYPE_INCOME else -entry.entry_amount


def _endereco_do_mes(mes: date) -> str:
    valor = mes.strftime("%Y-%m")
    consulta = urlencode({"start_month": valor, "end_month": valor, "mode": VIEW_ALL})
    return f"{reverse('reports:projections_view')}?{consulta}"


def montar_projecao(hoje: date, fim: date, *, incluir_vencidos: bool = True) -> dict:
    """A projeção do caixa de todas as contas, de `hoje` até `fim`.

    Cada conta fica na sua moeda e nada é somado entre moedas. O resultado é
    determinístico para um mesmo estado do banco e uma mesma data-base.
    """
    from core.services import get_recurring_projection_settings, system_start_date

    contas = _contas_v3()
    ids = [conta.id for conta in contas]
    moeda_da_conta = {conta.id: conta.currency for conta in contas}
    moedas = sorted(set(moeda_da_conta.values()))
    saldos = decimal_balances_before_by_account(ids, hoje + timedelta(days=1), VIEW_REALIZED)
    saldo_da_conta = {conta_id: saldos.get(conta_id, Decimal("0.00")) for conta_id in ids}
    saldo_inicial_da_conta = dict(saldo_da_conta)

    def saldo_da_moeda(moeda: str) -> Decimal:
        return sum(
            (saldo for conta_id, saldo in saldo_da_conta.items() if moeda_da_conta[conta_id] == moeda),
            Decimal("0.00"),
        )

    saldo_inicial_por_moeda = {moeda: saldo_da_moeda(moeda) for moeda in moedas}

    piso = system_start_date() or date.min
    abertos = list(
        CashFlowEntry.objects.select_related("category")
        .filter(account_id__in=ids)
        .filter(
            Q(status=STATUS_PROJECTED, due_date__gte=hoje, due_date__lte=fim)
            | Q(status__in=(STATUS_PROJECTED, STATUS_PENDING), due_date__lt=hoje, due_date__gte=piso)
        )
        .order_by("due_date", "-entry_type", "id")[: PROJECAO_MAX_LANCAMENTOS + 1]
    )
    if len(abertos) > PROJECAO_MAX_LANCAMENTOS:
        raise ValueError(
            f"a projeção passaria de {PROJECAO_MAX_LANCAMENTOS} lançamentos; encurte o período"
        )

    vencidos: dict[str, dict] = {}
    futuros: list[CashFlowEntry] = []
    for entry in abertos:
        if entry.due_date >= hoje:
            futuros.append(entry)
            continue
        moeda = moeda_da_conta[entry.account_id]
        bloco = vencidos.setdefault(
            moeda,
            {"moeda": moeda, "entradas": Decimal("0.00"), "saidas": Decimal("0.00"), "quantidade": 0},
        )
        chave = "entradas" if entry.entry_type == ENTRY_TYPE_INCOME else "saidas"
        bloco[chave] += entry.entry_amount
        bloco["quantidade"] += 1
        if incluir_vencidos:
            saldo_da_conta[entry.account_id] += _assinado(entry)

    menor_da_conta = {conta_id: (saldo, hoje) for conta_id, saldo in saldo_da_conta.items()}
    # Cada ponto da série é (dia, saldo da moeda, investido até o dia): o
    # investido acumula o que saiu do caixa para investimento menos o que voltou
    # dele. O consumidor soma esse valor aos investimentos no mesmo dia em que o
    # caixa cai -- sem ele, o patrimônio projetado despencaria no dia do aporte.
    investido = {moeda: Decimal("0.00") for moeda in moedas}
    serie = {moeda: [(hoje, saldo_da_moeda(moeda), investido[moeda])] for moeda in moedas}
    menor_da_moeda = {moeda: (hoje, serie[moeda][0][1]) for moeda in moedas}

    meses: dict[tuple[date, str], dict] = {}
    mes = _primeiro_dia(hoje)
    while mes <= fim:
        for moeda in moedas:
            meses[(mes, moeda)] = _mes_vazio(mes, moeda)
        mes = _proximo_mes(mes)

    # Os lançamentos vêm ordenados por data. O saldo registrado para um dia é o
    # do fim dele, depois de todos os lançamentos daquela data.
    for indice, entry in enumerate(futuros):
        saldo_da_conta[entry.account_id] += _assinado(entry)
        moeda = moeda_da_conta[entry.account_id]
        coluna = _coluna_do_lancamento(entry)
        meses[(_primeiro_dia(entry.due_date), moeda)][coluna] += entry.entry_amount
        if coluna == "investimentos_saida":
            investido[moeda] += entry.entry_amount
        elif coluna == "investimentos_entrada":
            investido[moeda] -= entry.entry_amount

        proximo = futuros[indice + 1] if indice + 1 < len(futuros) else None
        if proximo is not None and proximo.due_date == entry.due_date:
            continue
        for conta_id, saldo in saldo_da_conta.items():
            if saldo < menor_da_conta[conta_id][0]:
                menor_da_conta[conta_id] = (saldo, entry.due_date)
        for moeda_do_dia in moedas:
            atual = saldo_da_moeda(moeda_do_dia)
            _dia, saldo_anterior, investido_anterior = serie[moeda_do_dia][-1]
            if (atual, investido[moeda_do_dia]) != (saldo_anterior, investido_anterior):
                serie[moeda_do_dia].append((entry.due_date, atual, investido[moeda_do_dia]))
            if atual < menor_da_moeda[moeda_do_dia][1]:
                menor_da_moeda[moeda_do_dia] = (entry.due_date, atual)

    # O saldo no fim de cada mês é o último ponto da série até aquele mês.
    for (mes, moeda), linha in meses.items():
        fim_do_mes = _proximo_mes(mes) - timedelta(days=1)
        linha["saldo_final"] = [valor for dia, valor, _investido in serie[moeda] if dia <= fim_do_mes][-1]

    configuracao = get_recurring_projection_settings()
    ultima_recorrencia = CashFlowEntry.objects.filter(
        status=STATUS_PROJECTED, operation_type=OPERATION_RECURRING
    ).aggregate(fim=Max("due_date"))["fim"]
    # Série criada com um horizonte maior pode ter passado do fim atual; o
    # limite do gráfico é o fim do horizonte, onde TODAS as séries chegam.
    if ultima_recorrencia is not None:
        from transactions.recurring_projection import recurring_projection_horizon_end

        ultima_recorrencia = min(ultima_recorrencia, recurring_projection_horizon_end(hoje))

    return {
        "contrato": CONTRATO_V3,
        "recurso": "projecao",
        "sistema": SISTEMA,
        "gerado_em": timezone.now().isoformat(),
        "data_base": hoje.isoformat(),
        "fim": fim.isoformat(),
        "vencidos_incluidos": incluir_vencidos,
        "horizonte": {
            "meses_configurados": configuracao.horizon_months,
            "ultima_recorrencia": ultima_recorrencia.isoformat() if ultima_recorrencia else None,
        },
        "saldos_iniciais": [
            {"moeda": moeda, "saldo": _dinheiro(saldo_inicial_por_moeda[moeda])} for moeda in moedas
        ],
        "vencidos": [
            {
                "moeda": bloco["moeda"],
                "entradas": _dinheiro(bloco["entradas"]),
                "saidas": _dinheiro(bloco["saidas"]),
                "quantidade": bloco["quantidade"],
            }
            for _moeda, bloco in sorted(vencidos.items())
        ],
        "contas": [
            {
                **_conta_v3(conta),
                "saldo_inicial": _dinheiro(saldo_inicial_da_conta[conta.id]),
                "saldo_final": _dinheiro(saldo_da_conta[conta.id]),
                "menor_saldo": {
                    "valor": _dinheiro(menor_da_conta[conta.id][0]),
                    "data": menor_da_conta[conta.id][1].isoformat(),
                },
            }
            for conta in contas
        ],
        "menor_saldo": [
            {
                "moeda": moeda,
                "data": menor_da_moeda[moeda][0].isoformat(),
                "valor": _dinheiro(menor_da_moeda[moeda][1]),
            }
            for moeda in moedas
        ],
        "serie": [
            {
                "moeda": moeda,
                "data": dia.isoformat(),
                "saldo": _dinheiro(valor),
                "investido_acumulado": _dinheiro(acumulado),
            }
            for moeda in moedas
            for dia, valor, acumulado in serie[moeda]
        ],
        "meses": [
            {
                "mes": linha["mes"].strftime("%Y-%m"),
                "moeda": linha["moeda"],
                **{chave: _dinheiro(valor) for chave, valor in linha.items() if isinstance(valor, Decimal)},
                "deep_link": _endereco_do_mes(linha["mes"]),
            }
            for _chave, linha in sorted(meses.items())
        ],
    }


@require_GET
def projecao_v3_view(request):
    """Projeção do caixa a partir de hoje, somente leitura.

    Parâmetros: `fim` (AAAA-MM-DD; padrão hoje + 183 dias, no máximo
    `PROJECAO_MAX_DIAS` à frente) e `vencidos` (`incluir`, o padrão, ou
    `excluir`). A data-base é sempre hoje: projetar a partir de uma data
    passada misturaria o que aconteceu com o que estava previsto.
    """
    erro = _autorizacao_v3(request)
    if erro:
        return erro
    hoje = timezone.localdate()
    bruto_fim = request.GET.get("fim", "")
    try:
        fim = _data_da_query(bruto_fim, "fim") if bruto_fim else hoje + timedelta(days=PROJECAO_DIAS_PADRAO)
    except ValueError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)
    if fim < hoje:
        return JsonResponse({"erro": "fim não pode ser anterior a hoje"}, status=400)
    if (fim - hoje).days > PROJECAO_MAX_DIAS:
        return JsonResponse({"erro": f"fim excede o limite de {PROJECAO_MAX_DIAS} dias à frente"}, status=400)
    opcao_vencidos = request.GET.get("vencidos", "incluir")
    if opcao_vencidos not in VENCIDOS_OPCOES:
        return JsonResponse({"erro": "vencidos deve ser incluir ou excluir"}, status=400)
    try:
        payload = montar_projecao(hoje, fim, incluir_vencidos=opcao_vencidos == "incluir")
    except ValueError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)
    return _resposta_v3(payload)
