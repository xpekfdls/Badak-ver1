const Signals = (() => {
    let signals = [];
    const dismissed = new Set();

    function init() {
        loadInitial();
    }

    async function loadInitial() {
        try {
            const resp = await fetch('/api/signals?limit=50');
            const data = await resp.json();
            signals = data.reverse();
            renderAll();
        } catch (e) {
            console.error('Signal load error:', e);
        }
    }

    function addSignal(signal) {
        signals.unshift(signal);
        if (signals.length > 200) signals = signals.slice(0, 200);
        renderAll();
        playNotification(signal);
    }

    function dismiss(id) {
        dismissed.add(id);
        const card = document.querySelector(`.signal-card[data-id="${id}"]`);
        if (card) card.remove();
        signals = signals.filter(s => s.id !== id);
        document.getElementById('signal-count').textContent = signals.length;
    }

    function renderAll() {
        const feed = document.getElementById('signal-feed');
        const count = document.getElementById('signal-count');
        const visible = signals.filter(s => !dismissed.has(s.id));
        count.textContent = visible.length;

        feed.innerHTML = visible.map(s => renderCard(s)).join('');

        feed.querySelectorAll('.signal-card').forEach((card, i) => {
            card.addEventListener('click', (e) => {
                if (e.target.tagName === 'BUTTON') return;
                ChartManager.loadSymbol(visible[i].symbol, visible[i].timeframe);
            });
        });
    }

    function renderCard(s) {
        const dirClass = s.direction === 'LONG' ? 'long' : 'short';
        const time = new Date(s.timestamp * 1000).toLocaleTimeString('ko-KR');
        const scenarios = s.scenarios || [];
        const totalWidth = 100;

        const barHtml = scenarios.map(sc => {
            const w = Math.max(sc.pct * totalWidth / 100, 2);
            return `<div class="scenario-bar ${sc.scenario}" style="width:${w}px" title="${sc.scenario}: ${sc.pct}%"></div>`;
        }).join('');

        const scenarioText = scenarios.map(sc =>
            `${sc.scenario}:${sc.pct}%`
        ).join(' ');

        const buyMode = document.getElementById('mode-select').value;
        let actionHtml = '';
        if (buyMode === 'semi') {
            actionHtml = `
                <div class="signal-actions">
                    <button class="btn-accept" onclick="Signals.acceptSignal('${s.id}', '${s.symbol}', '${s.direction}', ${s.price})">Buy</button>
                    <button class="btn-reject" onclick="Signals.dismiss('${s.id}')">Skip</button>
                </div>`;
        }

        return `
            <div class="signal-card ${dirClass}" data-id="${s.id}">
                <div class="signal-header">
                    <span class="signal-symbol">${s.symbol}</span>
                    <span class="signal-dir ${dirClass}">${s.direction} ${s.tick_count}T</span>
                </div>
                <div class="signal-info">
                    <span>${s.price}</span>
                    <span>${time}</span>
                    <span>${s.timeframe}</span>
                    <span>${s.total_cases} cases</span>
                </div>
                <div class="signal-scenarios">${barHtml}</div>
                <div style="font-size:10px;color:#848e9c;margin-top:2px">${scenarioText}</div>
                ${actionHtml}
            </div>`;
    }

    async function acceptSignal(id, symbol, direction, price) {
        const amount = parseFloat(document.getElementById('order-amount').value) || 100;
        try {
            const resp = await fetch('/api/order/open', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol, direction, usdt_amount: amount, price }),
            });
            if (resp.ok) {
                App.toast(`${direction} ${symbol} opened via signal`, 'success');
                dismiss(id);
                Positions.refresh();
            } else {
                const err = await resp.json();
                App.toast(err.detail || 'Order failed', 'error');
            }
        } catch (e) {
            App.toast('Order failed', 'error');
        }
    }

    function playNotification(signal) {
        try {
            const ctx = new (window.AudioContext || window.webkitAudioContext)();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.value = signal.direction === 'LONG' ? 880 : 660;
            gain.gain.value = 0.3;
            osc.start();
            osc.stop(ctx.currentTime + 0.15);
            setTimeout(() => {
                const osc2 = ctx.createOscillator();
                const gain2 = ctx.createGain();
                osc2.connect(gain2);
                gain2.connect(ctx.destination);
                osc2.frequency.value = signal.direction === 'LONG' ? 1100 : 440;
                gain2.gain.value = 0.3;
                osc2.start();
                osc2.stop(ctx.currentTime + 0.2);
            }, 180);
        } catch (e) {}

        if (Notification.permission === 'granted') {
            new Notification(`Signal: ${signal.symbol} ${signal.direction} ${signal.tick_count}T`, {
                body: `Price: ${signal.price}`,
            });
        } else if (Notification.permission !== 'denied') {
            Notification.requestPermission();
        }
    }

    return { init, addSignal, acceptSignal, dismiss };
})();
