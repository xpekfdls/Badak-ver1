const Journal = (() => {
    let equityChart = null;

    function init() {
        refresh();
    }

    async function refresh() {
        await Promise.all([loadTrades(), loadStats(), loadEquityCurve(), loadDailyStats(), loadSymbolStats()]);
    }

    async function loadTrades() {
        try {
            const [binanceResp, localResp] = await Promise.all([
                fetch('/api/trades/binance?limit=100').catch(() => null),
                fetch('/api/trades?limit=100'),
            ]);

            let binanceTrades = [];
            if (binanceResp && binanceResp.ok) {
                binanceTrades = await binanceResp.json();
            }
            const localTrades = await localResp.json();

            if (binanceTrades.length && binanceTrades[0].source === 'binance') {
                renderBinanceTrades(binanceTrades, localTrades);
            } else {
                renderLocalTrades(localTrades);
            }
        } catch (e) {
            console.error('Trades load error:', e);
        }
    }

    function renderBinanceTrades(binanceTrades, localTrades) {
        const tbody = document.getElementById('trades-body');
        if (!binanceTrades.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">No trades yet</td></tr>';
            return;
        }

        const localMap = {};
        localTrades.forEach(lt => {
            const key = `${lt.symbol}_${lt.exit_time}`;
            localMap[key] = lt;
        });

        tbody.innerHTML = binanceTrades.map(t => {
            const ts = new Date(t.time).toLocaleDateString('ko-KR', {
                month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
            });
            const pnl = t.realized_pnl || 0;
            const pnlClass = pnl >= 0 ? 'positive' : 'negative';

            let reason = t.exit_reason || '';
            if (!reason) {
                for (const lt of localTrades) {
                    if (lt.symbol === t.symbol && Math.abs(lt.exit_time - t.time) < 5000) {
                        reason = lt.exit_reason;
                        break;
                    }
                }
            }

            const reasonClass = reason === 'TRAIL' ? 'positive' :
                               (reason === 'SL' || reason === 'LIQ') ? 'negative' :
                               reason === 'CYCLE_SELL' ? '' : '';
            const signal = t.signal_tick ? `${t.signal_tick}T` : '';

            return `<tr>
                <td>${ts}</td>
                <td><strong>${t.symbol.replace('USDT', '')}</strong></td>
                <td class="${pnlClass}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(4)}</td>
                <td class="${reasonClass}">${reason || '-'}</td>
                <td>${signal}</td>
                <td style="color:#565d69;font-size:10px">Binance</td>
            </tr>`;
        }).join('');
    }

    function renderLocalTrades(trades) {
        const tbody = document.getElementById('trades-body');
        if (!trades.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">No trades yet</td></tr>';
            return;
        }
        tbody.innerHTML = trades.map(t => {
            const time = new Date(t.exit_time).toLocaleDateString('ko-KR', {
                month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
            });
            const pnlClass = t.pnl_pct >= 0 ? 'positive' : 'negative';
            const reasonClass = t.exit_reason === 'TRAIL' ? 'positive' :
                               (t.exit_reason === 'SL' || t.exit_reason === 'LIQ') ? 'negative' : '';
            return `<tr>
                <td>${time}</td>
                <td><strong>${t.symbol.replace('USDT', '')}</strong></td>
                <td class="${pnlClass}">${t.realized_pnl >= 0 ? '+' : ''}$${t.realized_pnl.toFixed(4)}</td>
                <td class="${reasonClass}">${t.exit_reason}</td>
                <td>${t.signal_tick ? t.signal_tick + 'T' : ''}</td>
                <td style="color:#565d69;font-size:10px">Local</td>
            </tr>`;
        }).join('');
    }

    async function loadStats() {
        try {
            const [binResp, localResp] = await Promise.all([
                fetch('/api/trades/binance/summary?days=30').catch(() => null),
                fetch('/api/trades/stats'),
            ]);

            const localStats = await localResp.json();
            let binStats = null;
            if (binResp && binResp.ok) {
                binStats = await binResp.json();
            }

            renderStats(localStats, binStats);
        } catch (e) {
            console.error('Stats load error:', e);
        }
    }

    function renderStats(stats, binStats) {
        const el = document.getElementById('journal-stats');

        const useBin = binStats && binStats.source === 'binance';
        const src = useBin ? binStats : stats;
        const pnl = useBin ? src.net_pnl : src.total_pnl;
        const ret = useBin ? src.return_pct : (src.total_return_pct || 0);
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';
        const retClass = ret >= 0 ? 'positive' : 'negative';
        const label = useBin ? `Binance ${src.days}d` : 'Local';

        let cards = `
            <div class="stat-card">
                <div class="stat-label">Trades</div>
                <div class="stat-value">${useBin ? src.total_trades : src.total}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Win Rate</div>
                <div class="stat-value ${src.win_rate >= 50 ? 'positive' : 'negative'}">${src.win_rate}%</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">PnL</div>
                <div class="stat-value ${pnlClass}">${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Return</div>
                <div class="stat-value ${retClass}">${ret >= 0 ? '+' : ''}${ret}%</div>
            </div>`;

        if (useBin) {
            cards += `
            <div class="stat-card">
                <div class="stat-label">Fees</div>
                <div class="stat-value negative">$${Math.abs(src.total_fee).toFixed(2)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Funding</div>
                <div class="stat-value">${src.total_funding >= 0 ? '+' : ''}$${src.total_funding.toFixed(2)}</div>
            </div>`;
        } else {
            cards += `
            <div class="stat-card">
                <div class="stat-label">Best / Worst</div>
                <div class="stat-value"><span class="positive">+${stats.best_trade}</span> / <span class="negative">${stats.worst_trade}</span></div>
            </div>`;
        }

        cards += `<div class="stat-card"><div class="stat-label">Source</div><div class="stat-value" style="font-size:11px">${label}</div></div>`;

        el.innerHTML = cards;
    }

    async function loadDailyStats() {
        try {
            const resp = await fetch('/api/trades/daily');
            const data = await resp.json();
            renderDailyStats(data);
        } catch (e) {}
    }

    function renderDailyStats(data) {
        let container = document.getElementById('daily-stats');
        if (!container) {
            container = document.createElement('div');
            container.id = 'daily-stats';
            container.style.marginTop = '12px';
            document.getElementById('ptab-journal').appendChild(container);
        }
        if (!data.length) {
            container.innerHTML = '';
            return;
        }
        container.innerHTML = `
            <h4 style="color:#848e9c;font-size:11px;text-transform:uppercase;margin-bottom:6px">Daily P&L</h4>
            <table>
                <thead><tr><th>Date</th><th>Trades</th><th>Wins</th><th>PnL</th></tr></thead>
                <tbody>
                    ${data.map(d => {
                        const cl = d.pnl >= 0 ? 'positive' : 'negative';
                        return `<tr><td>${d.date}</td><td>${d.trades}</td><td>${d.wins}</td>
                                <td class="${cl}">${d.pnl >= 0 ? '+' : ''}$${Math.abs(d.pnl).toFixed(2)}</td></tr>`;
                    }).join('')}
                </tbody>
            </table>`;
    }

    async function loadSymbolStats() {
        try {
            const resp = await fetch('/api/trades/by_symbol');
            const data = await resp.json();
            renderSymbolStats(data);
        } catch (e) {}
    }

    function renderSymbolStats(data) {
        let container = document.getElementById('symbol-stats');
        if (!container) {
            container = document.createElement('div');
            container.id = 'symbol-stats';
            container.style.marginTop = '12px';
            document.getElementById('ptab-journal').appendChild(container);
        }
        if (!data.length) {
            container.innerHTML = '';
            return;
        }
        container.innerHTML = `
            <h4 style="color:#848e9c;font-size:11px;text-transform:uppercase;margin-bottom:6px">By Symbol</h4>
            <table>
                <thead><tr><th>Symbol</th><th>Trades</th><th>Win%</th><th>Avg PnL%</th><th>Total PnL</th></tr></thead>
                <tbody>
                    ${data.map(d => {
                        const cl = d.pnl >= 0 ? 'positive' : 'negative';
                        return `<tr><td><strong>${d.symbol.replace('USDT','')}</strong></td><td>${d.trades}</td>
                                <td>${d.win_rate}%</td><td>${d.avg_pnl_pct}%</td>
                                <td class="${cl}">${d.pnl >= 0 ? '+' : ''}$${Math.abs(d.pnl).toFixed(2)}</td></tr>`;
                    }).join('')}
                </tbody>
            </table>`;
    }

    async function loadEquityCurve() {
        try {
            const resp = await fetch('/api/trades/equity_curve');
            const data = await resp.json();
            renderEquityCurve(data);
        } catch (e) {
            console.error('Equity curve error:', e);
        }
    }

    function renderEquityCurve(data) {
        const container = document.getElementById('equity-chart');
        if (data.length < 2) {
            container.innerHTML = '<div class="empty-msg">No trades yet for equity curve</div>';
            return;
        }

        if (equityChart) {
            equityChart.remove();
        }

        equityChart = LightweightCharts.createChart(container, {
            layout: {
                background: { type: 'solid', color: '#0b0e11' },
                textColor: '#848e9c',
            },
            grid: {
                vertLines: { color: '#1e2329' },
                horzLines: { color: '#1e2329' },
            },
            timeScale: { borderColor: '#2b3139', timeVisible: true },
            rightPriceScale: { borderColor: '#2b3139' },
            width: container.clientWidth,
            height: container.clientHeight || 200,
        });

        const series = equityChart.addAreaSeries({
            topColor: 'rgba(240, 185, 11, 0.4)',
            bottomColor: 'rgba(240, 185, 11, 0.0)',
            lineColor: '#f0b90b',
            lineWidth: 2,
        });

        const chartData = data.filter(d => d.time > 0).map(d => ({
            time: Math.floor(d.time / 1000),
            value: d.equity,
        }));

        if (chartData.length > 0) {
            series.setData(chartData);
            equityChart.timeScale().fitContent();
        }
    }

    return { init, refresh };
})();
