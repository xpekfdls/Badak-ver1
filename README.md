# Coin Auto Trader

바이낸스 선물 계정과 연동하여, N틱 역추세 시그널을 기반으로 **자동/반자동/수동** 매매를 실행하는 웹 애플리케이션.

이 프로젝트는 "코인 매매 의사결정 지원 시스템"의 **세 번째 모듈**로, [coin-trader](../coin-trader/)(백테스트)와 [coin-monitor](../coin-monitor/)(실시간 모니터)에서 검증된 전략을 **실제 매매**에 적용한다.

---

## 핵심 기능

### 1. 3가지 매매 모드

| 모드 | 매수 | 매도 | 사용 시나리오 |
|------|------|------|-------------|
| **Auto** | 시그널 즉시 시장가 진입 | 트레일링 스탑 자동 청산 | 완전 자동. 자리를 비울 때 |
| **Semi-Auto** | 시그널 카드에서 확인 후 클릭 | 트레일링 스탑 자동 청산 | 진입은 직접 판단, 청산은 자동 |
| **Manual** | 주문 폼에서 직접 입력 | 포지션에서 수동 청산 | 완전 수동. 바이낸스 UI처럼 사용 |

### 2. 자동매매 엔진

- **시그널 감지**: 변동성·거래대금 기준으로 필터링된 코인을 WebSocket 감시 → 3틱 도달 시 시그널 생성
- **코인 필터링**: 일일 변동폭 ≥10%, 24h 거래대금 ≥$100M, 상장 90일 이상 (UI에서 설정 가능)
- **시나리오 분석**: coin-monitor의 DB에서 과거 시나리오(A/B/C/D) 확률 조회
- **자동 진입**: 잔고의 설정 비율(기본 11%)로 시장가 진입
- **배수 물타기**: 추가 틱 발생 시 **실제 포지션 마진 기준** 2배씩 금액 증가하며 추가 진입 (최대 3회)
- **순환매**: 평단 회복 시 50% 매도 → 재하락 시 재진입 반복
- **트레일링 스탑**: 1초마다 가격 체크, 활성화(+1%) 후 최고점 추적, 되돌림(0.5%) 시 청산
- **손절**: 평단 대비 -5% 도달 시 즉시 시장가 청산
- **시그널 알람**: 시그널 발생 시 Web Audio API 비프음 재생 (LONG: 상승음, SHORT: 하강음) + 브라우저 Notification

### 3. 주문 체결 안정성

"fire-and-forget"이 아닌, **이벤트 기반 주문 추적**:

```
주문 발행 → PENDING 상태로 DB 저장
  → Binance User Data Stream이 ORDER_TRADE_UPDATE 수신
  → FILLED: 실제 체결가로 포지션 생성/갱신
  → PARTIALLY_FILLED: 부분 체결 수량만 반영
  → CANCELED/EXPIRED: 주문 상태 정리
```

- **MARKET 주문**: 5초 내 FILLED 미수신 시 REST API로 상태 폴링
- **LIMIT 주문**: 비동기 처리, UI에서 취소 가능
- **STOP_MARKET**: Binance 서버에 상주 (네트워크 끊김에도 동작)
- **슬리피지 감시**: 예상가 vs 체결가 차이 기록, 0.1% 초과 시 경고
- **포지션 싱크**: 30초마다 Binance 실제 포지션과 로컬 DB 비교/보정

### 4. 실시간 시스템 로그

Order/Strategy 패널 옆 **Log 탭**에서 시스템 전체 활동을 실시간 모니터링:

- **SIGNAL**: 시그널 감지 (코인, 방향, 틱 수, 시나리오 확률)
- **DECIDE**: 매수/스킵 판단 근거 (잔고 부족, 쿨다운, 중복 포지션 등)
- **OPEN**: 포지션 진입 상세 (진입가, 수량, 레버리지, 마진, 포지션 가치, SL/Trail 가격)
- **ORDER**: 주문 체결 (체결가, 수량, 체결 금액)
- **TRAIL**: 트레일링 활성화/청산 이벤트
- **STATUS**: 30초마다 포지션 현황 (가격, 최고점, 트레일링 상태, PnL%)
- **CLOSE**: 포지션 청산 (진입/청산가, PnL)
- **WARN**: 슬리피지 경고

