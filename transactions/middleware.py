"""Dispara a projecao recorrente uma vez por mes, sem agendador.

O sistema nao tem cron, Celery nem comando de management. Este middleware le o
"Dia de execucao automatica" configurado na tela de Parametros.

O ponto de execucao e deliberadamente uma requisicao autenticada, e nao o
boot; ver os invariantes em `executar_projecao_mensal_se_devido`.

O custo por requisicao e proximo de zero. Depois que o mes esta resolvido, a
decisao sai de memoria e nao ha consulta nenhuma; antes disso, ha no maximo UMA
verificacao por processo por dia. A leitura em si e de uma linha de
`app_setting`.
"""

from __future__ import annotations

import logging
from datetime import date

from transactions.recurring_projection import executar_projecao_mensal_se_devido

logger = logging.getLogger(__name__)


class ProjecaoRecorrenteMensalMiddleware:
    """Verifica, no maximo uma vez por dia por processo, se a projecao e devida.

    O estado e por processo (o Django instancia o middleware uma vez). Com
    varios workers, cada um verifica por conta propria -- o que decide de fato
    e o `last_projection_run` no banco, compartilhado, mais o advisory lock da
    propria projecao. A memoria aqui poupa consulta; nao e a garantia.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Mes ja resolvido: enquanto for o mes corrente, nao ha o que fazer e
        # nem sequer se consulta o banco.
        self._mes_resolvido: tuple[int, int] | None = None
        # Dia ja verificado: evita reler `app_setting` a cada requisicao nos
        # dias ANTES do dia marcado, quando a resposta e "ainda nao" e nada
        # mudou. Sem isto seria uma consulta por requisicao ate o dia chegar.
        self._verificado_em: date | None = None

    def __call__(self, request):
        self._verificar(request)
        return self.get_response(request)

    def _verificar(self, request) -> None:
        # Requisicao sem sessao nao dispara escrita: a sonda de saude e a tela
        # de login batem aqui o tempo todo, e projecao disparada por sonda
        # amarraria o /health a uma operacao de escrita no banco.
        usuario = getattr(request, "user", None)
        if usuario is None or not usuario.is_authenticated:
            return

        hoje = date.today()
        if self._mes_resolvido == (hoje.year, hoje.month):
            return
        if self._verificado_em == hoje:
            return
        self._verificado_em = hoje

        try:
            decisao = executar_projecao_mensal_se_devido(hoje)
        except Exception:
            # Uma falha aqui NAO pode derrubar a pagina que o usuario pediu --
            # a projecao e trabalho de fundo. Mas tambem nao pode sumir: fica
            # no log em ERROR, e como nada foi marcado como resolvido, a
            # proxima verificacao tenta de novo.
            logger.exception("Projecao recorrente automatica falhou.")
            return

        if decisao.mes_resolvido:
            self._mes_resolvido = (hoje.year, hoje.month)
