#!/usr/bin/env python3
"""Gera o relatório de auditoria de segurança em PDF a partir de `achados.json`.

Uso (ambiente isolado, nada instalado globalmente):

    python -m venv .venv-relatorio
    .venv-relatorio/Scripts/python -m pip install reportlab matplotlib
    .venv-relatorio/Scripts/python docs/security-audit/gerar_relatorio.py

O conteúdo do relatório vive inteiro em `achados.json`, ao lado deste arquivo:
para corrigir um achado, reescrever uma recomendação ou acrescentar uma issue,
edite o JSON e rode o script de novo. Este módulo só decide a APRESENTAÇÃO.

Saída: `relatorio-auditoria-seguranca.pdf` no mesmo diretório.

O mesmo módulo gera o relatório de ESTADO FINAL, que é o de auditoria acrescido
da situação de cada achado depois da remediação:

    python docs/security-audit/gerar_relatorio.py \
        --dados achados-estado-final.json \
        --saida relatorio-auditoria-seguranca-estado-final.pdf

Não há um segundo script, de propósito: dois geradores divergiriam na primeira
correção de layout e o leitor passaria a comparar dois documentos que não são
mais comparáveis. Tudo o que o estado final acrescenta é condicional ao campo
`situacao` nos achados — sem ele, o JSON da auditoria original gera exatamente
o mesmo PDF de sempre.
"""
from __future__ import annotations

