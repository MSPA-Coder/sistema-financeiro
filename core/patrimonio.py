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

A CHAVE É A PERMISSÃO

A rota publica **todas as contas**, sem escopo de usuário, e isso é deliberado:
um resumo filtrado por titular produziria um patrimônio consolidado que esconde
contas sem avisar -- o pior tipo de número errado. Quem tem o token lê o saldo
de todas as contas deste sistema. Guarde-o como se guarda uma senha de banco.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import unicodedata
from datetime import date, timedelta
from decimal import Decimal

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET
from sharedauth.secrets import SegredoInvalidoError, resolver_segredo

from banking.models import FinancialAccount
from core.domain.finance import VIEW_REALIZED

CONTRATO = "patrimonio/v1"
SISTEMA = "controle-bancario"

#: O token não tem valor padrão e não é gerado: sem ele configurado, a rota não
#: atende. Um segredo gerado no arranque valeria até o próximo reinício e daria
#: a impressão de que a integração está configurada quando não está.
NOME_DO_SEGREDO = "PATRIMONIO_TOKEN"
COMPRIMENTO_MINIMO_DO_TOKEN = 32

logger = logging.getLogger(__name__)


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


def montar_resumo(referencia: date) -> dict:
    """A foto deste sistema na data pedida."""
    contas = list(
        FinancialAccount.objects.select_related("owner", "institution").order_by("id")
    )
    # Conta cujo saldo inicial é posterior à data pedida ainda não existia na
    # foto, e somá-la traria para o passado um dinheiro que só chegou depois.
    # Poder responder isso é o que a data no saldo inicial (U04a) comprou.
    vigentes = [conta for conta in contas if conta.initial_balance_date <= referencia]

    titulares = {}
    instituicoes = {}
    linhas = []
    totais: dict[str, dict] = {}
    for conta in vigentes:
        titular = identidade(conta.owner.name)
        instituicao = identidade(conta.institution.institution_name)
        titulares[titular] = {"id": titular, "nome": conta.owner.name}
        instituicoes[instituicao] = {
            "id": instituicao,
            "nome": conta.institution.institution_name,
            "tipo": conta.institution.institution_type,
        }
        saldo = saldo_da_conta(conta, referencia)
        linhas.append(
            {
                "id": f"{SISTEMA}:conta:{conta.id}",
                "titular": titular,
                "instituicao": instituicao,
                "nome": conta.account_name,
                "moeda": conta.currency,
                "saldo": str(saldo),
                "saldo_inicial_em": conta.initial_balance_date.isoformat(),
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
