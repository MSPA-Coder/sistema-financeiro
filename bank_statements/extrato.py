"""Plano do extrato de conta: o que cada linha pendente vai virar.

É o que `fatura.planejar` faz para o cartão, levado à conta corrente. A mesma
função monta a sugestão da tela de Conciliação e executa "Aplicar sugestões",
então o que a tela mostra é o que acontece.

O que uma linha vira, na ordem em que é decidido:

- **ignorar**: uma regra explícita manda ignorar;
- **concilia**: há um (e só um) lançamento candidato, da mesma conta, sinal e
  valor. Vários candidatos: vale o único na data da linha; sem ele, escolha manual;
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
from uuid import uuid4

from django.db import transaction as db_transaction

from accounts.models import AccountOwner
from accounts.services import can_use_transfer_destination
from banking.services import accessible_account_ids, can_access_account
from core.domain.finance import (
    CATEGORY_KIND_MANAGERIAL,
    CATEGORY_KIND_TRANSFER,
    ENTRY_TYPE_EXPENSE,
    ENTRY_TYPE_INCOME,
    OPERATION_INTERNAL_TRANSFER,
    OPERATION_SINGLE,
    STATUS_REALIZED,
)
from transactions.models import BankOperation, CashFlowCategory, CashFlowEntry
from transactions.services import (
    TransactionRequest,
    _account_label,
    create_transaction_batch,
    is_month_closed,
)

from . import reconciliation
from .classificacao import Sugestao, sugerir_categoria
from .meses import meses_reabertos
from .models import (
    LINE_STATUS_NEW,
    LINE_STATUS_RECONCILED,
    RULE_ACTION_CATEGORY,
    RULE_ACTION_IGNORE,
    RULE_ACTION_LINKED,
    RULE_ACTION_TRANSFER,
    BankStatementLine,
)
from .regras import Regras, conta_de_destino, normalizar

CONCILIA = "concilia"
CONCILIA_APROXIMADA = "concilia_aproximada"
CRIA = "cria"
TRANSFERENCIA_PAR = "transferencia_par"
TRANSFERENCIA_REGRA = "transferencia_regra"
TRANSFERENCIA_COM_LANCAMENTO = "transferencia_com_lancamento"
TRANSFERENCIA_SEM_PAR = "transferencia_sem_par"
IGNORA = "ignora"
AMBIGUA = "ambigua"
MANUAL = "manual"

# Ações que `aplicar` executa sozinha; as demais esperam o usuário.
EXECUTAVEIS = (
    CONCILIA, CONCILIA_APROXIMADA, CRIA, TRANSFERENCIA_PAR, TRANSFERENCIA_REGRA,
    TRANSFERENCIA_COM_LANCAMENTO, IGNORA,
)

ROTULOS = {
    CONCILIA: "Concilia com o lançamento existente",
    CONCILIA_APROXIMADA: "Concilia com a recorrência prevista (valor diferente)",
    CRIA: "Cria lançamento",
    TRANSFERENCIA_PAR: "Transferência entre contas suas (pareia as duas linhas)",
    TRANSFERENCIA_REGRA: "Transferência para a conta",
    TRANSFERENCIA_COM_LANCAMENTO: "Transferência entre contas suas: aproveita o lançamento já gravado na outra conta",
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
    TRANSFERENCIA_COM_LANCAMENTO: "transferência(s) com lançamento já gravado",
    IGNORA: "ignorada(s)",
}

_SEM_DESTINO = "Você não tem permissão de destino de transferência para {conta}."
_JANELA_DO_PAR = timedelta(days=2)
_LIMITE_DO_POOL = 3000
# Palavras que dão cara de transferência a uma linha de extrato.
_PALAVRAS_FORTES = re.compile(r"\b(TED|DOC|TRANSFERENCIA|TRANSF|EMISSAO DE CDB|RESGATE DE CDB)\b")
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
            if self.lancamento.status == STATUS_REALIZED:
                correcao = reconciliation.correcao_da_realizacao(
                    entry_antes=(
                        self.lancamento.realized_date,
                        self.lancamento.realized_amount or self.lancamento.entry_amount,
                    ),
                    line=self.linha,
                )
                if correcao:
                    return f"{texto} #{self.lancamento.id}; corrige a realização: {correcao}"
            return f"{texto} #{self.lancamento.id}"
        if self.acao == CONCILIA_APROXIMADA and self.lancamento is not None:
            return (
                f"{texto} #{self.lancamento.id}: previsto {self.lancamento.entry_amount}, "
                f"no extrato {abs(self.linha.amount)}"
            )
        if self.acao == TRANSFERENCIA_PAR and self.par is not None:
            return f"{texto}: {_rotulo_da_conta(self.par.account)}"
        if self.acao == TRANSFERENCIA_COM_LANCAMENTO and self.lancamento is not None:
            return (
                f"{texto} (#{self.lancamento.id}, {self.lancamento.account.account_name}, "
                f"{self.lancamento.realized_date:%d/%m/%Y})"
            )
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


def texto_cita_titular(descricao: str, padroes) -> bool:
    """Se o texto cita o nome de algum titular (palavra inteira, sem acento nem caixa)."""
    texto = normalizar(descricao)
    return any(padrao.search(texto) for padrao in padroes)


def texto_tem_cara_de_transferencia(descricao: str) -> bool:
    return bool(_PALAVRAS_DE_TRANSFERENCIA.search(normalizar(descricao)))


def texto_tem_transferencia_forte(descricao: str) -> bool:
    """Cara de transferência sem contar o "Pix", que também paga terceiros."""
    return bool(_PALAVRAS_FORTES.search(normalizar(descricao)))


nomes_dos_titulares = _nomes_dos_titulares


def _cita_titular(linha: BankStatementLine, padroes) -> bool:
    return texto_cita_titular(linha.description, padroes)


def _tem_cara_de_transferencia(linha: BankStatementLine) -> bool:
    return texto_tem_cara_de_transferencia(linha.description)


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


def _lancamentos_para_par(user, linhas, padroes) -> dict[int, CashFlowEntry]:
    """Para cada linha sem outra linha como par, o lançamento já gravado, em outra
    conta do usuário, que é a outra ponta da transferência.

    É o caso da corretora cujo extrato chega depois: o dinheiro que ela mandou
    para a conta digital já está lá, gravado como receita ("TED recebida",
    "Cashbak"). Vale quando o lançamento é realizado, avulso, gerencial, de sinal
    oposto, de mesmo valor, até dois dias de distância, na mesma moeda, e há
    indício (um titular citado em qualquer um dos textos, ou cara de
    transferência na linha). O par tem de ser único dos dois lados."""
    contas = accessible_account_ids(user, "update")
    if not contas or not linhas:
        return {}
    valores = {abs(linha.amount) for linha in linhas}
    pool = list(
        CashFlowEntry.objects.filter(
            account_id__in=contas,
            status=STATUS_REALIZED,
            operation_type=OPERATION_SINGLE,
            is_recurring=False,
            source_entry__isnull=True,
            category__kind=CATEGORY_KIND_MANAGERIAL,
            realized_amount__in=valores,
        )
        .exclude(account__account_kind="cartao_credito")
        .select_related("account__owner", "account__institution", "category")
    )
    candidatos: dict[int, list[CashFlowEntry]] = {}
    for linha in linhas:
        if linha.account.is_credit_card:
            continue
        tipo = ENTRY_TYPE_INCOME if linha.amount < 0 else ENTRY_TYPE_EXPENSE
        achados = []
        for entrada in pool:
            if entrada.account_id == linha.account_id or entrada.entry_type != tipo:
                continue
            if entrada.realized_amount != abs(linha.amount) or entrada.account.currency != linha.account.currency:
                continue
            if abs(entrada.realized_date - linha.statement_date) > _JANELA_DO_PAR:
                continue
            indicio = (
                _cita_titular(linha, padroes)
                or texto_cita_titular(entrada.description, padroes)
                or texto_tem_transferencia_forte(linha.description)
            )
            if indicio:
                achados.append(entrada)
        candidatos[linha.id] = achados
    reivindicacoes = Counter(
        entrada.id for achados in candidatos.values() for entrada in achados
    )
    return {
        linha_id: achados[0]
        for linha_id, achados in candidatos.items()
        if len(achados) == 1 and reivindicacoes[achados[0].id] == 1
    }


def _candidatos_sem_repetir(linhas, por_linha: dict) -> dict:
    """Os candidatos de cada linha, sem dar o mesmo lançamento a duas linhas.

    Duas linhas iguais no mês (o Pix de 1.500,00 nos dias 25 e 26) e um só
    lançamento: o da data da linha fica com a linha daquele dia, e a outra
    segue sem ele -- vira lançamento novo, em vez de colidir na hora de
    conciliar. Primeiro reserva quem casa pelo dia, depois o resto, pela data."""
    desempatados = {linha.id: reconciliation.desempatar_pelo_dia(linha, por_linha.get(linha.id, [])) for linha in linhas}
    reservado: dict[int, int] = {}  # lançamento -> linha
    for linha in linhas:
        achados = desempatados[linha.id]
        if len(achados) == 1 and (achados[0].realized_date or achados[0].due_date) == linha.statement_date:
            reservado.setdefault(achados[0].id, linha.id)
    resultado = {}
    for linha in sorted(linhas, key=lambda item: (item.statement_date, item.id)):
        livres = [e for e in desempatados[linha.id] if reservado.get(e.id, linha.id) == linha.id]
        if len(livres) == 1:
            reservado.setdefault(livres[0].id, linha.id)
        resultado[linha.id] = livres
    return resultado


def planejar(user, linhas) -> list[Plano]:
    """Decide o destino de cada linha nova, sem gravar nada."""
    linhas = [linha for linha in linhas if linha.status == LINE_STATUS_NEW]
    if not linhas:
        return []
    regras = Regras()
    padroes = _nomes_dos_titulares()
    pares = _pares(linhas, _pool_de_pareamento(user), padroes)
    com_lancamento = _lancamentos_para_par(
        user, [linha for linha in linhas if linha.id not in pares], padroes
    )
    candidatos = _candidatos_sem_repetir(linhas, reconciliation.candidate_entries_for_lines(linhas))

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
        if regra is not None and regra.action in (RULE_ACTION_TRANSFER, RULE_ACTION_LINKED):
            destino = conta_de_destino(regra, linha.account)
            if destino is None:
                motivo = (
                    f"A regra \"{regra.name}\" pede a aplicação vinculada a esta conta, e não há "
                    "exatamente uma: confira a conta de movimento das aplicações."
                    if regra.action == RULE_ACTION_LINKED else
                    f"A conta \"{regra.destination_account_name}\" da regra \"{regra.name}\" não existe para este titular."
                )
                planos.append(Plano(linha, MANUAL, regra=regra, motivo=motivo))
                continue
            quem_recebe = destino if linha.amount < 0 else linha.account
            if not can_use_transfer_destination(user, quem_recebe.id):
                planos.append(Plano(linha, MANUAL, regra=regra, motivo=_SEM_DESTINO.format(
                    conta=_rotulo_da_conta(quem_recebe))))
                continue
            planos.append(Plano(linha, TRANSFERENCIA_REGRA, conta_destino=destino, regra=regra))
            continue
        existente = com_lancamento.get(linha.id)
        if existente is not None:
            quem_recebe = linha.account if linha.amount > 0 else existente.account
            if not can_use_transfer_destination(user, quem_recebe.id):
                planos.append(Plano(linha, MANUAL, lancamento=existente, regra=regra, motivo=_SEM_DESTINO.format(
                    conta=_rotulo_da_conta(quem_recebe))))
            else:
                planos.append(Plano(linha, TRANSFERENCIA_COM_LANCAMENTO, lancamento=existente, regra=regra))
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


MOTIVO_DA_REABERTURA = "Transferência com lançamento já gravado, a partir do extrato"


def _aplicar_com_lancamento(user, plano: Plano, audit_context, autorizar_meses: bool) -> None:
    """Cria a ponta que falta e transforma o lançamento já gravado na outra.

    O lançamento existente só muda de natureza (categoria, operação interna e
    vínculo): valor, data, conta e conciliação ficam, e por isso o saldo do mês
    dele não muda -- é o que permite reabrir o mês fechado dele, com o saldo de
    fechamento conferido. A ponta nova nasce no mês da linha, que não pode estar
    fechado (criá-la mudaria o saldo daquele mês)."""
    from core.services import log_audit_event

    linha = _linha_travada(user, plano.linha.id)
    existente = (
        CashFlowEntry.objects.select_for_update()
        .select_related("account__owner", "account__institution", "category")
        .get(id=plano.lancamento.id)
    )
    if (
        existente.status != STATUS_REALIZED
        or existente.operation_type != OPERATION_SINGLE
        or existente.source_entry_id is not None
        or existente.category.kind != CATEGORY_KIND_MANAGERIAL
        or existente.realized_amount != abs(linha.amount)
    ):
        raise ValueError("O lançamento da outra conta mudou desde a prévia.")
    if existente.account.currency != linha.account.currency:
        raise ValueError("Transferência entre moedas diferentes é lançada à mão.")
    if is_month_closed(linha.account, linha.statement_date.year, linha.statement_date.month):
        raise ValueError(
            f"Não é possível lançar a ponta nova: mês {linha.statement_date.month:02d}/"
            f"{linha.statement_date.year} fechado para a conta {linha.account}."
        )
    categoria = _categoria_de_transferencia()
    saida_da_linha = linha.amount < 0
    valor = abs(linha.amount)
    meses = {(existente.account_id, existente.realized_date.year, existente.realized_date.month)}
    antes = (existente.category.category_name, existente.description)
    with meses_reabertos(
        user, meses, autorizar=autorizar_meses, motivo=MOTIVO_DA_REABERTURA, audit_context=audit_context
    ):
        operacao = BankOperation.objects.create(
            operation_key=f"{OPERATION_INTERNAL_TRANSFER}-{uuid4().hex}",
            operation_type=OPERATION_INTERNAL_TRANSFER,
            description="",
            status=STATUS_REALIZED,
            installment_total=1,
            responsible_user=user,
        )

        def nova_ponta(*, tipo, descricao, origem=None):
            return CashFlowEntry.objects.create(
                account=linha.account, category=categoria, entry_type=tipo, description=descricao,
                entry_amount=valor, installments=1, current_installment=1,
                due_date=linha.statement_date, realized_date=linha.statement_date, realized_amount=valor,
                status=STATUS_REALIZED, operation_type=OPERATION_INTERNAL_TRANSFER,
                bank_operation=operacao, source_entry=origem,
            )

        if saida_da_linha:
            # A linha é a origem; o lançamento existente (receita) vira a contraparte.
            nova = nova_ponta(tipo=ENTRY_TYPE_EXPENSE, descricao=f"Conta Destino: {_account_label(existente.account)}")
            existente.source_entry = nova
            existente.description = f"Conta Origem: {_account_label(linha.account)}"
        else:
            # O lançamento existente (despesa) vira a origem; a linha é a contraparte.
            existente.description = f"Conta Destino: {_account_label(linha.account)}"
        existente.category = categoria
        existente.operation_type = OPERATION_INTERNAL_TRANSFER
        existente.bank_operation = operacao
        existente.save(
            update_fields=["category", "operation_type", "bank_operation", "source_entry", "description", "updated_at"]
        )
        if not saida_da_linha:
            nova = nova_ponta(
                tipo=ENTRY_TYPE_INCOME, descricao=f"Conta Origem: {_account_label(existente.account)}",
                origem=existente,
            )
        _vincular(linha, nova)
        log_audit_event(
            "cash_flow_entry", existente.id, "update",
            old_values={"category": antes[0], "description": antes[1]},
            new_values={"category": categoria.category_name, "description": existente.description},
            user=user, request_context=audit_context,
            summary="Lançamento transformado em ponta de transferência a partir do extrato.",
        )
        log_audit_event(
            "cash_flow_entry", nova.id, "create", request_context=audit_context,
            summary="Ponta de transferência criada a partir do extrato.",
        )


def aplicar(
    user, linhas, audit_context=None, *, autorizar_meses: bool = False
) -> tuple[Counter, list[tuple[str, str]]]:
    """Executa o plano das linhas dadas. Cada item é uma transação própria: uma
    falha isolada (mês fechado, acesso negado) não desfaz as demais.

    `autorizar_meses` só vale para transferir com lançamento já gravado: permite
    reabrir o mês fechado do lançamento existente (ver `meses`).

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
                elif plano.acao == TRANSFERENCIA_COM_LANCAMENTO:
                    _aplicar_com_lancamento(user, plano, audit_context, autorizar_meses)
            feitas[plano.acao] += 1
        except (ValueError, BankStatementLine.DoesNotExist) as exc:
            erros.append((linha.description, str(exc) or "Linha não encontrada."))
    return feitas, erros