import argparse
import json
import textwrap
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patches  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import cm  # noqa: E402
from reportlab.platypus import (  # noqa: E402
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

AQUI = Path(__file__).resolve().parent
DADOS = AQUI / "achados.json"
SAIDA = AQUI / "relatorio-auditoria-seguranca.pdf"

# Paleta fixada pelo pedido da auditoria. Manter estes valores: os gráficos, os
# chips da tabela e a legenda leem daqui, e divergir entre eles faria a mesma
# severidade aparecer em duas cores na mesma página.
CORES = {
    "critica": colors.HexColor("#B91C1C"),
    "alta": colors.HexColor("#EA580C"),
    "media": colors.HexColor("#D97706"),
    "baixa": colors.HexColor("#2563EB"),
    "informativa": colors.HexColor("#64748B"),
    "forte": colors.HexColor("#059669"),
}
HEX = {k: v.hexval().replace("0x", "#") for k, v in CORES.items()}

ROTULO = {
    "critica": "CRÍTICA",
    "alta": "ALTA",
    "media": "MÉDIA",
    "baixa": "BAIXA",
    "informativa": "INFORMATIVA",
}
ORDEM = ["critica", "alta", "media", "baixa", "informativa"]

# Paleta da SITUACAO, deliberadamente fora da rampa de severidade. O verde já
# significa "bom" na paleta da auditoria (`forte`) e é reaproveitado aqui com o
# mesmo sentido; os outros dois evitam o vermelho-laranja-âmbar para que ninguém
# leia uma situação como se fosse uma severidade na mesma página.
CORES_SITUACAO = {
    "corrigido": colors.HexColor("#059669"),
    "parcial": colors.HexColor("#0891B2"),
    "aberto": colors.HexColor("#334155"),
}
ROTULO_SITUACAO = {
    "corrigido": "CORRIGIDO",
    "parcial": "PARCIAL",
    "aberto": "EM ABERTO",
}
ORDEM_SITUACAO = ["corrigido", "parcial", "aberto"]
HEX_SITUACAO = {
    k: v.hexval().replace("0x", "#") for k, v in CORES_SITUACAO.items()
}

TINTA = colors.HexColor("#0F172A")
TINTA_FRACA = colors.HexColor("#475569")
LINHA = colors.HexColor("#CBD5E1")
FUNDO_SUAVE = colors.HexColor("#F1F5F9")


# ---------------------------------------------------------------------------
# Estilos
# ---------------------------------------------------------------------------
def montar_estilos():
    base = getSampleStyleSheet()
    e = {}
    e["corpo"] = ParagraphStyle(
        "corpo", parent=base["BodyText"], fontName="Helvetica", fontSize=9.5,
        leading=13.5, alignment=TA_JUSTIFY, textColor=TINTA, spaceAfter=6,
    )
    e["corpo_peq"] = ParagraphStyle(
        "corpo_peq", parent=e["corpo"], fontSize=8.3, leading=11.2, alignment=0,
        spaceAfter=0,
    )
    e["mono_cell"] = ParagraphStyle(
        "mono_cell", parent=e["corpo_peq"], fontName="Courier", fontSize=7.4,
        leading=9.6, textColor=TINTA,
    )
    e["h1"] = ParagraphStyle(
        "h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=17,
        leading=21, textColor=TINTA, spaceBefore=2, spaceAfter=10,
    )
    e["h2"] = ParagraphStyle(
        "h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=12.5,
        leading=16, textColor=TINTA, spaceBefore=12, spaceAfter=6,
    )
    e["h3"] = ParagraphStyle(
        "h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=10.5,
        leading=14, textColor=TINTA_FRACA, spaceBefore=9, spaceAfter=4,
    )
    e["capa_titulo"] = ParagraphStyle(
        "capa_titulo", parent=base["Title"], fontName="Helvetica-Bold",
        fontSize=25, leading=30, textColor=TINTA, alignment=TA_CENTER,
        spaceAfter=4,
    )
    e["capa_sub"] = ParagraphStyle(
        "capa_sub", parent=base["Normal"], fontName="Helvetica", fontSize=12.5,
        leading=17, textColor=TINTA_FRACA, alignment=TA_CENTER,
    )
    e["chip"] = ParagraphStyle(
        "chip", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=7.2,
        leading=9, textColor=colors.white, alignment=TA_CENTER,
    )
    e["th"] = ParagraphStyle(
        "th", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=8.4,
        leading=11, textColor=colors.white,
    )
    e["issue"] = ParagraphStyle(
        "issue", parent=base["Code"], fontName="Courier", fontSize=7.2,
        leading=9.2, textColor=TINTA,
    )
    return e


# ---------------------------------------------------------------------------
# Gráficos
# ---------------------------------------------------------------------------
def graficos_do_resumo(achados: list[dict], destino: Path) -> Path | None:
    """Rosca de severidade e barras por categoria, NUMA FIGURA SÓ.

    Duas imagens separadas obrigavam o resumo executivo a caber os dois blocos
    na mesma página ou a empurrar o segundo inteiro para a seguinte, deixando um
    vão de quase dez centímetros no meio da seção. Uma figura só é um flowable
    só: ou cabe, ou desce inteira, e em nenhum dos casos sobra buraco.
    """
    if not achados:
        return None

    contagem = Counter(a["severidade"] for a in achados)
    itens = [(s, contagem[s]) for s in ORDEM if contagem.get(s)]

    categorias: list[str] = []
    for a in achados:
        if a["categoria"] not in categorias:
            categorias.append(a["categoria"])
    categorias.sort()

    altura = max(3.10, 0.68 * len(categorias) + 1.35)
    fig, (ax_rosca, ax_barras) = plt.subplots(
        1, 2, figsize=(6.05, altura), dpi=230,
        gridspec_kw={"width_ratios": [1, 1.42], "wspace": 0.38},
    )
    # `subplots_adjust` e não `tight_layout`: um eixo de pizza tem proporção
    # travada e o `tight_layout` avisa que não sabe acomodá-lo, deixando os dois
    # títulos em alturas diferentes. Aqui a moldura é fixada à mão e os títulos
    # saem do eixo para o nível da figura, na MESMA altura.
    fig.subplots_adjust(left=0.005, right=0.965, top=0.80, bottom=0.11)

    # --- rosca ------------------------------------------------------------
    # Rótulo em volta da fatia, não em legenda ao lado: a legenda roubava a
    # largura de que o gráfico de barras precisa para os nomes de categoria.
    ax_rosca.pie(
        [n for _, n in itens],
        colors=[HEX[s] for s, _ in itens],
        labels=[ROTULO[s] + "\n" + str(n) for s, n in itens],
        labeldistance=1.28,
        radius=0.70,
        startangle=90,
        counterclock=False,
        textprops={"fontsize": 6.6, "color": "#334155"},
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.6},
    )
    total = sum(n for _, n in itens)
    # As coordenadas do centro acompanham o raio da rosca (0.70): com o valor
    # antigo, feito para um raio maior, a palavra "achados" caía por cima do
    # anel em vez de ficar dentro do furo.
    ax_rosca.text(0, 0.07, str(total), ha="center", va="center",
                  fontsize=13, fontweight="bold", color="#0F172A")
    ax_rosca.text(0, -0.14, "achados", ha="center", va="center",
                  fontsize=5.8, color="#475569")
    ax_rosca.set(aspect="equal")

    # --- barras empilhadas ------------------------------------------------
    # No relatório de auditoria elas se empilham por SEVERIDADE. No de estado
    # final, por SITUAÇÃO: a severidade já está na rosca e na faixa de chips, e
    # a pergunta que sobra na segunda leitura é "o que ainda está de pé, e em
    # que categoria".
    por_situacao = tem_situacao(achados)
    if por_situacao:
        camadas = [(s, ROTULO_SITUACAO[s], HEX_SITUACAO[s], "situacao")
                   for s in ORDEM_SITUACAO]
    else:
        camadas = [(s, ROTULO[s], HEX[s], "severidade") for s in ORDEM]

    base = [0.0] * len(categorias)
    usadas = []
    for chave, rotulo, cor, campo in camadas:
        larguras = [
            sum(1 for a in achados
                if a["categoria"] == c and a.get(campo, "aberto") == chave)
            for c in categorias
        ]
        if not any(larguras):
            continue
        ax_barras.barh(categorias, larguras, left=base, color=cor,
                       height=0.5, edgecolor="white", linewidth=0.8)
        base = [b + w for b, w in zip(base, larguras, strict=True)]
        usadas.append((rotulo, cor))

    for i, soma in enumerate(base):
        if soma:
            ax_barras.text(soma + 0.09, i, str(int(soma)), va="center",
                           fontsize=7.6, color="#0F172A", fontweight="bold")

    ax_barras.set_yticks(range(len(categorias)))
    # 22 colunas e não 30: os rótulos de categoria são alinhados à direita e
    # crescem PARA A ESQUERDA a partir do eixo. Com 30, um nome comprido como
    # "4. Chaves expostas e endurecimento de configuração" invadia a área da
    # rosca e escrevia por cima do rótulo "BAIXA".
    ax_barras.set_yticklabels([textwrap.fill(c, 22) for c in categorias],
                              fontsize=6.6)
    ax_barras.invert_yaxis()
    ax_barras.set_xlim(0, (max(base) if base else 1) + 0.85)
    ax_barras.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    ax_barras.tick_params(axis="x", labelsize=6.6, colors="#475569")
    for lado in ("top", "right", "left"):
        ax_barras.spines[lado].set_visible(False)
    ax_barras.spines["bottom"].set_color("#CBD5E1")
    ax_barras.grid(axis="x", color="#E2E8F0", linewidth=0.7)
    ax_barras.set_axisbelow(True)

    if por_situacao and usadas:
        # A rosca rotula as severidades; as situações não aparecem em lugar
        # nenhum da figura fora daqui, então a legenda é obrigatória.
        ax_barras.legend(
            handles=[
                matplotlib.patches.Patch(facecolor=cor, label=rotulo)
                for rotulo, cor in usadas
            ],
            # `best` e não uma posição fixa: a barra mais comprida às vezes é a
            # última, e aí um canto inferior fixo escreveria por cima dela.
            loc="best", fontsize=5.8, frameon=False,
            handlelength=1.1, handleheight=0.9, borderaxespad=0.1,
        )

    titulo_barras = "Por categoria e situação" if por_situacao else "Por categoria"
    for eixo, titulo in ((ax_rosca, "Por severidade"), (ax_barras, titulo_barras)):
        centro = eixo.get_position().x0 + eixo.get_position().width / 2
        fig.text(centro, 0.95, titulo, ha="center", va="top",
                 fontsize=8, fontweight="bold", color="#0F172A")

    fig.savefig(destino, transparent=True, bbox_inches="tight")
    plt.close(fig)
    return destino


