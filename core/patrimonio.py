"""O resumo que este sistema publica para o consolidador de patrimônio.

POR QUE UM RESUMO PUBLICADO, E NÃO UM BANCO COMPARTILHADO

O `NetWorth` precisa somar o caixa daqui com os investimentos do Controle de
Renda Variável. Havia três formas de o dado chegar lá, e duas foram recusadas
na decisão de 16/09/2026: ler o banco alheio acopla os schemas e quebra a cada
migration, e abrir uma API de negócio é uma decisão de arquitetura que nenhum
dos dois sistemas pediu.

Sobrou a terceira, que é a mais barata e a mais honesta: **cada sistema publica
uma foto do que ele sabe**, no vocabulário comum, e o consolidador só soma. Ele
não escreve nada, não conhece regra financeira nenhuma e pode ser jogado fora
sem prejuízo -- mas o contrato que ele consome é o mesmo que a fusão dos dois
sistemas herdaria um dia.

O VOCABULÁRIO COMUM

O que os dois sistemas enxergam é identificado **pelo nome normalizado**, não
pelo id do banco: "C6" é a instituição 7 aqui e outra coisa lá, mas é a mesma
instituição para quem lê. O que só existe aqui -- a conta -- leva um id
prefixado pelo sistema, opaco para quem consome.

Todo valor é **texto**, nunca número JSON: `float` não representa 0,10 e o
consolidador somaria centavos que não existem. Toda data é ISO 8601. Toda moeda
é ISO 4217, explícita, e **nada é somado entre moedas** -- o total vem separado
por moeda, porque converter é decidir, e a decisão é de quem consolida.

O QUE ESTE SISTEMA NÃO PREENCHE

`posicoes`, `proventos` e `ativos_alternativos` saem vazios, de propósito: o
contrato é um só para os dois publicadores, e cada um preenche o que é dele.
Um consumidor que receba lista vazia sabe que este sistema não tem nada a dizer
sobre aquilo; um consumidor que não visse a chave não saberia de nada.

O ENDEREÇO É DAQUI

Cada conta leva `endereco`: o caminho, relativo à raiz deste sistema, da sua
própria página na data da foto. Quem consome não monta esse caminho a partir do
id, que é opaco; ele junta o caminho ao endereço público que já conhece e para
por aí. Assim, se a rota da conta mudar, muda aqui, e o link do outro lado
continua certo.

A CHAVE É A PERMISSÃO

A rota publica **todas as contas**, sem escopo de usuário, e isso é deliberado:
um resumo filtrado por titular produziria um patrimônio consolidado que esconde
contas sem avisar -- o pior tipo de número errado. Quem tem o token lê o saldo
de todas as contas deste sistema. Guarde-o como se guarda uma senha de banco.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import unicodedata
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Case, CharField, Count, DateField, F, Max, Min, Sum, Value, When
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from sharedauth.secrets import SegredoInvalidoError, resolver_segredo

from banking.models import FinancialAccount
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    STATUS_PENDING,
    STATUS_PROJECTED,
    STATUS_REALIZED,
    VIEW_REALIZED,
)
from reports.services import decimal_balances_before_by_account
from transactions.models import CashFlowCategory, CashFlowEntry

CONTRATO = "patrimonio/v1"
CONTRATO_V2 = "patrimonio/v2"
CONTRATO_V3 = "patrimonio/v3"
SISTEMA = "controle-bancario"

#: O token não tem valor padrão e não é gerado: sem ele configurado, a rota não
#: atende. Um segredo gerado no arranque valeria até o próximo reinício e daria
#: a impressão de que a integração está configurada quando não está.
NOME_DO_SEGREDO = "PATRIMONIO_TOKEN"
COMPRIMENTO_MINIMO_DO_TOKEN = 32
# O Dashboard oferece recortes de até cinco anos e "Tudo". O teto de dez
# anos impede uma consulta acidentalmente sem limite sem bloquear esses usos.
MAX_FLUXO_DIAS = 3654
MONEY_QUANT = Decimal("0.01")
V3_PAGE_SIZE = 50
V3_MAX_PAGE_SIZE = 100

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


def _autorizacao_v3(request):
    """Retorna uma resposta de erro ou ``None`` quando o Bearer é válido."""
    esperado = _token_configurado()
    if not esperado:
        return JsonResponse(
            {"erro": "integração de patrimônio não configurada neste servidor"},
            status=503,
        )
    recebido = _token_da_requisicao(request)
    if not recebido or not secrets.compare_digest(recebido, esperado):
        logger.warning("Contrato patrimonial v3 recusado: token ausente ou inválido.")
        return JsonResponse({"erro": "não autorizado"}, status=401)
    return None


def _paginacao_v3(request):
    """Lê os parâmetros de página, mantendo aliases em português."""
    bruto_pagina = request.GET.get("page", request.GET.get("pagina", "1"))
    bruto_tamanho = request.GET.get("page_size", request.GET.get("tamanho", str(V3_PAGE_SIZE)))
    try:
        pagina = int(bruto_pagina)
        tamanho = int(bruto_tamanho)
    except (TypeError, ValueError):
        raise ValueError("page e page_size devem ser inteiros positivos") from None
    if pagina < 1 or tamanho < 1 or tamanho > V3_MAX_PAGE_SIZE:
        raise ValueError(f"page deve ser positivo e page_size deve estar entre 1 e {V3_MAX_PAGE_SIZE}")
    return pagina, tamanho


def _link_pagina_v3(request, numero: int) -> str:
    params = request.GET.copy()
    params.pop("page", None)
    params.pop("pagina", None)
    pares = [("page", str(numero))]
    for chave in sorted(params):
        pares.extend((chave, valor) for valor in params.getlist(chave))
    return f"{request.path}?{urlencode(pares)}"


def _intervalo_v3(request, *, required: bool = False) -> tuple[date | None, date | None]:
    bruto_inicio = request.GET.get("inicio", "")
    bruto_fim = request.GET.get("fim", "")
    if required and not bruto_inicio and not bruto_fim:
        raise ValueError("informe inicio ou fim para o período")
    try:
        inicio = _data_da_query(bruto_inicio, "inicio") if bruto_inicio else None
        fim = _data_da_query(bruto_fim, "fim") if bruto_fim else None
    except ValueError as erro:
        raise ValueError(str(erro)) from None
    if inicio and fim and inicio > fim:
        raise ValueError("inicio não pode ser posterior a fim")
    if inicio and fim and (fim - inicio).days + 1 > MAX_FLUXO_DIAS:
        raise ValueError(f"intervalo excede o limite de {MAX_FLUXO_DIAS} dias")
    return inicio, fim


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


def _token_configurado() -> str | None:
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
            NOME_DO_SEGREDO,
            aceitar_variavel=not exige_arquivo,
            obrigatorio=False,
            comprimento_minimo=COMPRIMENTO_MINIMO_DO_TOKEN,
            valores_recusados=frozenset({"troque-este-token", "changeme"}),
        )
    except SegredoInvalidoError as erro:
        logger.error("%s configurado mas recusado: %s", NOME_DO_SEGREDO, erro)
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


def montar_resumo(referencia: date) -> dict:
    """A foto deste sistema na data pedida."""
    contas = list(
        FinancialAccount.objects.select_related("owner", "institution")
        .filter(initial_balance_date__lte=referencia)
        .order_by("id")
    )
    saldos = decimal_balances_before_by_account(
        (conta.id for conta in contas), referencia + timedelta(days=1), VIEW_REALIZED
    )

    titulares = {}
    instituicoes = {}
    linhas = []
    totais: dict[str, dict] = {}
    for conta in contas:
        titular = identidade(conta.owner.name)
        instituicao = identidade(conta.institution.institution_name)
        titulares[titular] = {"id": titular, "nome": conta.owner.name}
        instituicoes[instituicao] = {
            "id": instituicao,
            "nome": conta.institution.institution_name,
            "tipo": conta.institution.institution_type,
        }
        saldo = saldos.get(conta.id, Decimal("0.00"))
        linhas.append(
            {
                "id": f"{SISTEMA}:conta:{conta.id}",
                "titular": titular,
                "instituicao": instituicao,
                "nome": conta.account_name,
                "moeda": conta.currency,
                "saldo": str(saldo),
                "saldo_inicial_em": conta.initial_balance_date.isoformat(),
                "endereco": endereco_da_conta(conta, referencia),
            }
        )
        # `total` e `linhas`, e não `saldo` e `contas`: o mesmo envelope serve
        # aos dois publicadores, e do outro lado a linha é uma posição, não uma
        # conta. Um contrato com duas grafias para a mesma ideia é o tipo de
        # coisa que só cobra o preço depois.
        total = totais.setdefault(conta.currency, {"moeda": conta.currency, "total": Decimal("0.00"), "linhas": 0})
        total["total"] += saldo
        total["linhas"] += 1

    return {
        "contrato": CONTRATO,
        "sistema": SISTEMA,
        "papel": "caixa",
        "gerado_em": timezone.now().isoformat(),
        "data_de_referencia": referencia.isoformat(),
        "titulares": [titulares[chave] for chave in sorted(titulares)],
        "instituicoes": [instituicoes[chave] for chave in sorted(instituicoes)],
        "contas": linhas,
        "totais_por_moeda": [
            {"moeda": moeda, "total": str(dados["total"]), "linhas": dados["linhas"]}
            for moeda, dados in sorted(totais.items())
        ],
        # Preenchidos pelo outro publicador. Ver o cabeçalho deste módulo.
        "posicoes": [],
        "proventos": [],
        "ativos_alternativos": [],
    }


@require_GET
def resumo_view(request):
    """`GET /patrimonio/v1/resumo?data=AAAA-MM-DD`, autenticada por token."""
    esperado = _token_configurado()
    if not esperado:
        # Não é "não autorizado": é "ninguém ligou esta integração ainda". Dizer
        # 401 aqui mandaria o operador procurar um token errado por horas.
        return JsonResponse(
            {"erro": "integração de patrimônio não configurada neste servidor"},
            status=503,
        )

    recebido = _token_da_requisicao(request)
    if not recebido or not secrets.compare_digest(recebido, esperado):
        logger.warning("Resumo patrimonial recusado: token ausente ou inválido.")
        return JsonResponse({"erro": "não autorizado"}, status=401)

    bruto = request.GET.get("data", "")
    if bruto:
        try:
            referencia = date.fromisoformat(bruto)
        except ValueError:
            return JsonResponse({"erro": "data inválida: use AAAA-MM-DD"}, status=400)
    else:
        referencia = timezone.localdate()

    resposta = JsonResponse(montar_resumo(referencia))
    # A foto é de um instante e carrega saldo de todas as contas: nenhum
    # intermediário deve guardá-la.
    resposta["Cache-Control"] = "no-store"
    return resposta


def _novo_fluxo(data: date, moeda: str, natureza: str) -> dict:
    return {
        "data": data.isoformat(),
        "moeda": moeda,
        "natureza": natureza,
        "entradas": Decimal("0.00"),
        "saidas": Decimal("0.00"),
        "liquido": Decimal("0.00"),
        "linhas": 0,
    }


def _adicionar_fluxo(
    fluxo: dict, valor: Decimal, *, entrada: bool, linhas: int = 1
) -> None:
    valor = valor.quantize(MONEY_QUANT)
    if entrada:
        fluxo["entradas"] += valor
        fluxo["liquido"] += valor
    else:
        fluxo["saidas"] += valor
        fluxo["liquido"] -= valor
    fluxo["linhas"] += linhas


def montar_fluxos(inicio: date, fim: date) -> list[dict]:
    """Agrega fatos realizados e saldos iniciais no período inclusivo.

    A agregação deliberadamente não expõe movimentos individuais: a chave do
    grupo é data, moeda e natureza. A moeda nunca entra na soma da outra, e
    somente ``realized_date``/``realized_amount`` são considerados para
    lançamentos.
    """
    agregados: dict[tuple[date, str, str], dict] = {}

    def grupo(data: date, moeda: str, natureza: str) -> dict:
        chave = (data, moeda, natureza)
        return agregados.setdefault(chave, _novo_fluxo(data, moeda, natureza))

    # O saldo inicial é um fato datado. Quando a sua data cai no intervalo,
    # ele aparece como ajuste_de_base; bases anteriores ao período pertencem à
    # fotografia da conta, não são um fluxo diário deste recorte.
    contas = FinancialAccount.objects.filter(
        initial_balance_date__gte=inicio,
        initial_balance_date__lte=fim,
    ).only("id", "currency", "initial_balance", "initial_balance_date")
    for conta in contas:
        saldo = conta.initial_balance.quantize(MONEY_QUANT)
        fluxo = grupo(conta.initial_balance_date, conta.currency, "ajuste_de_base")
        _adicionar_fluxo(fluxo, abs(saldo), entrada=saldo >= 0)

    # O banco reduz primeiro por data/moeda/natureza/tipo. Mesmo um intervalo
    # de anos devolve apenas os grupos que o contrato publica, e nunca carrega
    # milhares de movimentos financeiros como objetos Python.
    natureza = Case(
        When(
            operation_type=OPERATION_INTERNAL_TRANSFER,
            then=Value(CATEGORY_KIND_TRANSFER),
        ),
        When(category__kind=CATEGORY_KIND_TRANSFER, then=Value(CATEGORY_KIND_TRANSFER)),
        When(category__kind=CATEGORY_KIND_MOVEMENT, then=Value(CATEGORY_KIND_MOVEMENT)),
        default=Value(CATEGORY_KIND_MANAGERIAL),
        output_field=CharField(),
    )
    lancamentos = (
        CashFlowEntry.objects
        .filter(
            status=STATUS_REALIZED,
            realized_date__gte=inicio,
            realized_date__lte=fim,
            realized_amount__isnull=False,
        )
        .annotate(moeda=F("account__currency"), natureza_publicada=natureza)
        .values("realized_date", "moeda", "natureza_publicada", "entry_type")
        .annotate(total=Sum("realized_amount"), linhas=Count("id"))
        .order_by("realized_date", "moeda", "natureza_publicada", "entry_type")
    )
    for lancamento in lancamentos:
        fluxo = grupo(
            lancamento["realized_date"],
            lancamento["moeda"],
            lancamento["natureza_publicada"],
        )
        _adicionar_fluxo(
            fluxo,
            lancamento["total"],
            entrada=lancamento["entry_type"] == ENTRY_TYPE_INCOME,
            linhas=lancamento["linhas"],
        )

    resultado = []
    for chave in sorted(agregados):
        fluxo = agregados[chave]
        resultado.append(
            {
                **{campo: fluxo[campo] for campo in ("data", "moeda", "natureza")},
                "entradas": str(fluxo["entradas"].quantize(MONEY_QUANT)),
                "saidas": str(fluxo["saidas"].quantize(MONEY_QUANT)),
                "liquido": str(fluxo["liquido"].quantize(MONEY_QUANT)),
                "linhas": fluxo["linhas"],
            }
        )
    return resultado


def _data_da_query(valor: str, nome: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{nome} inválida: use AAAA-MM-DD") from None


@require_GET
def resumo_v2_view(request):
    """`GET /patrimonio/v2/resumo`, foto no fim e fluxos no intervalo."""
    esperado = _token_configurado()
    if not esperado:
        return JsonResponse(
            {"erro": "integração de patrimônio não configurada neste servidor"},
            status=503,
        )

    recebido = _token_da_requisicao(request)
    if not recebido or not secrets.compare_digest(recebido, esperado):
        logger.warning("Resumo patrimonial v2 recusado: token ausente ou inválido.")
        return JsonResponse({"erro": "não autorizado"}, status=401)

    bruto_fim = request.GET.get("data", "")
    bruto_inicio = request.GET.get("inicio", "")
    try:
        fim = _data_da_query(bruto_fim, "data") if bruto_fim else timezone.localdate()
        inicio = _data_da_query(bruto_inicio, "inicio") if bruto_inicio else fim
    except ValueError as erro:
        return JsonResponse({"erro": str(erro)}, status=400)
    if inicio > fim:
        return JsonResponse({"erro": "inicio não pode ser posterior a data"}, status=400)
    if (fim - inicio).days + 1 > MAX_FLUXO_DIAS:
        return JsonResponse(
            {"erro": f"intervalo de fluxos excede o limite de {MAX_FLUXO_DIAS} dias"},
            status=400,
        )

    # A foto e os fluxos precisam enxergar o mesmo estado do banco. Sem este
    # snapshot, uma realização concorrente entre as duas consultas poderia
    # publicar saldo novo com fluxo antigo (ou o inverso).
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        resposta = montar_resumo(fim)
        resposta["contrato"] = CONTRATO_V2
        resposta["periodo_dos_fluxos"] = {
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
            "criterio": STATUS_REALIZED,
            "granularidade": "dia",
        }
        resposta["fluxos"] = montar_fluxos(inicio, fim)
    resultado = JsonResponse(resposta)
    resultado["Cache-Control"] = "no-store"
    return resultado


def _contas_v3() -> list[FinancialAccount]:
    return list(
        FinancialAccount.objects.select_related("owner", "institution").order_by("id")
    )


def _conta_v3(conta: FinancialAccount) -> dict:
    return {
        "id": _id_v3("conta", conta.id),
        "nome": conta.account_name,
        "moeda": conta.currency,
        "titular": identidade(conta.owner.name),
        "instituicao": identidade(conta.institution.institution_name),
        "deep_link": endereco_da_conta(conta, timezone.localdate()),
    }


def _categoria_v3(categoria) -> dict:
    return {
        "id": _id_v3("categoria", categoria.id),
        "nome": categoria.category_name,
        "natureza": categoria.kind,
        "criada_em": categoria.created_at.isoformat(),
        "atualizada_em": categoria.updated_at.isoformat(),
        "deep_link": reverse("transactions:categories_view"),
    }


def _mapa_ids_v3(model, recurso: str) -> dict[str, int]:
    return {
        _id_v3(recurso, int(pk)): int(pk)
        for pk in model.objects.values_list("id", flat=True)
    }


def _atividade_v3(entry: CashFlowEntry) -> dict:
    conta = entry.account
    categoria = entry.category
    data_atividade = entry.realized_date or entry.due_date
    operacao = None
    if entry.bank_operation_id:
        operacao = {
            "id": _id_v3("operacao", entry.bank_operation_id),
            "tipo": entry.operation_type,
            "parcela": entry.current_installment,
            "total_parcelas": entry.installments,
        }
    return {
        "id": _id_v3("atividade", entry.id),
        "data": data_atividade.isoformat(),
        "data_vencimento": entry.due_date.isoformat(),
        "data_realizacao": entry.realized_date.isoformat() if entry.realized_date else None,
        "descricao": entry.description,
        "tipo": entry.entry_type,
        "status": entry.status,
        "moeda": conta.currency,
        "valor_previsto": str(entry.entry_amount.quantize(MONEY_QUANT)),
        "valor_realizado": (
            str(entry.realized_amount.quantize(MONEY_QUANT))
            if entry.realized_amount is not None
            else None
        ),
        "conta": {
            "id": _id_v3("conta", conta.id),
            "nome": conta.account_name,
            "titular": identidade(conta.owner.name),
            "instituicao": identidade(conta.institution.institution_name),
            "deep_link": endereco_da_conta(conta, data_atividade),
        },
        "categoria": {
            "id": _id_v3("categoria", categoria.id),
            "nome": categoria.category_name,
            "natureza": categoria.kind,
            "deep_link": reverse("transactions:categories_view"),
        },
        "operacao": operacao,
        "deep_link": reverse("transactions:transaction_edit", kwargs={"tx_id": entry.id}),
    }


@require_GET
def atividades_v3_view(request):
    """Atividades individuais, somente leitura e com paginação estável.

    A rota usa o mesmo Bearer global dos contratos patrimoniais existentes.
    Ela não cria escopo de titular novo nem oferece mutações; a exposição é
    deliberadamente limitada aos campos já persistidos no lançamento.
    """
    erro = _autorizacao_v3(request)
    if erro:
        return erro
    try:
        inicio, fim = _intervalo_v3(request)
        pagina, tamanho = _paginacao_v3(request)
    except ValueError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)

    account_ids = _mapa_ids_v3(FinancialAccount, "conta")
    category_ids = _mapa_ids_v3(CashFlowCategory, "categoria")
    conta_ref = request.GET.get("conta", "")
    categoria_ref = request.GET.get("categoria", "")
    try:
        conta_id = account_ids[conta_ref] if conta_ref else None
        categoria_id = category_ids[categoria_ref] if categoria_ref else None
    except KeyError:
        return JsonResponse({"erro": "conta ou categoria desconhecida"}, status=400)

    status = request.GET.get("status", "")
    if status and status not in {STATUS_PROJECTED, STATUS_PENDING, STATUS_REALIZED}:
        return JsonResponse({"erro": "status inválido"}, status=400)
    natureza_filtro = request.GET.get("natureza", "")
    naturezas = {CATEGORY_KIND_MANAGERIAL, CATEGORY_KIND_MOVEMENT, CATEGORY_KIND_TRANSFER}
    if natureza_filtro and natureza_filtro not in naturezas:
        return JsonResponse({"erro": "natureza inválida"}, status=400)

    data_atividade = Case(
        When(realized_date__isnull=False, then=F("realized_date")),
        default=F("due_date"),
        output_field=DateField(),
    )
    queryset = (
        CashFlowEntry.objects.select_related("account__owner", "account__institution", "category")
        .annotate(data_atividade=data_atividade)
        .order_by("-data_atividade", "-id")
    )
    if inicio:
        queryset = queryset.filter(data_atividade__gte=inicio)
    if fim:
        queryset = queryset.filter(data_atividade__lte=fim)
    if conta_id:
        queryset = queryset.filter(account_id=conta_id)
    if categoria_id:
        queryset = queryset.filter(category_id=categoria_id)
    if status:
        queryset = queryset.filter(status=status)
    if natureza_filtro:
        queryset = queryset.filter(category__kind=natureza_filtro)

    paginado = Paginator(queryset, tamanho).get_page(pagina)
    numero_paginas = paginado.paginator.num_pages
    paginacao = {
        "pagina": paginado.number,
        "tamanho": tamanho,
        "total": paginado.paginator.count,
        "paginas": numero_paginas,
        "tem_anterior": paginado.has_previous(),
        "tem_proxima": paginado.has_next(),
        "anterior": _link_pagina_v3(request, paginado.previous_page_number()) if paginado.has_previous() else None,
        "proxima": _link_pagina_v3(request, paginado.next_page_number()) if paginado.has_next() else None,
    }
    return _resposta_v3({
        "contrato": CONTRATO_V3,
        "recurso": "atividades",
        "sistema": SISTEMA,
        "gerado_em": timezone.now().isoformat(),
        "filtros": {
            "inicio": inicio.isoformat() if inicio else None,
            "fim": fim.isoformat() if fim else None,
            "conta": conta_ref or None,
            "categoria": categoria_ref or None,
            "status": status or None,
            "natureza": natureza_filtro or None,
        },
        "paginacao": paginacao,
        "itens": [_atividade_v3(entry) for entry in paginado.object_list],
    })


@require_GET
def categorias_v3_view(request):
    """Categorias persistidas, em ordem determinística e somente leitura."""
    erro = _autorizacao_v3(request)
    if erro:
        return erro
    categorias = CashFlowCategory.objects.order_by("category_name", "id")
    natureza = request.GET.get("natureza", "")
    if natureza:
        if natureza not in {CATEGORY_KIND_MANAGERIAL, CATEGORY_KIND_MOVEMENT, CATEGORY_KIND_TRANSFER}:
            return JsonResponse({"erro": "natureza inválida"}, status=400)
        categorias = categorias.filter(kind=natureza)
    return _resposta_v3({
        "contrato": CONTRATO_V3,
        "recurso": "categorias",
        "sistema": SISTEMA,
        "gerado_em": timezone.now().isoformat(),
        "itens": [_categoria_v3(categoria) for categoria in categorias],
    })


@require_GET
def metadata_v3_view(request):
    """Metadados estáveis para montar filtros e links sem duplicar o domínio."""
    erro = _autorizacao_v3(request)
    if erro:
        return erro
    contas = _contas_v3()
    categorias = list(CashFlowCategory.objects.order_by("category_name", "id"))
    datas = CashFlowEntry.objects.aggregate(
        inicio=Min("due_date"), fim=Max("due_date")
    )
    return _resposta_v3({
        "contrato": CONTRATO_V3,
        "recurso": "metadata",
        "sistema": SISTEMA,
        "gerado_em": timezone.now().isoformat(),
        "capacidades": {
            "contas": True,
            "atividades": True,
            "categorias": True,
            "escrita": False,
            "paginacao_atividades": True,
        },
        "paginacao": {"padrao": V3_PAGE_SIZE, "maximo": V3_MAX_PAGE_SIZE},
        "periodo_disponivel": {
            "inicio": datas["inicio"].isoformat() if datas["inicio"] else None,
            "fim": datas["fim"].isoformat() if datas["fim"] else None,
        },
        "moedas": sorted({conta.currency for conta in contas}),
        "contas": [_conta_v3(conta) for conta in contas],
        "categorias": [_categoria_v3(categoria) for categoria in categorias],
    })
