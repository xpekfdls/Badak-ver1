const Positions = (() => {
    let positions = [];
    let refreshInterval = null;
    let _lastUpdate = 0;
    let _tickTimer = null;

    function init() {
        refresh();
        refreshInterval = setInterval(refresh, 3000);
        _tickTimer = setInterval(_updateTimerBar, 500);
    }

    async function refresh() {
        try {
            const resp = await fetch('/api/positions');
            positions = await resp.json();
            _lastUpdate = Date.now();
            render();
            _syncChartLines();
        } catch (e) {
            console.error('Position refresh error:', e);
        }
    }

    function _updateTimerBar() {
        const el = document.getElementById('pos-update-bar');
        if (!el || !_lastUpdate) return;
        const ago = ((Date.now() - _lastUpdate) / 1000).toFixed(1);
        const next = Math.max(0, 3 - parseFloat(ago)).toFixed(1);
        el.textContent = `updated ${ago}s ago · next in ${next}s`;
    }

    function render() {
        const tbody = document.getElementById('positions-body');
        const empty = document.getElementById('positions-empty');
        const countEl = document.getElementById('pos-count');

        countEl.textContent = positions.length;

        if (!positions.length) {
            tbody.innerHTML = '';
            empty.classList.remove('hidden');
            return;
        }
        empty.classList.add('hidden');

        tbody.innerHTML = positions.map(p => {
            const isLong = p.direction === 'LONG';
            const pnl = p.unrealized_pnl || 0;
            const pnlClass = pnl >= 0 ? 'positive' : 'negative';
            const markPrice = p.mark_price ? p.mark_price.toFixed(2) : '--';
            const posValue = p.avg_price && p.quantity ? (p.avg_price * p.quantity) : 0;
            const margin = p.leverage ? posValue / p.leverage : posValue;
            const pnlUsdt = p.avg_price && p.quantity
                ? ((pnl / 100) * margin).toFixed(2)
                : '--';

            return `<tr data-id="${p.id}" data-symbol="${p.symbol}">
                <td><strong>${p.symbol}</strong></td>
                <td class="${isLong ? 'positive' : 'negative'}">${p.direction}</td>
                <td title="Margin: $${margin.toFixed(2)}">$${posValue.toFixed(2)}</td>
                <td>${p.quantity}</td>
                <td>${p.avg_price}</td>
                <td>${markPrice}</td>
                <td class="${pnlClass}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)}%</td>
                <td class="${pnlClass}">${pnlUsdt}</td>
                <td>${p.leverage}x</td>
                <td>
                    <button class="btn-scale-in" onclick="Positions.scaleIn('${p.id}')">+</button>
                    <button class="btn-close-pos" onclick="Positions.closePos('${p.id}')">Close</button>
                </td>
            </tr>`;
        }).join('');

        // Click row to show position on chart
        tbody.querySelectorAll('tr').forEach(row => {
            row.addEventListener('click', (e) => {
                if (e.target.tagName === 'BUTTON') return;
                const symbol = row.dataset.symbol;
                const pos = positions.find(p => p.symbol === symbol);
                if (pos) _injectStrategyParams(pos);
                ChartManager.loadSymbol(symbol).then(() => {
                    ChartManager.drawPositionLines(pos);
                });
            });
        });
    }

    async function closePos(posId) {
        if (!confirm('Close this position?')) return;
        try {
            const resp = await fetch(`/api/order/close/${posId}`, { method: 'POST' });
            if (resp.ok) {
                App.toast('Position closed', 'success');
                refresh();
                Journal.refresh();
            }
        } catch (e) {
            App.toast('Close failed', 'error');
        }
    }

    async function scaleIn(posId) {
        const amount = parseFloat(document.getElementById('order-amount').value) || 100;
        try {
            const resp = await fetch(`/api/order/scale_in/${posId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ usdt_amount: amount }),
            });
            if (resp.ok) {
                App.toast('Scale-in executed', 'success');
                refresh();
            } else {
                const err = await resp.json();
                App.toast(err.detail || 'Scale-in failed', 'error');
            }
        } catch (e) {
            App.toast('Scale-in failed', 'error');
        }
    }

    function _injectStrategyParams(pos) {
        pos._sl_pct = parseFloat(document.getElementById('set-sl')?.value) || 5;
        pos._trail_act_pct = parseFloat(document.getElementById('set-trail-act')?.value) || 1;
        pos._trail_dist_pct = parseFloat(document.getElementById('set-trail-dist')?.value) || 0.5;
    }

    function _syncChartLines() {
        const chartSym = ChartManager.getCurrentSymbol();
        if (!chartSym) return;
        const pos = positions.find(p => p.symbol === chartSym && p.status === 'OPEN');
        if (pos) {
            _injectStrategyParams(pos);
            ChartManager.drawPositionLines(pos);
        }
    }

    function getPositions() { return positions; }

    return { init, refresh, render, closePos, scaleIn, getPositions };
})();