# ---------------------------------------------------------------------------
# Cabeçalho, rodapé e utilidades
# ---------------------------------------------------------------------------
def fazer_decorador(nome_relatorio: str):
    def decorar(canvas, doc):
        canvas.saveState()
        largura, altura = A4
        if doc.page > 1:
            canvas.setFont("Helvetica", 7.6)
            canvas.setFillColor(TINTA_FRACA)
            canvas.drawString(2 * cm, altura - 1.25 * cm, nome_relatorio)
            canvas.setStrokeColor(LINHA)
            canvas.setLineWidth(0.5)
            canvas.line(2 * cm, altura - 1.42 * cm, largura - 2 * cm, altura - 1.42 * cm)
        canvas.setFont("Helvetica", 7.6)
        canvas.setFillColor(TINTA_FRACA)
        canvas.setStrokeColor(LINHA)
        canvas.setLineWidth(0.5)
        canvas.line(2 * cm, 1.5 * cm, largura - 2 * cm, 1.5 * cm)
        canvas.drawString(2 * cm, 1.12 * cm, nome_relatorio)
        canvas.drawRightString(largura - 2 * cm, 1.12 * cm, f"Página {doc.page}")
        canvas.restoreState()

    return decorar


def esc(texto: str) -> str:
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def tem_situacao(achados: list[dict]) -> bool:
    """Distingue o relatório de auditoria do de estado final pelos DADOS.

    Não há sinalizador na linha de comando para isso: o que decide é o JSON
    trazer ou não a situação de cada achado. Assim é impossível pedir um
    relatório de estado final sobre dados que não têm estado nenhum.
    """
    return any(a.get("situacao") for a in achados)


