# MEXC 5m Signal Bot — FIXED v4

Виправлено проблему, коли `MEXC_SYMBOL` у Railway містить кілька символів через кому. Цей signal engine працює з одним символом, тому бере перший символ зі списку (наприклад, `ETH_USDT` з `ETH_USDT,BTC_USDT`). Логіку сигналів не змінено.

Railway Variables:
- TELEGRAM_BOT_TOKEN=ваш_токен
- TELEGRAM_CHAT_ID=7728565803,-5557760391
- MEXC_SYMBOL=ETH_USDT
- POLL_SECONDS=15
