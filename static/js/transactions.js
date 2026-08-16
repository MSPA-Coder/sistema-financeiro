/* ============================================================
   TRANSACTIONS - funcoes de UI e re-init apos AJAX
   ============================================================ */

function toggleEntryForm(force) {
    var card = document.getElementById('newTransactionCard');
    if (!card) return;
    var show = force === undefined ? !card.classList.contains('is-open') : force;
    card.classList.toggle('is-open', show);
}

function toggleRealizedFields(select) {
    if (!select || !select.closest) return;
    var form = select.closest('form');
    var show = select.value === 'realizado';
    if (!form) return;
    form.querySelectorAll('.realized-field').forEach(function (el) {
        el.style.display = show ? 'block' : 'none';
    });
}

function initRealizedFields() {
    document.querySelectorAll('.status-select').forEach(function (select) {
        toggleRealizedFields(select);
        if (select._realizedFieldsBound) return;
        select._realizedFieldsBound = true;
        select.addEventListener('change', function () { toggleRealizedFields(select); });
    });
}

function toggleCounterpartyFields(select) {
    if (!select || !select.closest) return;
    var form = select.closest('form');
    var selected = select.options[select.selectedIndex];
    var isInternal = selected && selected.dataset.internal === '1';
    if (!form) return;
    form.querySelectorAll('.counterparty-field').forEach(function (el) {
        el.style.display = isInternal ? 'block' : 'none';
        var acSel = el.querySelector('select[name="counterparty_account_id"]');
        if (acSel) acSel.required = isInternal;
    });
}

function initCounterpartyFields() {
    document.querySelectorAll('.category-select').forEach(function (select) {
        toggleCounterpartyFields(select);
        if (select._counterpartyFieldsBound) return;
        select._counterpartyFieldsBound = true;
        select.addEventListener('change', function () { toggleCounterpartyFields(select); });
    });
}

function openRealizeModal(txId, dueDate, plannedValue) {
    document.getElementById('modal_tx_id').value = txId;
    document.querySelector('#realizeModal input[name="realized_date"]').value   = dueDate;
    document.querySelector('#realizeModal input[name="realized_amount"]').value = plannedValue;
    var form = document.getElementById('realizeForm');
    var url  = new URL(form.action, window.location.origin);
    url.pathname = url.pathname.replace(/\/mark_realized\/\d+\/?$/, '/mark_realized/' + txId + '/');
    form.action  = url.pathname + url.search;
    document.getElementById('realizeModal').style.display = 'flex';
}

function closeRealizeModal() {
    var m = document.getElementById('realizeModal');
    if (m) m.style.display = 'none';
}

function openDeleteModal(actionUrl, supportsScope) {
    var modal        = document.getElementById('confirmDeleteModal');
    var form         = document.getElementById('deleteForm');
    var scopeOptions = document.getElementById('deleteScopeOptions');
    var scopeInput   = document.getElementById('deleteOperationScope');
    var scopeSelect  = document.getElementById('deleteScopeSelect');
    if (!modal) return;
    form.action           = actionUrl;
    form.setAttribute('hx-post', actionUrl);
    form.setAttribute('hx-target', '#transactions-table-container');
    form.setAttribute('hx-swap', 'outerHTML');
    if (window.htmx) window.htmx.process(form);
    scopeOptions.style.display = supportsScope ? 'block' : 'none';
    if (scopeSelect) scopeSelect.value = 'all';
    if (scopeInput)  scopeInput.value  = 'all';
    modal.style.display = 'flex';
    setTimeout(function () {
        var btn = document.getElementById('cancelDeleteButton');
        if (btn) btn.focus();
    }, 0);
}

function closeDeleteModal() {
    var m = document.getElementById('confirmDeleteModal');
    if (m) m.style.display = 'none';
}

/* Re-init dos modais (event-listeners perdidos apos AJAX swap) */
function _initTransactionModals() {
    var rm = document.getElementById('realizeModal');
    if (rm && !rm._modalBound) {
        rm._modalBound = true;
        rm.addEventListener('click', function (e) { if (e.target === this) closeRealizeModal(); });
    }

    var dm = document.getElementById('confirmDeleteModal');
    if (dm && !dm._modalBound) {
        dm._modalBound = true;
        dm.addEventListener('click', function (e) { if (e.target === this) closeDeleteModal(); });
    }

    var ss = document.getElementById('deleteScopeSelect');
    if (ss && !ss._scopeBound) {
        ss._scopeBound = true;
        ss.addEventListener('change', function () {
            var inp = document.getElementById('deleteOperationScope');
            if (inp) inp.value = this.value;
        });
    }
}

function _initTransactionActions(root) {
    (root || document).querySelectorAll('[data-transaction-action]').forEach(function (button) {
        if (button._transactionActionBound) return;
        button._transactionActionBound = true;
        button.addEventListener('click', function () {
            var action = button.dataset.transactionAction;
            if (action === 'toggle-entry-form') toggleEntryForm();
            if (action === 'close-realize') closeRealizeModal();
            if (action === 'close-delete') closeDeleteModal();
            if (action === 'delete') {
                openDeleteModal(button.dataset.deleteUrl, button.dataset.deleteSupportsScope === 'true');
            }
            if (action === 'realize') {
                openRealizeModal(button.dataset.transactionId, button.dataset.dueDate, button.dataset.plannedValue);
            }
        });
    });
}

function _initTransactionEditToggles(root) {
    (root || document).querySelectorAll('[data-toggle-edit]').forEach(function (button) {
        if (button._transactionEditBound || button._toggleBound) return;
        button._transactionEditBound = true;
        button.addEventListener('click', function () {
            var row = document.getElementById(button.dataset.toggleEdit);
            if (!row) return;
            var force = button.dataset.toggleForce;
            var show = force === undefined ? row.style.display !== 'table-row' : force === 'true';
            row.style.display = show ? 'table-row' : 'none';
        });
    });
}

/* -- DOMContentLoaded -- */
document.addEventListener('DOMContentLoaded', function () {
    initRealizedFields();
    initCounterpartyFields();
    _initTransactionModals();
    _initTransactionActions(document);
    _initTransactionEditToggles(document);
});

/* -- Re-init apos AJAX swap -- */
document.addEventListener('app:contentLoaded', function () {
    initRealizedFields();
    initCounterpartyFields();
    _initTransactionModals();
    _initTransactionActions(document);
    _initTransactionEditToggles(document);
});

/* -- HTMX events for table refresh -- */
document.addEventListener('htmx:afterSwap', function (event) {
    /* A resposta de filtros atualiza tanto a tabela quanto os cartoes de
       resumo (out-of-band). Reativa os controles apos qualquer uma dessas
       trocas, pois a ordem dos eventos nao e garantida pelo HTMX. */
    initRealizedFields();
    initCounterpartyFields();
    _initTransactionModals();
    _initTransactionActions(document);
    _initTransactionEditToggles(document);
});

document.addEventListener('htmx:load', function (event) {
    var root = event.detail && event.detail.elt;
    if (!root || (root.id !== 'transactions-table-container' && !root.querySelector('[data-transaction-action]'))) return;
    initRealizedFields();
    initCounterpartyFields();
    _initTransactionModals();
    _initTransactionActions(root);
    _initTransactionEditToggles(root);
});

document.addEventListener('htmx:afterRequest', function (event) {
    var source = event.detail && event.detail.elt;
    if (source && source.id === 'deleteForm' && event.detail.successful) {
        closeDeleteModal();
    }
});