def chip_texto(rotulo: str, cor, estilos, largura: float = 2.05) -> Table:
    t = Table([[Paragraph(rotulo, estilos["chip"])]], colWidths=[largura * cm],
              rowHeights=[0.46 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), cor),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 1),
        ("RIGHTPADDING", (0, 0), (-1, -1), 1),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    return t


def chip(sev: str, estilos) -> Table:
    return chip_texto(ROTULO[sev], CORES[sev], estilos)


def chip_situacao(sit: str, estilos) -> Table:
    return chip_texto(ROTULO_SITUACAO[sit], CORES_SITUACAO[sit], estilos)


def faixa_de_contadores(itens: list[tuple[str, int, object]], e) -> Table:
    """Os grandes números do topo do resumo: rótulo, contagem e cor de fundo."""
    celulas, estilo = [], [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]
    for i, (rotulo, quantos, cor) in enumerate(itens):
        celulas.append(Paragraph(
            f'<font size="15"><b>{quantos}</b></font><br/>'
            f'<font size="7">{rotulo}</font>',
            ParagraphStyle("n", parent=e["corpo_peq"], alignment=TA_CENTER,
                           textColor=colors.white, leading=17),
        ))
        estilo.append(("BACKGROUND", (i, 0), (i, 0), cor))
    largura = 16.5 / len(celulas)
    t = Table([celulas], colWidths=[largura * cm] * len(celulas))
    t.setStyle(TableStyle(estilo))
    return t


def quebrar_caminho(caminho: str) -> str:
    """Escapa o caminho e deixa o ReportLab quebrá-lo na célula estreita.

    Sem marcador de quebra próprio, de propósito: a entidade de espaço de
    largura zero (`&#8203;`) não existe na codificação WinAnsi das fontes
    padrão e sai como um quadrado preto no PDF. O `splitLongWords` do ReportLab
    já parte a palavra longa sozinho — a quebra cai em posição arbitrária, mas
    o texto continua correto, que é o que importa num caminho de arquivo.
    """
    return esc(caminho)


# ---------------------------------------------------------------------------
# Seções
# ---------------------------------------------------------------------------
def capa(d: dict, e) -> list:
    hist = []
    hist.append(Spacer(1, 3.4 * cm))
    hist.append(Paragraph("Relatório de Auditoria de Segurança", e["capa_titulo"]))
    hist.append(Paragraph(f"<b>{esc(d['projeto'])}</b>", e["capa_sub"]))
    if d.get("subtitulo"):
        hist.append(Spacer(1, 0.15 * cm))
        hist.append(Paragraph(esc(d["subtitulo"]), ParagraphStyle(
            "capa_sub2", parent=e["capa_sub"], fontSize=10.5, leading=14,
            textColor=CORES_SITUACAO["corrigido"], fontName="Helvetica-Bold")))
    hist.append(Spacer(1, 0.5 * cm))

    barra = Table([[""]], colWidths=[6 * cm], rowHeights=[0.12 * cm])
    barra.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), CORES["forte"])]))
    barra.hAlign = "CENTER"
    hist.append(barra)
    hist.append(Spacer(1, 0.5 * cm))
    hist.append(Paragraph(esc(d["data"]), e["capa_sub"]))
    hist.append(Spacer(1, 1.5 * cm))

    linhas = [[Paragraph("<b>Escopo auditado</b>", e["corpo_peq"]),
               Paragraph(esc(d["escopo"]), e["corpo_peq"])]]
    for rotulo, valor in d["stack"].items():
        linhas.append([Paragraph(f"<b>{esc(rotulo)}</b>", e["corpo_peq"]),
                       Paragraph(esc(valor), e["corpo_peq"])])
    t = Table(linhas, colWidths=[4.1 * cm, 12.4 * cm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINHA),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]))
    hist.append(t)

    hist.append(Spacer(1, 0.8 * cm))
    hist.append(Paragraph("Nota metodológica", e["h3"]))
    hist.append(Paragraph(esc(d["nota_metodologica"]), e["corpo"]))
    linhas = [[Paragraph("<b>Categoria pedida</b>", e["th"]),
               Paragraph("<b>Como foi mapeada nesta stack</b>", e["th"])]]
    for m in d["metodologia"]:
        linhas.append([Paragraph(esc(m["categoria"]), e["corpo_peq"]),
                       Paragraph(esc(m["mapeamento"]), e["corpo_peq"])])
    t = Table(linhas, colWidths=[5.0 * cm, 11.5 * cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), TINTA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, FUNDO_SUAVE]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    hist.append(t)
    hist.append(PageBreak())
    return hist