카테고리별 색상 구분, 필터링, 자동 스크롤 지원.

### 5. 매매일지 자동 기록

모든 거래가 SQLite에 자동 저장되며, 투자 통계 대시보드 제공:

- **기록 항목**: 진입/청산 시각, 코인, 방향, 레버리지, 진입가, 청산가, 수량, 수수료, PnL, 시그널 정보, 청산사유, 슬리피지
- **통계**: 승률, Profit Factor, 최대 낙폭(MDD), 평균 보유시간
- **시각화**: 누적 수익 곡선 (equity curve)
- **필터**: 코인별, 기간별, 일별 집계
- **시드 머니**: 설정한 초기 자본 대비 수익률 추적

---

## UI 레이아웃

바이낸스 Futures 거래 화면을 최대한 따라한 다크 테마 UI:

```
+----------------------------------------------------------------------+
| [DOT +4.25% v] [5m] [Lev 10x] [Mode: Semi v]  Balance: 1,234 Today: +3.50 |
+----------------------------------------------------------------------+
|                                |                                      |
|    TradingView 캔들 차트 (5m)   |      주문 패널                       |
|    - 진입선 (녹색 점선)          |   [시장가] [지정가]                  |
|    - 손절선 (빨강 점선)          |   가격: [___________]              |
|    - 트레일링선 (노랑)           |   수량: [___________]              |
|    - 실시간 WebSocket 업데이트   |   [LONG 매수]  [SHORT 매도]        |
|                                |   ─────────────────────            |
|                                |   자동매매 설정 패널                 |
+--------------------------------+--------------------------------------+
| 시그널 피드 (최신↑)       | 포지션 (활성)                              |
| SOLUSDT LONG 3T [Buy][Skip] | BTCUSDT LONG x10 Val:$500 +2.34% [청산] |
|  A:58% B:22% C:14%       | 미실현PnL: +$45.67                        |
+----------------------------+-------------------------------------------+
| [포지션] [주문내역] [로그] [매매일지] [통계]                            |
+----------------------------------------------------------------------+
```

### 주요 UI 요소

- **차트**: TradingView Lightweight Charts + SMA/EMA/볼린저 밴드 + 틱 마커 + 진입/SL/트레일링 오버레이
  - **실시간 업데이트**: Binance kline WebSocket 직접 연결로 캔들/거래량 실시간 반영
  - **포지션 라인 유지**: 진입선·손절선·트레일링선이 타임프레임 전환 시에도 유지
  - **기본 타임프레임**: 5분봉 (설정 변경 가능)
- **헤더 Balance/PnL**: 지갑 잔고 + 오늘 실현PnL과 미실현PnL 합산 표시 (hover 시 분리 확인)
- **코인 선택**: 심볼 드롭다운에 24h 등락률 표시 (예: `DOT +4.25%`)
- **주문 폼**: 시장가/지정가, 수량 입력, 잔고 비율 버튼(25%/50%/75%/100%)
- **포지션 패널**: 포지션 가치(Value), 실시간 미실현PnL, 진입가, 마크 가격, 레버리지, 물타기/청산 버튼
- **시그널 피드**: 최신 시그널이 상단에 표시, Skip 버튼으로 시그널 카드 제거, 매수 시 자동 제거
- **시스템 로그**: 실시간 시스템 활동 로그 (카테고리 필터, 자동 스크롤)
- **주문 내역**: 활성 주문 목록 (NEW, PARTIALLY_FILLED), 체결율, 취소 버튼
- **매매일지**: 전체 거래 이력, 통계 카드, equity curve, 일별/코인별 분석
- **설정 패널**: 포지션 크기, 최대 포지션 수, 물타기/순환매 옵션, 트레일링 파라미터, 운용자금 모드, 쿨다운

### 키보드 단축키

- `Esc`: 패널 닫기
- 시그널 알림: 비프음 (LONG 상승음 / SHORT 하강음) + 브라우저 Notification

