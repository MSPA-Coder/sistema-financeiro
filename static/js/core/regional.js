/* Formato regional (Brasil ou EUA) dos campos de data, mês e valor.

   Só UX. O navegador mostra <input type="date|month|number"> no idioma DELE,
   não no do usuário do sistema; por isso cada um desses campos ganha um campo
   de texto que mostra e aceita o formato escolhido (`<html data-regional>`).
   O campo original continua no formulário, escondido, com o mesmo `name`, e
   guarda o valor de sempre (AAAA-MM-DD, AAAA-MM, 1234.56): o servidor recebe
   exatamente o que recebia antes.

   O original também segue respondendo ao resto do código: `.value`,
   `.required` e `.disabled` são repassados ao campo visível, e ele dispara
   `input`/`change` quando o usuário edita. */
(function () {
    'use strict';

    var US = document.documentElement.getAttribute('data-regional') === 'us';
    var LOCALE = US ? 'en-US' : 'pt-BR';
    var DECIMAL = US ? '.' : ',';

    var MSG = {
        date: US ? 'Informe uma data válida (mm/dd/aaaa).' : 'Informe uma data válida (dd/mm/aaaa).',
        month: 'Informe um mês válido (mm/aaaa).',
        number: 'Informe um valor numérico válido.',
        required: 'Preencha este campo.',
        requiredDate: 'Informe uma data.',
        min: 'Informe um valor igual ou maior que o mínimo permitido.',
        max: 'Informe um valor igual ou menor que o máximo permitido.',
        step: 'Informe um valor válido para este campo.'
    };

    function pad(n, size) {
        var s = String(n);
        while (s.length < size) s = '0' + s;
        return s;
    }

    function validDay(y, m, d) {
        if (m < 1 || m > 12 || d < 1 || y < 1000) return false;
        var dt = new Date(Date.UTC(y, m - 1, d));
        return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
    }

    /* ---------- data ---------- */

    function isoToDisplayDate(iso) {
        var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
        if (!m) return '';
        return US ? m[2] + '/' + m[3] + '/' + m[1] : m[3] + '/' + m[2] + '/' + m[1];
    }

    /* '' = vazio; null = inválido; senão AAAA-MM-DD. */
    function displayToIsoDate(text) {
        var raw = String(text || '').trim();
        if (!raw) return '';
        var parts = raw.split(/[\/.\-\s]+/);
        if (parts.length === 1 && /^\d{8}$/.test(raw)) {
            parts = US ? [raw.slice(0, 2), raw.slice(2, 4), raw.slice(4)] : [raw.slice(0, 2), raw.slice(2, 4), raw.slice(4)];
        }
        if (parts.length !== 3 || !parts.every(function (p) { return /^\d+$/.test(p); })) return null;
        if (parts[2].length !== 4) return null;
        var day = parseInt(US ? parts[1] : parts[0], 10);
        var month = parseInt(US ? parts[0] : parts[1], 10);
        var year = parseInt(parts[2], 10);
        if (!validDay(year, month, day)) return null;
        return pad(year, 4) + '-' + pad(month, 2) + '-' + pad(day, 2);
    }

    /* ---------- mês ---------- */

    function isoToDisplayMonth(iso) {
        var m = /^(\d{4})-(\d{2})/.exec(iso || '');
        return m ? m[2] + '/' + m[1] : '';
    }

    function displayToIsoMonth(text) {
        var raw = String(text || '').trim();
        if (!raw) return '';
        var parts = raw.split(/[\/.\-\s]+/);
        if (parts.length === 1 && /^\d{6}$/.test(raw)) parts = [raw.slice(0, 2), raw.slice(2)];
        if (parts.length !== 2 || !parts.every(function (p) { return /^\d+$/.test(p); })) return null;
        var month = parseInt(parts[0], 10);
        var year = parseInt(parts[1], 10);
        if (parts[1].length !== 4 || month < 1 || month > 12 || year < 1000) return null;
        return pad(year, 4) + '-' + pad(month, 2);
    }

    /* ---------- número ---------- */

    /* Valor canônico ('1234.56') -> texto no formato do usuário. */
    function canonToDisplayNumber(canon) {
        var s = String(canon == null ? '' : canon).trim();
        if (!s) return '';
        return US ? s : s.replace('.', ',');
    }

    /* Texto -> valor canônico. '' = vazio; null = inválido.
       Aceita os dois estilos de separador: o do formato escolhido manda; o
       "do outro lado" só vale como decimal quando sobra 1 ou 2 dígitos depois
       dele (10.50 digitado no Brasil é 10,50, não 1050), e como milhar quando
       sobram exatamente 3 (1.234). */
    function displayToCanonNumber(text) {
        var raw = String(text || '').replace(/\s|R\$|US\$|\$/g, '');
        if (!raw) return '';
        var sign = '';
        if (raw.charAt(0) === '-' || raw.charAt(0) === '+') {
            sign = raw.charAt(0) === '-' ? '-' : '';
            raw = raw.slice(1);
        }
        if (!/^[\d.,]+$/.test(raw) || !/\d/.test(raw)) return null;

        var own = DECIMAL;
        var other = US ? ',' : '.';
        var ownCount = raw.split(own).length - 1;
        var otherCount = raw.split(other).length - 1;
        var integer, fraction = '';

        if (ownCount > 1) return null;
        if (ownCount === 1) {
            var i = raw.indexOf(own);
            integer = raw.slice(0, i);
            fraction = raw.slice(i + 1);
            if (fraction.indexOf(other) !== -1) return null;
            // milhar do outro lado só antes do decimal: 1.234,56
            if (otherCount && !/^\d{1,3}(?:[.,]\d{3})*$/.test(integer.replace(/\s/g, ''))) return null;
            integer = integer.split(other).join('');
        } else if (otherCount === 1) {
            var j = raw.indexOf(other);
            var depois = raw.slice(j + 1);
            if (depois.length === 3 && j > 0) {
                integer = raw.split(other).join('');
            } else if (depois.length >= 1 && depois.length <= 2) {
                integer = raw.slice(0, j);
                fraction = depois;
            } else {
                return null;
            }
        } else if (otherCount > 1) {
            if (!/^\d{1,3}(?:[.,]\d{3})+$/.test(raw)) return null;
            integer = raw.split(other).join('');
        } else {
            integer = raw;
        }
        if (!/^\d*$/.test(integer) || !/^\d*$/.test(fraction)) return null;
        if (!integer) integer = '0';
        integer = integer.replace(/^0+(?=\d)/, '');
        return sign + integer + (fraction ? '.' + fraction : '');
    }

    function decimalPlaces(canon) {
        var i = canon.indexOf('.');
        return i === -1 ? 0 : canon.length - i - 1;
    }

    function stepDecimals(step) {
        if (!step || step === 'any') return null;
        var i = String(step).indexOf('.');
        return i === -1 ? 0 : String(step).length - i - 1;
    }

    /* ---------- máscara de digitação ---------- */

    function maskDigits(text, groups) {
        var digits = String(text).replace(/\D/g, '');
        var out = [];
        var pos = 0;
        for (var g = 0; g < groups.length && pos < digits.length; g++) {
            out.push(digits.substr(pos, groups[g]));
            pos += groups[g];
        }
        return out.join('/');
    }

    /* ---------- o componente ---------- */

    var valueDescriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    var COPIED = ['class', 'placeholder', 'title', 'aria-label', 'aria-describedby', 'autofocus',
        'disabled', 'readonly', 'data-ui', 'style', 'tabindex', 'list'];
    var CALENDAR_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/></svg>';

    function kindOf(input) {
        if (input.type === 'date') return 'date';
        if (input.type === 'month') return 'month';
        var step = input.getAttribute('step');
        if (input.type === 'number' && step && (step === 'any' || step.indexOf('.') !== -1)) return 'number';
        if (input.getAttribute('data-regional-kind') === 'decimal') return 'number';
        return null;
    }

    function enhance(input) {
        if (input.getAttribute('data-regional-done')) return;
        var kind = kindOf(input);
        if (!kind) return;
        input.setAttribute('data-regional-done', '1');

        var min = input.getAttribute('min');
        var max = input.getAttribute('max');
        var step = input.getAttribute('step');
        var required = input.required;
        var original = input.type;

        var visible = document.createElement('input');
        visible.type = 'text';
        visible.setAttribute('data-regional-view', kind);
        visible.autocomplete = 'off';
        visible.inputMode = kind === 'number' ? 'decimal' : 'numeric';
        if (kind === 'date') visible.maxLength = 10;
        if (kind === 'month') visible.maxLength = 7;
        if (kind === 'date') visible.placeholder = input.getAttribute('placeholder') || (US ? 'mm/dd/aaaa' : 'dd/mm/aaaa');
        if (kind === 'month') visible.placeholder = input.getAttribute('placeholder') || 'mm/aaaa';
        COPIED.forEach(function (attr) {
            if (input.hasAttribute(attr)) visible.setAttribute(attr, input.getAttribute(attr));
        });
        if (input.id) {
            visible.id = input.id;
            input.removeAttribute('id');
        }
        visible.required = required;
        if (input.getAttribute('form')) visible.setAttribute('form', input.getAttribute('form'));

        // O original vira só portador do valor: nada de restrição nele, porque
        // um campo escondido que reprova na validação trava o envio sem aviso.
        input.removeAttribute('min');
        input.removeAttribute('max');
        input.removeAttribute('step');
        input.required = false;
        input.setAttribute('data-regional-original-type', original);
        input.hidden = true;
        input.tabIndex = -1;
        input.style.display = 'none';

        var wrapper = document.createElement('span');
        wrapper.className = 'regional-field';
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(visible);
        wrapper.appendChild(input);

        var carrier = function () { return valueDescriptor.get.call(input); };
        var setCarrier = function (v) { valueDescriptor.set.call(input, v); };

        function fromText(text) {
            if (kind === 'date') return displayToIsoDate(text);
            if (kind === 'month') return displayToIsoMonth(text);
            return displayToCanonNumber(text);
        }

        function toText(canon) {
            if (kind === 'date') return isoToDisplayDate(canon);
            if (kind === 'month') return isoToDisplayMonth(canon);
            return canonToDisplayNumber(canon);
        }

        function problem(canon) {
            if (canon === null) return kind === 'number' ? MSG.number : (kind === 'date' ? MSG.date : MSG.month);
            if (canon === '') return visible.required ? (kind === 'number' ? MSG.required : MSG.requiredDate) : '';
            if (kind === 'number') {
                var n = parseFloat(canon);
                var casas = stepDecimals(step);
                if (casas !== null && decimalPlaces(canon) > casas) return MSG.step;
                if (min !== null && n < parseFloat(min)) return MSG.min;
                if (max !== null && n > parseFloat(max)) return MSG.max;
                return '';
            }
            if (min && canon < min.slice(0, canon.length)) {
                return 'Informe ' + (kind === 'date' ? 'uma data' : 'um mês') + ' a partir de ' + toText(min) + '.';
            }
            if (max && canon > max.slice(0, canon.length)) {
                return 'Informe ' + (kind === 'date' ? 'uma data' : 'um mês') + ' até ' + toText(max) + '.';
            }
            return '';
        }

        function validate(canon) {
            visible.setCustomValidity(problem(canon));
        }

        function syncFromCarrier() {
            visible.value = toText(carrier());
            validate(carrier() === '' ? '' : fromText(visible.value));
        }

        function fire(name) {
            input.dispatchEvent(new Event(name, { bubbles: true }));
        }

        visible.addEventListener('input', function (event) {
            var text = visible.value;
            var inserting = !event.inputType || event.inputType.indexOf('insert') === 0;
            if (inserting && kind !== 'number' && /^[\d\/.\-\s]*$/.test(text)) {
                var masked = kind === 'date'
                    ? maskDigits(text, [2, 2, 4])
                    : maskDigits(text, [2, 4]);
                if (/\d$/.test(text) || text === '') visible.value = masked;
            }
            var canon = fromText(visible.value);
            validate(canon);
            setCarrier(canon === null ? '' : canon);
            fire('input');
        });

        visible.addEventListener('change', function () {
            var canon = fromText(visible.value);
            validate(canon);
            setCarrier(canon === null ? '' : canon);
            fire('change');
        });

        if (kind === 'number') {
            visible.addEventListener('blur', function () {
                var canon = fromText(visible.value);
                if (canon) visible.value = canonToDisplayNumber(canon);
            });
        }

        // Eventos do campo visível não vazam: quem escuta mudança escuta o original.
        ['input', 'change'].forEach(function (name) {
            wrapper.addEventListener(name, function (event) {
                if (event.target === visible) event.stopPropagation();
            });
        });

        if (kind !== 'number') {
            var proxy = document.createElement('input');
            proxy.type = original;
            proxy.className = 'regional-picker-proxy';
            proxy.tabIndex = -1;
            proxy.setAttribute('aria-hidden', 'true');
            if (min) proxy.min = min;
            if (max) proxy.max = max;
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'regional-picker-button';
            button.setAttribute('aria-label', kind === 'date' ? 'Abrir calendário' : 'Escolher mês');
            button.innerHTML = CALENDAR_ICON;
            button.addEventListener('click', function () {
                valueDescriptor.set.call(proxy, carrier());
                if (typeof proxy.showPicker === 'function') {
                    try { proxy.showPicker(); } catch (e) { proxy.focus(); }
                } else {
                    proxy.focus();
                }
            });
            proxy.addEventListener('change', function () {
                var iso = valueDescriptor.get.call(proxy);
                if (!iso) return;
                visible.value = toText(iso);
                validate(iso);
                setCarrier(iso);
                fire('input');
                fire('change');
            });
            ['input', 'change'].forEach(function (name) {
                proxy.addEventListener(name, function (event) { event.stopPropagation(); });
            });
            wrapper.appendChild(button);
            wrapper.appendChild(proxy);
        }

        // Código externo mexe no original (`.value = ...`, `.required = ...`):
        // o que for mostrado ao usuário precisa acompanhar.
        Object.defineProperty(input, 'value', {
            configurable: true,
            get: function () { return valueDescriptor.get.call(input); },
            set: function (v) {
                valueDescriptor.set.call(input, v);
                syncFromCarrier();
            }
        });
        Object.defineProperty(input, 'required', {
            configurable: true,
            get: function () { return visible.required; },
            set: function (v) { visible.required = !!v; syncFromCarrier(); }
        });
        Object.defineProperty(input, 'disabled', {
            configurable: true,
            get: function () { return visible.disabled; },
            set: function (v) {
                visible.disabled = !!v;
                var button = wrapper.querySelector('.regional-picker-button');
                if (button) button.disabled = !!v;
                input.toggleAttribute('disabled', !!v);
            }
        });
        input.toggleAttribute('disabled', visible.disabled);

        var form = input.form;
        if (form) {
            form.addEventListener('reset', function () {
                setTimeout(function () {
                    setCarrier(input.getAttribute('value') || '');
                    syncFromCarrier();
                }, 0);
            });
        }

        syncFromCarrier();
    }

    function enhanceAll(root) {
        var scope = root && root.querySelectorAll ? root : document;
        var found = scope.querySelectorAll(
            'input[type="date"], input[type="month"], input[type="number"][step], input[data-regional-kind="decimal"]'
        );
        for (var i = 0; i < found.length; i++) enhance(found[i]);
        if (scope.matches && scope.matches('input')) enhance(scope);
    }

    function start() {
        enhanceAll(document);
        new MutationObserver(function (mutations) {
            mutations.forEach(function (mutation) {
                mutation.addedNodes.forEach(function (node) {
                    if (node.nodeType === 1) enhanceAll(node);
                });
            });
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();

    window.regional = {
        us: US,
        locale: LOCALE,
        formatNumber: function (value, digits) {
            return Number(value || 0).toLocaleString(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits });
        },
        isoToDisplayDate: isoToDisplayDate,
        displayToIsoDate: displayToIsoDate,
        displayToCanonNumber: displayToCanonNumber,
        enhanceAll: enhanceAll
    };
}());