def resumo_executivo(d: dict, e, graficos, num: int = 1) -> list:
    from reportlab.platypus import Image

    achados = d["achados"]
    hist = [Paragraph(f"{num}. Resumo executivo", e["h1"])]
    hist.append(Paragraph(esc(d["resumo"]), e["corpo"]))
    hist.append(Spacer(1, 0.2 * cm))

    # A faixa grande do topo responde a pergunta principal do documento. No
    # relatório de auditoria a pergunta é "quão graves são"; no de estado final
    # é "quantos ainda estão de pé" -- e a gravidade desce para a rosca logo
    # abaixo, que não mudou.
    if tem_situacao(achados):
        c = Counter(a.get("situacao", "aberto") for a in achados)
        itens = [(ROTULO_SITUACAO[s], c[s], CORES_SITUACAO[s])
                 for s in ORDEM_SITUACAO if c.get(s)]
    else:
        c = Counter(a["severidade"] for a in achados)
        presentes = [s for s in ORDEM if c.get(s)] or ["informativa"]
        itens = [(ROTULO[s], c.get(s, 0), CORES[s]) for s in presentes]
    hist.append(faixa_de_contadores(itens, e))
    hist.append(Spacer(1, 0.55 * cm))

    # Título e figura viajam juntos (`KeepTogether`): sem isso o título fica no
    # pé de uma página e a imagem desce sozinha para a seguinte. A figura é uma
    # só — rosca e barras lado a lado —, então ou cabe, ou desce inteira.
    if graficos:
        img = Image(str(graficos))
        img._restrictSize(16.0 * cm, 8.6 * cm)
        img.hAlign = "CENTER"
        hist.append(KeepTogether([
            Paragraph("Achados por severidade e por categoria", e["h3"]), img,
        ]))
    hist.append(PageBreak())
    return hist


def estado_da_remediacao(d: dict, e, num: int) -> list:
    """Seção que só existe no relatório de estado final.

    É o resumo que o mantenedor lê primeiro numa segunda passagem: uma linha
    por achado, com o que mudou no código e como isso foi provado. O detalhe
    continua na seção de achados, junto do diagnóstico original -- aqui é a
    visão de conjunto.
    """
    rem = d.get("remediacao") or {}
    hist = [Paragraph(f"{num}. Estado da remediação", e["h1"])]
    if rem.get("texto"):
        hist.append(Paragraph(esc(rem["texto"]), e["corpo"]))
    for linha in rem.get("linhas", []):
        hist.append(Paragraph(f"• {esc(linha)}", e["corpo"]))
    hist.append(Spacer(1, 0.25 * cm))

    linhas = [[Paragraph("<b>Achado</b>", e["th"]),
               Paragraph("<b>Situação</b>", e["th"]),
               Paragraph("<b>O que mudou, e como foi verificado</b>", e["th"])]]
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), TINTA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    ordenados = sorted(
        d["achados"],
        key=lambda a: (ORDEM_SITUACAO.index(a.get("situacao", "aberto")),
                       ORDEM.index(a["severidade"])),
    )
    for i, a in enumerate(ordenados, start=1):
        sit = a.get("situacao", "aberto")
        corpo = f"<b>{esc(a['titulo'])}</b>"
        if a.get("correcao"):
            corpo += f"<br/>{esc(a['correcao'])}"
        if a.get("verificacao"):
            corpo += (f"<br/><font color='{HEX_SITUACAO['corrigido']}'>"
                      f"<b>Verificação:</b></font> {esc(a['verificacao'])}")
        linhas.append([
            # 6,4pt e não 7: "INFORMATIVA" tem onze caracteres e quebrava em
            # duas linhas nesta coluna, partindo a palavra ao meio.
            Paragraph(f"<b>{esc(a['id'])}</b><br/>"
                      f"<font size='6.4' color='{HEX[a['severidade']]}'>"
                      f"{ROTULO[a['severidade']]}</font>", e["corpo_peq"]),
            chip_situacao(sit, e),
            Paragraph(corpo, e["corpo_peq"]),
        ])
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), FUNDO_SUAVE))
    t = Table(linhas, colWidths=[2.1 * cm, 2.35 * cm, 12.05 * cm], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    hist.append(t)
    hist.append(PageBreak())
    return hist


def fortes_e_fracos(d: dict, e, num: int = 2) -> list:
    hist = [Paragraph(f"{num}. Pontos fortes e pontos fracos", e["h1"])]
    hist.append(Paragraph(f"{num}.1 O que está protegido (com evidência)", e["h2"]))
    linhas = [[Paragraph("<b>Controle verificado</b>", e["th"]),
               Paragraph("<b>Evidência no código</b>", e["th"])]]
    for p in d["pontos_fortes"]:
        linhas.append([
            Paragraph(f"<b>{esc(p['titulo'])}</b><br/>{esc(p['detalhe'])}", e["corpo_peq"]),
            Paragraph(quebrar_caminho(p["evidencia"]), e["mono_cell"]),
        ])
    t = Table(linhas, colWidths=[10.6 * cm, 5.9 * cm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CORES["forte"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, FUNDO_SUAVE]),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
    ]))
    hist.append(t)

    hist.append(Paragraph(f"{num}.2 Os riscos centrais", e["h2"]))
    for f in d["pontos_fracos"]:
        hist.append(Paragraph(f"• {esc(f)}", e["corpo"]))

    if d.get("nao_aplicaveis"):
        hist.append(Paragraph(
            f"{num}.3 Categorias que não se aplicam a esta stack", e["h2"]))
        itens = [
            Paragraph(f"• <b>{esc(na['categoria'])}</b> — {esc(na['motivo'])}", e["corpo"])
            for na in d["nao_aplicaveis"]
        ]
        # Os dois últimos viajam juntos. A seção termina logo abaixo desta
        # lista, então um item que caia sozinho no topo de uma página deixa a
        # folha inteira em branco -- foi o que aconteceu no relatório do
        # ControleRendaVariavel.
        hist += itens[:-2] + [KeepTogether(itens[-2:])] if len(itens) >= 2 else itens
    hist.append(PageBreak())
    return hist


