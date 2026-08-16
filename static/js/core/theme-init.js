(function () {
    'use strict';
    try {
        if (localStorage.getItem('app_values_hidden') === 'true') {
            document.documentElement.setAttribute('data-values-hidden', 'true');
        }
    } catch (_) {
        // Privacy preference is optional when storage is unavailable.
    }
})();
