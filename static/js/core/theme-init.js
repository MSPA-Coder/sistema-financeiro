(function () {
    'use strict';
    try {
        if (localStorage.getItem('app_values_hidden') === 'true') {
            document.documentElement.setAttribute('data-values-hidden', 'true');
        }
    } catch (_) {
        // Privacy preference is optional when storage is unavailable.
    }
    // Menu lateral: a pagina nasce recolhida; quem o expandiu nesta sessao do
    // navegador o encontra expandido nas paginas seguintes (nada vai ao servidor).
    try {
        if (sessionStorage.getItem('app_sidebar') === 'expanded') {
            document.documentElement.setAttribute('data-sidebar', 'expanded');
        }
    } catch (_) {
        // Sem armazenamento, vale o padrao recolhido.
    }
})();
