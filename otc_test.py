import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    print("❌ Не установлен пакет websockets.")
    print("Установи:")
    print("pip install websockets")
    sys.exit(1)


# ============================================================
# CONFIG
# ============================================================

DEFAULT_ASSET = os.getenv("OTC_ASSET", "EURUSD_otc")
TIMEFRAME = int(os.getenv("OTC_TIMEFRAME", "60"))
WAIT_SECONDS = int(os.getenv("OTC_WAIT_SECONDS", "90"))

# Можно переопределить через Render Environment Variables.
#
# Пример:
# OTC_WS_URL=wss://example.com/socket
#
OTC_WS_URL = os.getenv(
    "OTC_WS_URL",
    "wss://ws.quotex.io/socket.io/?EIO=4&transport=websocket",
)


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def print_header():
    print()
    print("=" * 70)
    print("        OTC LIVE DATA TEST")
    print("=" * 70)
    print(f"Time:       {now()}")
    print(f"Asset:      {DEFAULT_ASSET}")
    print(f"Timeframe:  {TIMEFRAME} sec")
    print(f"Wait:       {WAIT_SECONDS} sec")
    print(f"WebSocket:  {OTC_WS_URL}")
    print("=" * 70)
    print()


def extract_price(obj):
    """
    Пытается найти цену в произвольном JSON.
    """

    if isinstance(obj, dict):
        keys = [
            "close",
            "price",
            "rate",
            "quote",
            "value",
            "last",
            "current",
        ]

        for key in keys:
            value = obj.get(key)

            if isinstance(value, (int, float)):
                return float(value)

            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    pass

        for value in obj.values():
            result = extract_price(value)

            if result is not None:
                return result

    elif isinstance(obj, list):
        for value in obj:
            result = extract_price(value)

            if result is not None:
                return result

    return None


def extract_candle(obj):
    """
    Ищет структуру OHLC в JSON.
    """

    if isinstance(obj, dict):

        open_value = obj.get("open")
        high_value = obj.get("high")
        low_value = obj.get("low")
        close_value = obj.get("close")

        if all(
            isinstance(x, (int, float, str))
            for x in (
                open_value,
                high_value,
                low_value,
                close_value,
            )
        ):
            try:
                return {
                    "time": obj.get("time")
                    or obj.get("timestamp")
                    or obj.get("from"),
                    "open": float(open_value),
                    "high": float(high_value),
                    "low": float(low_value),
                    "close": float(close_value),
                    "volume": obj.get("volume"),
                }
            except (TypeError, ValueError):
                pass

        for value in obj.values():
            result = extract_candle(value)

            if result:
                return result

    elif isinstance(obj, list):

        for value in obj:
            result = extract_candle(value)

            if result:
                return result

    return None


def parse_message(message):
    """
    Безопасно пытается распознать Socket.IO/JSON сообщения.
    """

    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="ignore")

    message = message.strip()

    if not message:
        return None, None

    # Socket.IO handshake
    if message.startswith("0"):
        try:
            return "handshake", json.loads(message[1:])
        except Exception:
            return "handshake", message

    # Socket.IO ping
    if message == "2":
        return "ping", None

    # Socket.IO pong
    if message == "3":
        return "pong", None

    # Socket.IO event
    if message.startswith("42"):
        payload = message[2:].strip()

        try:
            data = json.loads(payload)

            if isinstance(data, list) and data:
                event_name = data[0]

                event_data = data[1] if len(data) > 1 else None

                return event_name, event_data

            return "event", data

        except Exception:
            return "socketio_raw", message

    # Обычный JSON
    try:
        return "json", json.loads(message)
    except Exception:
        return "raw", message


def build_subscribe_messages(asset):
    """
    Набор наиболее распространённых форматов подписки.

    Мы НЕ отправляем авторизацию и НЕ передаём SSID.
    """

    messages = []

    # Socket.IO connect namespace.
    messages.append("40")

    # Вариант subscribe_candles.
    messages.append(
        "42"
        + json.dumps(
            [
                "subscribe_candles",
                {
                    "asset": asset,
                    "timeframe": TIMEFRAME,
                },
            ],
            separators=(",", ":"),
        )
    )

    # Вариант instruments/update.
    messages.append(
        "42"
        + json.dumps(
            [
                "instruments/update",
                {
                    "asset": asset,
                    "period": TIMEFRAME,
                },
            ],
            separators=(",", ":"),
        )
    )

    return messages


# ============================================================
# TEST
# ============================================================

