function initProjectionDetail(root) {
    (root || document).querySelectorAll('[data-projection-detail]').forEach(function (button) {
        if (button.dataset.projectionDetailBound === '1') return;
        button.dataset.projectionDetailBound = '1';
        button.addEventListener('click', function () {
            const form = button.closest('form');
            const input = form ? form.querySelector('input[name="detail"]') : null;
            if (!form || !input) return;
            input.value = button.dataset.projectionDetail;
            window.submitWithMainPanelRestore(form);
        });
    });
}

document.addEventListener('DOMContentLoaded', function () { initProjectionDetail(document); });
/* `app:contentLoaded` era emitido pela navegacao AJAX propria, que deixou de
   existir; o evento equivalente do HTMX e `htmx:afterSwap`. */
document.addEventListener('htmx:afterSwap', function (event) {
    const alvo = (event.detail && event.detail.target) || event.target;
    initProjectionDetail(alvo && alvo.nodeType === Node.ELEMENT_NODE ? alvo : document);
});