def tabela_de_achados(d: dict, e, num: int = 3) -> list:
    hist = [Paragraph(f"{num}. Achados detalhados", e["h1"])]
    texto = (
        "Um bloco por categoria. Todo achado abaixo foi conferido no código real: "
        "arquivo, linha e trecho vêm da árvore auditada, não de inferência."
    )
    if tem_situacao(d["achados"]):
        texto += (
            " O diagnóstico está preservado exatamente como foi escrito na "
            "auditoria — inclusive nos achados já corrigidos, cujo trecho de "
            "código citado não existe mais na árvore. O que mudou vem depois "
            "dele, no bloco de correção."
        )
    hist.append(Paragraph(texto, e["corpo"]))

    por_categoria: dict[str, list[dict]] = {}
    for a in d["achados"]:
        por_categoria.setdefault(a["categoria"], []).append(a)

    for categoria in sorted(por_categoria):
        itens = sorted(por_categoria[categoria],
                       key=lambda a: ORDEM.index(a["severidade"]))
        # Sem isto, um título de categoria fica sozinho no pé da página e a
        # tabela dele começa na seguinte.
        hist.append(CondPageBreak(4.0 * cm))
        hist.append(Paragraph(esc(categoria), e["h2"]))
        linhas = [[
            Paragraph("<b>Severidade</b>", e["th"]),
            Paragraph("<b>Arquivo:linha</b>", e["th"]),
            Paragraph("<b>Descrição</b>", e["th"]),
        ]]
        estilo = [
            ("BACKGROUND", (0, 0), (-1, 0), TINTA),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
        for i, a in enumerate(itens, start=1):
            corpo = (
                f"<b>[{esc(a['id'])}] {esc(a['titulo'])}</b><br/>"
                f"{esc(a['descricao'])}<br/>"
                f"<font color='{HEX['critica']}'><b>Por que é explorável:</b></font> "
                f"{esc(a['porque'])}<br/>"
                f"<b>Impacto:</b> {esc(a['impacto'])}"
            )
            if a.get("condicoes"):
                corpo += f"<br/><b>Condição de explorabilidade:</b> {esc(a['condicoes'])}"
            if a.get("trecho"):
                corpo += (
                    f"<br/><br/><font face='Courier' size='7'>"
                    f"{esc(a['trecho']).replace(chr(10), '<br/>')}</font>"
                )
            if a.get("correcao"):
                sit = a.get("situacao", "aberto")
                corpo += (
                    f"<br/><br/><font color='{HEX_SITUACAO[sit]}'><b>"
                    f"{ROTULO_SITUACAO[sit]} — o que mudou:</b></font> "
                    f"{esc(a['correcao'])}"
                )
            if a.get("verificacao"):
                corpo += (f"<br/><b>Verificação:</b> {esc(a['verificacao'])}")
            # Os dois chips empilhados na mesma célula, e não uma quarta coluna:
            # a tabela já usa a largura útil inteira e uma coluna a mais
            # espremeria a descrição, que é onde está o conteúdo.
            celula_sev = [chip(a["severidade"], e)]
            if a.get("situacao"):
                celula_sev += [Spacer(1, 0.12 * cm),
                               chip_situacao(a["situacao"], e)]
            linhas.append([celula_sev,
                           Paragraph(quebrar_caminho(a["arquivo"]), e["mono_cell"]),
                           Paragraph(corpo, e["corpo_peq"])])
            if i % 2 == 0:
                estilo.append(("BACKGROUND", (1, i), (-1, i), FUNDO_SUAVE))
        # `splitInRow` permite que UMA linha se parta entre páginas. Sem isso, um
        # achado cujo bloco passe da altura útil não cabe em lugar nenhum senão
        # no topo de uma página nova, e a tabela inteira desce -- deixando o
        # título da categoria sozinho e três quartos de página em branco. É o
        # caso dos achados com bloco de correção, que são os mais compridos.
        t = Table(linhas, colWidths=[2.35 * cm, 4.0 * cm, 10.15 * cm], repeatRows=1,
                  splitInRow=1)
        t.setStyle(TableStyle(estilo))
        hist.append(t)
        hist.append(Spacer(1, 0.25 * cm))
    hist.append(PageBreak())
    return hist


def recomendacoes(d: dict, e, num: int = 4) -> list:
    hist = [Paragraph(f"{num}. Recomendações priorizadas", e["h1"])]
    linhas = [[Paragraph("<b>Prio</b>", e["th"]),
               Paragraph("<b>Ação</b>", e["th"]),
               Paragraph("<b>Achados</b>", e["th"])]]
    cor_prio = {"P1": CORES["critica"], "P2": CORES["alta"],
                "P3": CORES["media"], "P4": CORES["baixa"]}
    estilo = [
        ("BACKGROUND", (0, 0), (-1, 0), TINTA),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, LINHA),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
    ]
    for i, r in enumerate(d["recomendacoes"], start=1):
        cor = cor_prio.get(r["prioridade"], CORES["informativa"])
        marca = ""
        # A prioridade de uma recomendação já cumprida perde o sentido: o que
        # importa passa a ser que ela saiu da fila. A cor da célula muda junto,
        # senão a tabela continua parecendo uma lista de pendências.
        if r.get("situacao") == "concluida":
            cor = CORES_SITUACAO["corrigido"]
            marca = "<br/><font size='6'>FEITA</font>"
        elif r.get("situacao") == "parcial":
            cor = CORES_SITUACAO["parcial"]
            marca = "<br/><font size='6'>PARCIAL</font>"
        linhas.append([
            Paragraph(f"<font color='white'><b>{esc(r['prioridade'])}</b>{marca}</font>",
                      ParagraphStyle("p", parent=e["corpo_peq"], alignment=TA_CENTER)),
            Paragraph(esc(r["texto"]), e["corpo_peq"]),
            Paragraph(esc(", ".join(r.get("achados", []))) or "—", e["mono_cell"]),
        ])
        estilo.append(("BACKGROUND", (0, i), (0, i), cor))
        estilo.append(("VALIGN", (0, i), (0, i), "MIDDLE"))
    t = Table(linhas, colWidths=[1.5 * cm, 12.1 * cm, 2.9 * cm], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    hist.append(t)
    hist.append(PageBreak())
    return hist


def secao_issues(d: dict, e, num: int = 5) -> list:
    hist = [Paragraph(f"{num}. Issues para o GitHub", e["h1"])]
    texto = (
        "Cada bloco abaixo é o texto COMPLETO de uma issue, em Markdown, pronto "
        "para copiar e colar. Achados triviais do mesmo tema foram agrupados numa "
        "issue só, para não gerar ruído no rastreador."
    )
    if any(i.get("situacao") for i in d["issues"]):
        texto += (
            " As issues já resolvidas continuam aqui, marcadas — apagá-las "
            "deixaria o relatório sem o registro do que foi fechado —, mas não "
            "devem ser abertas no rastreador."
        )
    hist.append(Paragraph(texto, e["corpo"]))
    hist.append(Spacer(1, 0.2 * cm))

    for issue in d["issues"]:
        n = issue["n"]
        cabeca = ""
        if issue.get("situacao") == "resolvida":
            cabeca = "*** JÁ RESOLVIDA -- NÃO ABRIR NO RASTREADOR ***\n\n"
        elif issue.get("situacao") == "parcial":
            cabeca = "*** PARCIALMENTE RESOLVIDA -- ver o bloco de correção ***\n\n"
        texto = (
            f"--- ISSUE {n} ---\n"
            f"{cabeca}"
            f"Título: {issue['titulo']}\n"
            f"Labels: {issue['labels']}\n\n"
            f"{issue['corpo'].strip()}\n"
            f"--- FIM ISSUE {n} ---"
        )
        linhas_quebradas = []
        for linha in texto.split("\n"):
            if not linha:
                linhas_quebradas.append("")
                continue
            # 96 caracteres é o que cabe na largura útil em Courier 7,2pt sem
            # a linha estourar a moldura.
            linhas_quebradas.extend(
                textwrap.wrap(linha, width=96, subsequent_indent="    ",
                              break_long_words=True, break_on_hyphens=False,
                              replace_whitespace=False, drop_whitespace=False)
                or [""]
            )
        # UMA LINHA DE TEXTO POR LINHA DE TABELA, de propósito. Duas
        # alternativas mais óbvias não servem: uma célula única não quebra entre
        # páginas (e o corpo de uma issue passa de uma página com folga), e um
        # `Preformatted` solto quebra mas não desenha `backColor` nem borda --
        # `Preformatted.draw` não trata nenhum dos dois. Com uma linha por
        # linha, a tabela se divide entre páginas e cada pedaço mantém o fundo,
        # a moldura e a faixa colorida da severidade.
        cor = CORES.get(issue.get("severidade", "informativa"), CORES["informativa"])
        if issue.get("situacao") == "resolvida":
            cor = CORES_SITUACAO["corrigido"]
        linhas_tabela = [[Preformatted(linha or " ", e["issue"])]
                         for linha in linhas_quebradas]
        # Altura explícita na linha em branco: uma célula com só um espaço
        # mede zero e as seções do Markdown ficariam grudadas umas nas outras.
        alturas = [4.6 if not linha.strip() else None for linha in linhas_quebradas]
        moldura = Table(linhas_tabela, colWidths=[16.5 * cm], rowHeights=alturas)
        moldura.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F1F5F9")),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#94A3B8")),
            ("LINEBEFORE", (0, 0), (0, -1), 3, cor),
            ("LEFTPADDING", (0, 0), (-1, -1), 9),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        cabecalho = ParagraphStyle(f"issue_h{n}", parent=e["h3"], textColor=cor)
        prefixo = "✔ " if issue.get("situacao") == "resolvida" else ""
        hist.append(CondPageBreak(3.2 * cm))
        hist.append(Paragraph(f"{prefixo}Issue {n} — {esc(issue['titulo'])}", cabecalho))
        hist.append(moldura)
        hist.append(Spacer(1, 0.45 * cm))
    return hist


