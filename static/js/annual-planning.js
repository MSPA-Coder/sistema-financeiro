/* Pequenos aprimoramentos de acessibilidade para a tabela longa. */
(function () {
    function initAnnualPlanning(root) {
        (root || document).querySelectorAll('[data-annual-planning-table]').forEach(function (tableWrap) {
            if (tableWrap.dataset.annualPlanningBound === '1') return;
            tableWrap.dataset.annualPlanningBound = '1';

            var table = tableWrap.querySelector('table');
            if (table) {
                tableWrap.addEventListener('keydown', function (event) {
                    if (event.key !== 'Home' && event.key !== 'End') return;
                    if (event.target.closest('input, select, textarea, button, a')) return;
                    tableWrap.scrollLeft = event.key === 'Home' ? 0 : tableWrap.scrollWidth;
                });
            }
        });

        var summaryWrap = document.querySelector('.annual-summary-wrap');
        var planningWrap = document.querySelector('[data-annual-planning-table]');
        if (!summaryWrap || !planningWrap || summaryWrap.dataset.annualPlanningScrollBound === '1') return;

        summaryWrap.dataset.annualPlanningScrollBound = '1';
        planningWrap.dataset.annualPlanningScrollBound = '1';
        var syncing = false;
        function syncScroll(source, target) {
            if (syncing) return;
            syncing = true;
            target.scrollLeft = source.scrollLeft;
            syncing = false;
        }
        summaryWrap.addEventListener('scroll', function () { syncScroll(summaryWrap, planningWrap); });
        planningWrap.addEventListener('scroll', function () { syncScroll(planningWrap, summaryWrap); });
    }

    document.addEventListener('DOMContentLoaded', function () { initAnnualPlanning(document); });
    document.addEventListener('app:contentLoaded', function (event) { initAnnualPlanning(event.target); });
    document.addEventListener('htmx:afterSwap', function (event) { initAnnualPlanning(event.target); });
}());
