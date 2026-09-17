"""Reclassifica lançamentos que foram lançados na categoria errada.

POR QUE UM CLASSIFICADOR POR REGRA, E NÃO UM SCRIPT COM OS IDs MEDIDOS

Os lançamentos que motivaram este comando foram medidos no banco local, e seria
mais curto gravar os 108 IDs num script. Só que código viaja por `deploy.sh` e
**dado não viaja** -- o mesmo trabalho terá de ser refeito na base de produção,
onde os IDs são outros, as datas são outras e há lançamentos que aqui não
existem. Um script de IDs seria descartável no dia em que servisse.

As regras abaixo descrevem *famílias* de lançamento pelo texto do extrato, que é
o que se repete entre as bases. Daí as três propriedades que este comando tem:

- **simula por padrão.** Sem `--aplicar`, nada é gravado e o relatório sai
  inteiro. É o relatório que se confere, não o resultado;
- **é idempotente.** Rodar de novo não move nada: as regras olham o estado atual
  e o que já está classificado não casa mais com o escopo;
- **recusa-se a adivinhar.** Lançamento que não casa com nenhuma família fica
  exatamente onde está e sai listado como pendente. Nenhuma heurística de
  "parece um rendimento": ou a família está descrita aqui, ou o lançamento é seu
  para decidir.

O QUE ELE FAZ COM CADA FAMÍLIA

Três destinos, porque dinheiro se comporta de três maneiras (a decisão de
16/09/2026, registrada na U04c):

- **muda de dono** -- rendimento, aluguel de ações, provento, cashback, imposto,
  taxa: continua receita ou despesa, só que na categoria certa;
- **muda de bolso** -- CDB, Tesouro Direto, cofrinho, caixinha e compra de
  moeda: vira transferência entre contas, com a outra ponta numa conta que
  representa o veículo. O dinheiro continua aparecendo, agora onde ele está;
- **muda de forma** -- liquidação de bolsa: vira `movimentação`, que sai do
  resultado sem exigir contraparte, porque a outra ponta dela é a posição em
  ações, avaliada pelo Controle de Renda Variável, não por este sistema.

MESES FECHADOS

A reclassificação atravessa meses fechados, e isso é por desenho: o passado é
justamente o que está errado. O comando reabre cada mês afetado com motivo
registrado, altera, e fecha de novo com **o mesmo saldo de fechamento** -- que
não muda, porque nenhum valor muda. Trocar a categoria de um lançamento não move
um centavo de saldo; move o significado.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction as db_transaction

from accounts.models import AppUser
from accounts.services import can_use_transfer_destination
from banking.models import FinancialAccount
from banking.services import can_access_account
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_MOVEMENT,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
)
from transactions.models import AccountMonthClose, BankOperation, CashFlowCategory, CashFlowEntry

# --- Destinos possíveis de uma família -------------------------------------


@dataclass(frozen=True)
class ParaCategoria:
    """Continua receita ou despesa; só a categoria estava errada."""

    categoria: str
    criar_se_faltar: bool = False


@dataclass(frozen=True)
class ParaVeiculo:
    """Vira transferência para a conta que representa o veículo de aplicação.

    A conta destino é procurada pelo mesmo titular e pela mesma instituição do
    lançamento, com este nome. Não há ID aqui de propósito: em produção as
    contas são outras, mas a relação "o CDB do C6 do Mariano" é a mesma.
    """

    conta: str


@dataclass(frozen=True)
class ParaContaEmMoeda:
    """Vira transferência entre moedas para a conta do titular nesta instituição.

    O valor creditado no destino não é dedutível do valor em reais -- ele vem do
    extrato, e é por isso que `--valores-em-moeda` existe.
    """

    instituicao: str


@dataclass(frozen=True)
class Permanece:
    """Fica onde está; é a categoria inteira que passa a significar outra coisa."""

    kind: str


# --- As famílias -----------------------------------------------------------
#
# A ordem importa: vale a primeira família cujo padrão aparecer na descrição.
# Os padrões são comparados sem acento, em minúsculas, com espaços colapsados,
# porque o extrato escreve "Conta Remunerada", "Conta remunerada" e "Conta
# Remurada" para a mesma coisa.


@dataclass(frozen=True)
class Regra:
    nome: str
    categoria_origem: str
    padroes: tuple[str, ...]
    destino: ParaCategoria | ParaVeiculo | ParaContaEmMoeda | Permanece


REGRAS: tuple[Regra, ...] = (
    # -- Aplicações ---------------------------------------------------------
    Regra(
        "aluguel de ações",
        "Aplicações",
        ("taxa de remuneracao emprestimo acoes",),
        ParaCategoria("Aluguel de Açoes"),
    ),
    Regra(
        "rendimento de conta",
        "Aplicações",
        ("conta remunerada", "conta remurada"),
        ParaCategoria("Rendimentos"),
    ),
    Regra(
        "provento de ação",
        "Aplicações",
        ("cred reembolso jcp", "jcp + dividendos"),
        ParaCategoria("Rendimentos"),
    ),
    Regra(
        "cashback, prêmio e FGTS",
        "Aplicações",
        ("cashbak", "cashback", "premio", "pagt fgts"),
        ParaCategoria("Outros"),
    ),
    Regra(
        "compra de moeda",
        "Aplicações",
        ("avenue - compra",),
        ParaContaEmMoeda("Avenue"),
    ),
    Regra(
        "CDB",
        "Aplicações",
        ("resgate cdb", "cdb comodidade", "cdb "),
        ParaVeiculo("CDB"),
    ),
    Regra("cofrinho", "Aplicações", ("cofrinho",), ParaVeiculo("Cofrinho")),
    Regra("caixinha", "Aplicações", ("caixinha",), ParaVeiculo("Caixinha")),
    Regra(
        "Tesouro Direto",
        "Aplicações",
        ("tesouro direto",),
        ParaVeiculo("Tesouro Direto"),
    ),
    # -- Operações em Bolsa -------------------------------------------------
    #
    # O IRRF vem antes da liquidação porque a descrição dele também fala em
    # "Operações D+2" -- é o imposto retido sobre a liquidação, não a
    # liquidação.
    Regra(
        "imposto retido na fonte",
        "Operações em Bolsa",
        ("irrf s/ operacoes",),
        ParaCategoria("Impostos e Tributos"),
    ),
    Regra(
        "liquidação de bolsa",
        "Operações em Bolsa",
        ("operacoes bolsa d+",),
        Permanece(CATEGORY_KIND_MOVEMENT),
    ),
    Regra(
        "resultado de day trade",
        "Operações em Bolsa",
        ("ajuste day trade",),
        ParaCategoria("Resultado em Bolsa", criar_se_faltar=True),
    ),
    Regra(
        "taxa de operação",
        "Operações em Bolsa",
        ("operacoes bm&f",),
        ParaCategoria("Corretagem"),
    ),
    Regra(
        "estorno de custo",
        "Operações em Bolsa",
        ("estorno",),
        ParaCategoria("Corretagem"),
    ),
)


# --- Texto -----------------------------------------------------------------


def normalizar(texto: str | None) -> str:
    """Minúsculas, sem acento, espaços colapsados."""
    decomposto = unicodedata.normalize("NFKD", texto or "")
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento).strip().lower()


def regra_para(entry: CashFlowEntry, categoria_atual: str) -> Regra | None:
    descricao = normalizar(entry.description)
    for regra in REGRAS:
        if regra.categoria_origem != categoria_atual:
            continue
        if any(padrao in descricao for padrao in regra.padroes):
            return regra
    return None


def moeda(valor: Decimal, simbolo: str = "") -> str:
    inteiro, _, centavos = f"{valor:.2f}".partition(".")
    negativo = inteiro.startswith("-")
    inteiro = inteiro.lstrip("-")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    texto = f"{'.'.join(grupos)},{centavos}"
    if negativo:
        texto = f"-{texto}"
    return f"{simbolo} {texto}".strip()


def rotulo_conta(conta: FinancialAccount) -> str:
    return f"{conta.owner.name}/{conta.institution.institution_name}/{conta.account_name}"


# --- Valores em outra moeda ------------------------------------------------


@dataclass(frozen=True)
class ValorEmMoeda:
    data: date
    valor_origem: Decimal
    valor_destino: Decimal


def ler_valores_em_moeda(caminho: Path) -> list[ValorEmMoeda]:
    """Lê `data;valor_origem;valor_destino`, uma linha por operação.

    Cada compra de moeda tem a taxa do dia dela. Este arquivo é o extrato da
    conta em moeda estrangeira reduzido ao essencial -- não há conversão
    calculada aqui, nem deveria haver: taxa que o sistema inventa é taxa que um
    dia discorda do extrato.
    """
    if not caminho.exists():
        raise CommandError(f"Arquivo de valores não encontrado: {caminho}")
    valores: list[ValorEmMoeda] = []
    for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), start=1):
        limpa = linha.split("#", 1)[0].strip()
        if not limpa:
            continue
        partes = [p.strip() for p in limpa.split(";")]
        if len(partes) != 3:
            raise CommandError(
                f"{caminho}:{numero}: esperava 'data;valor_origem;valor_destino', veio {limpa!r}"
            )
        try:
            dia = date.fromisoformat(partes[0])
            origem = Decimal(partes[1].replace(".", "").replace(",", "."))
            destino = Decimal(partes[2].replace(".", "").replace(",", "."))
        except (ValueError, InvalidOperation) as exc:
            raise CommandError(f"{caminho}:{numero}: valor inválido em {limpa!r}") from exc
        if origem <= 0 or destino <= 0:
            raise CommandError(f"{caminho}:{numero}: os dois valores têm de ser positivos.")
        valores.append(ValorEmMoeda(dia, origem, destino))
    return valores


# --- O plano de uma linha --------------------------------------------------


@dataclass
class Linha:
    entry: CashFlowEntry
    regra: Regra | None = None
    acao: str = "pendente"       # 'categoria', 'transferencia', 'nada', 'pendente'
    detalhe: str = ""
    categoria_destino: CashFlowCategory | None = None
    nome_categoria_destino: str = ""
    conta_destino: FinancialAccount | None = None
    valor_destino: Decimal | None = None
    motivo_pendencia: str = ""


@dataclass
class Plano:
    linhas: list[Linha] = field(default_factory=list)
    categorias_a_criar: set[str] = field(default_factory=set)
    kinds_a_trocar: dict[str, str] = field(default_factory=dict)
    fechamentos_a_reabrir: list[AccountMonthClose] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def mudancas(self) -> list[Linha]:
        return [linha for linha in self.linhas if linha.acao in {"categoria", "transferencia"}]

    @property
    def pendentes(self) -> list[Linha]:
        return [linha for linha in self.linhas if linha.acao == "pendente"]


class Command(BaseCommand):
    help = (
        "Reclassifica, por regra, os lançamentos de Aplicações e Operações em Bolsa. "
        "Simula por padrão; grava só com --aplicar."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--usuario",
            required=True,
            help="Quem responde pela reclassificação: assina a reabertura, o fechamento e a auditoria.",
        )
        parser.add_argument(
            "--aplicar",
            action="store_true",
            help="Grava. Sem isto o comando só simula e imprime o relatório.",
        )
        parser.add_argument(
            "--valores-em-moeda",
            default=None,
            help=(
                "Arquivo 'data;valor_origem;valor_destino' com o que entrou na conta em moeda "
                "estrangeira. Sem ele, as compras de moeda ficam pendentes."
            ),
        )
        parser.add_argument(
            "--motivo",
            default="Reclassificação U04c: aplicação e liquidação deixam de contar como despesa.",
            help="Motivo registrado na reabertura de cada mês fechado.",
        )

    # -- orquestração -------------------------------------------------------

    def handle(self, *args, **opcoes):
        usuario = self._resolver_usuario(opcoes["usuario"])
        valores = (
            ler_valores_em_moeda(Path(opcoes["valores_em_moeda"]))
            if opcoes["valores_em_moeda"]
            else []
        )
        plano = self._planejar(usuario, valores)
        self._relatar(plano, usuario, aplicar=opcoes["aplicar"])

        if not opcoes["aplicar"]:
            self.stdout.write("")
            self.stdout.write("SIMULAÇÃO: nada foi gravado. Repita com --aplicar.")
            return
        if not plano.mudancas and not plano.kinds_a_trocar:
            self.stdout.write("")
            self.stdout.write("Nada a fazer: tudo já está classificado.")
            return

        self._aplicar(plano, usuario, opcoes["motivo"])
        self.stdout.write("")
        self.stdout.write(
            f"APLICADO: {len(plano.mudancas)} lançamentos reclassificados, "
            f"{len(plano.fechamentos_a_reabrir)} fechamentos reabertos e refeitos."
        )
        if plano.pendentes:
            self.stdout.write(
                f"ATENÇÃO: {len(plano.pendentes)} lançamentos continuam onde estavam "
                "porque nenhuma família os descreve. Eles estão listados acima."
            )

    def _resolver_usuario(self, username: str) -> AppUser:
        try:
            return AppUser.objects.get(username=username)
        except AppUser.DoesNotExist as exc:
            raise CommandError(f"Usuário '{username}' não existe.") from exc

    # -- planejamento (não grava nada) --------------------------------------

    def _planejar(self, usuario: AppUser, valores: list[ValorEmMoeda]) -> Plano:
        plano = Plano()
        categorias = {c.category_name: c for c in CashFlowCategory.objects.all()}
        categoria_transferencia = self._categoria_de_transferencia(categorias)
        origens = {regra.categoria_origem for regra in REGRAS}
        valores_livres = list(valores)

        em_escopo = (
            CashFlowEntry.objects.filter(category__category_name__in=origens)
            .select_related("category", "account", "account__owner", "account__institution")
            .order_by("due_date", "id")
        )

        permanecem: dict[str, set[int]] = {}
        for entry in em_escopo:
            linha = Linha(entry=entry)
            plano.linhas.append(linha)
            regra = regra_para(entry, entry.category.category_name)
            if regra is None:
                linha.motivo_pendencia = "nenhuma família descreve esta descrição"
                continue
            linha.regra = regra
            destino = regra.destino

            if isinstance(destino, Permanece):
                permanecem.setdefault(entry.category.category_name, set()).add(entry.id)
                if entry.category.kind == destino.kind:
                    linha.acao = "nada"
                    linha.detalhe = f"já é {destino.kind}"
                else:
                    linha.acao = "nada"
                    linha.detalhe = f"fica aqui; a categoria passa a ser {destino.kind}"
                    plano.kinds_a_trocar[entry.category.category_name] = destino.kind
                continue

            if isinstance(destino, ParaCategoria):
                self._planejar_categoria(plano, linha, destino, categorias)
                continue

            self._planejar_transferencia(
                plano, linha, destino, categoria_transferencia, usuario, valores_livres
            )

        self._conferir_troca_de_kind(plano, permanecem)
        plano.fechamentos_a_reabrir = self._fechamentos_afetados(plano)
        return plano

    def _categoria_de_transferencia(self, categorias: dict[str, CashFlowCategory]) -> CashFlowCategory:
        candidatas = [c for c in categorias.values() if c.kind == CATEGORY_KIND_TRANSFER]
        if not candidatas:
            raise CommandError(
                "Nenhuma categoria do tipo transferência existe. Crie-a antes: é ela que "
                "liga as duas pontas de cada par."
            )
        if len(candidatas) > 1:
            nomes = ", ".join(sorted(c.category_name for c in candidatas))
            raise CommandError(
                f"Há mais de uma categoria de transferência ({nomes}) e este comando não "
                "escolhe por você. Informe qual delas usar acertando o cadastro."
            )
        return candidatas[0]

    def _planejar_categoria(
        self,
        plano: Plano,
        linha: Linha,
        destino: ParaCategoria,
        categorias: dict[str, CashFlowCategory],
    ) -> None:
        categoria = categorias.get(destino.categoria)
        if categoria is None:
            if not destino.criar_se_faltar:
                linha.motivo_pendencia = f"a categoria '{destino.categoria}' não existe"
                return
            plano.categorias_a_criar.add(destino.categoria)
            linha.acao = "categoria"
            linha.nome_categoria_destino = destino.categoria
            linha.detalhe = f"{destino.categoria} (será criada)"
            return
        if categoria.id == linha.entry.category_id:
            linha.acao = "nada"
            linha.detalhe = "já está na categoria certa"
            return
        if categoria.kind != CATEGORY_KIND_MANAGERIAL:
            linha.motivo_pendencia = (
                f"'{destino.categoria}' é {categoria.kind}, e esta família continua "
                "sendo receita ou despesa"
            )
            return
        linha.acao = "categoria"
        linha.categoria_destino = categoria
        linha.nome_categoria_destino = destino.categoria
        linha.detalhe = destino.categoria

    def _planejar_transferencia(
        self,
        plano: Plano,
        linha: Linha,
        destino: ParaVeiculo | ParaContaEmMoeda,
        categoria_transferencia: CashFlowCategory,
        usuario: AppUser,
        valores_livres: list[ValorEmMoeda],
    ) -> None:
        entry = linha.entry
        origem = entry.account
        if isinstance(destino, ParaVeiculo):
            conta = (
                FinancialAccount.objects.select_related("owner", "institution")
                .filter(
                    owner_id=origem.owner_id,
                    institution_id=origem.institution_id,
                    account_name=destino.conta,
                )
                .first()
            )
            faltou = (
                f"não existe a conta '{destino.conta}' de {origem.owner.name} "
                f"em {origem.institution.institution_name}"
            )
        else:
            conta = (
                FinancialAccount.objects.select_related("owner", "institution")
                .filter(owner_id=origem.owner_id, institution__institution_name=destino.instituicao)
                .first()
            )
            faltou = (
                f"não existe conta de {origem.owner.name} em {destino.instituicao}"
            )
        if conta is None:
            linha.motivo_pendencia = faltou
            return
        if conta.id == origem.id:
            linha.motivo_pendencia = "a conta destino seria a própria conta de origem"
            return
        if not can_use_transfer_destination(usuario, conta.id):
            linha.motivo_pendencia = (
                f"'{rotulo_conta(conta)}' não está concedida como destino de transferência "
                f"para {usuario.username}"
            )
            return
        if not can_access_account(usuario, origem.id, "update"):
            linha.motivo_pendencia = f"{usuario.username} não pode alterar {rotulo_conta(origem)}"
            return

        if conta.currency != origem.currency:
            valor = self._casar_valor_em_moeda(entry, valores_livres)
            if valor is None:
                linha.motivo_pendencia = (
                    f"sai em {origem.currency} e entra em {conta.currency}: falta o valor "
                    "creditado no destino (--valores-em-moeda)"
                )
                return
            linha.valor_destino = valor

        linha.acao = "transferencia"
        linha.conta_destino = conta
        linha.categoria_destino = categoria_transferencia
        simbolo = "US$" if conta.currency == "USD" else conta.currency
        linha.detalhe = rotulo_conta(conta) + (
            f" · {moeda(linha.valor_destino, simbolo)}" if linha.valor_destino is not None else ""
        )

    def _casar_valor_em_moeda(
        self, entry: CashFlowEntry, valores_livres: list[ValorEmMoeda]
    ) -> Decimal | None:
        """Casa por data e valor de origem, e consome a linha usada.

        Consumir importa: duas compras do mesmo valor no mesmo dia são duas
        operações, e cada uma tem a sua linha no extrato.
        """
        for indice, valor in enumerate(valores_livres):
            if valor.data == entry.due_date and valor.valor_origem == entry.entry_amount:
                return valores_livres.pop(indice).valor_destino
        return None

    def _conferir_troca_de_kind(self, plano: Plano, permanecem: dict[str, set[int]]) -> None:
        """Só troca o tipo de uma categoria se tudo que sobra nela for da família que fica.

        Trocar o tipo reclassifica de uma vez todo lançamento que restar na
        categoria. Se sobrasse ali um lançamento pendente, ele seria arrastado
        junto sem que ninguém tivesse decidido isso -- e arrastado para fora do
        resultado, que é o efeito mais silencioso possível.
        """
        for nome in list(plano.kinds_a_trocar):
            ficam = permanecem.get(nome, set())
            restantes = {
                linha.entry.id
                for linha in plano.linhas
                if linha.entry.category.category_name == nome
                and linha.acao in {"nada", "pendente"}
            }
            intrusos = restantes - ficam
            if not intrusos:
                continue
            del plano.kinds_a_trocar[nome]
            for linha in plano.linhas:
                if linha.entry.id in ficam:
                    linha.detalhe = "fica aqui, e a categoria continua como está (veja o aviso)"
            plano.avisos.append(
                f"'{nome}' NÃO muda de tipo: {len(intrusos)} lançamentos continuam nela sem "
                "família definida, e trocar o tipo os tiraria do resultado sem decisão. "
                "Resolva os pendentes e rode de novo."
            )

    def _fechamentos_afetados(self, plano: Plano) -> list[AccountMonthClose]:
        periodos: set[tuple[int, int, int]] = set()
        for linha in plano.mudancas:
            entry = linha.entry
            for dia in (entry.due_date, entry.realized_date):
                if dia:
                    periodos.add((entry.account_id, dia.year, dia.month))
            if linha.conta_destino is not None:
                for dia in (entry.due_date, entry.realized_date):
                    if dia:
                        periodos.add((linha.conta_destino.id, dia.year, dia.month))
        if not periodos:
            return []
        fechados = AccountMonthClose.objects.filter(active=True).select_related(
            "account", "account__owner", "account__institution"
        )
        return sorted(
            [f for f in fechados if (f.account_id, f.year, f.month) in periodos],
            key=lambda f: (f.account_id, f.year, f.month),
        )

    # -- relatório ----------------------------------------------------------

    def _relatar(self, plano: Plano, usuario: AppUser, *, aplicar: bool) -> None:
        cabecalho = "RECLASSIFICAÇÃO — aplicação" if aplicar else "RECLASSIFICAÇÃO — simulação"
        self.stdout.write(cabecalho)
        self.stdout.write(f"usuário: {usuario.username}")
        self.stdout.write(f"lançamentos em escopo: {len(plano.linhas)}")
        self.stdout.write("")

        if plano.categorias_a_criar:
            self.stdout.write("Categorias que passam a existir")
            for nome in sorted(plano.categorias_a_criar):
                self.stdout.write(f"  {nome} (gerencial)")
            self.stdout.write("")

        if plano.kinds_a_trocar:
            self.stdout.write("Categorias que mudam de tipo")
            for nome, kind in sorted(plano.kinds_a_trocar.items()):
                self.stdout.write(f"  {nome} -> {kind}")
            self.stdout.write("")

        if plano.fechamentos_a_reabrir:
            self.stdout.write(f"Fechamentos a reabrir e refazer: {len(plano.fechamentos_a_reabrir)}")
            for fechamento in plano.fechamentos_a_reabrir:
                self.stdout.write(
                    f"  {fechamento.month:02d}/{fechamento.year}  "
                    f"{moeda(fechamento.closing_balance)}  {rotulo_conta(fechamento.account)}"
                )
            self.stdout.write("")

        self.stdout.write("Lançamento a lançamento")
        for linha in plano.linhas:
            self.stdout.write(self._linha_do_relatorio(linha))
        self.stdout.write("")

        self.stdout.write("Resumo por família")
        resumo: dict[str, list[int | Decimal]] = {}
        for linha in plano.linhas:
            chave = linha.regra.nome if linha.regra else "(sem família)"
            atual = resumo.setdefault(chave, [0, Decimal("0.00")])
            atual[0] += 1
            atual[1] += linha.entry.entry_amount
        for nome, (quantidade, total) in sorted(resumo.items()):
            self.stdout.write(f"  {nome:<26} {quantidade:>4} lançamentos   {moeda(total):>16}")
        self.stdout.write("")

        if plano.avisos:
            self.stdout.write("AVISOS")
            for aviso in plano.avisos:
                self.stdout.write(f"  {aviso}")
            self.stdout.write("")

        if plano.pendentes:
            self.stdout.write(f"PENDENTES — ficam onde estão: {len(plano.pendentes)}")
            for linha in plano.pendentes:
                self.stdout.write(
                    f"  #{linha.entry.id} {linha.entry.description[:50]!r}: {linha.motivo_pendencia}"
                )
        else:
            self.stdout.write("PENDENTES: nenhum. Toda linha do escopo casou com uma família.")

    def _linha_do_relatorio(self, linha: Linha) -> str:
        entry = linha.entry
        sinal = "R" if entry.entry_type == ENTRY_TYPE_INCOME else "D"
        cabeca = (
            f"  #{entry.id:<5} {entry.due_date.strftime('%d/%m/%Y')} {sinal} "
            f"{moeda(entry.entry_amount):>14}  {entry.description[:44]:<44}"
        )
        if linha.acao == "categoria":
            return f"{cabeca} -> categoria {linha.detalhe}"
        if linha.acao == "transferencia":
            return f"{cabeca} -> transferência para {linha.detalhe}"
        if linha.acao == "nada":
            return f"{cabeca} = {linha.detalhe}"
        return f"{cabeca} ! {linha.motivo_pendencia}"

    # -- aplicação ----------------------------------------------------------

    @db_transaction.atomic
    def _aplicar(self, plano: Plano, usuario: AppUser, motivo: str) -> None:
        from transactions.services import close_month, reopen_month

        saldos = {
            (f.account_id, f.year, f.month): f.closing_balance for f in plano.fechamentos_a_reabrir
        }
        contas = {f.account_id: f.account for f in plano.fechamentos_a_reabrir}
        for fechamento in plano.fechamentos_a_reabrir:
            reopen_month(fechamento.account, fechamento.year, fechamento.month, motivo, usuario)

        criadas = {
            nome: CashFlowCategory.objects.create(
                category_name=nome, kind=CATEGORY_KIND_MANAGERIAL
            )
            for nome in sorted(plano.categorias_a_criar)
        }

        for linha in plano.mudancas:
            if linha.acao == "categoria":
                categoria = linha.categoria_destino or criadas[linha.nome_categoria_destino]
                self._trocar_categoria(linha.entry, categoria, linha.regra, usuario)
            else:
                self._converter_em_transferencia(linha, usuario)

        for nome, kind in plano.kinds_a_trocar.items():
            categoria = CashFlowCategory.objects.get(category_name=nome)
            categoria.kind = kind
            categoria.save(update_fields=["kind", "updated_at"])
            self._auditar(
                "cash_flow_category",
                categoria.id,
                "update",
                usuario,
                f"Categoria '{nome}' passa a ser {kind} (reclassificação U04c).",
            )

        for (conta_id, ano, mes), saldo in sorted(saldos.items()):
            close_month(contas[conta_id], ano, mes, saldo, usuario)

    def _trocar_categoria(
        self,
        entry: CashFlowEntry,
        categoria: CashFlowCategory,
        regra: Regra | None,
        usuario: AppUser,
    ) -> None:
        anterior = entry.category.category_name
        entry.category = categoria
        entry.save(update_fields=["category", "updated_at"])
        self._auditar(
            "cash_flow_entry",
            entry.id,
            "update",
            usuario,
            f"Reclassificado de '{anterior}' para '{categoria.category_name}' "
            f"(família: {regra.nome if regra else '?'}).",
        )

    def _converter_em_transferencia(self, linha: Linha, usuario: AppUser) -> None:
        entry = linha.entry
        conta_destino = linha.conta_destino
        anterior = entry.category.category_name

        operacao = BankOperation.objects.create(
            operation_key=f"{OPERATION_INTERNAL_TRANSFER}-{uuid4().hex}",
            operation_type=OPERATION_INTERNAL_TRANSFER,
            description=entry.description[:255],
            status=entry.status,
            installment_total=1,
            first_due_date=entry.due_date,
            last_due_date=entry.due_date,
            entry_count=2,
            responsible_user=usuario,
        )
        entry.category = linha.categoria_destino
        entry.operation_type = OPERATION_INTERNAL_TRANSFER
        entry.bank_operation = operacao
        entry.installments = 1
        entry.current_installment = 1
        entry.save(
            update_fields=[
                "category",
                "operation_type",
                "bank_operation",
                "installments",
                "current_installment",
                "updated_at",
            ]
        )

        # A descrição do extrato é preservada nas duas pontas, em vez da forma
        # "Conta Destino: ..." que a tela usa ao criar uma transferência nova.
        # Ela é a única ligação entre este lançamento e a linha do extrato que o
        # originou, e a conta destino não se perde: o par é estrutural
        # (`source_entry`/`bank_operation`), não texto.
        contraparte = CashFlowEntry.objects.create(
            account=conta_destino,
            category=linha.categoria_destino,
            entry_type=(
                ENTRY_TYPE_EXPENSE if entry.entry_type == ENTRY_TYPE_INCOME else ENTRY_TYPE_INCOME
            ),
            description=entry.description[:255],
            entry_amount=linha.valor_destino if linha.valor_destino is not None else entry.entry_amount,
            installments=1,
            current_installment=1,
            due_date=entry.due_date,
            realized_date=entry.realized_date,
            realized_amount=(
                None
                if entry.realized_amount is None
                else (linha.valor_destino if linha.valor_destino is not None else entry.realized_amount)
            ),
            is_recurring=False,
            status=entry.status,
            operation_type=OPERATION_INTERNAL_TRANSFER,
            bank_operation=operacao,
            source_entry=entry,
        )
        self._auditar(
            "cash_flow_entry",
            entry.id,
            "update",
            usuario,
            f"Reclassificado de '{anterior}' para transferência até "
            f"{rotulo_conta(conta_destino)} (família: {linha.regra.nome}).",
        )
        self._auditar(
            "cash_flow_entry",
            contraparte.id,
            "create",
            usuario,
            f"Contraparte criada pela reclassificação do lançamento #{entry.id}.",
        )

    def _auditar(self, entidade: str, entidade_id, acao: str, usuario: AppUser, resumo: str) -> None:
        from core.services import log_audit_event

        log_audit_event(entidade, entidade_id, acao, user=usuario, summary=resumo)
