/* Gráficos do Painel.

   Havia DUAS construções dos mesmos seis gráficos: uma no `load` da página e
   outra no `app:contentLoaded`, para quando um filtro troca o conteúdo. Cópias
   não são só verbosidade -- elas divergem. E divergiram: a cópia da troca de
   filtro perdeu os dois plugins que desenham os rótulos (o número sobre cada
   barra de cobertura e o percentual dentro da rosca de categorias), então
   mudar qualquer filtro devolvia gráficos parecidos, porém sem rótulo, e a
   diferença passava por "mudou de forma sozinho".

   Agora há um construtor só, chamado pelos dois caminhos. Qualquer ajuste de
   gráfico vale para a primeira carga e para toda troca seguinte, sem ninguém
   ter de lembrar de mexer nos dois lugares.

   `currencySymbol` vem do servidor: o Painel mostra uma moeda por vez (o
   filtro global de moeda diz qual), e um `R$` fixo no código
   escreveria real num gráfico em dólar. */
(function () {
    var CANVAS_IDS = ['coverageChart', 'generationChart', 'catChart', 'dailyChart', 'projChart', 'evolutionChart'];

    function lerDados() {
        var el = document.getElementById('dashChartData');
        if (!el) return null;
        var d;
        try {
            d = JSON.parse(el.textContent);
        } catch (err) {
            console.error('Dados do dashboard invalidos', err);
            return null;
        }
        d.projMonths = d.projMonths || [];
        d.projRec = d.projRec || [];
        d.projDesp = d.projDesp || [];
        d.projSaldo = d.projSaldo || [];
        d.catLabels = d.catLabels || [];
        d.catValues = d.catValues || [];
        d.dailyDates = d.dailyDates || [];
        d.dailyBal = d.dailyBal || [];
        d.chartPeriods = d.chartPeriods || [];
        d.chartLabels = d.chartLabels || [];
        d.chartIncome = d.chartIncome || [];
        d.chartExpense = d.chartExpense || [];
        d.chartBalance = d.chartBalance || [];
        d.health = d.health || {};
        return d;
    }

    /* Os gráficos criados aqui, para poder destruí-los depois.

       Esta lista existe porque procurar pelo `id` no documento NÃO acha o que
       precisa morrer: quando um filtro troca o conteúdo, os canvas antigos saem
       do documento junto com o HTML anterior, e `getElementById` já devolve os
       NOVOS, que ainda não têm gráfico. O resultado era que cada troca deixava
       seis gráficos vivos presos a canvas que não existem mais -- 6, 12, 18, 24
       a cada troca, cada um segurando o seu bitmap e o seu observador de
       tamanho. O Chrome tem um teto de memória de canvas: passando dele, os
       canvas novos deixam de conseguir contexto e simplesmente não desenham.

       Era isso que fazia a tela piorar quanto mais se troca de filtro, com
       gráficos sumindo e voltando distorcidos. */
    var graficos = [];

    function guardar(grafico) {
        graficos.push(grafico);
        return grafico;
    }

    function destruirGraficos() {
        graficos.forEach(function (grafico) {
            try { grafico.destroy(); } catch (erro) { /* já morto: nada a fazer */ }
        });
        graficos = [];
        /* E o que por acaso esteja preso aos canvas atuais -- na primeira carga
           não há nada, mas isto cobre um canvas reaproveitado pelo HTMX. */
        CANVAS_IDS.forEach(function (id) {
            var canvas = document.getElementById(id);
            if (!canvas) return;
            var existente = window.Chart && Chart.getChart ? Chart.getChart(canvas) : null;
            if (existente) existente.destroy();
        });
    }

    function construirGraficos(d) {
        var styles = getComputedStyle(document.documentElement);
        var colorPositive = styles.getPropertyValue('--semantic-positive').trim() || '#16a34a';
        var colorNegative = styles.getPropertyValue('--semantic-negative').trim() || '#dc2626';
        var colorNeutral = styles.getPropertyValue('--semantic-neutral').trim() || '#64748b';
        var colorPrimary = styles.getPropertyValue('--primary').trim() || '#2563eb';
        var colorMuted = styles.getPropertyValue('--muted').trim() || '#64748b';
        var colorBorder = styles.getPropertyValue('--border').trim() || '#e2e8f0';
        var simboloMoeda = d.currencySymbol || 'R$';

        var formatNumber = function (value, digits) {
            return Number(value || 0).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
        };
        var setParamIfPresent = function (params, key, value) {
            if (value !== null && value !== undefined && value !== '') {
                params.set(key, String(value));
            }
        };
        var transactionUrlFor = function (extraParams) {
            var params = new URLSearchParams();
            setParamIfPresent(params, 'period', d.selectedPeriod);
            setParamIfPresent(params, 'mode', d.viewMode);
            setParamIfPresent(params, 'owner_id', d.currentOwnerId);
            setParamIfPresent(params, 'institution_id', d.currentInstitutionId);
            setParamIfPresent(params, 'account_id', d.currentAccountId);
            /* A troca de página aqui é por `location.href`, que o repasse dos
               filtros globais em `application.js` (feito em cliques de link)
               não vê. Sem estes dois, a fatia clicada abria Lançamentos em outra
               moeda e com todos os grupos -- outra soma. */
            setParamIfPresent(params, 'currency', d.currency);
            setParamIfPresent(params, d.groupsParam || 'grupos', d.accountGroups);
            Object.entries(extraParams || {}).forEach(function (kv) { setParamIfPresent(params, kv[0], kv[1]); });
            var query = params.toString();
            return query ? d.transactionsUrl + '?' + query : d.transactionsUrl;
        };
        var chartClickCursor = function (event, elements) {
            var target = event && event.native && event.native.target;
            if (target) target.classList.toggle('has-pointer', elements.length > 0);
        };
        var handleCategoryClick = function (_event, elements) {
            if (!elements.length) return;
            var label = d.catLabels[elements[0].index];
            if (!label || label === 'Sem dados') return;
            window.location.href = transactionUrlFor({
                dashboard_drilldown: '1',
                filter_type: d.filterType,
                filter_category: label
            });
        };
        var handleEvolutionClick = function (_event, elements) {
            if (!elements.length) return;
            var hit = elements[0];
            var period = d.chartPeriods[hit.index];
            if (!period) return;
            window.location.href = transactionUrlFor({
                dashboard_drilldown: '1',
                period: period,
                filter_type: hit.datasetIndex === 0 ? 'receita' : 'despesa'
            });
        };

        /* Saúde financeira: cobertura mensal. */
        var coverageCanvas = document.getElementById('coverageChart');
        var coverageLabelPlugin = {
            id: 'coverageLabelPlugin',
            afterDatasetsDraw: function (chart) {
                var ctx = chart.ctx;
                ctx.save();
                ctx.font = '700 11px Arial';
                ctx.textAlign = 'center';
                chart.getDatasetMeta(0).data.forEach(function (bar, index) {
                    var raw = chart.data.datasets[0].data[index];
                    if (!raw) return;
                    var value = Array.isArray(raw) ? raw[1] : raw;
                    ctx.fillStyle = value >= 1 ? colorPositive : colorNegative;
                    ctx.textBaseline = value >= 1 ? 'bottom' : 'top';
                    ctx.fillText(formatNumber(value, 2), bar.x, bar.y + (value >= 1 ? -8 : 8));
                });
                ctx.restore();
            }
        };
        if (coverageCanvas && d.health.labels && d.health.labels.length > 0) {
            var coverageValues = d.health.coverage || [];
            var validCoverage = coverageValues.filter(function (value) { return value !== null && Number.isFinite(Number(value)); });
            var yMin = Math.max(0, Math.min.apply(null, [1].concat(validCoverage)) - 0.25);
            var yMax = Math.max(1.25, Math.max.apply(null, [1].concat(validCoverage)) + 0.25);
            guardar(new Chart(coverageCanvas, {
                type: 'bar',
                data: {
                    labels: d.health.labels,
                    datasets: [{
                        label: 'cobertura',
                        data: coverageValues.map(function (value) { return value === null ? null : [1, Number(value)]; }),
                        backgroundColor: coverageValues.map(function (value) {
                            return value === null ? colorNeutral : Number(value) >= 1 ? 'rgba(22, 163, 74, 0.55)' : 'rgba(220, 38, 38, 0.62)';
                        }),
                        borderRadius: 4,
                        borderSkipped: false,
                        barPercentage: 0.84,
                        categoryPercentage: 0.96
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (ctx) { return 'Cobertura: ' + formatNumber(ctx.raw ? ctx.raw[1] : 0, 2) + 'x'; } } } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: colorMuted } },
                        y: { min: yMin, max: yMax, ticks: { color: colorMuted, callback: function (value) { return value === 1 ? '1,0' : ''; } }, grid: { color: function (ctx) { return ctx.tick.value === 1 ? colorMuted : 'transparent'; }, borderDash: [5, 5] } }
                    }
                },
                plugins: [coverageLabelPlugin]
            }));
        }

        /* Saúde financeira: geração líquida e média móvel. */
        var generationCanvas = document.getElementById('generationChart');
        if (generationCanvas && d.health.labels && d.health.labels.length > 0) {
            var generationValues = d.health.generation || [];
            guardar(new Chart(generationCanvas, {
                type: 'line',
                data: {
                    labels: d.health.labels,
                    datasets: [{
                        label: 'geração mensal',
                        data: generationValues,
                        borderColor: 'rgba(100, 116, 139, 0.65)',
                        backgroundColor: generationValues.map(function (value) { return Number(value) >= 0 ? colorPositive : colorNegative; }),
                        pointBackgroundColor: generationValues.map(function (value) { return Number(value) >= 0 ? colorPositive : colorNegative; }),
                        pointBorderColor: generationValues.map(function (value) { return Number(value) >= 0 ? colorPositive : colorNegative; }),
                        borderDash: [4, 5],
                        tension: 0.25,
                        pointRadius: 4,
                        fill: false
                    }, {
                        label: 'média 3m',
                        data: d.health.moving_average || [],
                        borderColor: colorPrimary,
                        backgroundColor: colorPrimary,
                        tension: 0.25,
                        pointRadius: 0,
                        borderWidth: 3,
                        fill: false
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: { legend: { position: 'top', align: 'end' }, tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + ': ' + simboloMoeda + ' ' + formatNumber(ctx.raw, 2); } } } },
                    scales: {
                        x: { grid: { display: false }, ticks: { color: colorMuted } },
                        y: { ticks: { color: colorMuted, callback: function (value) { return simboloMoeda + ' ' + formatNumber(value, 0); } }, grid: { color: function (ctx) { return ctx.tick.value === 0 ? colorBorder : 'transparent'; } } }
                    }
                }
            }));
        }

        /* Distribuição por categoria. */
        var catCanvas = document.getElementById('catChart');
        var percentLabelPlugin = {
            id: 'percentLabelPlugin',
            afterDatasetsDraw: function (chart) {
                var dataset = chart.data.datasets[0];
                var total = dataset.data.reduce(function (acc, value) { return acc + Number(value || 0); }, 0);
                if (!total) return;
                var ctx = chart.ctx;
                ctx.save();
                ctx.font = '600 12px Arial';
                ctx.fillStyle = '#ffffff';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                chart.getDatasetMeta(0).data.forEach(function (arc, i) {
                    var value = Number(dataset.data[i] || 0);
                    var pct = value / total * 100;
                    if (pct <= 3) return;
                    var pos = arc.tooltipPosition();
                    ctx.fillText(pct.toFixed(1) + '%', pos.x, pos.y);
                });
                ctx.restore();
            }
        };
        if (catCanvas && d.catLabels.length > 0) {
            guardar(new Chart(catCanvas, {
                type: 'doughnut',
                data: { labels: d.catLabels, datasets: [{ data: d.catValues, backgroundColor: ['#2563eb', '#10b981', '#ef4444', '#f59e0b', '#8b5cf6', '#ec4899', '#64748b', '#14b8a6', '#f97316', '#84cc16'], borderWidth: 0 }] },
                options: {
                    responsive: true,
                    onClick: handleCategoryClick,
                    onHover: chartClickCursor,
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: { callbacks: { label: function (ctx) {
                            var total = ctx.dataset.data.reduce(function (a, v) { return a + Number(v || 0); }, 0);
                            var pct = total ? (Number(ctx.raw || 0) / total * 100).toFixed(1) : '0.0';
                            return ctx.label + ': ' + simboloMoeda + ' ' + Number(ctx.raw || 0).toFixed(2) + ' (' + pct + '%)';
                        } } }
                    }
                },
                plugins: [percentLabelPlugin]
            }));
        }

        /* Evolução do saldo no mês. */
        var dailyCanvas = document.getElementById('dailyChart');
        if (dailyCanvas && d.dailyDates.length > 0) {
            guardar(new Chart(dailyCanvas, {
                type: 'line',
                data: { labels: d.dailyDates, datasets: [{ label: 'Saldo Diário', data: d.dailyBal, borderColor: '#2563eb', fill: true, tension: 0.4 }] },
                options: { responsive: true, plugins: { legend: { display: false } } }
            }));
        }

        /* Projeção. */
        var projCanvas = document.getElementById('projChart');
        if (projCanvas && d.projMonths.length > 0) {
            var geracao = d.projRec.map(function (r, i) { return r - (d.projDesp[i] || 0); });
            guardar(new Chart(projCanvas, {
                type: 'line',
                data: { labels: d.projMonths, datasets: [
                    { label: 'Receitas', data: d.projRec, borderColor: '#10b981', fill: true, tension: 0.3 },
                    { label: 'Despesas', data: d.projDesp, borderColor: '#ef4444', fill: true, tension: 0.3 },
                    { label: 'Geração', data: geracao, borderColor: '#8b5cf6', borderDash: [5, 5], fill: false, tension: 0.3 },
                    { label: 'Saldo', data: d.projSaldo, borderColor: '#2563eb', fill: false, tension: 0.3, yAxisID: 'y1' }
                ] },
                options: { responsive: true, scales: {
                    y: { beginAtZero: false, position: 'left', title: { display: true, text: 'Valores' } },
                    y1: { beginAtZero: false, position: 'right', grid: { drawOnChartArea: false } },
                    x: { grid: { display: false } }
                } }
            }));
        }

        /* Evolução mensal. */
        var evoCanvas = document.getElementById('evolutionChart');
        if (evoCanvas && d.chartLabels.length > 0) {
            guardar(new Chart(evoCanvas, {
                type: 'bar',
                data: { labels: d.chartLabels, datasets: [
                    { label: 'Receitas', data: d.chartIncome, backgroundColor: '#10b981', borderRadius: 4 },
                    { label: 'Despesas', data: d.chartExpense, backgroundColor: '#ef4444', borderRadius: 4 }
                ] },
                options: {
                    responsive: true,
                    onClick: handleEvolutionClick,
                    onHover: chartClickCursor,
                    plugins: { legend: { position: 'top' } },
                    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: '#f1f5f9' } } }
                }
            }));
        }
    }

    /* O Chart.js mede o contêiner UMA vez, no momento em que o gráfico nasce, e
       grava o resultado no `style` do canvas. Se naquele instante o conteúdo
       ainda não tinha o tamanho final -- e numa troca por filtro ele pode não
       ter --, o gráfico nasce pequeno, ou com altura zero, e **não se conserta
       sozinho**: o `ResizeObserver` dele vigia o PAI, e o pai não mudou de
       tamanho; quem ficou errado foi o canvas.

       Era esse o segundo sintoma relatado: depois de mexer num filtro, os
       gráficos vinham menores e os dois da Saúde Financeira (que têm altura
       livre) não vinham.

       A correção é remedir depois que o layout assentou. `resize()` relê o
       contêiner e redesenha; quando está tudo certo não muda nada, e custa um
       milissegundo.

       A construção continua sendo síncrona de propósito: adiá-la para o próximo
       quadro deixaria o Painel sem gráfico nenhum numa aba aberta em segundo
       plano, porque `requestAnimationFrame` não roda enquanto a aba não é
       pintada. Por isso a remedida vai pelos dois caminhos -- o quadro, para a
       aba visível, e o temporizador, para a que ainda não é. Chamar `resize()`
       duas vezes não faz diferença nenhuma. */
    function ficouDoTamanhoDoContainer(canvas) {
        var container = canvas.parentElement;
        if (!container) return true;
        var caixa = canvas.getBoundingClientRect();
        if (caixa.width < 1 || caixa.height < 1) return false;
        /* 2px de folga: o Chart.js desconta margem e arredonda. */
        return caixa.width >= container.clientWidth - 2;
    }

    /* Um `resize()` não salva um gráfico que nasceu sem tamanho: a proporção
       dele foi calculada naquele momento e já está errada. Quando a medida não
       bate com o contêiner, o jeito é construir de novo, agora com o layout
       que existe de verdade. Uma vez só -- se ainda assim não bater, é outro
       problema, e insistir viraria laço. */
    var jaReconstruiu = false;

    function conferirTamanhos(d) {
        var torto = CANVAS_IDS.some(function (id) {
            var canvas = document.getElementById(id);
            if (!canvas) return false;
            var grafico = window.Chart && Chart.getChart ? Chart.getChart(canvas) : null;
            return grafico ? !ficouDoTamanhoDoContainer(canvas) : false;
        });
        if (!torto) return;
        if (jaReconstruiu) return;
        jaReconstruiu = true;
        destruirGraficos();
        construirGraficos(d);
    }

    function renderizar() {
        if (typeof Chart === 'undefined') { console.error('Chart.js não carregado'); return; }
        var d = lerDados();
        if (!d) return;
        jaReconstruiu = false;
        destruirGraficos();
        construirGraficos(d);
        var conferir = function () { conferirTamanhos(d); };
        requestAnimationFrame(conferir);
        setTimeout(conferir, 120);
    }

    window.addEventListener('load', renderizar);
    /* Emitido por `core/application.js` a cada troca de `#appMain` -- é o que
       acontece quando qualquer filtro do Painel muda. */
    document.addEventListener('app:contentLoaded', renderizar);
})();
