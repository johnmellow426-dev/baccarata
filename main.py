import os
import time
import re
import json
import datetime
import threading
import requests
import telebot

# --- НАСТРОЙКИ ---
BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHANNEL_ID              = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID   = os.getenv("PREDICTION_CHANNEL_ID")
STATS_SOURCE_CHANNEL_ID = int(os.getenv("STATS_SOURCE_CHANNEL_ID"))

API_URL = "https://melbet-2814.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}

SERIES_START = int(os.getenv("SERIES_START", 4))      # с какой длины серии начинаем публиковать
PRED_TIMEOUT = int(os.getenv("PRED_TIMEOUT", 720))
MAX_ACTIVE   = int(os.getenv("MAX_ACTIVE", 15))

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

sent_games   = set()
active_preds = []
# текущая серия одинаковых mod4: {mod, dis:[...], published:set}
current_series = {"mod": None, "dis": [], "published": set()}

def normalize(n): return ((n - 1) % 1440) + 1

def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200: return resp.json().get("Value", [])
    except Exception as e: print(f"⚠️ API: {e}")
    return []

def format_game_info(game):
    try:
        return (
            f"🎮 ИГРА #N{game.get('I','N/A')}   Display ID: {game.get('DI','N/A')}\n"
            f"──────────────────────────────\n📊 Информация:\n  Спорт: {game.get('SN','N/A')}\n"
            f"  Системные данные:\n  Event Counter: {game.get('EC','N/A')}\n"
            f"  League ID: {game.get('LI','N/A')}\n  Sport ID: {game.get('SI','N/A')}")
    except Exception as e:
        print(f"⚠️ fmt: {e}"); return None

def send_to_channel(text):
    try: bot.send_message(CHANNEL_ID, text, parse_mode="HTML"); return True
    except Exception as e: print(f"⚠️ send: {e}"); return False

def send_prediction(text):
    try: return bot.send_message(PREDICTION_CHANNEL_ID, text)
    except Exception as e: print(f"⚠️ pred: {e}"); return None

def parse_stats(text):
    if not text: return None
    m = re.search(r'#N(\d+)', text)
    if not m: return None
    return int(m.group(1)), bool(re.search(r'#R\b', text))

def finalize(pred, success, detail):
    try:
        mark = "✅" if success else "❌"
        t = pred["target_n"]
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID, message_id=pred["msg_id"],
            text=(f"🎯 Игра #N{t}\nВозможна Раздача (серия потока)\n"
                  f"проверка #N{t}\n{mark} {detail}"))
    except Exception as e: print(f"⚠️ edit: {e}")
    if pred in active_preds: active_preds.remove(pred)

def publish_for(dis):
    """Публикует прогноз на каждую игру из dis, если ещё не публиковали и есть место."""
    for d in dis:
        if d in current_series["published"]: continue
        with lock:
            if len(active_preds) >= MAX_ACTIVE:
                print(f"⛔ лимит активных {MAX_ACTIVE}, пропуск #N{d}")
                continue
        t = int(d)
        sent = send_prediction(f"🎯 Игра #N{t}\nВозможна Раздача (серия потока)\nпроверка #N{t}\n⏳ Ожидание #R...")
        if sent:
            with lock:
                active_preds.append({"msg_id": sent.message_id, "target_n": t, "created_at": time.time()})
            current_series["published"].add(d)
            print(f"🚀 ПРОГНОЗ #N{t} (серия mod4={current_series['mod']}, длина={len(current_series['dis'])})")

@bot.channel_post_handler()
def on_stats(msg):
    print(f"📨 CHANNEL_POST chat.id={msg.chat.id} | {(msg.text or '')[:60]!r}")
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID: return
    parsed = parse_stats(msg.text)
    if not parsed: return
    num, has_nat = parsed
    print(f"🔎 #N{num} #R={has_nat} | активных={len(active_preds)}")
    with lock:
        for pred in list(active_preds):
            if num == normalize(pred["target_n"]):     # строго свой номер
                if has_nat:
                    finalize(pred, True, f"#N{num} → Натурал #R")
                    print(f"🎉 НАТУРАЛ #N{num}")
                else:
                    finalize(pred, False, f"#N{num} без #R")
                    print(f"❌ нет #R на #N{num}")

def api_cycle():
    global current_series
    games = fetch_data()
    if not games: return

    # таймауты
    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, "⏰ таймаут ожидания #R")

    # новые игры, отсортированы по DI (чтобы серия собиралась по порядку)
    new_games = []
    for game in games:
        gid = game.get("I"); di = game.get("DI")
        if not gid or gid in sent_games: continue
        sent_games.add(gid)
        new_games.append(game)
        text = format_game_info(game)
        if text: send_to_channel(text)
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I"); di = game.get("DI")
        if not di: continue
        m = int(gid) % 4

        # ведём текущую серию
        if m == current_series["mod"]:
            current_series["dis"].append(di)
        else:
            current_series = {"mod": m, "dis": [di], "published": set()}   # обрыв → новая серия

        # серия доросла до порога → публикуем на ВСЕ её игры (ретроактивно + новую)
        if len(current_series["dis"]) >= SERIES_START:
            publish_for(current_series["dis"])

    if new_games: print(f"✅ Новых игр: {len(new_games)} | серия mod4={current_series['mod']} len={len(current_series['dis'])}")
    if len(sent_games) > 300: sent_games.clear()

def main():
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | series_start={SERIES_START} | timeout={PRED_TIMEOUT} | max_active={MAX_ACTIVE}")
    send_to_channel("🟢 <b>Бот запущен (серийные прогнозы Натурал)</b>")
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    while True:
        try:
            api_cycle(); time.sleep(15)
        except Exception as e:
            print(f"⚠️ цикл: {e}"); time.sleep(30)

if __name__ == "__main__":
    main()
