const Orders = (() => {
    let activeOrders = [];
    let refreshInterval = null;

    function init() {
        refresh();
        refreshInterval = setInterval(refresh, 3000);
    }

    async function refresh() {
        try {
            const resp = await fetch('/api/orders/active');
            activeOrders = await resp.json();
            render();
        } catch (e) {
            console.error('Orders refresh error:', e);
        }
    }

    function render() {
        const tbody = document.getElementById('orders-body');
        const empty = document.getElementById('orders-empty');

        if (!activeOrders.length) {
            tbody.innerHTML = '';
            empty.classList.remove('hidden');
            return;
        }
        empty.classList.add('hidden');

        tbody.innerHTML = activeOrders.map(o => {
            const time = new Date(o.created_at).toLocaleTimeString('ko-KR');
            const isBuy = o.side === 'BUY';
            const sideClass = isBuy ? 'positive' : 'negative';
            const fillPct = o.quantity > 0
                ? ((o.filled_qty / o.quantity) * 100).toFixed(1)
                : '0';
            const statusClass = o.status === 'FILLED' ? 'positive' :
                               o.status === 'CANCELED' ? 'negative' :
                               o.status === 'PARTIALLY_FILLED' ? 'positive' : '';
            const slippage = o.slippage_pct || 0;
            const slipClass = slippage > 0.1 ? 'negative' : slippage > 0 ? '' : '';
            const priceStr = o.order_type === 'MARKET' ? 'Market' :
                            o.price ? o.price.toFixed(2) : '--';

            const canCancel = ['NEW', 'PARTIALLY_FILLED', 'PENDING'].includes(o.status);

            return `<tr>
                <td>${time}</td>
                <td><strong>${o.symbol.replace('USDT','')}</strong></td>
                <td class="${sideClass}">${o.side}</td>
                <td>${o.order_type}</td>
                <td>${o.quantity}</td>
                <td>${priceStr}</td>
                <td>${fillPct}%</td>
                <td class="${statusClass}">${o.status}</td>
                <td class="${slipClass}">${slippage > 0 ? slippage.toFixed(3) + '%' : '--'}</td>
                <td>${canCancel
                    ? `<button class="btn-close-pos" onclick="Orders.cancelOrder('${o.id}')">Cancel</button>`
                    : ''}</td>
            </tr>`;
        }).join('');
    }

    async function cancelOrder(orderId) {
        if (!confirm('Cancel this order?')) return;
        try {
            const resp = await fetch(`/api/order/cancel/${orderId}`, { method: 'POST' });
            if (resp.ok) {
                App.toast('Order canceled', 'success');
                refresh();
            } else {
                const err = await resp.json();
                App.toast(err.detail || 'Cancel failed', 'error');
            }
        } catch (e) {
            App.toast('Cancel failed', 'error');
        }
    }

    function onOrderUpdate(order) {
        const idx = activeOrders.findIndex(o => o.id === order.id);
        if (idx >= 0) {
            if (['FILLED', 'CANCELED', 'EXPIRED', 'ERROR'].includes(order.status)) {
                activeOrders.splice(idx, 1);
            } else {
                activeOrders[idx] = order;
            }
        } else if (!['FILLED', 'CANCELED', 'EXPIRED', 'ERROR'].includes(order.status)) {
            activeOrders.unshift(order);
        }
        render();
    }

    return { init, refresh, cancelOrder, onOrderUpdate };
})();
