window.addEventListener('load', function() {
    if (typeof Chart === 'undefined') { console.error('Chart.js não carregado'); return; }
    const dataEl = document.getElementById('dashChartData');
    if (!dataEl) return;
    let d;
    try {
        d = JSON.parse(dataEl.textContent);
    } catch (err) {
        console.error('Dados do dashboard invalidos', err);
        return;
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
    const styles = getComputedStyle(document.documentElement);
    const colorPositive = styles.getPropertyValue('--semantic-positive').trim() || '#16a34a';
    const colorNegative = styles.getPropertyValue('--semantic-negative').trim() || '#dc2626';
    const colorNeutral = styles.getPropertyValue('--semantic-neutral').trim() || '#64748b';
    const colorPrimary = styles.getPropertyValue('--primary').trim() || '#2563eb';
    const colorMuted = styles.getPropertyValue('--muted').trim() || '#64748b';
    const colorBorder = styles.getPropertyValue('--border').trim() || '#e2e8f0';
    const formatNumber = function(value, digits) {
        return Number(value || 0).toLocaleString('pt-BR', { minimumFractionDigits: digits, maximumFractionDigits: digits });
    };
    const setParamIfPresent = function(params, key, value) {
        if (value !== null && value !== undefined && value !== '') {
            params.set(key, String(value));
        }
    };
    const transactionUrlFor = function(extraParams) {
        const params = new URLSearchParams();
        setParamIfPresent(params, 'period', d.selectedPeriod);
        setParamIfPresent(params, 'mode', d.viewMode);
        setParamIfPresent(params, 'owner_id', d.currentOwnerId);
        setParamIfPresent(params, 'institution_id', d.currentInstitutionId);
        setParamIfPresent(params, 'account_id', d.currentAccountId);
        Object.entries(extraParams || {}).forEach(([key, value]) => setParamIfPresent(params, key, value));
        const query = params.toString();
        return query ? d.transactionsUrl + '?' + query : d.transactionsUrl;
    };
    const chartClickCursor = function(event, elements) {
        const target = event && event.native && event.native.target;
        if (target) target.style.cursor = elements.length ? 'pointer' : 'default';
    };
    const handleCategoryClick = function(_event, elements) {
        if (!elements.length) return;
        const label = d.catLabels[elements[0].index];
        if (!label || label === 'Sem dados') return;
        window.location.href = transactionUrlFor({
            dashboard_drilldown: '1',
            filter_type: d.filterType,
            filter_category: label,
        });
    };
    const handleEvolutionClick = function(_event, elements) {
        if (!elements.length) return;
        const hit = elements[0];
        const period = d.chartPeriods[hit.index];
        if (!period) return;
        window.location.href = transactionUrlFor({
            dashboard_drilldown: '1',
            period: period,
            filter_type: hit.datasetIndex === 0 ? 'receita' : 'despesa',
        });
    };

    // Financial health: monthly coverage.
    const coverageCanvas = document.getElementById('coverageChart');
    const coverageLabelPlugin = {
        id: 'coverageLabelPlugin',
        afterDatasetsDraw(chart) {
            const {ctx} = chart;
            ctx.save();
            ctx.font = '700 11px Arial';
            ctx.textAlign = 'center';
            chart.getDatasetMeta(0).data.forEach((bar, index) => {
                const raw = chart.data.datasets[0].data[index];
                if (!raw) return;
                const value = Array.isArray(raw) ? raw[1] : raw;
                ctx.fillStyle = value >= 1 ? colorPositive : colorNegative;
                ctx.textBaseline = value >= 1 ? 'bottom' : 'top';
                ctx.fillText(formatNumber(value, 2), bar.x, bar.y + (value >= 1 ? -8 : 8));
            });
            ctx.restore();
        }
    };
    if (coverageCanvas && d.health.labels && d.health.labels.length > 0) {
        const coverageValues = d.health.coverage || [];
        const validCoverage = coverageValues.filter((value) => value !== null && Number.isFinite(Number(value)));
        const yMin = Math.max(0, Math.min(1, ...validCoverage) - 0.25);
        const yMax = Math.max(1.25, Math.max(1, ...validCoverage) + 0.25);
        new Chart(coverageCanvas, {
            type: 'bar',
            data: {
                labels: d.health.labels,
                datasets: [{
                    label: 'cobertura',
                    data: coverageValues.map((value) => value === null ? null : [1, Number(value)]),
                    backgroundColor: coverageValues.map((value) => value === null ? colorNeutral : Number(value) >= 1 ? 'rgba(22, 163, 74, 0.55)' : 'rgba(220, 38, 38, 0.62)'),
                    borderRadius: 4,
                    borderSkipped: false,
                    barPercentage: 0.84,
                    categoryPercentage: 0.96
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => 'Cobertura: ' + formatNumber(ctx.raw ? ctx.raw[1] : 0, 2) + 'x' } } },
                scales: {
                    x: { grid: { display: false }, ticks: { color: colorMuted } },
                    y: { min: yMin, max: yMax, ticks: { color: colorMuted, callback: (value) => value === 1 ? '1,0' : '' }, grid: { color: (ctx) => ctx.tick.value === 1 ? colorMuted : 'transparent', borderDash: [5, 5] } }
                }
            },
            plugins: [coverageLabelPlugin]
        });
    }

    // Financial health: generation and moving average.
    const generationCanvas = document.getElementById('generationChart');
    if (generationCanvas && d.health.labels && d.health.labels.length > 0) {
        new Chart(generationCanvas, {
            type: 'line',
            data: {
                labels: d.health.labels,
                datasets: [{
                    label: 'geração mensal',
                    data: d.health.generation || [],
                    borderColor: 'rgba(100, 116, 139, 0.65)',
                    backgroundColor: (d.health.generation || []).map((value) => Number(value) >= 0 ? colorPositive : colorNegative),
                    pointBackgroundColor: (d.health.generation || []).map((value) => Number(value) >= 0 ? colorPositive : colorNegative),
                    pointBorderColor: (d.health.generation || []).map((value) => Number(value) >= 0 ? colorPositive : colorNegative),
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
                plugins: { legend: { position: 'top', align: 'end' }, tooltip: { callbacks: { label: (ctx) => ctx.dataset.label + ': R$ ' + formatNumber(ctx.raw, 2) } } },
                scales: {
                    x: { grid: { display: false }, ticks: { color: colorMuted } },
                    y: { ticks: { color: colorMuted, callback: (value) => 'R$ ' + formatNumber(value, 0) }, grid: { color: (ctx) => ctx.tick.value === 0 ? colorBorder : 'transparent' } }
                }
            }
        });
    }

    // Chart 1.
    const catCanvas = document.getElementById('catChart');
    const percentLabelPlugin = {
        id: 'percentLabelPlugin',
        afterDatasetsDraw(chart) {
            const dataset = chart.data.datasets[0];
            const total = dataset.data.reduce((acc, value) => acc + Number(value || 0), 0);
            if (!total) return;
            const {ctx} = chart;
            ctx.save();
            ctx.font = '600 12px Arial';
            ctx.fillStyle = '#ffffff';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            chart.getDatasetMeta(0).data.forEach((arc, i) => {
                const value = Number(dataset.data[i] || 0);
                const pct = value / total * 100;
                if (pct <= 3) return;
                const pos = arc.tooltipPosition();
                ctx.fillText(pct.toFixed(1) + '%', pos.x, pos.y);
            });
            ctx.restore();
        }
    };
    if (catCanvas && d.catLabels.length > 0) {
        new Chart(catCanvas, {
            type: 'doughnut',
            data: { labels: d.catLabels, datasets: [{ data: d.catValues, backgroundColor: ['#2563eb','#10b981','#ef4444','#f59e0b','#8b5cf6','#ec4899','#64748b','#14b8a6','#f97316','#84cc16'], borderWidth: 0 }] },
            options: { responsive: true, onClick: handleCategoryClick, onHover: chartClickCursor, plugins: { legend: { position: 'bottom' }, tooltip: { callbacks: { label: function(ctx) { const total = ctx.dataset.data.reduce((a, v) => a + Number(v || 0), 0); const pct = total ? (Number(ctx.raw || 0) / total * 100).toFixed(1) : '0.0'; return ctx.label + ': R$ ' + Number(ctx.raw || 0).toFixed(2) + ' (' + pct + '%)'; } } } } },
            plugins: [percentLabelPlugin]
        });
    }
    // Chart 2.
    const dailyCanvas = document.getElementById('dailyChart');
    if (dailyCanvas && d.dailyDates.length > 0) {
        new Chart(dailyCanvas, { type: 'line', data: { labels: d.dailyDates, datasets: [{ label: 'Saldo Diário', data: d.dailyBal, borderColor: '#2563eb', fill: true, tension: 0.4 }] }, options: { responsive: true, plugins: { legend: { display: false } } } });
    }
    // Chart 3.
    const projCanvas = document.getElementById('projChart');
    if (projCanvas && d.projMonths.length > 0) {
        const geracao = d.projRec.map((r, i) => r - (d.projDesp[i] || 0));
        new Chart(projCanvas, { type: 'line', data: { labels: d.projMonths, datasets: [{ label: 'Receitas', data: d.projRec, borderColor: '#10b981', fill: true, tension: 0.3 }, { label: 'Despesas', data: d.projDesp, borderColor: '#ef4444', fill: true, tension: 0.3 }, { label: 'Geração', data: geracao, borderColor: '#8b5cf6', borderDash: [5,5], fill: false, tension: 0.3 }, { label: 'Saldo', data: d.projSaldo, borderColor: '#2563eb', fill: false, tension: 0.3, yAxisID: 'y1' }] }, options: { responsive: true, scales: { y: { beginAtZero: false, position: 'left', title: { display: true, text: 'Valores' } }, y1: { beginAtZero: false, position: 'right', grid: { drawOnChartArea: false } }, x: { grid: { display: false } } } } });
    }
    // Chart 4.
    const evoCanvas = document.getElementById('evolutionChart');
    if (evoCanvas && d.chartLabels.length > 0) {
        new Chart(evoCanvas, { type: 'bar', data: { labels: d.chartLabels, datasets: [{ label: 'Receitas', data: d.chartIncome, backgroundColor: '#10b981', borderRadius: 4 }, { label: 'Despesas', data: d.chartExpense, backgroundColor: '#ef4444', borderRadius: 4 }] }, options: { responsive: true, onClick: handleEvolutionClick, onHover: chartClickCursor, plugins: { legend: { position: 'top' } }, scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: '#f1f5f9' } } } } });
    }

    /* Reinitialize dashboard charts after an AJAX content swap. */
    (function () {
        var CANVAS_IDS = ['coverageChart', 'generationChart', 'catChart', 'dailyChart', 'projChart', 'evolutionChart'];
        function destroyAll() {
            CANVAS_IDS.forEach(function (id) {
                var c = document.getElementById(id);
                if (!c) return;
                var existing = window.Chart && Chart.getChart ? Chart.getChart(c) : null;
                if (existing) existing.destroy();
            });
        }
        document.addEventListener('app:contentLoaded', function () {
            var el = document.getElementById('dashChartData');
            if (!el || typeof Chart === 'undefined') return;
            var nd;
            try { nd = JSON.parse(el.textContent); } catch (e) { return; }
            destroyAll();
            /* Recreate charts from the refreshed JSON data block. */
            var styles   = getComputedStyle(document.documentElement);
            var cPos     = styles.getPropertyValue('--semantic-positive').trim() || '#16a34a';
            var cNeg     = styles.getPropertyValue('--semantic-negative').trim() || '#dc2626';
            var cPrim    = styles.getPropertyValue('--primary').trim()           || '#2563eb';
            var cMuted   = styles.getPropertyValue('--muted').trim()             || '#64748b';
            var cBorder  = styles.getPropertyValue('--border').trim()            || '#e2e8f0';
            var cNeutral = styles.getPropertyValue('--semantic-neutral').trim()  || '#64748b';
            var fmt      = function (v, d) { return Number(v||0).toLocaleString('pt-BR',{minimumFractionDigits:d,maximumFractionDigits:d}); };
            var setP     = function (p,k,v) { if(v!==null&&v!==undefined&&v!=='') p.set(k,String(v)); };
            var txUrl    = function (extra) {
                var p = new URLSearchParams();
                setP(p,'period',nd.selectedPeriod); setP(p,'mode',nd.viewMode);
                setP(p,'owner_id',nd.currentOwnerId); setP(p,'institution_id',nd.currentInstitutionId);
                setP(p,'account_id',nd.currentAccountId);
                Object.entries(extra||{}).forEach(function(kv){setP(p,kv[0],kv[1]);});
                var q=p.toString(); return q ? nd.transactionsUrl+'?'+q : nd.transactionsUrl;
            };
            var hover = function(ev,els){var t=ev&&ev.native&&ev.native.target;if(t)t.style.cursor=els.length?'pointer':'default';};
            var onCatClick = function(_e,els){
                if(!els.length) return;
                var label=nd.catLabels[els[0].index];
                if(!label||label==='Sem dados') return;
                window.location.href=txUrl({dashboard_drilldown:'1',filter_type:nd.filterType,filter_category:label});
            };
            var onEvoClick = function(_e,els){
                if(!els.length) return;
                var hit=els[0]; var period=nd.chartPeriods[hit.index];
                if(!period) return;
                window.location.href=txUrl({dashboard_drilldown:'1',period:period,filter_type:hit.datasetIndex===0?'receita':'despesa'});
            };
            /* coverage */
            var cov=document.getElementById('coverageChart');
            if(cov&&nd.health&&nd.health.labels&&nd.health.labels.length>0){
                var cv=nd.health.coverage||[];
                var valid=cv.filter(function(v){return v!==null&&Number.isFinite(Number(v));});
                var yMin=Math.max(0,Math.min(1,Math.min.apply(null,valid))-0.25);
                var yMax=Math.max(1.25,Math.max(1,Math.max.apply(null,valid))+0.25);
                new Chart(cov,{type:'bar',data:{labels:nd.health.labels,datasets:[{label:'cobertura',data:cv.map(function(v){return v===null?null:[1,Number(v)];}),backgroundColor:cv.map(function(v){return v===null?cNeutral:Number(v)>=1?'rgba(22,163,74,0.55)':'rgba(220,38,38,0.62)';}),borderRadius:4,borderSkipped:false,barPercentage:0.84,categoryPercentage:0.96}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return 'Cobertura: '+fmt(c.raw?c.raw[1]:0,2)+'x';}}}},scales:{x:{grid:{display:false},ticks:{color:cMuted}},y:{min:yMin,max:yMax,ticks:{color:cMuted,callback:function(v){return v===1?'1,0':''}},grid:{color:function(c){return c.tick.value===1?cMuted:'transparent';},borderDash:[5,5]}}}}});
            }
            /* generation */
            var gen=document.getElementById('generationChart');
            if(gen&&nd.health&&nd.health.labels&&nd.health.labels.length>0){
                var gv=nd.health.generation||[];
                new Chart(gen,{type:'line',data:{labels:nd.health.labels,datasets:[{label:'geração mensal',data:gv,borderColor:'rgba(100,116,139,0.65)',backgroundColor:gv.map(function(v){return Number(v)>=0?cPos:cNeg;}),pointBackgroundColor:gv.map(function(v){return Number(v)>=0?cPos:cNeg;}),pointBorderColor:gv.map(function(v){return Number(v)>=0?cPos:cNeg;}),borderDash:[4,5],tension:0.25,pointRadius:4,fill:false},{label:'média 3m',data:nd.health.moving_average||[],borderColor:cPrim,backgroundColor:cPrim,tension:0.25,pointRadius:0,borderWidth:3,fill:false}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'top',align:'end'},tooltip:{callbacks:{label:function(c){return c.dataset.label+': R$ '+fmt(c.raw,2);}}}},scales:{x:{grid:{display:false},ticks:{color:cMuted}},y:{ticks:{color:cMuted,callback:function(v){return 'R$ '+fmt(v,0);}},grid:{color:function(c){return c.tick.value===0?cBorder:'transparent';}}}}}});
            }
            /* categories */
            var cat=document.getElementById('catChart');
            if(cat&&nd.catLabels&&nd.catLabels.length>0){
                new Chart(cat,{type:'doughnut',data:{labels:nd.catLabels,datasets:[{data:nd.catValues,backgroundColor:['#2563eb','#10b981','#ef4444','#f59e0b','#8b5cf6','#ec4899','#64748b','#14b8a6','#f97316','#84cc16'],borderWidth:0}]},options:{responsive:true,onClick:onCatClick,onHover:hover,plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:function(c){var t=c.dataset.data.reduce(function(a,v){return a+Number(v||0);},0);var pct=t?(Number(c.raw||0)/t*100).toFixed(1):'0.0';return c.label+': R$ '+Number(c.raw||0).toFixed(2)+' ('+pct+'%)';}}}}}});
            }
            /* daily */
            var day=document.getElementById('dailyChart');
            if(day&&nd.dailyDates&&nd.dailyDates.length>0){
                new Chart(day,{type:'line',data:{labels:nd.dailyDates,datasets:[{label:'Saldo Diário',data:nd.dailyBal,borderColor:'#2563eb',fill:true,tension:0.4}]},options:{responsive:true,plugins:{legend:{display:false}}}});
            }
            /* projection */
            var proj=document.getElementById('projChart');
            if(proj&&nd.projMonths&&nd.projMonths.length>0){
                var ger=nd.projRec.map(function(r,i){return r-(nd.projDesp[i]||0);});
                new Chart(proj,{type:'line',data:{labels:nd.projMonths,datasets:[{label:'Receitas',data:nd.projRec,borderColor:'#10b981',fill:true,tension:0.3},{label:'Despesas',data:nd.projDesp,borderColor:'#ef4444',fill:true,tension:0.3},{label:'Geração',data:ger,borderColor:'#8b5cf6',borderDash:[5,5],fill:false,tension:0.3},{label:'Saldo',data:nd.projSaldo,borderColor:'#2563eb',fill:false,tension:0.3,yAxisID:'y1'}]},options:{responsive:true,scales:{y:{beginAtZero:false,position:'left',title:{display:true,text:'Valores'}},y1:{beginAtZero:false,position:'right',grid:{drawOnChartArea:false}},x:{grid:{display:false}}}}});
            }
            /* evolution */
            var evo=document.getElementById('evolutionChart');
            if(evo&&nd.chartLabels&&nd.chartLabels.length>0){
                new Chart(evo,{type:'bar',data:{labels:nd.chartLabels,datasets:[{label:'Receitas',data:nd.chartIncome,backgroundColor:'#10b981',borderRadius:4},{label:'Despesas',data:nd.chartExpense,backgroundColor:'#ef4444',borderRadius:4}]},options:{responsive:true,onClick:onEvoClick,onHover:hover,plugins:{legend:{position:'top'}},scales:{x:{grid:{display:false}},y:{beginAtZero:true,grid:{color:'#f1f5f9'}}}}});
            }
        });

    })();
});
