(function () {
    'use strict';

    /* Os botões de abrir/fechar linhas de edição aparecem também em conteúdo
       trocado pela navegação AJAX. Delegar o clique no documento evita uma
       janela em que o novo botão ainda não recebeu o listener individual. O
       script desta aplicação é carregado antes dos scripts de cada página,
       então o handler já existe inclusive durante a primeira troca. */
    document.addEventListener('click', function (event) {
        var button = event.target.closest && event.target.closest('[data-toggle-edit]');
        if (!button) return;

        var row = document.getElementById(button.dataset.toggleEdit);
        if (!row) return;

        var force = button.dataset.toggleForce;
        var show = force === undefined ? row.classList.contains('is-collapsed') : force === 'true';
        row.classList.toggle('is-collapsed', !show);
    });

    /* ============================================================
       SCROLL RESTORE
       ============================================================ */
    var _scrollKey = 'app_main_scroll_restore';

    function rememberMainPanelScroll() {
        var panel = document.getElementById('appMain');
        if (!panel) return;
        try {
            sessionStorage.setItem(_scrollKey, JSON.stringify({
                pathname: window.location.pathname,
                scrollTop: panel.scrollTop
            }));
        } catch (_) {}
    }

    function restoreMainPanelScroll() {
        var panel = document.getElementById('appMain');
        if (!panel) return;
        var raw;
        try { raw = sessionStorage.getItem(_scrollKey); } catch (_) { return; }
        if (!raw) return;
        var saved;
        try { saved = JSON.parse(raw); } catch (_) { sessionStorage.removeItem(_scrollKey); return; }
        sessionStorage.removeItem(_scrollKey);
        if (!saved || saved.pathname !== window.location.pathname) return;
        var top = Number(saved.scrollTop);
        if (!Number.isFinite(top) || top < 0) return;
        requestAnimationFrame(function () { panel.scrollTop = top; });
    }

    /* submitWithMainPanelRestore - POST forms: flush e submit convencional.
       `submitter` é o botão que disparou o submit (quando houver) - sem ele,
       requestSubmit() não inclui o name/value do botão clicado, o que quebra
       formulários com mais de um botão de submit distinguido por name/value
       (ex.: ações em lote "Criar"/"Ignorar" no mesmo form). */
    function _submitSync(form, submitter) {
        if (!form) return;
        rememberMainPanelScroll();
        if (typeof form.requestSubmit === 'function') form.requestSubmit(submitter || undefined);
        else form.submit();
    }

    /* htmx (nesta versão) monta o corpo do POST com `new FormData(form)` sem
       submitter - o name/value do botão clicado nunca entra, mesmo passando
       o botão pra requestSubmit(). Espelha esse par num campo hidden real no
       form antes de submeter, pra funcionar tanto no caminho htmx quanto no
       submit nativo. Só faz sentido pra botões com name (ações em lote,
       formulários com mais de um botão de submit); botões sem name (a
       maioria) são no-op aqui. */
    function _mirrorSubmitterAsHiddenField(form, submitter) {
        if (!form || !submitter || !submitter.name) return;
        var proxy = form.querySelector('input[data-submitter-proxy="' + submitter.name + '"]');
        if (!proxy) {
            proxy = document.createElement('input');
            proxy.type = 'hidden';
            proxy.name = submitter.name;
            proxy.dataset.submitterProxy = submitter.name;
            form.appendChild(proxy);
        }
        proxy.value = submitter.value;
    }

    /* O espelhamento acima e um quirk geral do htmx e precisa acontecer
       incondicionalmente no listener de captura do document, antes do
       listener delegado de
       `sharedauth-ui.js` (que carrega com `defer`, depois deste script
       sincrono - dois listeners de captura no mesmo no disparam na ordem em
       que foram registrados). Isso importa porque aquele listener chama
       `stopPropagation()` e só submete depois de uma decisão assíncrona
       (a Promise da confirmação); quando o clique original já não se
       propaga, o rastreamento interno do htmx do "último botão clicado" não
       tem chance de rodar, e o par name/value do botão se perderia mesmo
       indo para `requestSubmit()`. Espelhar no clique, antes de qualquer
       `stopPropagation()`, resolve para os dois casos: botão com
       confirmação e botão sem. */
    document.addEventListener('click', function (ev) {
        var btn = ev.target.closest && ev.target.closest(
            'button[type="submit"][name], input[type="submit"][name]'
        );
        if (!btn) return;
        _mirrorSubmitterAsHiddenField(btn.form, btn);
    }, true);

    /* Um GET declarado com `{% nav_filtro %}` ja sabe se trocar sozinho: basta
       disparar o gatilho que ele escuta. POST continua sincrono. */
    window.submitWithMainPanelRestore = function (form) {
        if (!form) return;
        var method = (form.getAttribute('method') || 'get').toLowerCase();
        if (method === 'get' && form.getAttribute('hx-get')) {
            window.htmx.trigger(form, 'change');
        } else {
            _submitSync(form);
        }
    };

    /* ============================================================
       VALUES PRIVACY
       ============================================================ */
    var _valuesPrivacyKey = 'app_values_hidden';
    var _moneyMatchRe = /[+-]?\s*R\$\s*(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}/;
    var _moneyReplaceRe = /[+-]?\s*R\$\s*(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2}/g;
    var _sensitiveInputNameRe = /(^|_)(amount|balance|value|valor|saldo)(_|$)/;
    var _sensitiveElementSelector = [
        '[data-sensitive-value]',
        'td.amount', 'th.amount', '.value-col', '.card-value',
        '.health-metric-value'
    ].join(', ');

    function _readValuesPrivacyPreference() {
        try { return localStorage.getItem(_valuesPrivacyKey) === 'true'; } catch (_) { return false; }
    }

    function _writeValuesPrivacyPreference(hidden) {
        try { localStorage.setItem(_valuesPrivacyKey, hidden ? 'true' : 'false'); } catch (_) {}
    }

    function _maskMoneyText(text) {
        _moneyReplaceRe.lastIndex = 0;
        return String(text || '').replace(_moneyReplaceRe, function (match) {
            return match.replace(/R\$\s*.+$/, 'R$ ****');
        });
    }

    function _isTextNodeEligibleForPrivacy(node) {
        if (!node || !node.parentElement) return false;
        if (!_moneyMatchRe.test(node.nodeValue || '')) return false;
        return !node.parentElement.closest(
            '[data-privacy-toggle], [data-sensitive-value], script, style, textarea, input, select, option, svg, canvas, code, pre'
        );
    }

    function _wrapSensitiveTextNode(node) {
        if (!_isTextNodeEligibleForPrivacy(node)) return;

        var text = node.nodeValue || '';
        var fragment = document.createDocumentFragment();
        var cursor = 0;

        _moneyReplaceRe.lastIndex = 0;
        text.replace(_moneyReplaceRe, function (match, offset) {
            if (offset > cursor) {
                fragment.appendChild(document.createTextNode(text.slice(cursor, offset)));
            }

            var span = document.createElement('span');
            span.className = 'sensitive-value';
            span.dataset.sensitiveValue = 'true';
            span.textContent = match;
            fragment.appendChild(span);

            cursor = offset + match.length;
            return match;
        });

        if (cursor < text.length) {
            fragment.appendChild(document.createTextNode(text.slice(cursor)));
        }

        node.parentNode.replaceChild(fragment, node);
    }

    function _markSensitiveText(root) {
        var scope = root || document;
        var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
        var nodes = [];
        var node;
        while ((node = walker.nextNode())) {
            if (_isTextNodeEligibleForPrivacy(node)) nodes.push(node);
        }
        nodes.forEach(_wrapSensitiveTextNode);
    }

    function _markSensitiveInputs(root) {
        (root || document).querySelectorAll('input[name]').forEach(function (input) {
            var name = String(input.name || '').toLowerCase();
            if (!_sensitiveInputNameRe.test(name)) return;
            input.dataset.sensitiveInput = 'true';
        });
    }

    function _markSensitiveElements(root) {
        (root || document).querySelectorAll(_sensitiveElementSelector).forEach(function (el) {
            el.dataset.sensitiveValue = 'true';
        });
    }

    function _syncSensitiveOptions(root, hidden) {
        (root || document).querySelectorAll('option').forEach(function (option) {
            var original = option.dataset.sensitiveOriginalText || option.textContent || '';
            var explicitlySensitive = option.dataset.sensitiveValue === 'true';
            if (!explicitlySensitive && !_moneyMatchRe.test(original)) return;
            option.dataset.sensitiveOriginalText = original;
            option.textContent = hidden
                ? (explicitlySensitive ? '****' : _maskMoneyText(original))
                : original;
        });
    }

    function _syncSensitiveLabels(root, hidden) {
        (root || document).querySelectorAll('[data-sensitive-value="true"]').forEach(function (el) {
            if (hidden) {
                el.setAttribute('aria-label', 'Valor oculto');
                el.dataset.privacyAriaManaged = 'true';
            } else if (el.dataset.privacyAriaManaged === 'true') {
                el.removeAttribute('aria-label');
                delete el.dataset.privacyAriaManaged;
            }
        });
    }

    function _updatePrivacyToggle(toggle, hidden) {
        toggle.setAttribute('aria-pressed', hidden ? 'true' : 'false');
        toggle.setAttribute('aria-label', hidden ? 'Mostrar valores' : 'Ocultar valores');
        toggle.setAttribute('title', hidden ? 'Mostrar valores' : 'Ocultar valores');

        var visibleIcon = toggle.querySelector('[data-privacy-icon-visible]');
        var hiddenIcon = toggle.querySelector('[data-privacy-icon-hidden]');
        if (visibleIcon) visibleIcon.hidden = hidden;
        if (hiddenIcon) hiddenIcon.hidden = !hidden;
    }

    function _applyValuesPrivacy(hidden, root) {
        document.documentElement.setAttribute('data-values-hidden', hidden ? 'true' : 'false');
        if (document.body) document.body.setAttribute('data-values-hidden', hidden ? 'true' : 'false');

        var scope = root || document;
        _markSensitiveElements(scope);
        _markSensitiveText(scope);
        _markSensitiveInputs(scope);
        _syncSensitiveOptions(scope, hidden);
        _syncSensitiveLabels(scope, hidden);

        document.querySelectorAll('[data-privacy-toggle]').forEach(function (toggle) {
            _updatePrivacyToggle(toggle, hidden);
        });
    }

    function _initPrivacyToggles(root) {
        (root || document).querySelectorAll('[data-privacy-toggle]').forEach(function (toggle) {
            if (toggle._privacyBound) return;
            toggle._privacyBound = true;
            toggle.addEventListener('click', function () {
                var hidden = !_readValuesPrivacyPreference();
                _writeValuesPrivacyPreference(hidden);
                _applyValuesPrivacy(hidden, document);
            });
        });
    }

    /* ============================================================
       NAVEGAÇÃO POR FILTRO — feita pelo HTMX
       ============================================================

       Trocar uma tela por filtro troca duas regiões: `#appMain`, com o
       conteúdo, e `#appPageHeader`, com os próprios seletores. Isso era feito
       aqui, à mão: `fetch` da página inteira, recorte das duas regiões com
       `DOMParser`, `pushState`, reexecução de `<script>`, reinstalação de
       listeners, aborto de requisição concorrente e um temporizador de 15s.

       Hoje quem faz é o HTMX, declarado no template pela tag `{% nav_filtro %}`
       (`core/templatetags/navegacao.py`). O que sobrou aqui é só o que ele não
       tem como saber. */

    /* O servidor às vezes CORRIGE um filtro em silêncio: escolher um titular
       que não é o dono da conta selecionada faz `selected_context` anular a
       conta. O conteúdo e o cabeçalho já voltam coerentes na mesma resposta, e
       o endereço também -- `selected_context` anota o que descartou e
       `core/navegacao.py` devolve a barra já sem aquele parâmetro.

       Isto já foi ~40 linhas aqui, comparando o formulário devolvido com a
       barra e refazendo a busca quando divergiam. Além de custar uma segunda
       requisição, era uma corrida: no instante em que `htmx:afterSwap` do
       `#appMain` dispara, o cabeçalho ainda não foi trocado, então a comparação
       lia o formulário ANTIGO e concluía que estava tudo certo. Quem sabe o que
       foi descartado é o servidor, e é lá que a correção mora. */

    /* `htmx:afterSwap` chega DUAS vezes por troca: o evento e disparado no
       elemento e sobe ate o `document`, onde este listener esta. Sem a guarda
       abaixo, `_initContentArea` rodaria em dobro (inofensivo, mas desperdicio)
       e `app:contentLoaded` sairia em dobro (nao inofensivo: os tres
       consumidores reconstroem grafico e calendario, e reconstruir duas vezes
       pisca). `ev.detail.xhr` e a mesma instancia nas duas passagens e distinta
       entre requisicoes -- e a chave certa para deduplicar. */
    var _ultimaTroca = null;

    document.addEventListener('htmx:afterSwap', function (ev) {
        var alvo = (ev.detail && ev.detail.target) || ev.target;
        var xhr = ev.detail && ev.detail.xhr;
        if (xhr && xhr === _ultimaTroca) return;
        _ultimaTroca = xhr;

        if (alvo && alvo.nodeType === Node.ELEMENT_NODE) _initContentArea(alvo);
        _updateTableOverflow();

        var principal = document.getElementById('appMain');
        if (!principal || alvo !== principal) return;

        /* `app:contentLoaded` era emitido pela navegacao propria e tem tres
           consumidores (dashboard, planejamento anual, lancamentos), cada um
           reconstruindo o que so ele sabe -- graficos, calendario, atalhos de
           linha. Continua sendo emitido daqui, um por troca de `#appMain`,
           para que esses tres nao precisem cada um descobrir o HTMX sozinho. */
        document.dispatchEvent(new CustomEvent('app:contentLoaded', {
            detail: { root: principal, url: window.location.href }
        }));
    });

    /* sharedauth-ui.js só varre `[data-sa-avisos]` em dois gatilhos:
       `DOMContentLoaded` (carga cheia) e `htmx:afterSwap`. O bloco de
       mensagens chega fora de banda (`hx-swap-oob`, ver `core/htmx.py`), e
       troca fora de banda dispara `htmx:oobAfterSwap` -- evento distinto, que
       o componente não escuta. Sem a ponte abaixo, mensagem gerada numa
       requisição HTMX nunca vira toast: fica no DOM, escondida, esperando um
       `htmx:afterSwap` que não vem.
       Reemitir o evento que o componente já escuta é mais simples e mais
       seguro que duplicar `lerAvisosDoServidor` aqui - e não exige tocar no
       arquivo vendorizado. `ev.target` de um evento disparado direto no
       `document` é o próprio `document`, e `lerAvisosDoServidor(document)`
       varre a página inteira, o que cobre o bloco recém-inserido. */
    function _maskServerAvisos(root) {
        if (!_readValuesPrivacyPreference()) return;
        (root || document).querySelectorAll('[data-sa-avisos]').forEach(function (el) {
            var raw = el.getAttribute('data-sa-avisos');
            if (!raw) return;
            try {
                var avisos = JSON.parse(raw);
                avisos.forEach(function (aviso) {
                    if (aviso && typeof aviso.mensagem === 'string') {
                        aviso.mensagem = _maskMoneyText(aviso.mensagem);
                    }
                });
                el.setAttribute('data-sa-avisos', JSON.stringify(avisos));
            } catch (_) {}
        });
    }

    function _announceServerAvisos() {
        _maskServerAvisos(document);
        document.dispatchEvent(new CustomEvent('htmx:afterSwap'));
    }

    /* `#flashMessages` chega nas respostas htmx via `hx-swap-oob` (ver
       core/htmx.py), e trocas fora de banda disparam `htmx:oobAfterSwap`,
       não `htmx:afterSwap` - são eventos distintos, e o segundo (que
       sharedauth-ui.js escuta) só cobre o alvo PRINCIPAL do swap, nunca o
       bloco OOB. Sem esta ponte, mensagem gerada numa requisição htmx comum
       (ex.: as ações de conciliação) nunca vira toast. */
    document.addEventListener('htmx:oobAfterSwap', function (ev) {
        if (ev.target && ev.target.id === 'flashMessages') _announceServerAvisos();
        _applyValuesPrivacy(_readValuesPrivacyPreference(), ev.target || document);
    });

    document.addEventListener('htmx:afterSwap', function (ev) {
        var root = ev.detail && ev.detail.elt ? ev.detail.elt : (ev.target || document);
        _initPrivacyToggles(root);
        _applyValuesPrivacy(_readValuesPrivacyPreference(), root);
    });

    document.addEventListener('htmx:load', function (ev) {
        var root = ev.detail && ev.detail.elt ? ev.detail.elt : (ev.target || document);
        _initPrivacyToggles(root);
        _applyValuesPrivacy(_readValuesPrivacyPreference(), root);
    });

    function _observeDynamicPrivacyContent() {
        if (!window.MutationObserver || !document.body) return;
        var observer = new MutationObserver(function (records) {
            if (!_readValuesPrivacyPreference()) return;
            records.forEach(function (record) {
                record.addedNodes.forEach(function (node) {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        _applyValuesPrivacy(true, node);
                    }
                });
            });
        });
        observer.observe(document.body, { childList: true, subtree: true });
    }

    /* ============================================================
       SELECT-ALL CHECKBOX (data-select-all="<checkbox name>")
       ============================================================ */
    function _initSelectAllCheckboxes(root) {
        (root || document).querySelectorAll('[data-select-all]').forEach(function (master) {
            if (master._selectAllBound) return;
            master._selectAllBound = true;
            master.addEventListener('change', function () {
                var name = master.dataset.selectAll;
                document.querySelectorAll('input[type="checkbox"][name="' + name + '"]').forEach(function (cb) {
                    cb.checked = master.checked;
                });
            });
        });
    }

    function _initToggleButtons(root) {
        /* Mantido como ponto de extensão de _initContentArea. Os toggles são
           delegados acima para atender elementos inseridos após AJAX. */
        void root;
    }

    /* ============================================================
       FILTER ACTIVE INDICATOR
       ============================================================ */
    function _updateFilterSelects(root) {
        (root || document).querySelectorAll('.filter-header-row select, select[data-filter]').forEach(function (sel) {
            sel.classList.toggle('filter-active', sel.value !== '');
        });
    }

    /* ============================================================
       TABLE SCROLL WRAPPERS
       ============================================================ */
    function _initTableScrollWrappers(root) {
        var maxRows = Number((document.body && document.body.dataset.tableScrollRows) || 15);
        if (!Number.isFinite(maxRows) || maxRows < 1) return;

        (root || document).querySelectorAll(
            '.table-container table, .table-scroll table, .table-inspection-scroll table'
        ).forEach(function (table) {
            var tbody = table.querySelector('tbody');
            if (!tbody) return;

            var dataRows = Array.from(tbody.querySelectorAll('tr')).filter(function (r) {
                return !r.classList.contains('edit-row') && !r.querySelector('.empty-state');
            });
            if (dataRows.length <= maxRows) return;
            if (table.parentElement && table.parentElement.classList.contains('auto-table-scroll-wrapper')) return;

            /* A altura maxima e o resto da aparencia vivem em
               `.auto-table-scroll-wrapper`, no CSS. Aqui isto era quatro
               atributos inline, e a CSP (`style-src 'self'`) descartava os
               quatro em silencio -- a rolagem nunca recebeu o teto que este
               codigo pretendia dar. */
            var wrapper = document.createElement('div');
            wrapper.className = 'auto-table-scroll-wrapper';

            table.parentNode.insertBefore(wrapper, table);
            wrapper.appendChild(table);
        });
    }

    /* ============================================================
       HORIZONTAL OVERFLOW FADE
       ============================================================ */
    function _updateTableOverflow() {
        document.querySelectorAll('.table-scroll-wrap').forEach(function (wrap) {
            wrap.classList.toggle('has-overflow', wrap.scrollWidth > wrap.clientWidth + 2);
        });
    }

    /* ============================================================
       CONTENT AREA INIT - chamado na carga e apos AJAX swap
       ============================================================ */
    function _initContentArea(root) {
        _initToggleButtons(root);
        _initSelectAllCheckboxes(root);
        _updateFilterSelects(root);
        _initTableScrollWrappers(root);
        _initPrivacyToggles(root);
        _applyValuesPrivacy(_readValuesPrivacyPreference(), root || document);
    }

    /* ============================================================
       DOM CONTENT LOADED
       ============================================================ */
    document.addEventListener('DOMContentLoaded', function () {
        restoreMainPanelScroll();

        /* -- Sidebar navigation -- */
        function _setSidebarOpen(group, isOpen) {
            group.classList.toggle('is-open', isOpen);
            var toggle = group.querySelector(':scope > [data-sidebar-toggle]');
            if (toggle) toggle.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        }
        function _closeSiblings(group) {
            var parent = group.parentElement;
            if (!parent) return;
            Array.from(parent.children).forEach(function (sib) {
                if (sib !== group && sib.matches('[data-sidebar-group]')) _setSidebarOpen(sib, false);
            });
        }
        function _clearSidebarActive() {
            document.querySelectorAll('#appSidebar .active').forEach(function (el) {
                el.classList.remove('active');
            });
            document.querySelectorAll('#appSidebar [aria-current="page"]').forEach(function (el) {
                el.removeAttribute('aria-current');
            });
        }
        function _setSidebarGroupActive(group) {
            _clearSidebarActive();
            group.classList.add('active');
            var toggle = group.querySelector(':scope > [data-sidebar-toggle]');
            if (toggle) toggle.classList.add('active');
        }
        function _setSidebarLinkActive(link) {
            _clearSidebarActive();
            link.classList.add('active');
            link.setAttribute('aria-current', 'page');
            var parentGroup = link.closest('[data-sidebar-group]');
            while (parentGroup) {
                _setSidebarOpen(parentGroup, true);
                parentGroup = parentGroup.parentElement ? parentGroup.parentElement.closest('[data-sidebar-group]') : null;
            }
        }
        document.querySelectorAll('[data-sidebar-toggle]').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var group = btn.closest('[data-sidebar-group]');
                if (!group) return;
                var willOpen = !group.classList.contains('is-open');
                _closeSiblings(group);
                _setSidebarOpen(group, willOpen);
                if (willOpen) _setSidebarGroupActive(group);
                else _clearSidebarActive();
            });
        });
        document.querySelectorAll('#appSidebar a.sidebar-link').forEach(function (link) {
            link.addEventListener('click', function (e) {
                if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                _setSidebarLinkActive(link);
            });
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') {
                document.querySelectorAll('[data-sidebar-group]').forEach(function (g) {
                    _setSidebarOpen(g, false);
                });
            }
        });

        /* -- POST form: preserva scroll -- */
        document.addEventListener('submit', function (e) {
            var f = e.target;
            if (!f || f.tagName !== 'FORM') return;
            if (f.dataset.preserveMainScroll === 'false') return;
            if ((f.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
            rememberMainPanelScroll();
        }, true);

        /* -- Horizontal overflow -- */
        _updateTableOverflow();
        window.addEventListener('resize', _updateTableOverflow);

        /* -- Init da area de conteudo -- */
        _maskServerAvisos(document);
        _initContentArea(document);
        _observeDynamicPrivacyContent();
    });

})();
