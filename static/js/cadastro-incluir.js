/* Telas de cadastro: o formulario de inclusao so aparece depois de "Incluir".
   Os botoes moram no cabecalho da pagina (que o HTMX troca na navegacao), por isso
   o clique e delegado no documento. A "chave" distingue formularios da mesma pagina:
   vazia para o principal; "grupo" para o de grupos de categoria. */
(function () {
    'use strict';

    function _sufixo(chave) {
        return chave ? '-' + chave : '';
    }

    function _partes(chave) {
        var s = _sufixo(chave);
        function botao(atributo) {
            return document.querySelector('[' + atributo + '="' + chave + '"]');
        }
        return {
            card: document.getElementById('cadastro-novo' + s),
            incluir: botao('data-cadastro-incluir'),
            salvar: botao('data-cadastro-salvar'),
            cancelar: botao('data-cadastro-cancelar'),
            form: document.getElementById('cadastro-form' + s)
        };
    }

    function _abrir(chave, aberto) {
        var p = _partes(chave);
        if (!p.card || !p.incluir) return;
        p.card.hidden = !aberto;
        p.incluir.hidden = aberto;
        p.incluir.setAttribute('aria-expanded', aberto ? 'true' : 'false');
        if (p.salvar) p.salvar.hidden = !aberto;
        if (p.cancelar) p.cancelar.hidden = !aberto;
        if (aberto) {
            var campo = p.card.querySelector('input:not([type="hidden"]), select, textarea');
            if (campo) campo.focus();
        } else if (p.form) {
            p.form.reset();
        }
    }

    document.addEventListener('click', function (e) {
        var alvo = e.target.closest ? e.target.closest('[data-cadastro-incluir], [data-cadastro-cancelar]') : null;
        if (!alvo) return;
        var abrir = alvo.hasAttribute('data-cadastro-incluir');
        var chave = alvo.getAttribute(abrir ? 'data-cadastro-incluir' : 'data-cadastro-cancelar') || '';
        _abrir(chave, abrir);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        document.querySelectorAll('[data-cadastro-cancelar]:not([hidden])').forEach(function (b) {
            _abrir(b.getAttribute('data-cadastro-cancelar') || '', false);
        });
    });
})();
