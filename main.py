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
STATS_SOURCE_CHANNEL_ID = int(os.getenv("STATS_SOURCE_CHANNEL_ID"))   # 🔥 int() — было str!

API_URL = "https://melbet-2814.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}

SERIES_LEN   = 3
CHECK_RANGE  = 2
PRED_TIMEOUT = int(os.getenv("PRED_TIMEOUT", 720))   # 🔥 12 мин по умолчанию
MAX_ACTIVE   = int(os.getenv("MAX_ACTIVE", 3))        # 🔥 до 3 прогнозов

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

sent_games  = set()
recent_mods = []
in_series   = False
active_preds = []   # 🔥 список активных прогнозов

def normalize(n): return ((n - 1) % 1440) + 1

# --- API ---
def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ Ошибка запроса: {e}")
    return []

def format_game_info(game):
    try:
        return (
            f"🎮 ИГРА #N{game.get('I','N/A')}   Display ID: {game.get('DI','N/A')}\n"
            f"──────────────────────────────\n"
            f"📊 Информация:\n  Спорт: {game.get('SN','N/A')}\n  Системные данные:\n"
            f"  Event Counter: {game.get('EC','N/A')}\n  League ID: {game.get('LI','N/A')}\n  Sport ID: {game.get('SI','N/A')}")
    except Exception as e:
        print(f"⚠️ Ошибка форматирования: {e}")
        return None

def send_to_channel(text):
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"⚠️ Ошибка отправки: {e}")
        return False

def send_prediction(text):
    try:
        return bot.send_message(PREDICTION_CHANNEL_ID, text)
    except Exception as e:
        print(f"⚠️ Ошибка прогноза: {e}")
        return None

# --- ПРОВЕРКА #R ---
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
            text=(f"🎯 Игра #N{t}\nВозможна Раздача\n"
                  f"проверка ({t}-{normalize(t+1)})\n{mark} {detail}"))
    except Exception as e:
        print(f"⚠️ edit: {e}")
    if pred in active_preds:
        active_preds.remove(pred)

@bot.channel_post_handler()
def on_stats(msg):
    # 🔥 диагностика: видно ВСЕ посты из каналов, где бот админ
    print(f"📨 CHANNEL_POST chat.id={msg.chat.id} | text={(msg.text or '')[:70]!r}")
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID:
        return
    parsed = parse_stats(msg.text)
    if not parsed: return
    num, has_nat = parsed
    print(f"🔎 #N{num} #R={has_nat} | активных прогнозов={len(active_preds)}")
    with lock:
        for pred in list(active_preds):          # один пост может закрыть несколько
            t = pred["target_n"]
            rng = {normalize(t), normalize(t + 1)}
            if num in rng and num not in pred["checked"]:
                pred["checked"].add(num)
                if has_nat:
                    finalize(pred, True, f"#N{num} → Натурал #R")
                    print(f"🎉 НАТУРАЛ #N{num} (закрыл прогноз на #N{t})")
                elif len(pred["checked"]) >= CHECK_RANGE:
                    finalize(pred, False, f"в #N{t}/#N{normalize(t+1)} нет #R")
                    print(f"❌ Натурала нет для #N{t}")

# --- ЦИКЛ API ---
def api_cycle():
    global in_series, recent_mods
    games = fetch_data()
    if not games: return

    # таймауты
    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, "⏰ таймаут ожидания #R")

    new_count = 0
    for game in games:
        gid = game.get("I")
        di  = game.get("DI")
        if not gid or gid in sent_games: continue
        sent_games.add(gid)
        new_count += 1

        text = format_game_info(game)
        if text: send_to_channel(text)

        if not di: continue
        m = int(gid) % 4
        recent_mods.append(m); recent_mods = recent_mods[-10:]

        if len(recent_mods) >= 2 and recent_mods[-1] != recent_mods[-2]:
            in_series = False

        with lock:
            can = len(active_preds) < MAX_ACTIVE
        if can and not in_series and len(recent_mods) >= SERIES_LEN \
                and len(set(recent_mods[-SERIES_LEN:])) == 1:
            in_series = True
            t = int(di); t2 = normalize(t + 1)
            sent = send_prediction(f"🎯 Игра #N{t}\nВозможна Раздача\nпроверка ({t}-{t2})\n⏳ Ожидание #R...")
            if sent:
                with lock:
                    active_preds.append({"msg_id": sent.message_id, "target_n": t,
                                         "checked": set(), "created_at": time.time()})
                print(f"🚀 ПРОГНОЗ Натурал #N{t} (mod4={m}) | активных={len(active_preds)}")

    if new_count: print(f"✅ Новых игр: {new_count}")
    if len(sent_games) > 200: sent_games.clear()

def main():
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | timeout={PRED_TIMEOUT} | max_active={MAX_ACTIVE}")
    send_to_channel("🟢 <b>Бот запущен</b>")
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    while True:
        try:
            api_cycle()
            time.sleep(15)
        except Exception as e:
            print(f"⚠️ Ошибка цикла: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
