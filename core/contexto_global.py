"""Contexto de titular, instituição e conta que acompanha o menu.

Decisão de 09/10/2026 (auditoria de filtros): quem escolhe um titular ou uma
conta numa tela e vai para outra pelo menu espera continuar no mesmo recorte,
como já acontecia com a moeda e os grupos. Os três parâmetros passam a viajar
**entre as telas que usam o mesmo seletor** (`reports.services.selected_context`)
-- e só entre elas: o Planejamento anual, a Reclassificação e o Fechamento
têm seletores próprios, com outro formato, e ficam de fora.

O contrato dos filtros globais vale aqui também: nada é gravado em sessão,
perfil ou banco; o contexto vive na URL de cada aba, e o repasse é feito no
clique do link (`static/js/core/application.js`). Período e status não
viajam -- cada tela tem o seu.
"""
from __future__ import annotations

from django.urls import reverse

PARAMETROS_DE_CONTEXTO = ("owner_id", "institution_id", "account_id")

#: Tela -> parâmetros de contexto que ela entende. Posição por conta recorta
#: por titular e instituição, mas mostra todas as contas do recorte.
TELAS_COM_CONTEXTO = {
    "transactions:transactions_view": PARAMETROS_DE_CONTEXTO,
    "dashboard:dashboard": PARAMETROS_DE_CONTEXTO,
    "reports:projections_view": PARAMETROS_DE_CONTEXTO,
    "reports:upcoming_movements_view": PARAMETROS_DE_CONTEXTO,
    "management:management_view": PARAMETROS_DE_CONTEXTO,
    "reports:account_position_view": ("owner_id", "institution_id"),
}


def telas_com_contexto() -> dict[str, list[str]]:
    """Caminho -> parâmetros, para o JavaScript saber o que repassar."""
    return {reverse(nome): list(params) for nome, params in TELAS_COM_CONTEXTO.items()}


def faixa_de_contexto(request, user) -> dict:
    """O recorte ativo para a faixa "Mostrando só..." e o link que o limpa.

    Só nas telas que entendem o contexto, e só com o que
    `reports.services.selected_context` já validou para este usuário (ela
    anota em ``request.contexto_resolvido``): um id de outro titular na URL
    não vira nome na tela, e a faixa não paga consulta para a conta, que já
    veio com titular e instituição.
    """
    vazio = {"global_context_labels": [], "global_context_reset_url": ""}
    resolvido = getattr(request, "contexto_resolvido", None)
    if user is None or not resolvido or request.path not in telas_com_contexto():
        return vazio

    from accounts.models import AccountOwner
    from banking.models import FinancialInstitution

    conta = resolvido.get("account")
    rotulos = []
    owner_id = resolvido.get("owner_id")
    if owner_id:
        if conta is not None and conta.owner_id == owner_id:
            rotulos.append(conta.owner.name)
        else:
            dono = AccountOwner.objects.filter(id=owner_id).first()
            if dono:
                rotulos.append(dono.name)
    institution_id = resolvido.get("institution_id")
    if institution_id and conta is None:
        inst = FinancialInstitution.objects.filter(id=institution_id).first()
        if inst:
            rotulos.append(inst.institution_name)
    if conta is not None:
        rotulos.append(f"{conta.institution.institution_name} / {conta.account_name}")
    if not rotulos:
        return vazio

    # Os três vão vazios, e não ausentes: o repasse no clique completa o que
    # falta no link e devolveria o recorte que este link quer tirar (o mesmo
    # truque de "Mostrar todas as contas" com `grupos=`).
    limpo = request.GET.copy()
    for nome in PARAMETROS_DE_CONTEXTO:
        limpo[nome] = ""
    return {
        "global_context_labels": rotulos,
        "global_context_reset_url": f"{request.path}?{limpo.urlencode()}",
    }
