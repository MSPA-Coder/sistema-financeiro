/* Telas de cadastro: o formulario de inclusao so aparece depois de "Incluir".
   Os botoes moram no cabecalho da pagina (que o HTMX troca na navegacao), por isso
   o clique e delegado no documento. */
(function () {
    'use strict';

    function _partes() {
        return {
            card: document.getElementById('cadastro-novo'),
            incluir: document.querySelector('[data-cadastro-incluir]'),
            salvar: document.querySelector('[data-cadastro-salvar]'),
            cancelar: document.querySelector('[data-cadastro-cancelar]'),
            form: document.getElementById('cadastro-form')
        };
    }

    function _abrir(aberto) {
        var p = _partes();
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
        _abrir(alvo.hasAttribute('data-cadastro-incluir'));
    });

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape') return;
        var p = _partes();
        if (p.card && !p.card.hidden) _abrir(false);
    });
})();
