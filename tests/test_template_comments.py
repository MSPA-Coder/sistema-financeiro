"""Comentários internos de template nunca podem vazar para o HTML."""

from __future__ import annotations

import re
from pathlib import Path

from django.template.loader import get_template, render_to_string
from django.test import RequestFactory

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
TEMPLATE_PATHS = sorted(TEMPLATES_DIR.rglob("*.html"))


class _AuthenticatedUser:
    is_authenticated = True
    is_staff = False
    username = "teste"
    user_type = "Teste"
    ui_theme = "light"
    table_scroll_rows = 15

    @staticmethod
    def has_perm(_permission: str) -> bool:
        return True


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


def test_authenticated_pages_do_not_render_global_flash_comment():
    request_factory = RequestFactory()

    for template_name, visible_text in (
        ("dashboard/index.html", "Dashboard"),
        ("reports/projections.html", "Projeções"),
    ):
        request = request_factory.get("/")
        request.user = _AuthenticatedUser()
        rendered_html = render_to_string(template_name, request=request)

        assert visible_text in rendered_html
        _assert_internal_comments_are_absent(rendered_html)