async def run_test():
    print_header()

    print("🔌 Подключение к WebSocket...")
    print()

    try:
        async with websockets.connect(
            OTC_WS_URL,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=5 * 1024 * 1024,
        ) as ws:

            print("✅ WebSocket-соединение установлено.")
            print()

            print("📡 Ожидаем handshake...")

            handshake_received = False
            first_deadline = time.monotonic() + 10

            while time.monotonic() < first_deadline:

                try:
                    message = await asyncio.wait_for(
                        ws.recv(),
                        timeout=3,
                    )
                except asyncio.TimeoutError:
                    continue

                event, data = parse_message(message)

                print(f"← {event}")

                if event == "handshake":
                    handshake_received = True
                    print("✅ Socket.IO handshake получен.")

                    if isinstance(data, dict):
                        print(
                            f"   sid: {str(data.get('sid', 'N/A'))[:12]}..."
                        )

                    break

                if event == "ping":
                    await ws.send("3")

            print()

            if not handshake_received:
                print(
                    "⚠️ Handshake Socket.IO не получен."
                )
                print(
                    "Это может означать несовместимый endpoint."
                )
                return

            print("📨 Отправляем запрос подписки без авторизации...")
            print()

            messages = build_subscribe_messages(DEFAULT_ASSET)

            for outgoing in messages:

                # Никогда не печатаем потенциальные секреты.
                print(
                    "→",
                    outgoing[:180],
                    "..."
                    if len(outgoing) > 180
                    else "",
                )

                try:
                    await ws.send(outgoing)
                except Exception as exc:
                    print(f"⚠️ Ошибка отправки: {exc}")

                await asyncio.sleep(1)

            print()
            print(
                f"📈 Слушаем поток {DEFAULT_ASSET} "
                f"{WAIT_SECONDS} секунд..."
            )
            print()

            start_time = time.monotonic()

            messages_received = 0
            candles_received = 0
            prices = []

            last_candle = None
            last_price = None

            while time.monotonic() - start_time < WAIT_SECONDS:

                remaining = WAIT_SECONDS - (
                    time.monotonic() - start_time
                )

                try:
                    message = await asyncio.wait_for(
                        ws.recv(),
                        timeout=min(5, max(1, remaining)),
                    )

                except asyncio.TimeoutError:
                    print("⏳ Пока новых сообщений нет...")
                    continue

                except Exception as exc:
                    print()
                    print(f"❌ WebSocket закрыт/ошибка: {exc}")
                    break

                messages_received += 1

                event, data = parse_message(message)

                if event == "ping":
                    try:
                        await ws.send("3")
                    except Exception:
                        pass

                    continue

                candle = extract_candle(data)

                if candle:
                    candles_received += 1

                    changed = candle != last_candle

                    print(
                        f"🕯 CANDLE #{candles_received}"
                    )
                    print(
                        f"   time : {candle['time']}"
                    )
                    print(
                        f"   open : {candle['open']}"
                    )
                    print(
                        f"   high : {candle['high']}"
                    )
                    print(
                        f"   low  : {candle['low']}"
                    )
                    print(
                        f"   close: {candle['close']}"
                    )
                    print(
                        f"   new  : {'YES' if changed else 'NO'}"
                    )
                    print()

                    last_candle = candle

                    try:
                        prices.append(
                            float(candle["close"])
                        )
                    except Exception:
                        pass

                    continue

                price = extract_price(data)

                if price is not None:

                    if (
                        last_price is None
                        or price != last_price
                    ):
                        prices.append(price)

                        print(
                            f"💹 PRICE: {price}"
                        )

                        last_price = price

                    continue

                # Показываем только короткую информацию,
                # чтобы терминал не превращался в мусор.
                if event not in {
                    "pong",
                    "raw",
                }:
                    print(
                        f"📨 EVENT: {event}"
                    )

            # ====================================================
            # RESULT
            # ====================================================

            print()
            print("=" * 70)
            print("                         RESULT")
            print("=" * 70)

            print(
                f"WebSocket connected: {'YES' if handshake_received else 'NO'}"
            )

            print(
                f"Messages received:   {messages_received}"
            )

            print(
                f"Candles detected:    {candles_received}"
            )

            print(
                f"Prices detected:     {len(prices)}"
            )

            if len(prices) >= 2:

                unique_prices = len(
                    set(
                        round(price, 8)
                        for price in prices
                    )
                )

                print(
                    f"Different prices:    {unique_prices}"
                )

                if unique_prices > 1:
                    print()
                    print(
                        "🟢 LIVE DATA: Похоже, поток реально "
                        "меняет цену."
                    )
                else:
                    print()
                    print(
                        "🟡 DATA FOUND: данные есть, "
                        "но цена не изменилась за время теста."
                    )

            elif candles_received > 0:

                print()
                print(
                    "🟢 CANDLES FOUND: свечные данные получены."
                )

            else:

                print()
                print(
                    "🔴 LIVE OTC DATA НЕ ПОЛУЧЕНЫ."
                )
                print()
                print(
                    "Возможные причины:"
                )
                print(
                    "1. Endpoint требует авторизацию."
                )
                print(
                    "2. Endpoint не является публичным."
                )
                print(
                    "3. Формат подписки отличается."
                )
                print(
                    "4. Asset недоступен."
                )
                print(
                    "5. Сервер требует другой регион."
                )

            print("=" * 70)
            print()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(run_test())

    except KeyboardInterrupt:
        print()
        print("🛑 Тест остановлен.")

    except Exception as exc:
        print()
        print("=" * 70)
        print("❌ КРИТИЧЕСКАЯ ОШИБКА")
        print("=" * 70)
        print(type(exc).__name__, str(exc))
        print("=" * 70)
