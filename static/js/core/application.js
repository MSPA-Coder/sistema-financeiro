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
        var show = force === undefined ? row.style.display !== 'table-row' : force === 'true';
        row.style.display = show ? 'table-row' : 'none';
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
        if (form.dataset.ajaxPostRedirect === 'true') {
            submitPostRedirectAjax(form);
            return;
        }
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

    window.submitWithMainPanelRestore = function (form) {
        if (!form) return;
        var method = (form.getAttribute('method') || 'get').toLowerCase();
        if (method === 'get') {
            submitFormAjax(form);
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
       AJAX NAVIGATION
       ============================================================ */
    var _ajaxInflight = false;
    var _ajaxController = null;
    var _ajaxRequestSeq = 0;
    var _ajaxActiveCanonicalUrl = null;
    var _ajaxActivePromise = null;
    var _ajaxTimeoutId = null;
    var _ajaxTimeoutMs = 15000;
    var _maxFilterCascadeDepth = 2;

    function _showLoadingBar() {
        var bar = document.getElementById('ajaxLoadingBar');
        var main = document.getElementById('appMain');
        if (bar) bar.classList.add('active');
        if (main) main.classList.add('content-loading');
    }

    function _hideLoadingBar() {
        var bar  = document.getElementById('ajaxLoadingBar');
        var main = document.getElementById('appMain');
        if (bar) bar.classList.remove('active');
        if (main) {
            main.classList.remove('content-loading');
            main.classList.remove('content-refreshed');
            /* force reflow para re-disparar a animacao */
            void main.offsetHeight;
            main.classList.add('content-refreshed');
            setTimeout(function () { main.classList.remove('content-refreshed'); }, 300);
        }
    }

    function _clearAjaxTimeout() {
        if (_ajaxTimeoutId !== null) {
            clearTimeout(_ajaxTimeoutId);
            _ajaxTimeoutId = null;
        }
    }

    function _resetAjaxState(seq) {
        if (seq !== undefined && seq !== _ajaxRequestSeq) return;
        _clearAjaxTimeout();
        _ajaxInflight = false;
        _ajaxController = null;
        _ajaxActiveCanonicalUrl = null;
        _ajaxActivePromise = null;
        _hideLoadingBar();
    }

    function _navigateWithoutAjax(url) {
        rememberMainPanelScroll();
        window.location.assign(url);
    }

    function _buildFormUrl(form) {
        var action = form.getAttribute('action') || window.location.pathname;
        var url = new URL(action, window.location.href);
        var params = new URLSearchParams();
        new FormData(form).forEach(function (v, k) {
            if (v !== '') params.append(k, v);
        });
        url.search = params.toString();
        return url.toString();
    }

    function _canonicalAjaxUrl(rawUrl) {
        try {
            var url = new URL(rawUrl, window.location.href);
            var pairs = [];
            url.searchParams.forEach(function (value, key) {
                if (value !== '') pairs.push([key, value]);
            });
            pairs.sort(function (a, b) {
                if (a[0] === b[0]) return a[1] < b[1] ? -1 : (a[1] > b[1] ? 1 : 0);
                return a[0] < b[0] ? -1 : 1;
            });
            var params = new URLSearchParams();
            pairs.forEach(function (pair) { params.append(pair[0], pair[1]); });
            var search = params.toString();
            return url.origin + url.pathname + (search ? '?' + search : '');
        } catch (_) {
            return String(rawUrl || '');
        }
    }

    function _sameAjaxUrl(left, right) {
        return _canonicalAjaxUrl(left) === _canonicalAjaxUrl(right);
    }

    /* Re-executa <script> tags no conteudo trocado (ex.: graficos inline) */
    function _execScriptsIn(root) {
        root.querySelectorAll('script').forEach(function (old) {
            var neo = document.createElement('script');
            Array.from(old.attributes).forEach(function (a) {
                neo.setAttribute(a.name, a.value);
            });
            neo.textContent = old.textContent;
            old.parentNode.replaceChild(neo, old);
        });
    }

    function _initPageHeader(root) {
        _initAutoSubmitForms(root);
        _initFilterFormSelects(root);
        _initClearFilterButtons(root);
        _updateFilterSelects(root);
        _initPrivacyToggles(root);
        _applyValuesPrivacy(_readValuesPrivacyPreference(), root);
    }

    /* -- Sincroniza o cabecalho apos AJAX -----------------------------------
       O #appPageHeader fica fora do #appMain. Quando filtros como titular
       alteram opcoes dependentes de banco/conta, o header antigo precisa ser
       substituido pelo header renderizado pelo servidor e re-inicializado.
       Se o servidor saneou parametros invalidos, uma cascata curta re-busca
       o conteudo com a URL canonica do formulario ja corrigido.
    ------------------------------------------------------------------------ */
    function _syncPageHeader(parsedDoc, currentUrl) {
        var newHeader = parsedDoc.getElementById('appPageHeader');
        var curHeader = document.getElementById('appPageHeader');
        if (!newHeader || !curHeader) return false;

        curHeader.innerHTML = newHeader.innerHTML;
        _initPageHeader(curHeader);

        var form = curHeader.querySelector('form[data-auto-submit="true"]');
        if (!form) return false;

        var correctedUrl = _buildFormUrl(form);
        return !_sameAjaxUrl(correctedUrl, currentUrl);
    }

    function _syncSidebarLinks(parsedDoc) {
        var newSidebar = parsedDoc.getElementById('appSidebar');
        var curSidebar = document.getElementById('appSidebar');
        if (!newSidebar || !curSidebar) return;

        var newLinks = newSidebar.querySelectorAll('a.sidebar-link');
        var curLinks = curSidebar.querySelectorAll('a.sidebar-link');
        curLinks.forEach(function (link, index) {
            var newLink = newLinks[index];
            if (!newLink) return;
            link.setAttribute('href', newLink.getAttribute('href') || '#');
        });
    }

    /* sharedauth-ui.js só varre `[data-sa-avisos]` em dois gatilhos:
       `DOMContentLoaded` (carga cheia) e `htmx:afterSwap` (troca via htmx de
       verdade). A navegação AJAX própria deste app (`fetchAndSwapMain` /
       `submitPostRedirectAjax`) não é nenhum dos dois - ela clona o bloco de
       mensagens na hora, sem disparar evento algum. Sem isto, uma mensagem
       gerada por essas rotas nunca vira toast: fica no DOM, escondida,
       esperando um `htmx:afterSwap` que não vai vir.
       Reemitir o mesmo evento que o componente já escuta é mais simples e
       mais seguro que duplicar `lerAvisosDoServidor` aqui - e não exige tocar
       no arquivo vendorizado. `ev.target` de um evento disparado direto no
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

    function _syncFlashMessages(parsedDoc) {
        var appContent = document.querySelector('.app-content');
        if (!appContent) return;
        var currentFlash = document.getElementById('flashMessages');
        if (currentFlash) currentFlash.remove();
        var newFlash = parsedDoc.getElementById('flashMessages');
        if (!newFlash) return;
        var main = document.getElementById('appMain');
        appContent.insertBefore(newFlash.cloneNode(true), main || null);
        _announceServerAvisos();
    }

    function _scheduleFilterCascade(form, currentUrl, cascadeDepth) {
        if (!form) return;

        var nextDepth = (Number(cascadeDepth) || 0) + 1;
        if (nextDepth > _maxFilterCascadeDepth) {
            console.warn('[AJAX nav] cascata de filtros interrompida');
            return;
        }

        var nextUrl = _buildFormUrl(form);
        if (_sameAjaxUrl(nextUrl, currentUrl) || _sameAjaxUrl(nextUrl, window.location.href)) return;

        setTimeout(function () {
            submitFormAjax(form, { replaceHistory: true, cascadeDepth: nextDepth });
        }, 0);
    }

    function fetchAndSwapMain(url, options) {
        options = options || {};
        var canonicalUrl = _canonicalAjaxUrl(url);

        if (!options.force && !_ajaxInflight && canonicalUrl === _canonicalAjaxUrl(window.location.href)) {
            return Promise.resolve(true);
        }

        if (_ajaxInflight && _ajaxActiveCanonicalUrl === canonicalUrl) {
            return _ajaxActivePromise || Promise.resolve(true);
        }

        var seq = _ajaxRequestSeq + 1;
        if (_ajaxController && typeof _ajaxController.abort === 'function') {
            _ajaxController.abort();
        }
        _clearAjaxTimeout();

        _ajaxRequestSeq = seq;
        _ajaxInflight = true;
        _ajaxActiveCanonicalUrl = canonicalUrl;

        var controller = null;
        if ('AbortController' in window) {
            controller = new AbortController();
            _ajaxController = controller;
        } else {
            _ajaxController = null;
        }

        _showLoadingBar();
        _ajaxTimeoutId = setTimeout(function () {
            if (seq !== _ajaxRequestSeq) return;
            console.warn('[AJAX nav] tempo limite excedido; interface liberada');
            _ajaxRequestSeq += 1;
            if (_ajaxController && typeof _ajaxController.abort === 'function') {
                _ajaxController.abort();
            }
            _resetAjaxState();
        }, _ajaxTimeoutMs);

        var fetchOptions = {
            headers: { 'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest' }
        };
        if (controller) fetchOptions.signal = controller.signal;

        var requestPromise = fetch(url, fetchOptions).then(function (resp) {
            if (seq !== _ajaxRequestSeq) return true;
            var responseUrl = resp.url || url;
            if (!resp.ok) return false;
            return resp.text().then(function (html) {
                if (seq !== _ajaxRequestSeq) return true;
                var historyUrl = responseUrl || url;
                var doc = (new DOMParser()).parseFromString(html, 'text/html');
                var newMain = doc.getElementById('appMain');
                var curMain = document.getElementById('appMain');
                if (!newMain || !curMain) return false;

                /* Preserva posicao de scroll - nao joga o usuario para o topo */
                var savedScroll = curMain.scrollTop;

                curMain.innerHTML = newMain.innerHTML;
                if (!_sameAjaxUrl(historyUrl, window.location.href)) {
                    if (options.replaceHistory) history.replaceState({ ajaxNav: true }, '', historyUrl);
                    else history.pushState({ ajaxNav: true }, '', historyUrl);
                } else if (options.replaceHistory) {
                    history.replaceState({ ajaxNav: true }, '', historyUrl);
                }

                /* Sincroniza o cabecalho que fica fora do #appMain. */
                var cascadeNeeded = _syncPageHeader(doc, historyUrl);
                _syncSidebarLinks(doc);
                _syncFlashMessages(doc);

                /* re-init bindings e scripts no conteudo novo */
                _initContentArea(curMain);
                _execScriptsIn(curMain);

                document.dispatchEvent(new CustomEvent('app:contentLoaded', {
                    detail: { root: curMain, url: historyUrl }
                }));

                /* Restaura scroll apos o repaint */
                requestAnimationFrame(function () {
                    curMain.scrollTop = savedScroll;
                });

                /* Cascata: um select ficou invalido (ex: conta de outro titular).
                   Re-busca com os params corrigidos para que o conteudo e os
                   selects fiquem em sincronia. */
                if (cascadeNeeded) {
                    var cascadeForm = document.getElementById('contextForm') ||
                        document.querySelector('#appPageHeader form[data-auto-submit="true"]');
                    _scheduleFilterCascade(cascadeForm, historyUrl, options.cascadeDepth);
                }

                return true;
            });
        }).catch(function (e) {
            if (e && e.name === 'AbortError') return true;
            if (seq !== _ajaxRequestSeq) return true;
            console.warn('[AJAX nav]', e);
            return false;
        }).finally(function () {
            if (seq !== _ajaxRequestSeq) return;
            _resetAjaxState(seq);
        });

        _ajaxActivePromise = requestPromise;
        return requestPromise;
    }

    function submitFormAjax(form, options) {
        if (!form) return;
        var method = (form.getAttribute('method') || 'get').toLowerCase();
        if (method !== 'get') {
            _submitSync(form);
            return;
        }
        var url = _buildFormUrl(form);
        fetchAndSwapMain(url, options).then(function (ok) {
            if (!ok) _navigateWithoutAjax(url);
        });
    }

    function submitPostRedirectAjax(form) {
        if (!form) return;
        rememberMainPanelScroll();
        _showLoadingBar();

        var panel = document.getElementById('appMain');
        var savedScroll = panel ? panel.scrollTop : 0;
        var action = form.getAttribute('action') || window.location.href;
        var url = new URL(action, window.location.href).toString();

        fetch(url, {
            method: (form.getAttribute('method') || 'post').toUpperCase(),
            body: new FormData(form),
            headers: { 'Accept': 'text/html', 'X-Requested-With': 'XMLHttpRequest' },
            redirect: 'follow'
        }).then(function (resp) {
            if (!resp.ok) return false;
            var responseUrl = resp.url || url;
            return resp.text().then(function (html) {
                var doc = (new DOMParser()).parseFromString(html, 'text/html');
                var newMain = doc.getElementById('appMain');
                var curMain = document.getElementById('appMain');
                if (!newMain || !curMain) return false;

                curMain.innerHTML = newMain.innerHTML;
                if (!_sameAjaxUrl(responseUrl, window.location.href)) {
                    history.pushState({ ajaxNav: true }, '', responseUrl);
                }

                _syncPageHeader(doc, responseUrl);
                _syncSidebarLinks(doc);
                _syncFlashMessages(doc);
                _initContentArea(curMain);
                _execScriptsIn(curMain);

                document.dispatchEvent(new CustomEvent('app:contentLoaded', {
                    detail: { root: curMain, url: responseUrl }
                }));

                requestAnimationFrame(function () {
                    curMain.scrollTop = savedScroll;
                });
                return true;
            });
        }).catch(function (e) {
            console.warn('[AJAX post]', e);
            return false;
        }).then(function (ok) {
            if (!ok) {
                form.dataset.ajaxPostRedirect = 'false';
                _submitSync(form);
                form.dataset.ajaxPostRedirect = 'true';
            }
        }).finally(function () {
            _hideLoadingBar();
        });
    }

    window.fetchAndSwapMain = fetchAndSwapMain;
    window.submitFormAjax   = submitFormAjax;

    /* Links que redefinem filtros devem trocar cabeçalho e conteúdo juntos.
       Links de menu continuam convencionais: assim sempre abrem os valores
       padrão da tela de destino, enquanto links de contexto conservam os
       parâmetros explícitos no próprio href. */
    document.addEventListener('click', function (event) {
        var link = event.target.closest && event.target.closest('a[data-ajax-nav]');
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey ||
            event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        fetchAndSwapMain(link.href).then(function (ok) {
            if (!ok) _navigateWithoutAjax(link.href);
        });
    });

    /* Popstate: back/forward recarrega normalmente */
    window.addEventListener('popstate', function () {
        window.location.reload();
    });

    /* ============================================================
       AUTO-SUBMIT FORMS
       ============================================================ */
    function _initAutoSubmitForms(root) {
        root.querySelectorAll('form[data-auto-submit="true"]').forEach(function (form) {
            form.querySelectorAll('select, input[type="date"], input[type="month"], input[type="checkbox"], input[type="radio"]').forEach(function (field) {
                if (field.type === 'hidden' || field.dataset.autoSubmitIgnore === 'true') return;
                if (field._autoSubmitBound) return;
                field._autoSubmitBound = true;
                field.addEventListener('change', function () {
                    submitFormAjax(form);
                });
            });
        });
    }

    /* ============================================================
       FILTER-FORM SELECTS (data-filter-form / data-filter-param)
       ============================================================ */
    function _initFilterFormSelects(root) {
        root.querySelectorAll('select[data-filter-form]').forEach(function (sel) {
            if (sel._filterBound) return;
            sel._filterBound = true;
            sel.addEventListener('change', function () {
                var form = document.getElementById(sel.dataset.filterForm);
                if (!form) return;
                var param = sel.dataset.filterParam;
                if (!param) return;
                var hidden = form.querySelector('input[name="' + param + '"]');
                if (hidden) hidden.remove();
                if (sel.value !== '') {
                    var inp = document.createElement('input');
                    inp.type = 'hidden';
                    inp.name  = param;
                    inp.value = sel.value;
                    form.appendChild(inp);
                }
                submitFormAjax(form);
            });
        });
    }

    /* ============================================================
       CLEAR-FILTERS BUTTONS (data-clear-filters / data-clear-params)
       ============================================================ */
    function _initClearFilterButtons(root) {
        root.querySelectorAll('[data-clear-filters]').forEach(function (btn) {
            if (btn._clearBound) return;
            btn._clearBound = true;
            btn.addEventListener('click', function () {
                var form = document.getElementById(btn.dataset.clearFilters);
                if (!form) return;
                var params = (btn.dataset.clearParams || '').split(',')
                    .map(function (s) { return s.trim(); }).filter(Boolean);
                params.forEach(function (p) {
                    var old = form.querySelector('input[name="' + p + '"]');
                    if (old) old.remove();
                });
                submitFormAjax(form);
            });
        });
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

            var thead     = table.querySelector('thead');
            var firstRow  = dataRows[0];
            var hdrH      = thead ? thead.getBoundingClientRect().height : 0;
            var rowH      = firstRow ? firstRow.getBoundingClientRect().height : 36;
            var maxH      = Math.ceil(hdrH + rowH * maxRows);

            var wrapper = document.createElement('div');
            wrapper.className = 'auto-table-scroll-wrapper';
            wrapper.style.maxHeight  = maxH + 'px';
            wrapper.style.overflowY  = 'auto';
            wrapper.style.overflowX  = 'auto';
            wrapper.style.width      = '100%';

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
        _initAutoSubmitForms(root);
        _initFilterFormSelects(root);
        _initClearFilterButtons(root);
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
