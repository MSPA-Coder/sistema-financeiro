# Diretrizes de teste

Texto comum a todos os repositórios. A cópia canônica fica em
`manutencao/docs/TESTES.md`; as demais são idênticas. Para mudar uma regra,
mude a canônica e copie para todos na mesma rodada. O que é particular de um
projeto (camadas, marcadores, comandos) fica no `AGENTS.md` dele.

Base: os quatro pilares de Khorikov (*Unit Testing Principles, Practices, and
Patterns*), o *Test Desiderata* de Kent Beck, testar comportamento e não
implementação, *fitness functions* de arquitetura e teste de mutação.

## T1. Duas perguntas antes de existir

Um teste só entra se responder "sim" às duas:

1. Ele **falharia** se a regra estivesse errada?
2. Ele **continuaria passando** se o código fosse reescrito sem mudar o
   comportamento?

Só a primeira = detector de mudança: quebra a cada edição legítima. Só a
segunda = decoração. Os dois casos custam manutenção e não protegem nada.

## T2. Regra se testa pela porta pública, nunca pelo texto do código

Chame a função de domínio, o serviço ou a rota e confira o efeito: dado
gravado, resposta, recusa. **Procurar palavra no código Python é proibido**
(`'has_perm' in inspect.getsource(...)`, `'exigir_troca=False' in fonte`):
passa enquanto a palavra estiver escrita, mesmo que nunca seja alcançada. Foi
assim que seis rotas do ConfortoTermico ficaram sem controle de área (ver o
docstring de `test_autorizacao_por_area.py` naquele repositório).

## T3. Ler arquivo do repositório só como varredura de política

Legítimo quando a regra vale para **todos** os arquivos de um tipo e a
violação é um padrão proibido, com o motivo na mensagem de falha: nenhum JS
escreve `style` (a CSP bloqueia), nenhum `{# #}` ocupa duas linhas (vaza no
HTML), nenhum template busca host externo, todo input numérico tem marcador de
privacidade. Não vale conferir que **um** arquivo contém **um** trecho exato.

## T4. Configuração: conferir a propriedade, não o texto

Compose, CI, Dockerfile e `pyproject`: carregue o YAML/TOML e confira o valor
(`servico["read_only"] is True`). Melhor ainda, execute (`docker compose
config`, subir a pilha, smoke da CI). Trecho com indentação exata quebra ao
reordenar uma chave e não diz qual propriedade se perdeu.

## T5. Tela: o que o servidor decide, não a aparência

Testa-se:

- se o dado aparece ou não (permissão, dono);
- se o marcador de privacidade está no valor sensível;
- se a rota só de leitura recusa POST;
- se o erro devolve 4xx e mantém o formulário;
- o estado vazio contra o indisponível.

Quando o framework dá o contexto (`response.context` no Django), confira o
contexto em vez do HTML. Não se testa classe CSS, cor, fonte, ordem de coluna
ou rótulo de botão. Texto só quando **é** a regra (aviso legal, mensagem que
orienta a pessoa numa recusa). Aparência se verifica olhando a tela.

## T6. Front-end crítico: sentinela declarada

Nenhum projeto executa o navegador nos testes. Para os poucos comportamentos
de JS ou CSS em que a falha custa dinheiro ou privacidade (confirmação que
falha fechada, resposta HTMX antiga que não sobrescreve a nova, modo discreto),
o teste pode conferir o trecho que define o mecanismo, desde que:

- seja marcado com `@pytest.mark.sentinela_front`;
- tenha no docstring o risco que protege;
- confira o mínimo que define o mecanismo, não cada linha dele.

Todo o resto que é cosmético sai. Se as sentinelas passarem de meia dúzia num
projeto, o próximo passo é teste em navegador (Playwright) para esses fluxos,
não mais literais.

## T7. Banco real quando a regra vive no banco

PostgreSQL efêmero para:

- constraint, índice único, lock e concorrência;
- migração;
- SQL de data e fuso;
- contagem de consultas;
- atomicidade.

Regra de domínio pura roda sem banco. **Mock só na fronteira externa** (SMTP,
provedor de cotação, RTD, rede, relógio). Nunca mock de módulo próprio para
fugir do banco: congela a divisão interna, que é o que a refatoração muda.
SQLite não substitui o PostgreSQL.

## T8. Migração que mexe em dado tem teste com dado

Revisão só de schema já é coberta pelo bootstrap (a cadeia inteira num banco
vazio). Revisão que transforma ou apaga dado ganha teste com o antes e o
depois; se ela pode abortar, o teste prova o aborto.

## T9. Regressão descreve o certo e prova que falhava

O nome diz a regra, não o bug ("leitura das 22h30 pertence ao dia local"). O
docstring conta o incidente em poucas frases. Antes do merge, mutação: o teste
precisa falhar no código antigo.

Teste de **tumba** (algo removido não volta) só quando a volta seria
perigosa. O admin do Django abria um segundo login sem trava de força bruta:
fica. Uma opção de filtro que saiu da tela: não fica.

## T10. Produção não ganha ramo para o teste

Configuração de teste entra por fixture ou injeção (o `config` da factory,
`settings` do pytest-django). É aceita a bandeira padrão do framework
(`TESTING`) se a produção se recusar a subir com ela ligada.

## T11. Densidade proporcional ao dano

Cobertura densa onde o erro custa dinheiro, dado ou acesso: saldo, fatura,
fechamento, posição, autorização, senha, backup. Smoke onde o erro é visível e
barato: telas de leitura, páginas institucionais. Percentual de cobertura não
é meta; mede o que foi executado, não o que foi verificado.

## T12. O que a documentação pode impor

**Pode:**

- descrever as camadas e o marcador de cada uma;
- dar o comando do portão;
- proibir desmarcar a camada com banco para o laço rápido passar.

**Não deve:**

- fixar a contagem de testes;
- listar quais arquivos formam uma camada (o marcador diz, e a lista apodrece);
- afirmar "ainda não tem teste" sem data;
- exigir teste para toda mudança, independentemente do risco.

## Diante de vermelho

Decida de quem é o defeito antes de mexer. Um teste que mede texto literal
reprova mudança legítima, e nesse caso quem se corrige é a asserção. Nunca
escreva código de produção para um teste passar. Se o teste estava certo, o
código está errado, mesmo que a mudança pareça inocente.

## Ao remover um teste

Confira por mutação que outro teste ainda pega o defeito que ele dizia
proteger. Se nenhum pega e o risco é real, reescreva em vez de remover.
