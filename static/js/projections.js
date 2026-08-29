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
document.addEventListener('app:contentLoaded', function (event) { initProjectionDetail(event.target); });
