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

SERIES_START = int(os.getenv("SERIES_START", 4))   # мин. длина серии для публикаций (2 = одна пара)
PRED_TIMEOUT = int(os.getenv("PRED_TIMEOUT", 720))
MAX_ACTIVE   = int(os.getenv("MAX_ACTIVE", 10))

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

sent_games   = set()
active_preds = []
current_series = {"pair": None, "dis": [], "published": set()}

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
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID, message_id=pred["msg_id"],
            text=(f"🎯 Игра #N{pred['first_n']}\nВозможна Раздача (серия потока)\n"
                  f"проверка {pred['label']}\n{mark} {detail}"))
    except Exception as e: print(f"⚠️ edit: {e}")
    if pred in active_preds: active_preds.remove(pred)

def _make_pred(first_n, label, targets_norm):
    with lock:
        if len(active_preds) >= MAX_ACTIVE:
            print(f"⛔ лимит {MAX_ACTIVE}, пропуск {label}"); return False
    text = (f"🎯 Игра #N{first_n}\nВозможна Раздача (серия потока)\n"
            f"проверка {label}\n⏳ Ожидание #R...")
    sent = send_prediction(text)
    if not sent: return False
    with lock:
        active_preds.append({"msg_id": sent.message_id, "targets_norm": targets_norm,
                             "checked": set(), "created_at": time.time(),
                             "first_n": first_n, "label": label})
    return True

def publish_pair(a, b, published):
    label = f"#N{int(a)}/#N{int(b)}"
    if _make_pred(int(a), label, {normalize(int(a)), normalize(int(b))}):
        published.add(a); published.add(b)
        print(f"🚀 ПРОГНОЗ-ПАРА {label} (длина серии={len(current_series['dis'])})")

def publish_single(a, published):
    label = f"#N{int(a)}"
    if _make_pred(int(a), label, {normalize(int(a))}):
        published.add(a)
        print(f"🚀 ПРОГНОЗ-ОДИНОЧКА {label} (хвост серии)")

def flush_series(series, tail):
    """Публикует полные пары из series; при tail=True — и непарный хвост."""
    dis, published = series["dis"], series["published"]
    if len(dis) < SERIES_START: return
    i = 0
    while i + 1 < len(dis):
        if dis[i + 1] not in published:
            publish_pair(dis[i], dis[i + 1], published)
        i += 2
    if tail and len(dis) % 2 == 1 and dis[-1] not in published:
        publish_single(dis[-1], published)

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
            if num in pred["targets_norm"] and num not in pred["checked"]:
                pred["checked"].add(num)
                if has_nat:
                    finalize(pred, True, f"#N{num} → Натурал #R"); print(f"🎉 НАТУРАЛ #N{num}")
                elif len(pred["checked"]) >= len(pred["targets_norm"]):
                    finalize(pred, False, f"в {pred['label']} нет #R"); print(f"❌ нет #R {pred['label']}")

def api_cycle():
    global current_series
    games = fetch_data()
    if not games: return

    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, "⏰ таймаут ожидания #R")

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
        pair = (int(gid) // 100) % 100

        if current_series["pair"] is not None and pair == (current_series["pair"] + 1) % 100:
            current_series["dis"].append(di)
        else:
            flush_series(current_series, tail=True)                      # обрыв → допубликовать хвост
            current_series = {"pair": pair, "dis": [di], "published": set()}
        current_series["pair"] = pair

        flush_series(current_series, tail=False)                         # рост → полные пары ретроактивно

    if new_games:
        print(f"✅ Новых: {len(new_games)} | серия пара={current_series['pair']} len={len(current_series['dis'])}")
    if len(sent_games) > 300: sent_games.clear()

def main():
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | series_start={SERIES_START} | timeout={PRED_TIMEOUT} | max_active={MAX_ACTIVE}")
    send_to_channel("🟢 <b>Бот запущен (прогнозы ПАРАМИ по серии потока)</b>")
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    while True:
        try:
            api_cycle(); time.sleep(15)
        except Exception as e:
            print(f"⚠️ цикл: {e}"); time.sleep(30)

if __name__ == "__main__":
    main()
