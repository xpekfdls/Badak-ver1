const SystemLog = (() => {
    const MAX_DISPLAY = 500;
    let logs = [];
    let filterCat = 'ALL';
    let autoScroll = true;
    let feed = null;

    function init() {
        feed = document.getElementById('log-feed');
        if (!feed) return;

        document.getElementById('log-filter')?.addEventListener('change', (e) => {
            filterCat = e.target.value;
            renderAll();
        });
        document.getElementById('log-clear')?.addEventListener('click', () => {
            logs = [];
            if (feed) feed.innerHTML = '';
        });
        document.getElementById('log-autoscroll')?.addEventListener('change', (e) => {
            autoScroll = e.target.checked;
        });

        loadHistory();
    }

    async function loadHistory() {
        try {
            const resp = await fetch('/api/logs?limit=200');
            if (!resp.ok) return;
            const data = await resp.json();
            logs = data.reverse();
            renderAll();
        } catch (e) {
            console.warn('[Log] Failed to load history:', e);
        }
    }

    function add(entry) {
        logs.push(entry);
        if (logs.length > MAX_DISPLAY) {
            logs = logs.slice(-MAX_DISPLAY);
        }
        if (filterCat === 'ALL' || entry.cat === filterCat) {
            appendRow(entry);
        }
    }

    function renderAll() {
        if (!feed) return;
        feed.innerHTML = '';
        const filtered = filterCat === 'ALL' ? logs : logs.filter(l => l.cat === filterCat);
        const frag = document.createDocumentFragment();
        for (const l of filtered) {
            frag.appendChild(createRow(l));
        }
        feed.appendChild(frag);
        scrollToBottom();
    }

    function appendRow(entry) {
        if (!feed) return;
        const el = createRow(entry);
        feed.appendChild(el);
        if (feed.children.length > MAX_DISPLAY) {
            feed.removeChild(feed.firstChild);
        }
        scrollToBottom();
    }

    function createRow(l) {
        const div = document.createElement('div');
        div.className = 'log-entry';

        const ts = document.createElement('span');
        ts.className = 'log-ts';
        ts.textContent = fmtTime(l.ts);

        const cat = document.createElement('span');
        cat.className = `log-cat ${l.cat}`;
        cat.textContent = l.cat;

        const msg = document.createElement('span');
        msg.className = 'log-msg';

        let text = l.msg || '';
        if (l.data) {
            const extras = [];
            if (l.data.sl_price) extras.push(`SL=$${fmtPrice(l.data.sl_price)}`);
            if (l.data.trail_act_price) extras.push(`TrailAct=$${fmtPrice(l.data.trail_act_price)}`);
            if (l.data.sl_pct) extras.push(`SL=${l.data.sl_pct}%`);
            if (l.data.trail_act_pct) extras.push(`TrailAct=${l.data.trail_act_pct}%`);
            if (l.data.error) extras.push(`err: ${l.data.error}`);
            if (extras.length) text += ` [${extras.join(', ')}]`;
        }
        msg.textContent = text;

        div.append(ts, cat, msg);
        return div;
    }

    function fmtTime(epoch) {
        const d = new Date(epoch * 1000);
        return d.toLocaleTimeString('ko-KR', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    function fmtPrice(p) {
        if (p >= 1000) return p.toFixed(2);
        if (p >= 1) return p.toFixed(4);
        return p.toPrecision(4);
    }

    function scrollToBottom() {
        if (autoScroll && feed) {
            feed.scrollTop = feed.scrollHeight;
        }
    }

    return { init, add };
})();
