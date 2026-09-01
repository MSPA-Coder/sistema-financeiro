# Regras de domínio

## Lançamentos e saldos

- Valores monetários usam `Decimal`.
- `entry_amount` e `realized_amount`, quando informado, são positivos; o tipo
  `receita` ou `despesa` determina o sinal no saldo.
- Os status de lançamentos e operações são `a_vencer`, `vencidos` e
  `realizado`.
- O saldo inicial cadastrado na conta integra a base dos cálculos.
- A visão `realizado` considera movimentos realizados. A visão `vencidos`
  combina realizados e abertos vencidos. A visão `a_vencer` combina realizados
  e movimentos projetados futuros.
- Transferências internas afetam as contas envolvidas, mas categorias internas
  são excluídas das receitas e despesas gerenciais.

`BankOperation` agrupa lançamentos relacionados. Parcelas, recorrências e as
duas pontas de transferências preservam o agrupamento durante criação, edição,
realização, exclusão e conciliação. Operações compostas são atômicas: uma falha
reverte o conjunto.

## Fechamento mensal

O fechamento pertence a uma conta e a um mês. Enquanto estiver ativo, bloqueia
criação, edição, exclusão, realização e conciliação de movimentos que atinjam o
período. A reabertura exige ação explícita, motivo e registro de auditoria.

## Extratos, conciliação e comprovantes

Extratos CSV, OFX, OFC e QFX são normalizados antes da importação. PDF é aceito
somente para instituições homologadas pelo código. A importação limita tamanho
e número de linhas, detecta duplicidades por conta e não preserva o arquivo de
extrato original como mídia.

A conciliação verifica acesso à conta, tipo, valor, status, duplicidade e
fechamento do período. Um lançamento pode ter no máximo uma conciliação ativa,
restrição também garantida no PostgreSQL.

Comprovantes são arquivos distintos do registro relacional. O sistema valida
tamanho, extensão e assinatura, grava o arquivo sob `MEDIA_ROOT/attachments` e
mantém seus metadados no PostgreSQL. Downloads passam novamente pelo controle
de acesso ao lançamento.

## Projeção de recorrências

O horizonte e o dia mensal de execução são configurados em Parâmetros. A
projeção estende cada `BankOperation` recorrente até o fim do horizonte, a
partir da ocorrência de maior vencimento. Reexecutar para o mesmo horizonte é
idempotente e uma ocorrência removida no meio da série não é recriada.

`transactions.middleware.ProjecaoRecorrenteMensalMiddleware` verifica a
necessidade em requisições autenticadas. Cada processo consulta no máximo uma
vez ao dia até resolver o mês; a execução acontece no máximo uma vez no mês, a
partir do dia configurado. Se o dia não existir naquele mês, usa-se o último
dia do mês. O campo `last_projection_run` no PostgreSQL e um advisory lock
coordenam os workers. Falhas são registradas e não derrubam a resposta pedida
pelo usuário; uma verificação posterior tenta novamente.

O botão **Executar Projeção Agora** antecipa ou repete a operação a qualquer
momento. A mesma rotina idempotente é usada, e a mensagem informa quantos
lançamentos foram gerados. Não há agendador externo: sem requisições
autenticadas após o dia configurado, a execução automática aguarda o próximo
acesso.

## Datas e visibilidade

Datas de vencimento e realização são datas civis. Datas e horas de auditoria e
controle usam timezone e são persistidas pelo PostgreSQL com suporte a fuso;
`TIME_ZONE` é `America/Sao_Paulo` e `USE_TZ=True`.

As preferências pessoais de ocultação afetam somente os agregados do Dashboard
e de Projeções. Uma conta explicitamente escolhida no filtro continua visível,
assim como nos seletores e nas demais telas permitidas ao usuário.

## Gestão gerencial: ciclo de vida

Tags, projetos/centros de custo e orçamentos podem ser excluídos somente se
não houver histórico dependente. Tag ou projeto com vínculo a lançamento é
arquivado/desativado, preservando a classificação já registrada e impedindo
novos vínculos. Orçamento com lançamento realizado no seu titular, categoria e
mês é arquivado; sem realizado, pode ser excluído. Itens arquivados continuam
visíveis no histórico, mas não aparecem nos seletores de novos vínculos.
