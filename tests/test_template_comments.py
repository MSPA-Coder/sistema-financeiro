"""Comentários internos de template nunca podem vazar para o HTML."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.template.loader import get_template, render_to_string
from django.test import RequestFactory

from core.domain.identity import USER_TYPE_ADMINISTRATOR

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
TEMPLATE_PATHS = sorted(TEMPLATES_DIR.rglob("*.html"))


def _assert_internal_comments_are_absent(rendered_html: str) -> None:
    assert "Pre-autenticacao" not in rendered_html
    assert "Resultados de acao" not in rendered_html
    assert "{#" not in rendered_html
    assert "#}" not in rendered_html


def test_short_comment_syntax_is_never_used_across_lines():
    multiline_comments = []
    for path in TEMPLATE_PATHS:
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"{#", source):
            closing_index = source.find("#}", match.end())
            comment = source[match.start() : closing_index + 2]
            if closing_index == -1 or "\n" in comment:
                multiline_comments.append(path.relative_to(TEMPLATES_DIR).as_posix())

    assert multiline_comments == []


def test_all_html_templates_compile():
    for path in TEMPLATE_PATHS:
        get_template(path.relative_to(TEMPLATES_DIR).as_posix())


def test_login_does_not_render_internal_comments(client):
    response = client.get("/login")

    assert response.status_code == 200
    _assert_internal_comments_are_absent(response.content.decode())


@pytest.mark.django_db
def test_authenticated_pages_do_not_render_global_flash_comment():
    # Usuário de verdade: o menu lateral conta as contas de cada grupo do
    # filtro global, e essa contagem consulta o banco.
    user = get_user_model().objects.create_user(
        username="teste-comentarios", password="troca-esta-senha-no-primeiro-acesso",
        user_type=USER_TYPE_ADMINISTRATOR,
    )
    request_factory = RequestFactory()

    for template_name, visible_text in (
        ("dashboard/index.html", "Dashboard"),
        ("reports/projections.html", "Projeções"),
    ):
        request = request_factory.get("/")
        request.user = user
        rendered_html = render_to_string(template_name, request=request)

        assert visible_text in rendered_html
        _assert_internal_comments_are_absent(rendered_html)