---

## 프로젝트 구조

```
coin-auto-trader/
├── .env                         # API Key/Secret (gitignore)
├── .env.example                 # 환경변수 템플릿
├── trader.db                    # SQLite (자동 생성, gitignore)
├── backend/
│   ├── main.py                  # FastAPI 서버 (port 8002)
│   ├── config.py                # 전략 파라미터 + Binance 설정 + 매매 기본값
│   ├── requirements.txt
│   ├── strategy/
│   │   ├── tick_counter.py      # 틱 카운팅 엔진 (coin-monitor와 동일)
│   │   ├── signal_detector.py   # 실시간 시그널 감지 + 시나리오 조회
│   │   └── scenario.py          # 시나리오 분류 (A/B/C/D)
│   ├── trading/
│   │   ├── binance_account.py   # Binance Futures API 래퍼 (잔고/주문/레버리지)
│   │   ├── order_manager.py     # 주문 실행 + 관리 (진입/청산/물타기/순환매)
│   │   ├── order_tracker.py     # User Data Stream 기반 체결 추적
│   │   ├── auto_trader.py       # 자동매매 엔진 (시그널→주문 자동 실행)
│   │   └── trailing_stop.py     # 트레일링 스탑 (1초 주기 가격 추적)
│   ├── data/
│   │   ├── market_ws.py         # 시세 WebSocket + User Data Stream
│   │   ├── binance_rest.py      # REST API (캔들/심볼)
│   │   └── db.py                # SQLite (포지션, 거래, 주문, 설정)
│   └── models/
│       └── schemas.py           # 데이터 클래스 (Signal, Position, TradeRecord 등)
└── frontend/
    ├── index.html               # 메인 UI
    ├── css/style.css            # Binance 스타일 다크 테마
    └── js/
        ├── app.js               # 메인 앱 로직 + WebSocket + PnL 계산
        ├── chart.js             # TradingView 차트 + Binance kline WS + 포지션 라인 유지
        ├── orderForm.js         # 주문 폼
        ├── positions.js         # 포지션 패널 (포지션 가치 표시)
        ├── orders.js            # 주문 추적 패널
        ├── signals.js           # 시그널 피드 + 알람음 + Skip/Buy 제거
        ├── systemLog.js         # 실시간 시스템 로그 UI
        └── journal.js           # 매매일지 + 통계
```

---

## 설치 및 실행

### 1. 환경 설정

```bash
cd E:\ToyProject\coin-auto-trader
python -m venv .venv
.venv\Scripts\activate
pip install -r backend/requirements.txt
```

### 2. API Key 설정

```bash
copy .env.example .env
# .env 파일을 편집하여 Binance API Key/Secret 입력
```

```
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here
```

> Binance에서 API Key 발급 시 **Futures 거래 권한**을 활성화해야 합니다.
> IP 제한을 걸어두는 것을 권장합니다.

### 3. 서버 시작

```bash
cd backend
python main.py    # http://localhost:8002
```

### 4. (선택) 시나리오 통계 연동

coin-monitor의 `prepare.py`로 사전 캐시를 생성해두면, 시그널 카드에 시나리오 확률이 표시됩니다. 없어도 매매 자체는 가능하지만, 시나리오 분석 없이 진입하게 됩니다.

```bash
cd E:\ToyProject\coin-monitor
python prepare.py --days 30 --coins 5
```

---

## 서버 시작 시퀀스

1. DB 초기화 (`trader.db` 자동 생성)
2. 시그널 감지기 시작 (상위 30개 코인 WebSocket 연결)
3. 마크 가격 스트림 연결 (실시간 가격)
4. User Data Stream 연결 (주문/포지션 업데이트)
5. 트레일링 스탑 엔진 시작 (1초 주기)
6. 포지션 싱크 태스크 시작 (30초 주기)
7. 미체결 주문 체크 태스크 시작 (5초 주기)

---

## 설정

