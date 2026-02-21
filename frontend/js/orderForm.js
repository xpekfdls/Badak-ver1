const OrderForm = (() => {
    let orderType = 'market';

    function init() {
        document.querySelectorAll('.btn-order-type').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.btn-order-type').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                orderType = btn.dataset.type;
                document.getElementById('limit-price-row').classList.toggle('hidden', orderType !== 'limit');
            });
        });

        document.querySelectorAll('.btn-size').forEach(btn => {
            btn.addEventListener('click', () => {
                const pct = parseInt(btn.dataset.pct);
                const bal = parseFloat(document.getElementById('balance-val').textContent.replace(/,/g, '')) || 0;
                const amount = (bal * pct / 100).toFixed(2);
                document.getElementById('order-amount').value = amount;
            });
        });

        document.getElementById('btn-long').addEventListener('click', () => placeOrder('LONG'));
        document.getElementById('btn-short').addEventListener('click', () => placeOrder('SHORT'));
    }

    async function placeOrder(direction) {
        const symbol = ChartManager.getCurrentSymbol();
        if (!symbol) {
            App.toast('Select a symbol first', 'error');
            return;
        }

        const amount = parseFloat(document.getElementById('order-amount').value);
        if (!amount || amount < 1) {
            App.toast('Enter a valid amount', 'error');
            return;
        }

        const body = {
            symbol, direction, usdt_amount: amount,
            order_type: orderType.toUpperCase(),
        };
        if (orderType === 'limit') {
            const price = parseFloat(document.getElementById('order-price').value);
            if (!price || price <= 0) {
                App.toast('Enter a valid limit price', 'error');
                return;
            }
            body.price = price;
        }

        try {
            const resp = await fetch('/api/order/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                App.toast(err.detail || 'Order failed', 'error');
                return;
            }
            const pos = await resp.json();
            App.toast(`${direction} ${symbol} opened`, 'success');
            Positions.refresh();
        } catch (e) {
            App.toast('Network error: ' + e.message, 'error');
        }
    }

    return { init };
})();
