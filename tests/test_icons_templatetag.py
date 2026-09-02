"""`icon(name, label)` escapa `label` sozinho (CB-06).

A categoria fica fechada no arquivo: mesmo um chamador que esqueça de
escapar o rótulo antes de passar não injeta HTML na página.
"""
from __future__ import annotations

from core.templatetags.icons import icon


def test_label_com_html_e_escapado() -> None:
    resultado = icon("save", label="<script>alert(1)</script>")

    assert "<script>" not in resultado
    assert "&lt;script&gt;" in resultado


def test_label_comum_continua_visivel() -> None:
    resultado = icon("save", label="Salvar")

    assert "<span>Salvar</span>" in resultado


def test_sem_label_nao_acrescenta_span() -> None:
    resultado = icon("save")

    assert "<span>" not in resultado


def test_nome_desconhecido_devolve_vazio() -> None:
    assert icon("nao-existe", label="qualquer") == ""
