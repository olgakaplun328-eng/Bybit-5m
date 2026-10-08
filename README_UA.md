# ETHUSDT Bybit 5m Telegram Signal Bot

Signal-only Telegram bot using public Bybit Futures market data.

## Важливо
- Джерело свічок: **Bybit**
- Таймфрейм: **5m**
- API-ключі Bybit не потрібні
- Автоматичні ордери не виконуються
- Логіка сигналів у `bot.py` збережена; змінено лише джерело даних з MEXC на Bybit та перехід на нативні 5m свічки.
- Telegram ID за замовчуванням: `7728565803,-5557760391`

## Railway Variables
```text
TELEGRAM_BOT_TOKEN=ВАШ_ТОКЕН
TELEGRAM_CHAT_ID=7728565803,-5557760391
BYBIT_SYMBOL=ETHUSDT
POLL_SECONDS=15
STATE_FILE=state.json
```

Після зміни токена/змінних зробіть Redeploy.
