const App = (() => {
    let ws = null;
    let reconnectTimer = null;
    let symbols = [];

    async function init() {
        try {
            ChartManager.init(document.getElementById('chart-container'));
        } catch (e) {
            console.error('[App] ChartManager.init failed:', e);
        }

        try {
            OrderForm.init();
            Positions.init();
            Orders.init();
            Signals.init();
            Journal.init();
        } catch (e) {
            console.error('[App] Module init failed:', e);
        }

        setupTabs();
        setupPanelTabs();
        setupLeverageToggle();
        setupModeSelect();
        setupTargetSymbol();
        setupEmergency();
        setupSettings();
        SystemLog.init();

        connectWebSocket();

        await loadAccount().catch(() => {});
        await loadSymbols();

        if (symbols.length > 0) {
            document.getElementById('symbol-select').value = symbols[0];
            ChartManager.loadSymbol(symbols[0]);
        }

        setInterval(loadAccount, 10000);
        setInterval(Positions.refresh, 5000);
        setInterval(updateSymbolChanges, 60000);
    }

    // --- WebSocket ---
    function connectWebSocket() {
        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        ws = new WebSocket(`${proto}://${location.host}/ws`);

        ws.onopen = () => {
            document.getElementById('conn-status').className = 'conn-dot connected';
            document.getElementById('conn-status').title = 'Connected';
            if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
        };

        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                handleWsMessage(msg);
            } catch (err) {}
        };

        ws.onclose = () => {
            document.getElementById('conn-status').className = 'conn-dot disconnected';
            document.getElementById('conn-status').title = 'Disconnected';
            reconnectTimer = setTimeout(connectWebSocket, 3000);
        };

        ws.onerror = () => { ws.close(); };

        setInterval(() => {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    }

    function handleWsMessage(msg) {
        switch (msg.type) {
            case 'signal':
                Signals.addSignal(msg.data);
                break;
            case 'position_opened':
                toast(`Position opened: ${msg.data.symbol} ${msg.data.direction}`, 'success');
                Positions.refresh();
                break;
            case 'position_closed':
                toast(`Position closed: ${msg.data.symbol} (${msg.data.exit_reason})`, 'info');
                Positions.refresh();
                Journal.refresh();
                break;
            case 'position_updated':
                Positions.refresh();
                Journal.refresh();
                break;
            case 'order_submitted':
                toast(`Order submitted: ${msg.data.symbol} ${msg.data.side}`, 'info');
                Orders.refresh();
                break;
            case 'order_update':
                Orders.onOrderUpdate(msg.data);
                if (msg.data.status === 'FILLED') {
                    Positions.refresh();
                }
                break;
            case 'order_canceled':
                toast('Order canceled', 'info');
                Orders.refresh();
                break;
            case 'slippage_warning': {
                const d = msg.data;
                toast(
                    `Slippage warning: ${d.symbol} ${d.slippage_pct.toFixed(3)}% ` +
                    `(expected ${d.expected}, got ${d.actual})`,
                    'error'
                );
                break;
            }
            case 'tf_changed':
                toast(`Signal TF changed: ${msg.data.timeframe}`, 'info');
                break;
            case 'system_log':
                SystemLog.add(msg.data);
                break;
            case 'warning':
                toast(msg.data.message, 'error');
                break;
            case 'emergency_complete':
                toast(`Emergency close: ${msg.data.closed.length} closed, ${msg.data.failed.length} failed`, 'error');
                Positions.refresh();
                break;
        }
    }

    // --- Account ---
    async function loadAccount() {
        try {
            const resp = await fetch('/api/account');
            const data = await resp.json();
            const walletBal = parseFloat(data.balance.totalWalletBalance || 0);
            document.getElementById('balance-val').textContent = walletBal.toLocaleString('en-US', {
                minimumFractionDigits: 2, maximumFractionDigits: 2,
            });
            const upnl = parseFloat(data.balance.unrealizedPnl || 0);
            const seed = parseFloat(data.balance.seedMoney || 0);
            const equity = walletBal + upnl;
            const totalPnl = seed > 0 ? equity - seed : upnl;
            const pnlEl = document.getElementById('unrealized-pnl');
            pnlEl.textContent = `${totalPnl >= 0 ? '+' : ''}${totalPnl.toFixed(2)}`;
            pnlEl.className = 'pnl-val ' + (totalPnl >= 0 ? 'positive' : 'negative');
            const pnlPct = seed > 0 ? (totalPnl / seed * 100).toFixed(2) : '0.00';
            pnlEl.title = `시드: $${seed.toFixed(0)} | 자산: $${equity.toFixed(2)} | 수익률: ${pnlPct}%`;
        } catch (e) {}
    }

    // --- Symbols ---
    async function loadSymbols(retries) {
        retries = retries || 0;
        try {
            const resp = await fetch('/api/state');
            const states = await resp.json();

            if ((!states || states.length === 0) && retries < 20) {
                document.getElementById('symbol-select').innerHTML =
                    '<option value="">Initializing... (' + (retries + 1) + ')</option>';
                await new Promise(r => setTimeout(r, 3000));
                return loadSymbols(retries + 1);
            }

            symbols = states.map(s => s.symbol);

            const select = document.getElementById('symbol-select');
            const fmtOpt = (s) => {
                const st = states.find(x => x.symbol === s);
                const chg = st ? st.change_24h : 0;
                const sign = chg >= 0 ? '+' : '';
                return `<option value="${s}">${s.replace('USDT', '')}  ${sign}${chg.toFixed(1)}%</option>`;
            };
            select.innerHTML = symbols.map(fmtOpt).join('');

            select.addEventListener('change', () => {
                const sym = select.value;
                if (sym) {
                    ChartManager.loadSymbol(sym);
                    const positions = Positions.getPositions();
                    const pos = positions.find(p => p.symbol === sym && p.status === 'OPEN');
                    if (pos) {
                        pos._sl_pct = parseFloat(document.getElementById('set-sl')?.value) || 5;
                        pos._trail_act_pct = parseFloat(document.getElementById('set-trail-act')?.value) || 1;
                        pos._trail_dist_pct = parseFloat(document.getElementById('set-trail-dist')?.value) || 0.5;
                    }
                    ChartManager.drawPositionLines(pos || null);
                }
            });

            const targetSelect = document.getElementById('target-symbol-select');
            const fmtTarget = (s) => {
                const st = states.find(x => x.symbol === s);
                const chg = st ? st.change_24h : 0;
                const sign = chg >= 0 ? '+' : '';
                return `<option value="${s}">${s.replace('USDT', '')}  ${sign}${chg.toFixed(1)}%</option>`;
            };
            targetSelect.innerHTML = '<option value="">All Coins</option>' +
                symbols.map(fmtTarget).join('');

            const settings = await fetch('/api/settings').then(r => r.json());
            if (settings.target_symbol) {
                targetSelect.value = settings.target_symbol;
            }
        } catch (e) {}
    }

    async function updateSymbolChanges() {
        try {
            const states = await fetch('/api/state').then(r => r.json());
            const select = document.getElementById('symbol-select');
            const curSym = select.value;
            const targetSelect = document.getElementById('target-symbol-select');
            const curTarget = targetSelect.value;

            const fmtOpt = (sym) => {
                const st = states.find(x => x.symbol === sym);
                const chg = st ? st.change_24h : 0;
                const sign = chg >= 0 ? '+' : '';
                return `<option value="${sym}">${sym.replace('USDT', '')}  ${sign}${chg.toFixed(1)}%</option>`;
            };

            select.innerHTML = symbols.map(fmtOpt).join('');
            select.value = curSym;

            targetSelect.innerHTML = '<option value="">All Coins</option>' +
                symbols.map(fmtOpt).join('');
            targetSelect.value = curTarget;
        } catch (e) {}
    }

    // --- Tabs ---
    function setupTabs() {
        document.querySelectorAll('.order-tabs .tab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.order-tabs .tab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');
            });
        });
    }

    function setupPanelTabs() {
        document.querySelectorAll('.ptab').forEach(tab => {
            tab.addEventListener('click', () => {
                document.querySelectorAll('.ptab').forEach(t => t.classList.remove('active'));
                document.querySelectorAll('.ptab-content').forEach(c => c.classList.remove('active'));
                tab.classList.add('active');
                document.getElementById(`ptab-${tab.dataset.ptab}`).classList.add('active');
                if (tab.dataset.ptab === 'journal') Journal.refresh();
            });
        });
    }

    // --- Leverage Quick Toggle ---
    function setupLeverageToggle() {
        document.querySelectorAll('.btn-leverage-quick').forEach(btn => {
            btn.addEventListener('click', async () => {
                const lev = parseInt(btn.dataset.lev);
                const symbol = ChartManager.getCurrentSymbol();
                if (!symbol) { toast('Select a symbol first', 'error'); return; }

                try {
                    await fetch(`/api/leverage/${symbol}/${lev}`, { method: 'POST' });
                    document.querySelectorAll('.btn-leverage-quick').forEach(b => b.classList.remove('active'));
                    btn.classList.add('active');
                    toast(`Leverage set to ${lev}x for ${symbol}`, 'success');
                } catch (e) {
                    toast('Failed to set leverage', 'error');
                }
            });
        });

        fetch('/api/settings').then(r => r.json()).then(s => {
            const lev = s.leverage || 10;
            document.querySelectorAll('.btn-leverage-quick').forEach(b => {
                b.classList.toggle('active', parseInt(b.dataset.lev) === lev);
            });
        });
    }

    // --- Mode ---
    function setupModeSelect() {
        const select = document.getElementById('mode-select');
        fetch('/api/settings').then(r => r.json()).then(settings => {
            if (settings.buy_mode) select.value = settings.buy_mode;
        });
        select.addEventListener('change', () => {
            fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: 'buy_mode', value: select.value }),
            });
            toast(`Mode: ${select.value}`, 'info');
        });
    }

    // --- Target Symbol ---
    function setupTargetSymbol() {
        const select = document.getElementById('target-symbol-select');
        select.addEventListener('change', async () => {
            const sym = select.value;
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: 'target_symbol', value: sym }),
            });
            toast(sym ? `Target: ${sym}` : 'Target: All Coins', 'info');
        });
    }

    // --- Emergency Close All ---
    function setupEmergency() {
        const btn = document.getElementById('btn-emergency');
        btn.addEventListener('click', async () => {
            if (!confirm('ALL positions will be closed immediately.\nAre you sure?')) return;
            btn.disabled = true;
            btn.textContent = 'CLOSING...';
            try {
                const resp = await fetch('/api/emergency/close_all', { method: 'POST' });
                const result = await resp.json();
                toast(`Emergency: ${result.closed?.length || 0} closed, ${result.failed?.length || 0} failed`, 'error');
                Positions.refresh();
                document.getElementById('mode-select').value = 'manual';
            } catch (e) {
                toast('Emergency close failed!', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'EMERGENCY';
            }
        });
    }

    // --- Signal Timeframe (no longer affects chart) ---
    document.getElementById('tf-select').addEventListener('change', async function() {
        const tf = this.value;
        await fetch(`/api/timeframe/${tf}`, { method: 'POST' });
    });

    // --- Settings ---
    function setupSettings() {
        fetch('/api/settings').then(r => r.json()).then(s => {
            if (s.position_size_pct) document.getElementById('set-pos-size').value = s.position_size_pct;
            if (s.max_open_positions) document.getElementById('set-max-pos').value = s.max_open_positions;
            if (s.max_entries) document.getElementById('set-max-entries').value = s.max_entries;
            if (s.scale_multiplier) document.getElementById('set-scale-mult').value = s.scale_multiplier;
            if (s.trail_activation_pct) document.getElementById('set-trail-act').value = s.trail_activation_pct;
            if (s.trail_distance_pct) document.getElementById('set-trail-dist').value = s.trail_distance_pct;
            if (s.sl_pct) document.getElementById('set-sl').value = s.sl_pct;
            if (s.cycle_mode !== undefined) document.getElementById('set-cycle').checked = !!s.cycle_mode;
            if (s.cycle_sell_pct) document.getElementById('set-cycle-sell').value = s.cycle_sell_pct;
            if (s.seed_money) document.getElementById('set-seed-money').value = s.seed_money;
            if (s.operating_fund_mode) document.getElementById('set-fund-mode').value = s.operating_fund_mode;
            if (s.operating_fund_amount) document.getElementById('set-fund-amount').value = s.operating_fund_amount;
            if (s.entry_interval) document.getElementById('set-entry-interval').value = s.entry_interval;
            if (s.cooldown_seconds !== undefined) document.getElementById('set-cooldown').value = s.cooldown_seconds;

            toggleFundAmountRow(s.operating_fund_mode || 'fixed');
        });

        document.getElementById('set-fund-mode').addEventListener('change', function() {
            toggleFundAmountRow(this.value);
        });

        document.getElementById('btn-save-settings').addEventListener('click', async () => {
            const settings = {
                position_size_pct: document.getElementById('set-pos-size').value,
                max_open_positions: document.getElementById('set-max-pos').value,
                max_entries: document.getElementById('set-max-entries').value,
                scale_multiplier: document.getElementById('set-scale-mult').value,
                trail_activation_pct: document.getElementById('set-trail-act').value,
                trail_distance_pct: document.getElementById('set-trail-dist').value,
                sl_pct: document.getElementById('set-sl').value,
                cycle_mode: document.getElementById('set-cycle').checked ? 'true' : 'false',
                cycle_sell_pct: document.getElementById('set-cycle-sell').value,
                seed_money: document.getElementById('set-seed-money').value,
                operating_fund_mode: document.getElementById('set-fund-mode').value,
                operating_fund_amount: document.getElementById('set-fund-amount').value,
                entry_interval: document.getElementById('set-entry-interval').value,
                cooldown_seconds: document.getElementById('set-cooldown').value,
            };

            try {
                for (const [key, value] of Object.entries(settings)) {
                    await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ key, value }),
                    });
                }
                toast('Settings saved', 'success');
            } catch (e) {
                toast('Failed to save settings', 'error');
            }
        });
    }

    function toggleFundAmountRow(mode) {
        const row = document.getElementById('fund-amount-row');
        if (row) {
            row.style.display = mode === 'fixed' ? '' : 'none';
        }
    }

    // --- Toast ---
    function toast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        const el = document.createElement('div');
        el.className = `toast ${type}`;
        el.textContent = message;
        container.appendChild(el);
        setTimeout(() => el.remove(), 4000);
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;

        switch(e.key.toLowerCase()) {
            case 'b':
                e.preventDefault();
                document.getElementById('btn-long').click();
                break;
            case 's':
                e.preventDefault();
                document.getElementById('btn-short').click();
                break;
        }
    });

    // --- Tooltip system (fixed position, escapes overflow containers) ---
    function setupTooltips() {
        const box = document.getElementById('tooltip-box');
        if (!box) return;

        document.addEventListener('mouseenter', (e) => {
            if (!e.target || !e.target.closest) return;
            const tip = e.target.closest('.tip');
            if (!tip) return;
            const text = tip.getAttribute('data-tip');
            if (!text) return;

            box.textContent = text;
            box.classList.add('visible');

            const rect = tip.getBoundingClientRect();
            const bw = box.offsetWidth;
            const bh = box.offsetHeight;

            let top = rect.top - bh - 6;
            let left = rect.left + rect.width / 2 - bw / 2;

            if (top < 4) {
                top = rect.bottom + 6;
            }
            if (left < 4) left = 4;
            if (left + bw > window.innerWidth - 4) left = window.innerWidth - bw - 4;

            box.style.top = top + 'px';
            box.style.left = left + 'px';
        }, true);

        document.addEventListener('mouseleave', (e) => {
            if (!e.target || !e.target.closest) return;
            if (e.target.closest('.tip')) {
                box.classList.remove('visible');
            }
        }, true);
    }

    document.addEventListener('DOMContentLoaded', () => {
        init().catch(e => console.error('[App] init error:', e));
        setupTooltips();
    });

    return { toast };
})();
