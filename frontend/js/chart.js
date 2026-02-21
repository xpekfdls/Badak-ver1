const ChartManager = (() => {
    let chart = null;
    let candleSeries = null;
    let volumeSeries = null;
    let currentSymbol = '';
    let chartTf = '5m';
    let priceLines = [];
    let markers = [];
    let rawData = [];
    let _currentPosition = null;
    let _klineWs = null;
    let _klineReconnectTimer = null;

    const indicatorSeries = {};
    let activeIndicators = new Set(['sma']);

    const INDICATOR_COLORS = {
        sma7: '#f0b90b',
        sma25: '#1e80ff',
        sma99: '#e040fb',
        ema7: '#f0b90b',
        ema25: '#1e80ff',
        ema99: '#e040fb',
        bb_upper: 'rgba(100,181,246,0.5)',
        bb_middle: 'rgba(100,181,246,0.3)',
        bb_lower: 'rgba(100,181,246,0.5)',
    };

    const BARS_FOR_TF = {
        '1m': 500, '5m': 500, '15m': 500,
        '1h': 720, '4h': 500, '1d': 365,
    };

    function init(container) {
        if (!container) return;
        chart = LightweightCharts.createChart(container, {
            layout: {
                background: { type: 'solid', color: '#0b0e11' },
                textColor: '#848e9c',
            },
            grid: {
                vertLines: { color: '#1e2329' },
                horzLines: { color: '#1e2329' },
            },
            crosshair: {
                mode: LightweightCharts.CrosshairMode.Normal,
                vertLine: { color: '#363c45', labelBackgroundColor: '#2b3139' },
                horzLine: { color: '#363c45', labelBackgroundColor: '#2b3139' },
            },
            timeScale: {
                borderColor: '#2b3139',
                timeVisible: true,
                secondsVisible: false,
            },
            rightPriceScale: { borderColor: '#2b3139' },
        });

        candleSeries = chart.addCandlestickSeries({
            upColor: '#0ecb81',
            downColor: '#f6465d',
            borderUpColor: '#0ecb81',
            borderDownColor: '#f6465d',
            wickUpColor: '#0ecb81',
            wickDownColor: '#f6465d',
        });

        volumeSeries = chart.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: 'volume',
        });
        chart.priceScale('volume').applyOptions({
            scaleMargins: { top: 0.85, bottom: 0 },
        });

        new ResizeObserver(() => {
            if (chart && container.clientWidth > 0) {
                chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
            }
        }).observe(container);

        _setupChartTfButtons();
        _setupIndicatorButtons();
    }

    // --- Chart TF buttons ---
    function _setupChartTfButtons() {
        const btns = document.querySelectorAll('.ctf-btn');
        btns.forEach(btn => {
            btn.addEventListener('click', () => {
                const tf = btn.dataset.tf;
                if (tf === chartTf) return;
                document.querySelectorAll('.ctf-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                chartTf = tf;
                if (currentSymbol) loadSymbol(currentSymbol, tf);
            });
        });
    }

    // --- Indicator buttons ---
    function _setupIndicatorButtons() {
        const btns = document.querySelectorAll('.ci-btn');
        btns.forEach(btn => {
            btn.addEventListener('click', () => {
                const ind = btn.dataset.ind;
                if (activeIndicators.has(ind)) {
                    activeIndicators.delete(ind);
                    btn.classList.remove('active');
                } else {
                    activeIndicators.add(ind);
                    btn.classList.add('active');
                }
                _renderIndicators();
            });
        });
    }

    // --- Load symbol ---
    async function loadSymbol(symbol, tf) {
        if (!chart || !candleSeries) return;
        currentSymbol = symbol;
        if (tf) chartTf = tf;
        document.querySelectorAll('.ctf-btn').forEach(b => {
            b.classList.toggle('active', b.dataset.tf === chartTf);
        });

        const bars = BARS_FOR_TF[chartTf] || 500;

        try {
            const resp = await fetch(`/api/chart/${symbol}?timeframe=${chartTf}&bars=${bars}`);
            if (!resp.ok) {
                console.error('[Chart] API error:', resp.status);
                return;
            }
            const data = await resp.json();
            if (!Array.isArray(data) || !data.length) return;
            rawData = data;
            clearAnnotations();

            const samplePrice = data[data.length - 1].close;
            const precision = _getPrecision(samplePrice);
            const minMove = parseFloat((1 / Math.pow(10, precision)).toFixed(precision));
            candleSeries.applyOptions({
                priceFormat: { type: 'price', precision: precision, minMove: minMove },
            });

            candleSeries.setData(data.map(d => ({
                time: d.time, open: d.open, high: d.high, low: d.low, close: d.close,
            })));

            if (volumeSeries) {
                volumeSeries.setData(data.map(d => ({
                    time: d.time,
                    value: d.volume,
                    color: d.close >= d.open ? 'rgba(14,203,129,0.3)' : 'rgba(246,70,93,0.3)',
                })));
            }

            const mkrs = [];
            data.forEach(d => {
                if (d.bear_new && d.bear_tick >= 1) {
                    mkrs.push({
                        time: d.time,
                        position: 'aboveBar',
                        color: d.bear_tick >= 3 ? '#f6465d' : '#f0b90b',
                        shape: 'circle',
                        text: `${d.bear_tick}T▼`,
                    });
                }
                if (d.bull_new && d.bull_tick >= 1) {
                    mkrs.push({
                        time: d.time,
                        position: 'belowBar',
                        color: d.bull_tick >= 3 ? '#0ecb81' : '#9b59b6',
                        shape: 'circle',
                        text: `${d.bull_tick}T▲`,
                    });
                }
            });
            mkrs.sort((a, b) => a.time - b.time);
            candleSeries.setMarkers(mkrs);
            markers = mkrs;

            _renderIndicators();

            const last = data[data.length - 1];
            _updateChartHeader(symbol, last.close, data);
            chart.timeScale().fitContent();

            if (_currentPosition && _currentPosition.symbol === symbol) {
                _redrawPositionLines();
            }

            _load24hTicker(symbol);
            _connectKlineStream(symbol, chartTf);
        } catch (e) {
            console.error('[Chart] Load error:', e);
        }
    }

    // --- 24h ticker ---
    async function _load24hTicker(symbol) {
        try {
            const resp = await fetch(`/api/ticker/${symbol}`);
            const d = await resp.json();
            document.getElementById('h24-high').textContent = _formatPrice(d.high);
            document.getElementById('h24-low').textContent = _formatPrice(d.low);
            document.getElementById('h24-vol').textContent = _formatVolume(d.volume);

            const changeEl = document.getElementById('chart-change');
            changeEl.textContent = `${d.change_pct >= 0 ? '+' : ''}${d.change_pct.toFixed(2)}%`;
            changeEl.className = 'chart-change ' + (d.change_pct >= 0 ? 'positive' : 'negative');
        } catch (e) {}
    }

    // --- Kline WebSocket for real-time chart updates ---
    const TF_MAP = { '1m': '1m', '5m': '5m', '15m': '15m', '1h': '1h', '4h': '4h', '1d': '1d' };

    function _disconnectKlineStream() {
        if (_klineReconnectTimer) { clearTimeout(_klineReconnectTimer); _klineReconnectTimer = null; }
        if (_klineWs) {
            _klineWs.onclose = null;
            _klineWs.close();
            _klineWs = null;
        }
    }

    function _connectKlineStream(symbol, tf) {
        _disconnectKlineStream();
        const stream = `${symbol.toLowerCase()}@kline_${TF_MAP[tf] || tf}`;
        const url = `wss://fstream.binance.com/ws/${stream}`;

        try {
            _klineWs = new WebSocket(url);
        } catch (e) {
            console.warn('[Chart] Kline WS connect failed:', e);
            return;
        }

        _klineWs.onmessage = (evt) => {
            try {
                const msg = JSON.parse(evt.data);
                if (msg.e !== 'kline') return;
                const k = msg.k;
                if (k.s !== symbol || k.i !== (TF_MAP[tf] || tf)) return;

                const bar = {
                    time: Math.floor(k.t / 1000),
                    open: parseFloat(k.o),
                    high: parseFloat(k.h),
                    low: parseFloat(k.l),
                    close: parseFloat(k.c),
                };
                const vol = parseFloat(k.v);

                candleSeries.update(bar);
                if (volumeSeries) {
                    volumeSeries.update({
                        time: bar.time,
                        value: vol,
                        color: bar.close >= bar.open ? 'rgba(14,203,129,0.3)' : 'rgba(246,70,93,0.3)',
                    });
                }

                _updateChartHeader(symbol, bar.close, null);

                if (k.x) {
                    const last = rawData[rawData.length - 1];
                    if (last && last.time === bar.time) {
                        Object.assign(last, bar, { volume: vol });
                    } else {
                        rawData.push({ ...bar, volume: vol });
                    }
                } else {
                    const last = rawData[rawData.length - 1];
                    if (last && last.time === bar.time) {
                        last.open = bar.open; last.high = bar.high;
                        last.low = bar.low; last.close = bar.close;
                        last.volume = vol;
                    }
                }
            } catch (e) {}
        };

        _klineWs.onclose = () => {
            _klineReconnectTimer = setTimeout(() => {
                if (currentSymbol === symbol && chartTf === tf) {
                    _connectKlineStream(symbol, tf);
                }
            }, 3000);
        };

        _klineWs.onerror = () => {};
    }

    function _getPrecision(price) {
        if (price >= 1000) return 2;
        if (price >= 100) return 3;
        if (price >= 1) return 4;
        if (price >= 0.01) return 5;
        if (price >= 0.0001) return 6;
        return 8;
    }

    function _formatPrice(p) {
        return p.toFixed(_getPrecision(p));
    }

    function _formatVolume(v) {
        if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B';
        if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
        if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
        return v.toFixed(0);
    }

    function _updateChartHeader(symbol, price, data) {
        document.getElementById('chart-symbol').textContent = symbol;
        document.getElementById('chart-price').textContent = _formatPrice(price);
    }

    // --- Indicators ---
    function _renderIndicators() {
        if (!chart || !rawData.length) return;
        try {
            _clearIndicatorSeries();

            const closes = rawData.map(d => d.close);
            const times = rawData.map(d => d.time);

            if (activeIndicators.has('sma')) {
                _addMA(times, closes, 7, INDICATOR_COLORS.sma7, 'SMA 7');
                _addMA(times, closes, 25, INDICATOR_COLORS.sma25, 'SMA 25');
                _addMA(times, closes, 99, INDICATOR_COLORS.sma99, 'SMA 99');
            }

            if (activeIndicators.has('ema')) {
                _addEMA(times, closes, 7, INDICATOR_COLORS.ema7, 'EMA 7');
                _addEMA(times, closes, 25, INDICATOR_COLORS.ema25, 'EMA 25');
                _addEMA(times, closes, 99, INDICATOR_COLORS.ema99, 'EMA 99');
            }

            if (activeIndicators.has('bb')) {
                _addBollingerBands(times, closes, 20, 2);
            }
        } catch (e) {
            console.error('[Chart] Indicator render error:', e);
        }
    }

    function _clearIndicatorSeries() {
        for (const key of Object.keys(indicatorSeries)) {
            try { chart.removeSeries(indicatorSeries[key]); } catch (e) {
                console.warn('[Chart] removeSeries failed for', key, e.message);
            }
            delete indicatorSeries[key];
        }
    }

    function _addMA(times, closes, period, color, title) {
        const vals = _calcSMA(closes, period);
        const data = [];
        for (let i = period - 1; i < closes.length; i++) {
            if (vals[i] !== null) data.push({ time: times[i], value: vals[i] });
        }
        if (!data.length) return;

        const s = chart.addLineSeries({
            color: color,
            lineWidth: 1,
            title: title,
            priceLineVisible: false,
            lastValueVisible: false,
        });
        s.setData(data);
        indicatorSeries[title] = s;
    }

    function _addEMA(times, closes, period, color, title) {
        const vals = _calcEMA(closes, period);
        const data = [];
        for (let i = period - 1; i < closes.length; i++) {
            if (vals[i] !== null) data.push({ time: times[i], value: vals[i] });
        }
        if (!data.length) return;

        const s = chart.addLineSeries({
            color: color,
            lineWidth: 1,
            title: title,
            priceLineVisible: false,
            lastValueVisible: false,
        });
        s.setData(data);
        indicatorSeries[title] = s;
    }

    function _addBollingerBands(times, closes, period, mult) {
        const sma = _calcSMA(closes, period);
        const upper = [], lower = [], mid = [];

        for (let i = period - 1; i < closes.length; i++) {
            if (sma[i] === null) continue;
            let sumSq = 0;
            for (let j = i - period + 1; j <= i; j++) {
                sumSq += (closes[j] - sma[i]) ** 2;
            }
            const std = Math.sqrt(sumSq / period);
            upper.push({ time: times[i], value: sma[i] + mult * std });
            mid.push({ time: times[i], value: sma[i] });
            lower.push({ time: times[i], value: sma[i] - mult * std });
        }

        if (!upper.length) return;

        const sUp = chart.addLineSeries({
            color: INDICATOR_COLORS.bb_upper, lineWidth: 1, title: 'BB Upper',
            priceLineVisible: false, lastValueVisible: false,
        });
        sUp.setData(upper);
        indicatorSeries['BB Upper'] = sUp;

        const sMid = chart.addLineSeries({
            color: INDICATOR_COLORS.bb_middle, lineWidth: 1, title: 'BB Mid',
            lineStyle: LightweightCharts.LineStyle.Dashed,
            priceLineVisible: false, lastValueVisible: false,
        });
        sMid.setData(mid);
        indicatorSeries['BB Mid'] = sMid;

        const sLow = chart.addLineSeries({
            color: INDICATOR_COLORS.bb_lower, lineWidth: 1, title: 'BB Lower',
            priceLineVisible: false, lastValueVisible: false,
        });
        sLow.setData(lower);
        indicatorSeries['BB Lower'] = sLow;
    }

    // --- Math helpers ---
    function _calcSMA(data, period) {
        const result = new Array(data.length).fill(null);
        if (data.length < period) return result;
        let sum = 0;
        for (let i = 0; i < period; i++) sum += data[i];
        result[period - 1] = sum / period;
        for (let i = period; i < data.length; i++) {
            sum += data[i] - data[i - period];
            result[i] = sum / period;
        }
        return result;
    }

    function _calcEMA(data, period) {
        const result = new Array(data.length).fill(null);
        if (data.length < period) return result;
        let sum = 0;
        for (let i = 0; i < period; i++) sum += data[i];
        result[period - 1] = sum / period;
        const k = 2 / (period + 1);
        for (let i = period; i < data.length; i++) {
            result[i] = data[i] * k + result[i - 1] * (1 - k);
        }
        return result;
    }

    // --- Price lines ---
    function clearAnnotations() {
        priceLines.forEach(pl => {
            try { candleSeries.removePriceLine(pl); } catch(e) {}
        });
        priceLines = [];
    }

    function addPriceLine(price, color, title, style) {
        const lineStyle = style || LightweightCharts.LineStyle.Dashed;
        const pl = candleSeries.createPriceLine({
            price: price, color: color, lineWidth: 1, lineStyle: lineStyle,
            axisLabelVisible: true, title: title,
        });
        priceLines.push(pl);
        return pl;
    }

    function drawPositionLines(pos) {
        _currentPosition = pos;
        _redrawPositionLines();
    }

    function _redrawPositionLines() {
        clearAnnotations();
        const pos = _currentPosition;
        if (!pos) return;

        addPriceLine(pos.entry_price, '#0ecb81', 'Entry', LightweightCharts.LineStyle.Dotted);

        if (pos.avg_price !== pos.entry_price) {
            addPriceLine(pos.avg_price, '#eaecef', 'Avg', LightweightCharts.LineStyle.Solid);
        }

        const slPct = pos._sl_pct || 5.0;
        const isLong = pos.direction === 'LONG';
        const slPrice = isLong
            ? pos.avg_price * (1 - slPct / 100)
            : pos.avg_price * (1 + slPct / 100);
        addPriceLine(slPrice, '#f6465d', `SL ${slPct}%`, LightweightCharts.LineStyle.Dashed);

        const trailAct = pos._trail_act_pct || 1.0;
        const trailPrice = isLong
            ? pos.avg_price * (1 + trailAct / 100)
            : pos.avg_price * (1 - trailAct / 100);
        addPriceLine(trailPrice, '#f0b90b', `Trail ${trailAct}%`, LightweightCharts.LineStyle.Dotted);

        if (pos.trail_active && pos.peak_price) {
            const trailDist = pos._trail_dist_pct || 0.5;
            const trailStop = isLong
                ? pos.peak_price * (1 - trailDist / 100)
                : pos.peak_price * (1 + trailDist / 100);
            addPriceLine(trailStop, '#f0b90b', 'Trail Stop', LightweightCharts.LineStyle.Solid);
        }
    }

    function getCurrentSymbol() { return currentSymbol; }
    function getChartTf() { return chartTf; }

    function resize() {
        if (chart) {
            const container = document.getElementById('chart-container');
            chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
        }
    }

    return {
        init, loadSymbol, drawPositionLines, clearAnnotations,
        addPriceLine, getCurrentSymbol, getChartTf, resize,
    };
})();