# ---------------------------------------------------------------------------
def argumentos(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dados", type=Path, default=DADOS,
                   help="JSON de entrada (padrão: achados.json ao lado do script)")
    p.add_argument("--saida", type=Path, default=SAIDA,
                   help="PDF de saída (padrão: relatorio-auditoria-seguranca.pdf)")
    a = p.parse_args(argv)
    # Caminho relativo se resolve contra o diretório do script, não contra o
    # diretório de onde se chamou: o comando do README funciona da raiz do
    # repositório e de dentro de `docs/security-audit/` sem mudar de forma.
    a.dados = a.dados if a.dados.is_absolute() else (AQUI / a.dados)
    a.saida = a.saida if a.saida.is_absolute() else (AQUI / a.saida)
    return a


def main(argv=None) -> None:
    args = argumentos(argv)
    d = json.loads(args.dados.read_text(encoding="utf-8"))
    e = montar_estilos()
    nome = d.get("rodape") or f"Auditoria de Segurança — {d['projeto']}"

    graficos = graficos_do_resumo(
        d["achados"], AQUI / f"_graficos-{args.saida.stem}.png")

    doc = BaseDocTemplate(
        str(args.saida), pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=(f"Relatório de Auditoria de Segurança — {d['projeto']}"
               + (f" ({d['subtitulo']})" if d.get("subtitulo") else "")),
        author="Auditoria de segurança", subject="Segurança de aplicação",
    )
    quadro = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                   id="corpo", leftPadding=0, rightPadding=0,
                   topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(id="padrao", frames=[quadro], onPage=fazer_decorador(nome))
    ])

    # A numeração das seções é contada, não escrita à mão: o relatório de
    # estado final insere "Estado da remediação" em segundo lugar e empurra as
    # demais.
    n = iter(range(1, 9))
    hist = []
    hist += capa(d, e)
    hist += resumo_executivo(d, e, graficos, next(n))
    if tem_situacao(d["achados"]):
        hist += estado_da_remediacao(d, e, next(n))
    hist += fortes_e_fracos(d, e, next(n))
    hist += tabela_de_achados(d, e, next(n))
    hist += recomendacoes(d, e, next(n))
    hist += secao_issues(d, e, next(n))
    doc.build(hist)

    # O PNG é insumo do build, não entregável: o PDF já os carrega embutidos.
    # Deixá-lo no diretório só criaria um arquivo que envelhece em silêncio
    # quando alguém editar o JSON e esquecer de regerar.
    if graficos is not None:
        graficos.unlink(missing_ok=True)

    print(f"PDF gerado: {args.saida}")


if __name__ == "__main__":
    main()
