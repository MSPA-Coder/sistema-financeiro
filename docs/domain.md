# Regras de domínio

## Moeda

A moeda pertence à **conta** (`financial_account.currency`), nunca ao
lançamento: todo lançamento é de uma conta e herda a moeda dela. Repetir a
moeda no lançamento criaria duas verdades para o mesmo valor, e um dia elas
divergiriam sem que nenhum relatório soubesse qual vale. Uma instituição que
guarde duas moedas vira duas contas, como o extrato dela mesma apresenta.

- as siglas válidas são `BRL` e `USD`, garantidas pela `CheckConstraint`
  `ck_financial_account_currency_valid`;
- **conta com lançamento não muda de moeda**: a troca não converteria nada, só
  faria os valores já gravados passarem a valer outra coisa. `update_account`
  recusa, e a tela de Contas trava o seletor nesse caso;
- `initial_balance` vem acompanhado de `initial_balance_date`, a data a que ele
  se refere. Sem data, saldo inicial em moeda estrangeira não teria como ser
  convertido nem posicionado numa série histórica. As contas existentes foram
  datadas em 31/12/2025, o corte a partir do qual há lançamentos;
- o que não tem conta está na **moeda base** (`BRL`): é o caso do orçamento
  mensal, que é por categoria e mês;
- **converter moeda não acontece aqui.** Este sistema registra o fato: a compra
  de moeda estrangeira é uma transferência entre duas contas suas, e a taxa
  efetiva é a divisão de uma ponta pela outra. Cotação e conversão para uma
  moeda de exibição pertencem a quem consolida.

## Agregação por moeda

Somar valores de contas de moedas diferentes é erro, não arredondamento — um
total que mistura real com dólar sai formatado e alinhado, e ninguém desconfia
dele até conferir à mão.

A resposta certa não é recusar a seleção: é **um bloco de totais por moeda**.
Sem conversão, "quanto eu tenho" tem uma resposta por moeda, e é isso que as
telas mostram. O número único convertido pertence a quem consolida.

- `banking.services.account_ids_by_currency` reparte a seleção por moeda
  (moeda base primeiro), e `currency_blocks` garante pelo menos um bloco — uma
  seleção sem conta nenhuma continua escrevendo `R$ 0,00`;
- **com uma moeda só, nada muda na tela**: um bloco, sem cabeçalho, idêntico ao
  que a tela sempre foi. O cabeçalho da moeda só aparece a partir do segundo
  bloco;
- os agregados **não** mudaram: continuam exigindo um conjunto de contas de uma
  moeda só. Quem mudou foi o chamador — a tela pede um total por moeda, uma
  chamada por bloco;
- `banking.services.currency_of_accounts` continua sendo a porta única dessa
  exigência: devolve a moeda comum e levanta
  `core.domain.finance.MixedCurrencyError` quando o conjunto atravessa moedas.
  Conjunto vazio vale a moeda base. Ela hoje não dispara em nenhuma tela, e é
  essa a intenção: é a rede para o próximo agregado escrito sem a regra;
- os agregados guardados por ela: `decimal_base_balance`, os totais assinados de
  lançamentos (por período e por mês) e a grade de planejamento anual;
- na tela, `core.htmx.recusa_moedas_misturadas` transforma o erro em aviso
  (HTTP 400 com o gatilho `app:moedas-misturadas`), nunca em 500;
- **o painel é a exceção**: ele é feito de gráficos, e duas moedas não cabem no
  mesmo eixo. Em vez de repetir a página, ele tem um filtro de moeda ao lado dos
  demais — visível só quando há mais de uma. Moeda pedida que não existe na
  seleção não vence: escolher a conta em dólar traz a moeda junto;
- linha por conta não é agregação: relatórios detalhados continuam listando
  contas de moedas diferentes lado a lado, cada uma com o seu símbolo. O que não
  existe é o TOTAL delas — esse é um por moeda;
- uma transferência entre moedas aparece **nos dois blocos**, uma ponta em cada:
  é o mesmo dinheiro visto de cada lado, e não há total que junte os dois;
- o orçamento mensal compara apenas o realizado em moeda base, porque
  `MonthlyBudget.planned_amount` também é em moeda base.

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

Realizar é um fato de uma ocorrência, não do grupo. Criar um parcelado ou um
recorrente como realizado realiza só a primeira ocorrência, com a data
informada; as demais seguem o vencimento, como a projeção mensal já fazia. Na
edição em grupo ("todos" ou "este e os próximos"), status, data e valor
realizados do formulário valem só para a linha editada e a outra ponta dela;
as outras ocorrências mantêm a realização que têm, e as em aberto só
acompanham o vencimento. Decidido assim em 17/09/2026 porque o status pedido
era aplicado ao grupo inteiro: meses futuros nasciam realizados na mesma data,
o saldo realizado daquele dia perdia o valor uma vez por ocorrência, e editar o
grupo a partir de uma linha em aberto apagava a realização das já pagas. A
`BankOperation` resume o status das ocorrências também na criação.

Transferências internas exigem concessão explícita do usuário para a conta de
destino. Recorrências internas guardam um responsável; a projeção global só as
estende enquanto a concessão continuar válida. Legados sem responsável ficam
pausados até atribuição administrativa auditada.

### Transferência entre moedas

As duas pontas de uma transferência são espelhadas — o mesmo valor em cada uma —
enquanto as contas estão na mesma moeda. Quando não estão, o espelho seria o
defeito: gravar o valor em reais também na conta em dólar é escrever um número
que nunca existiu no extrato dela.

- o valor creditado no destino é **obrigatório** quando as moedas diferem e
  **recusado** quando são iguais — `transactions.services.counterparty_amount_for_transfer`
  é o único lugar que decide isso, e vale na criação, na edição e na conversão
  de um lançamento simples em transferência;
- **não há parcelamento nem recorrência** entre moedas diferentes: cada compra
  de moeda tem a taxa do seu dia, e repetir o mesmo par de valores registraria
  taxas que não existiram;
- realizar uma ponta **não** copia o valor para a outra quando as moedas
  diferem — cada lado realiza pelo próprio valor. Vale também para a
  conciliação de extrato, que realiza pelo valor da linha;
- a **taxa efetiva não é gravada**: é a divisão de uma ponta pela outra. Número
  derivado que se guarda é número que um dia discorda das pontas;
- na listagem de operações, o valor da operação é o da ponta de origem, na moeda
  dela: somar as duas pontas contaria o mesmo dinheiro duas vezes, e a maior das
  duas seria só a de número maior.

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
