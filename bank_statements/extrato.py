"""Plano do extrato de conta: o que cada linha pendente vai virar.

É o que `fatura.planejar` faz para o cartão, levado à conta corrente. A mesma
função monta a sugestão da tela de Conciliação e executa "Aplicar sugestões",
então o que a tela mostra é o que acontece.

O que uma linha vira, na ordem em que é decidido:

- **ignorar**: uma regra explícita manda ignorar;
- **concilia**: há um (e só um) lançamento candidato, da mesma conta, sinal e
  valor. Vários candidatos pedem escolha manual;
- **concilia com valor diferente**: não há candidato de valor exato, mas há uma
  (e só uma) recorrência aberta da conta, do mesmo sinal, com valor até 25% acima
  ou abaixo do previsto e vencimento a até 5 dias (energia, condomínio). O
  lançamento é realizado pelo valor do extrato e o previsto fica como estava;
- **transferência pareada**: a linha tem, em outra conta, a linha de sinal
  oposto, mesmo valor e até dois dias de distância, e há indício de que as duas
  são a mesma transferência (o nome de um titular no texto, ou as duas com
  cara de transferência). As duas linhas viram uma transferência só;
- **transferência por regra**: uma regra diz para qual conta do mesmo titular o
  dinheiro foi (Rende Fácil, caixinha) e a outra ponta não tem extrato;
- **transferência sem par**: o texto cita um titular, mas a outra ponta ainda
  não foi importada. Fica pendente: criar receita e despesa para uma
  transferência própria é exatamente o erro que esta etapa existe para evitar;
- **cria**: um lançamento novo, com a categoria de `classificacao.sugerir_categoria`.

Nada aqui altera dados fora de `aplicar`.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction as db_transaction

from accounts.models import AccountOwner
from accounts.services import can_use_transfer_destination
from banking.services import accessible_account_ids, can_access_account
from core.domain.finance import (
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    STATUS_REALIZED,
)
from transactions.models import CashFlowCategory, CashFlowEntry
from transactions.services import TransactionRequest, create_transaction_batch

from . import reconciliation
from .classificacao import Sugestao, sugerir_categoria
from .models import (
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    RULE_ACTION_CATEGORY,
    RULE_ACTION_IGNORE,
    RULE_ACTION_TRANSFER,
    BankStatementLine,
)
from .regras import Regras, conta_de_destino, normalizar

CONCILIA = "concilia"
CONCILIA_APROXIMADA = "concilia_aproximada"
CRIA = "cria"
TRANSFERENCIA_PAR = "transferencia_par"
TRANSFERENCIA_REGRA = "transferencia_regra"
TRANSFERENCIA_SEM_PAR = "transferencia_sem_par"
IGNORA = "ignora"
AMBIGUA = "ambigua"
MANUAL = "manual"

# Ações que `aplicar` executa sozinha; as demais esperam o usuário.
EXECUTAVEIS = (CONCILIA, CONCILIA_APROXIMADA, CRIA, TRANSFERENCIA_PAR, TRANSFERENCIA_REGRA, IGNORA)

ROTULOS = {
    CONCILIA: "Concilia com o lançamento existente",
    CONCILIA_APROXIMADA: "Concilia com a recorrência prevista (valor diferente)",
    CRIA: "Cria lançamento",
    TRANSFERENCIA_PAR: "Transferência entre contas suas (pareia as duas linhas)",
    TRANSFERENCIA_REGRA: "Transferência para a conta",
    TRANSFERENCIA_SEM_PAR: "Parece transferência sua; falta importar a outra ponta",
    IGNORA: "Ignora (regra)",
    AMBIGUA: "Mais de um lançamento candidato: escolha",
    MANUAL: "Decida manualmente",
}

ROTULOS_CURTOS = {
    CONCILIA: "conciliada(s)",
    CONCILIA_APROXIMADA: "recorrência(s) conciliada(s) com valor diferente",
    CRIA: "lançamento(s) criado(s)",
    TRANSFERENCIA_PAR: "transferência(s) pareada(s)",
    TRANSFERENCIA_REGRA: "transferência(s) por regra",
    IGNORA: "ignorada(s)",
}

_SEM_DESTINO = "Você não tem permissão de destino de transferência para {conta}."
_JANELA_DO_PAR = timedelta(days=2)
_LIMITE_DO_POOL = 3000
# Palavras que dão cara de transferência a uma linha de extrato.
_PALAVRAS_DE_TRANSFERENCIA = re.compile(
    r"\b(PIX|TED|DOC|TRANSFERENCIA|TRANSF|EMISSAO DE CDB|RESGATE DE CDB|RESGATE|APLICACAO)\b"
)


@dataclass
class Plano:
    linha: BankStatementLine
    acao: str
    sugestao: Sugestao | None = None
    lancamento: CashFlowEntry | None = None
    par: BankStatementLine | None = None
    conta_destino: object | None = None
    regra: object | None = None
    motivo: str = ""

    @property
    def executavel(self) -> bool:
        return self.acao in EXECUTAVEIS

    @property
    def rotulo(self) -> str:
        texto = ROTULOS.get(self.acao, self.acao)
        if self.acao == CRIA and self.sugestao is not None and self.sugestao.categoria is not None:
            return f"{texto}: {self.sugestao.categoria.category_name} ({self.sugestao.rotulo})"
        if self.acao == CONCILIA and self.lancamento is not None:
            return f"{texto} #{self.lancamento.id}"
        if self.acao == CONCILIA_APROXIMADA and self.lancamento is not None:
            return (
                f"{texto} #{self.lancamento.id}: previsto {self.lancamento.entry_amount}, "
                f"no extrato {abs(self.linha.amount)}"
            )
        if self.acao == TRANSFERENCIA_PAR and self.par is not None:
            return f"{texto}: {_rotulo_da_conta(self.par.account)}"
        if self.acao == TRANSFERENCIA_REGRA and self.conta_destino is not None:
            return f"{texto} {self.conta_destino.account_name}"
        if self.motivo:
            return f"{texto}. {self.motivo}"
        return texto


def _rotulo_da_conta(conta) -> str:
    return " / ".join(
        parte for parte in (conta.owner.name, conta.institution.institution_name, conta.account_name) if parte
    )


def _nomes_dos_titulares() -> list[re.Pattern]:
    padroes = []
    for titular in AccountOwner.objects.all():
        nome = normalizar(titular.name)
        if len(nome) >= 3:
            padroes.append(re.compile(rf"\b{re.escape(nome)}\b"))
    return padroes


def _cita_titular(linha: BankStatementLine, padroes) -> bool:
    texto = normalizar(linha.description)
    return any(padrao.search(texto) for padrao in padroes)


def _tem_cara_de_transferencia(linha: BankStatementLine) -> bool:
    return bool(_PALAVRAS_DE_TRANSFERENCIA.search(normalizar(linha.description)))


def _pool_de_pareamento(user) -> list[BankStatementLine]:
    """Linhas novas, de contas que o usuário pode lançar, que não são de cartão."""
    contas = accessible_account_ids(user, "update")
    if not contas:
        return []
    return list(
        BankStatementLine.objects.filter(account_id__in=contas, status=LINE_STATUS_NEW)
        .exclude(account__account_kind="cartao_credito")
        .select_related("account__owner", "account__institution")
        .order_by("statement_date", "id")[:_LIMITE_DO_POOL]
    )


def _pares(alvos: list[BankStatementLine], pool: list[BankStatementLine], padroes) -> dict[int, BankStatementLine]:
    """Pareia linhas de sinal oposto de contas diferentes, só quando o par é único
    dos dois lados e há indício de transferência."""
    por_valor: dict = defaultdict(list)
    for linha in pool:
        por_valor[abs(linha.amount)].append(linha)

    def vizinhas(linha):
        achadas = []
        for outra in por_valor.get(abs(linha.amount), ()):
            if outra.id == linha.id or outra.account_id == linha.account_id:
                continue
            if (outra.amount > 0) == (linha.amount > 0):
                continue
            if outra.account.currency != linha.account.currency:
                continue
            if abs(outra.statement_date - linha.statement_date) > _JANELA_DO_PAR:
                continue
            indicio = (
                _cita_titular(linha, padroes)
                or _cita_titular(outra, padroes)
                or (_tem_cara_de_transferencia(linha) and _tem_cara_de_transferencia(outra))
            )
            if indicio:
                achadas.append(outra)
        return achadas

    resultado: dict[int, BankStatementLine] = {}
    for linha in alvos:
        if linha.id in resultado or linha.account.is_credit_card:
            continue
        candidatas = vizinhas(linha)
        if len(candidatas) != 1:
            continue
        outra = candidatas[0]
        de_volta = vizinhas(outra)
        if len(de_volta) == 1 and de_volta[0].id == linha.id:
            resultado[linha.id] = outra
            resultado[outra.id] = linha
    return resultado


def planejar(user, linhas) -> list[Plano]:
    """Decide o destino de cada linha nova, sem gravar nada."""
    linhas = [linha for linha in linhas if linha.status == LINE_STATUS_NEW]
    if not linhas:
        return []
    regras = Regras()
    padroes = _nomes_dos_titulares()
    pares = _pares(linhas, _pool_de_pareamento(user), padroes)
    candidatos = reconciliation.candidate_entries_for_lines(linhas, limit=2)

    planos: list[Plano] = []
    for linha in sorted(linhas, key=lambda item: (item.statement_date, item.id)):
        if linha.account.is_credit_card:
            planos.append(Plano(linha, MANUAL, motivo="Linha de cartão: use a tela de Faturas."))
            continue
        regra = regras.da_linha(linha)
        if regra is not None and regra.action == RULE_ACTION_IGNORE:
            planos.append(Plano(linha, IGNORA, regra=regra))
            continue
        achados = candidatos.get(linha.id, [])
        if len(achados) == 1:
            planos.append(Plano(linha, CONCILIA, lancamento=achados[0], regra=regra))
            continue
        if len(achados) > 1:
            planos.append(Plano(linha, AMBIGUA, regra=regra))
            continue
        par = pares.get(linha.id)
        if par is not None:
            quem_recebe = linha if linha.amount > 0 else par
            if not can_use_transfer_destination(user, quem_recebe.account_id):
                planos.append(Plano(linha, MANUAL, par=par, regra=regra, motivo=_SEM_DESTINO.format(
                    conta=_rotulo_da_conta(quem_recebe.account))))
            else:
                planos.append(Plano(linha, TRANSFERENCIA_PAR, par=par, regra=regra))
            continue
        if regra is not None and regra.action == RULE_ACTION_TRANSFER:
            destino = conta_de_destino(regra, linha.account)
            if destino is None:
                planos.append(
                    Plano(
                        linha, MANUAL, regra=regra,
                        motivo=f"A conta \"{regra.destination_account_name}\" da regra \"{regra.name}\" não existe para este titular.",
                    )
                )
                continue
            quem_recebe = destino if linha.amount < 0 else linha.account
            if not can_use_transfer_destination(user, quem_recebe.id):
                planos.append(Plano(linha, MANUAL, regra=regra, motivo=_SEM_DESTINO.format(
                    conta=_rotulo_da_conta(quem_recebe))))
                continue
            planos.append(Plano(linha, TRANSFERENCIA_REGRA, conta_destino=destino, regra=regra))
            continue
        aproximados = reconciliation.candidatos_aproximados(linha, limit=2)
        if len(aproximados) == 1:
            planos.append(Plano(linha, CONCILIA_APROXIMADA, lancamento=aproximados[0], regra=regra))
            continue
        if len(aproximados) > 1:
            planos.append(Plano(linha, AMBIGUA, regra=regra))
            continue
        if _cita_titular(linha, padroes):
            planos.append(Plano(linha, TRANSFERENCIA_SEM_PAR, regra=regra))
            continue
        regra_de_categoria = regra if regra is not None and regra.action == RULE_ACTION_CATEGORY else None
        sugestao = sugerir_categoria(linha.description, linha.bank_category, regra=regra_de_categoria)
        if sugestao.categoria is None:
            planos.append(
                Plano(linha, MANUAL, motivo="Sem categoria: cadastre \"Outros\" ou escolha uma.", regra=regra)
            )
        else:
            planos.append(Plano(linha, CRIA, sugestao=sugestao, regra=regra))
    return planos


def _categoria_de_transferencia() -> CashFlowCategory:
    categoria = CashFlowCategory.objects.filter(kind=CATEGORY_KIND_TRANSFER).order_by("id").first()
    if categoria is None:
        raise ValueError("Não há categoria de transferência cadastrada.")
    return categoria


def _criar_transferencia(user, *, origem, destino, valor, data_origem, data_destino, audit_context):
    """As duas pontas de uma transferência já realizada, cada uma na sua data."""
    if origem.currency != destino.currency:
        raise ValueError("Transferência entre moedas diferentes é lançada à mão.")
    categoria = _categoria_de_transferencia()
    criadas = create_transaction_batch(
        TransactionRequest(
            account_id=origem.id,
            category_id=categoria.id,
            entry_type=ENTRY_TYPE_EXPENSE,
            description="",
            entry_amount=valor,
            installments=1,
            due_date=data_origem,
            status=STATUS_REALIZED,
            realized_date=data_origem,
            realized_amount=valor,
            counterparty_account_id=destino.id,
        ),
        audit_context=audit_context,
        user=user,
    )
    saida = next(entry for entry in criadas if entry.account_id == origem.id)
    entrada = next(entry for entry in criadas if entry.account_id == destino.id)
    if data_destino != data_origem:
        # O banco de destino creditou em outro dia: a ponta dele fica na data dele.
        entrada.due_date = data_destino
        entrada.realized_date = data_destino
        entrada.save(update_fields=["due_date", "realized_date", "updated_at"])
    return saida, entrada


def _vincular(linha: BankStatementLine, lancamento: CashFlowEntry) -> None:
    linha.matched_entry = lancamento
    linha.status = LINE_STATUS_RECONCILED
    linha.save(update_fields=["matched_entry", "status", "updated_at"])


def _linha_travada(user, linha_id, acao: str = "update") -> BankStatementLine:
    linha = (
        BankStatementLine.objects.select_for_update()
        .select_related("account__owner", "account__institution")
        .get(id=linha_id)
    )
    if not can_access_account(user, linha.account_id, acao):
        raise ValueError("Acesso negado para esta linha de extrato.")
    if linha.status != LINE_STATUS_NEW or linha.matched_entry_id is not None:
        raise ValueError("Linha de extrato já conciliada ou ignorada.")
    return linha


def _aplicar_par(user, plano: Plano, audit_context) -> None:
    primeira = _linha_travada(user, plano.linha.id)
    segunda = _linha_travada(user, plano.par.id)
    saida, entrada = (primeira, segunda) if primeira.amount < 0 else (segunda, primeira)
    if saida.amount + entrada.amount != 0:
        raise ValueError("As duas linhas não têm o mesmo valor.")
    lancamento_saida, lancamento_entrada = _criar_transferencia(
        user,
        origem=saida.account,
        destino=entrada.account,
        valor=abs(saida.amount),
        data_origem=saida.statement_date,
        data_destino=entrada.statement_date,
        audit_context=audit_context,
    )
    _vincular(saida, lancamento_saida)
    _vincular(entrada, lancamento_entrada)


def _aplicar_regra_de_transferencia(user, plano: Plano, audit_context) -> None:
    linha = _linha_travada(user, plano.linha.id)
    conta, destino = linha.account, plano.conta_destino
    saida = linha.amount < 0
    origem, contraparte = (conta, destino) if saida else (destino, conta)
    lancamento_origem, lancamento_destino = _criar_transferencia(
        user,
        origem=origem,
        destino=contraparte,
        valor=abs(linha.amount),
        data_origem=linha.statement_date,
        data_destino=linha.statement_date,
        audit_context=audit_context,
    )
    _vincular(linha, lancamento_origem if saida else lancamento_destino)


def aplicar(user, linhas, audit_context=None) -> tuple[Counter, list[tuple[str, str]]]:
    """Executa o plano das linhas dadas. Cada item é uma transação própria: uma
    falha isolada (mês fechado, acesso negado) não desfaz as demais.

    Devolve a contagem por ação e a lista `(descrição, erro)`."""
    feitas: Counter = Counter()
    erros: list[tuple[str, str]] = []
    resolvidas: set[int] = set()
    for plano in planejar(user, linhas):
        linha = plano.linha
        if linha.id in resolvidas:
            continue
        if not plano.executavel:
            erros.append((linha.description, plano.rotulo))
            continue
        try:
            with db_transaction.atomic():
                if plano.acao in (CONCILIA, CONCILIA_APROXIMADA):
                    reconciliation.reconcile_line_with_entry(
                        user, line_id=linha.id, entry_id=plano.lancamento.id, audit_context=audit_context,
                        aceitar_valor_diferente=plano.acao == CONCILIA_APROXIMADA,
                    )
                elif plano.acao == CRIA:
                    reconciliation.create_entry_from_line(
                        user, line_id=linha.id, category_id=plano.sugestao.categoria.id,
                        audit_context=audit_context,
                    )
                elif plano.acao == IGNORA:
                    reconciliation.ignore_statement_line(user, line_id=linha.id)
                elif plano.acao == TRANSFERENCIA_PAR:
                    _aplicar_par(user, plano, audit_context)
                    resolvidas.add(plano.par.id)
                elif plano.acao == TRANSFERENCIA_REGRA:
                    _aplicar_regra_de_transferencia(user, plano, audit_context)
            feitas[plano.acao] += 1
        except (ValueError, BankStatementLine.DoesNotExist) as exc:
            erros.append((linha.description, str(exc) or "Linha não encontrada."))
    return feitas, erros