### 전략 파라미터 (config.py `STRATEGY`)

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `tick_threshold_pct` | 0.5% | 1틱 인정 최소 변동률 |
| `reset_ratio` | 0.7 | 되돌림 리셋 비율 |
| `entry_tick` | 3 | 진입 틱 수 |
| `leverage` | 5 | 기본 레버리지 |
| `sl_pct` | 5.0% | 손절 기준 |
| `trail_activation_pct` | 1.0% | 트레일링 활성화 수익률 |
| `trail_distance_pct` | 0.5% | 트레일링 되돌림 거리 |
| `max_entries` | 3 | 최대 물타기 횟수 |
| `scale_multiplier` | 2.0x | 물타기 금액 배수 |
| `cycle_mode` | ON | 순환매 활성화 |
| `cycle_sell_pct` | 50% | 순환매 시 매도 비율 |
| `fee_pct` | 0.04% | 거래 수수료 |

### 매매 기본값 (config.py `TRADE_DEFAULTS`)

| 파라미터 | 기본값 | 설명 |
|---------|--------|------|
| `position_size_pct` | 11% | 운용자금 대비 포지션 크기 |
| `max_open_positions` | 5 | 최대 동시 오픈 포지션 |
| `buy_mode` | semi | 매수 모드 (auto/semi/manual) |
| `sell_mode` | trailing | 매도 모드 (trailing/manual) |
| `operating_fund_mode` | fixed | 운용자금 모드 (fixed/balance) |
| `operating_fund_amount` | 100 USDT | 고정 운용자금 |
| `cooldown_seconds` | 300 | 청산 후 재진입 대기 (초) |

모든 설정은 UI에서 실시간 변경 가능하며, SQLite `settings` 테이블에 영구 저장됩니다.

---

## DB 스키마

### positions (활성 포지션)

```sql
id, symbol, direction, leverage, entry_price, avg_price, quantity,
unrealized_pnl, entry_time, signal_tick, signal_scenario,
status, num_entries, cycles, peak_price, trail_active, last_entry_tick
```

### trades (매매일지)

```sql
id, symbol, direction, leverage, entry_price, exit_price, quantity,
entry_time, exit_time, realized_pnl, pnl_pct, fee,
exit_reason, signal_tick, signal_scenario, num_entries, cycles,
notes, slippage_pct
```

### orders (주문 추적)

```sql
id, binance_order_id, client_order_id, symbol, side, order_type,
quantity, price, stop_price, expected_price, filled_qty, avg_fill_price,
status, reduce_only, position_id, purpose, created_at, updated_at,
error_msg, slippage_pct, leverage, signal_tick, signal_scenario
```

### settings (설정 KV)

```sql
key TEXT PRIMARY KEY, value TEXT
```

---

## API 엔드포인트

### 계정

| 메서드 | 경로 | 설명 |
|-------|------|------|
| GET | `/api/account` | 잔고 + 오늘 실현PnL + API 연결 상태 |
| POST | `/api/leverage/{symbol}/{leverage}` | 레버리지 설정 |

### 매매

| 메서드 | 경로 | 설명 |
|-------|------|------|
| POST | `/api/order/open` | 포지션 열기 (수동) |
| POST | `/api/order/close/{pos_id}` | 포지션 청산 |
| POST | `/api/order/scale_in/{pos_id}` | 물타기 |
| POST | `/api/order/cancel/{order_id}` | 주문 취소 |
| POST | `/api/emergency/close_all` | 긴급 전체 청산 |

### 시장 데이터

| 메서드 | 경로 | 설명 |
|-------|------|------|
| GET | `/api/state` | 코인 현재 상태 |
| GET | `/api/signals` | 최근 시그널 |
| GET | `/api/chart/{symbol}` | 차트 데이터 |
| GET | `/api/ticker/{symbol}` | 24h 티커 |

### 매매일지

| 메서드 | 경로 | 설명 |
|-------|------|------|
| GET | `/api/trades` | 거래 이력 |
| GET | `/api/trades/stats` | 통계 (승률, PF, MDD) |
| GET | `/api/trades/daily` | 일별 집계 |
| GET | `/api/trades/by_symbol` | 코인별 집계 |
| GET | `/api/trades/equity_curve` | 누적 수익 곡선 |

