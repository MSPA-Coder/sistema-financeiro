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
- Lançamento `realizado` tem data e valor de realização. A data é obrigatória
  porque o saldo realizado é filtrado por ela: sem data, o lançamento sumia de
  todos os saldos sem aviso (o #1236, achado em 17/09/2026). Valor realizado
  vazio grava o previsto do próprio lançamento, como o botão "Realizar" já
  fazia; decidido assim porque o vazio era lido como o previsto pelo saldo e
  como zero pelo planejamento anual. O serviço recusa a data vazia, e o banco
  tem o piso `ck_cash_flow_entry_realized_has_date_and_amount`. Lançamento em
  aberto não guarda realização: o serviço limpa a data e o valor.
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

### Vencimento na edição em grupo

O formulário de edição envia os campos da **linha editada**, e o escopo diz
até onde eles valem. Para os demais campos, "todos os registros do grupo"
significa repetir o valor; para o vencimento, não — cada ocorrência tem o mês
dela, e repetir a data gravaria todas no mesmo dia.

O que o grupo acompanha é a **diferença** entre o vencimento novo e o antigo da
linha editada: tantos meses, e o novo dia quando o dia muda. Ela se aplica à
data **de cada ocorrência**, não à posição dela na série. Disso decorre o que
uma edição de grupo nunca faz:

- editar sem tocar no vencimento não move vencimento nenhum;
- uma ocorrência adiantada ou adiada a mão — fim de semana, feriado —
  continua onde foi posta, e só acompanha o deslocamento;
- um mês removido no meio da série continua ausente; a ocorrência seguinte não
  ocupa o lugar dele.

Vale igualmente para "este registro e os próximos", que renumera o bloco
apagando e recriando as linhas: os ids mudam, os vencimentos são os antigos
deslocados. Um dia que o calendário já aparou (31 em fevereiro vira 28) viaja
aparado: o sistema desloca a data que existe, não uma intenção de "todo dia 31"
que ele não guarda.

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

## Cartão de crédito

O cartão é uma conta com `account_kind = "cartao_credito"`. Ele segue as mesmas
regras das outras contas, com três diferenças de significado:

- **o saldo é dívida.** Compras são despesas na conta do cartão, na data da
  compra e com a categoria real; o saldo fica negativo, e esse negativo é a
  fatura em aberto. Um cartão negativo não é alerta de caixa;
- **pagar a fatura é transferência, não despesa.** O pagamento sai de uma ou
  mais contas comuns para o cartão, uma transferência de cada, e pode ser
  dividido entre contas. Lançar o pagamento como despesa contaria o mesmo gasto
  duas vezes: na compra e na fatura;
- **os dias guiam a projeção.** `card_closing_day` e `card_due_day` (1 a 31)
  são obrigatórios no cartão. A conta de pagamento padrão
  (`card_payment_account`) é opcional e só diz de onde sai a fatura projetada;
  ela tem de ser uma conta comum, na mesma moeda. Uma conta que paga algum
  cartão não pode virar cartão.

O banco garante que um cartão tem os dois dias e que uma conta comum não tem
nenhum dado de cartão (`ck_financial_account_card_fields`). Os contratos
patrimoniais publicam o tipo em cada conta (`tipo`), para o consumidor tratar o
saldo do cartão como passivo.

O modelo anterior -- a fatura inteira como uma despesa recorrente de valor
estimado na conta corrente -- continua válido para o histórico. Cada cartão
passa ao modelo novo a partir da primeira fatura importada, sem reescrever o
passado.

### Importação da fatura

A fatura em CSV (C6 e XP, reconhecidas pelo cabeçalho) entra por Bancos >
Importações, na conta do cartão, e passa por duas etapas:

1. **importar** grava só as linhas (`BankStatementLine`), com o sinal da conta:
   compra negativa, pagamento e estorno positivos -- o contrário do arquivo. A
   parcela recebe a data em que entra na fatura (a da compra mais N-1 meses) e
   guarda N, o total, a data da compra, o portador e a categoria do banco.
   Parcela cuja conta passaria do fechamento da própria fatura já veio com a
   data do lançamento (a anuidade da C6 é assim) e não é deslocada; o
   fechamento sai do dia de fechamento do cartão e da última data que com
   certeza é de lançamento (compra à vista, 1ª parcela ou pagamento).
   Reenviar o arquivo não duplica, pelo mesmo hash por conta dos extratos. Uma
   fatura enviada para conta comum, ou um extrato enviado para cartão, é
   recusado antes de gravar: os dois invertem o sinal de tudo;
2. **processar** mostra uma prévia e só então grava, tudo ou nada
   (`bank_statements/fatura.py`). Cada linha vira:
   - *já no saldo inicial*, se a data é anterior à do saldo inicial do cartão.
     O saldo soma todos os lançamentos, inclusive os anteriores a essa data, e
     lançar a linha contaria a dívida duas vezes. Se for parcela, são criadas
     as parcelas seguintes que caem a partir dessa data, "a vencer";
   - *pagamento*, casado com a transferência que chega ao cartão (mesmo valor,
     até 10 dias) e realizado nas duas pontas. Sem transferência, a linha fica
     pendente em Conciliação;
   - *parcela já lançada*, casada pela descrição e pela data (até 5 dias), não
     pelo número: o CB renumera parcelas quando o grupo é editado;
   - *compra parcelada nova*, que cria as parcelas N a M
     (`TransactionRequest.first_installment`) e realiza a N;
   - *compra já lançada* à mão (mesmo valor, até 3 dias), que é conciliada;
   - *compra* ou *estorno*, criados já realizados.

A categoria sugerida vem da última compra com a mesma descrição no cartão,
depois da categoria escolhida da última vez para a mesma categoria do banco
(a prévia ensina as faturas seguintes), depois da categoria do banco quando ela
tem o nome de uma categoria cadastrada, e por fim de "Outros"; a prévia deixa
trocar cada uma. Uma fatura inteira anterior ao saldo inicial não lança nada, e
a prévia avisa isso em destaque: para importar histórico, a data do saldo
inicial do cartão tem de ser anterior à primeira fatura. Quando o arquivo tem mais
de um portador, o nome de quem comprou vai para a descrição.

### Reclassificação em lote

Banking > Reclassificação (`bank_statements/reclassificacao.py`, permissão
`banking.reclassify`) troca a categoria de vários lançamentos gerenciais de uma
vez; transferência e movimentação ficam de fora. A prévia mostra tudo antes de
gravar:

- **iguais** -- a mesma descrição depois de tirar portador, valor em dólar e
  termos com número (`chave_da_descricao`) -- entram junto, já marcadas;
- **parecidas** -- mesma primeira palavra, chave diferente -- só são sugeridas;
- **mesma categoria do banco** pode ser aplicada a todas as compras dela;
- parcelado e recorrente mudam inteiros.

Mês fechado só é atravessado com autorização explícita e com a permissão de
fechamento mensal: cada mês é reaberto com motivo, alterado e fechado de novo;
o saldo de fechamento é recalculado e, se diferir do anterior, nada é gravado.
Não há tabela de regras: o importador de fatura sugere a categoria pela última
compra com a mesma chave, em qualquer conta, então a reclassificação é a regra.

## Fechamento mensal

O fechamento pertence a uma conta e a um mês. Enquanto estiver ativo, bloqueia
criação, edição, exclusão, realização e conciliação de movimentos que atinjam o
período. A reabertura exige ação explícita, motivo e registro de auditoria.

## Extratos, conciliação e comprovantes

Extratos CSV, OFX, OFC e QFX são normalizados antes da importação. PDF é aceito
somente para instituições homologadas pelo código. A importação limita tamanho
e número de linhas, detecta duplicidades por conta e não preserva o arquivo de
extrato original como mídia. A fatura de cartão em CSV tem leitura e
processamento próprios, descritos em "Importação da fatura".

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

A projeção só estende. Por isso, salvar os Parâmetros recolhe as séries ao
horizonte atual (`recolher_alem_do_horizonte`): as ocorrências recorrentes
não realizadas além do fim são apagadas, com auditoria, e a série não é
encerrada -- volta a crescer quando o horizonte avançar. Sem isso, diminuir o
horizonte deixava os meses além do novo fim com só as séries antigas, e o
saldo projetado desses meses perdia o sentido. Fica tudo até a última
ocorrência que recebeu atenção própria (conciliada, com comprovante, etiqueta
ou projeto, ou editada em "somente este"), para não abrir um buraco que a
extensão não preenche. Parcelas não são recolhidas: são dívida contratada. O
contrato de projeção publica `ultima_recorrencia` limitada ao fim do
horizonte.

Excluir uma recorrência com o escopo "este e os próximos", ou editá-la nesse
escopo desmarcando "recorrente", encerra a série: a `BankOperation` guarda o vencimento do corte em `recurrence_ended_on` e a
projeção deixa de estendê-la. Vale para a recorrente simples e para a
transferência interna recorrente, cujas duas pernas pertencem à mesma operação.
As ocorrências que restam mantêm `is_recurring`, porque o Planejamento anual
separa recorrente de não recorrente por esse campo e o histórico não muda de
classificação; na edição, as linhas do bloco ficam como lançamentos não
recorrentes da mesma operação. Hoje a interface não reabre uma série
encerrada; retomar a recorrência é criar um novo lançamento recorrente. Excluir
só uma ocorrência abre uma lacuna e não encerra nada. Desmarcar "recorrente"
em "somente este" também não encerra: a linha vira avulsa, e a projeção não
cria outra ocorrência num mês em que a operação já tenha linha, recorrente ou
não, mesmo que o dia tenha sido trocado. Linhas com `is_recurring` desligado à mão, como o contorno aplicado às
operações 162 e 164 em 23/09/2026, também ficam fora da projeção.
O middleware é o gatilho atual; um scheduler ou worker também é válido se
preservar idempotência, retry seguro, auditoria e coordenação entre processos.

`transactions.middleware.ProjecaoRecorrenteMensalMiddleware` verifica a
necessidade em requisições autenticadas. Cada processo consulta no máximo uma
vez ao dia até resolver o mês; a execução acontece no máximo uma vez no mês, a
partir do dia configurado. Se o dia não existir naquele mês, usa-se o último
dia do mês. O campo `last_projection_run` no PostgreSQL e um advisory lock
coordenam os workers. Falhas são registradas e não derrubam a resposta pedida
pelo usuário; uma verificação posterior tenta novamente.

O botão **Executar Projeção Agora** antecipa ou repete a operação a qualquer
momento. A mesma rotina idempotente é usada, e a mensagem informa quantos
lançamentos foram gerados. No desenho atual não há agendador externo: sem
requisições autenticadas após o dia configurado, a execução automática aguarda
o próximo acesso. Isso é uma decisão operacional atual, não uma restrição de
domínio.

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
