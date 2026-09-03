# Auditoria de segurança

Relatórios de auditoria de segurança deste projeto, em PDF, e o material que os
produz.

São **dois relatórios**, e os dois são entregáveis:

| Arquivo | O que é |
| --- | --- |
| `relatorio-auditoria-seguranca.pdf` | **A auditoria**, como foi entregue em 01/09/2026. É a foto do que foi encontrado. Não é atualizado quando um achado é corrigido. |
| `relatorio-auditoria-seguranca-estado-final.pdf` | **O estado final**: o mesmo relatório acrescido da situação de cada achado depois da remediação — o que foi corrigido, como foi verificado e o que continua em aberto. |
| `achados.json` | Fonte de verdade do conteúdo da auditoria: escopo, stack, nota metodológica, pontos fortes e fracos, achados, recomendações e o texto completo das issues. |
| `achados-estado-final.json` | O mesmo conteúdo mais `situacao`, `correcao` e `verificacao` em cada achado, a seção `remediacao` e os pontos fortes que as correções acrescentaram. |
| `gerar_relatorio.py` | O gerador, um só para os dois. Decide apenas a APRESENTAÇÃO (capa, gráficos, tabelas, chips, cabeçalho e rodapé). |

Para corrigir um achado, reescrever uma recomendação ou acrescentar uma issue,
edite o JSON e rode o gerador de novo. Não há conteúdo no `.py`.

## Por que o relatório da auditoria não é atualizado

Um relatório de auditoria vale por registrar **o que foi encontrado na data em
que foi encontrado**. Reescrevê-lo a cada correção apaga exatamente a
informação que ele existe para guardar, e some com o rastro de quando o defeito
esteve aberto.

Por isso o estado final é um segundo documento e não uma revisão do primeiro. O
diagnóstico aparece nele palavra por palavra — inclusive nos achados já
corrigidos, cujo trecho de código citado não existe mais na árvore. O que mudou
entra **ao lado**, num bloco de correção com a verificação que o prova.

## Regerar os PDFs

O gerador precisa de `reportlab` e `matplotlib`. **Nada é instalado
globalmente** — use um ambiente isolado, descartável, fora da árvore do
projeto:

```powershell
python -m venv $env:TEMP\venv-relatorio
& $env:TEMP\venv-relatorio\Scripts\python.exe -m pip install reportlab matplotlib
```

A auditoria (os dois argumentos são os padrões, pode omitir):

```powershell
& $env:TEMP\venv-relatorio\Scripts\python.exe docs\security-audit\gerar_relatorio.py
```

O estado final:

```powershell
& $env:TEMP\venv-relatorio\Scripts\python.exe docs\security-audit\gerar_relatorio.py --dados achados-estado-final.json --saida relatorio-auditoria-seguranca-estado-final.pdf
```

Caminho relativo se resolve contra `docs/security-audit/`, não contra o
diretório de onde se chamou: o comando funciona igual da raiz do repositório.

O script apaga os PNGs intermediários dos gráficos — o PDF já os carrega
embutidos.

### O que decide qual relatório sai

Os dados, não a linha de comando: o gerador produz a seção "Estado da
remediação", os chips de situação e as barras por situação **quando os achados
do JSON trazem o campo `situacao`**. Um JSON sem esse campo gera exatamente o
relatório de auditoria de sempre. Não existe um segundo script, de propósito —
dois geradores divergiriam na primeira correção de layout, e o leitor passaria
a comparar dois documentos que não são mais comparáveis.

### Nota sobre o PDF da auditoria já entregue

O `relatorio-auditoria-seguranca.pdf` no diretório foi produzido **antes** de
três correções de layout feitas no gerador em 01/09 (quebra de linha de tabela
entre páginas, órfão na lista de categorias inaplicáveis e colisão do rótulo do
gráfico). Regerá-lo hoje produz o mesmo conteúdo em uma página a menos. O
arquivo foi preservado como entregue, de propósito; se preferir a paginação
nova, basta rodar o comando acima.

## Conferir o resultado

A verificação visual usada nesta rodada rasteriza as páginas e confere
contagem, gráficos e legibilidade das tabelas:

```powershell
& $env:TEMP\venv-relatorio\Scripts\python.exe -m pip install pymupdf
& $env:TEMP\venv-relatorio\Scripts\python.exe -c "import pymupdf; d=pymupdf.open('docs/security-audit/relatorio-auditoria-seguranca-estado-final.pdf'); print(d.page_count); [d[i].get_pixmap(dpi=110).save(f'pagina-{i+1}.png') for i in range(d.page_count)]"
```

## Convenções do relatório

- **Severidades e cores**, fixadas na paleta do pedido: crítica `#B91C1C`, alta
  `#EA580C`, média `#D97706`, baixa `#2563EB`, ponto forte `#059669`.
  `informativa` usa cinza e conta separado — é registro, não risco.
- **Situações**, no relatório de estado final: corrigido `#059669`, parcial
  `#0891B2`, em aberto `#334155`. Deliberadamente fora da rampa de severidade,
  para que ninguém leia uma situação como se fosse uma gravidade.
- **Todo achado é verificado no código real**: caminho de arquivo, número de
  linha e trecho vêm da árvore auditada. Nada de especulação. O mesmo vale para
  cada correção: o bloco traz arquivo, linha e o teste que a fixa.
- **A cobertura também é registrada**: a seção de pontos fortes existe para
  provar o que foi conferido e está correto, não só o que está errado.
- **Categoria que não se aplica é dita explicitamente**, na subseção final dos
  pontos fortes e fracos, em vez de render achado forçado.
- **Página A4, margens de 2 cm**, cabeçalho e rodapé com o nome do relatório e
  o número da página.