### 설정

| 메서드 | 경로 | 설명 |
|-------|------|------|
| GET | `/api/settings` | 현재 설정 |
| POST | `/api/settings` | 설정 변경 |

### WebSocket

| 경로 | 이벤트 |
|------|--------|
| `/ws` | `signal`, `position_update`, `order_submitted`, `order_update`, `order_canceled`, `slippage_warning`, `system_log` |

---

## 안전장치

| 장치 | 설명 |
|------|------|
| **서버 SL** | 포지션 진입 시 Binance에 STOP_MARKET 주문을 함께 배치. 네트워크 끊김에도 작동 |
| **포지션 싱크** | 30초마다 Binance 실제 포지션과 로컬 DB 비교. 불일치 시 자동 보정 |
| **주문 복구** | MARKET 주문 5초 미체결 시 REST API 폴링. API 실패 시 ERROR 기록 후 재조회 |
| **포지션 크기 제한** | 운용자금의 N% 상한 + 최대 동시 포지션 수 제한 |
| **잔고 체크** | 주문 전 잔고/마진 확인, 부족 시 거부 |
| **청산 경고** | 레버리지 기준 청산 가격 사전 계산 |
| **슬리피지 경고** | 0.1% 초과 시 UI 토스트 경고 |
| **쿨다운** | 청산 후 설정 시간(기본 5분) 동안 재진입 차단 |
| **긴급 청산** | Emergency Close All 버튼으로 모든 포지션 즉시 시장가 청산 |
| **Rate Limit** | Binance API 1100 req/min 제한 준수 |

---

## 업데이트 이력

### 2026-02-21

- **코인 필터링 강화**: 일일 변동폭 ≥10%, 24h 거래대금 ≥$100M 필터 추가 (저변동성·소형 코인 제외)
- **코인 드롭다운 등락률 표시**: 심볼 선택 시 24h 등락률 함께 표시 (`DOT +4.25%`)
- **실시간 차트 업데이트**: Binance kline WebSocket 직접 연결로 캔들 실시간 갱신
- **차트 포지션 라인 유지**: 진입선·손절선·트레일링선이 타임프레임 변경 시에도 유지
- **기본 타임프레임 5m**: 차트 기본값을 5분봉으로 변경
- **타임프레임 전환 버그 수정**: 15m→5m 직접 전환 불가 버그 해결
- **시스템 로그 탭 추가**: Order/Strategy 옆 Log 탭에서 실시간 시스템 활동 모니터링
- **상세 로그 출력**: 매매 시 마진·포지션 가치·수량·진입/청산가·시나리오 분석 포함
- **물타기 금액 개선**: 설정값이 아닌 실제 포지션 마진 기준으로 물타기 금액 산출
- **포지션 가치(Value) 표시**: 포지션 패널에 구매 총 금액 표시
- **시그널 카드 제거**: Skip 클릭 시 시그널 카드 제거, 매수 시 자동 제거
- **시그널 정렬**: 최신 시그널이 상단에 표시
- **시그널 알람**: 시그널 발생 시 비프음 재생 (LONG/SHORT 구분음) + 브라우저 Notification
- **PnL 표시 개선**: 오늘 실현PnL + 미실현PnL 합산 표시, hover 시 분리 확인 가능
- **로딩 지연 해결**: 동기 HTTP 호출을 스레드 풀로 분리하여 asyncio 이벤트루프 블로킹 방지

---

## 주의사항

- **실제 자금이 사용됩니다.** 반드시 소액으로 먼저 테스트하세요.
- API Key는 `.env`에만 저장하고 절대 커밋하지 마세요.
- Binance API Key 발급 시 IP 제한을 설정하세요.
- 과거 백테스트 결과가 미래 수익을 보장하지 않습니다.
- 레버리지 10x 기준 약 9.5% 역행 시 청산됩니다.
- 네트워크가 불안정한 환경에서는 서버 SL이 반드시 필요합니다.
- 운용자금을 넘는 금액을 투자하지 마세요.
