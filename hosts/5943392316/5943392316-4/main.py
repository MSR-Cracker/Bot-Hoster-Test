import json
import time
import urllib.parse
import urllib.request

BOT_TOKEN = "8521585556:AAFK4RP56TWMqcJy_GgOABjJ6xCrmBELoyY"

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def telegram(method, data=None):
    data = data or {}

    encoded = urllib.parse.urlencode(data).encode()

    req = urllib.request.Request(
        f"{API}/{method}",
        data=encoded,
        headers={
            "User-Agent": "SimplePythonBot/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode())


def send_message(chat_id, text):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text
        }
    )


def main():
    print("Python Telegram Bot started successfully.")

    offset = 0

    while True:
        try:
            result = telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50
                }
            )

            if not result.get("ok"):
                print("Telegram error:", result)
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                message = update.get("message")

                if not message:
                    continue

                chat = message.get("chat", {})
                chat_id = chat.get("id")
                text = message.get("text", "")

                if not chat_id:
                    continue

                if text == "/start":
                    send_message(
                        chat_id,
                        "👋 أهلاً بك!\n\n"
                        "🤖 البوت شغال بنجاح.\n"
                        "☁️ تم تشغيله من الاستضافة.\n\n"
                        "الأمر: /ping"
                    )

                elif text == "/ping":
                    send_message(
                        chat_id,
                        "🏓 Pong!\n"
                        "✅ الاستضافة تعمل."
                    )

                elif text == "/status":
                    send_message(
                        chat_id,
                        "🟢 Bot Status: Online\n"
                        "🐍 Python: Running"
                    )

                else:
                    send_message(
                        chat_id,
                        f"📩 استلمت رسالتك:\n{text}"
                    )

        except Exception as e:
            print("ERROR:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()