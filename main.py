import os
import time
import re
import datetime
import threading
import requests
import telebot
from telebot.apihelper import ApiTelegramException

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")
STATS_SOURCE_CHANNEL_ID = int(os.getenv("STATS_SOURCE_CHANNEL_ID", "0"))

PRED_TIMEOUT = int(os.getenv("PRED_TIMEOUT", 720))
MAX_ACTIVE = int(os.getenv("MAX_ACTIVE", 10))

# Параметры плотного потока генератора (delta ID)
MIN_DELTA_ID = int(os.getenv("MIN_DELTA_ID", 110))
MAX_DELTA_ID = int(os.getenv("MAX_DELTA_ID", 160))

# Актуальная ссылка с поддержкой Баккары (sports=146,236)
API_URL = "https://melbet-0018.pro/service-api/LiveFeed/Get1x2_VZip?sports=146,236&champs=1643503,2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

ARROW_CHAR = '\U0001F448'

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
active_preds = []
processed_stats_nums = set()

# Состояние для отслеживания смены 5-й цифры
last_processed_gid = None
last_digit_5th = None


# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
def normalize(n): 
    return ((n - 1) % 1440) + 1


def is_final_result(text):
    if not text: 
        return False
    if ARROW_CHAR in text: 
        return False
    return True


def get_5th_digit_from_end(gid):
    """Возвращает 5-ю цифру с конца у game_id"""
    s = str(gid)
    if len(s) >= 5:
        return int(s[-5])
    return None


# ==================== API / TG ====================
def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ API: {e}")
    return []


def format_game_info(game):
    try:
        g_i = game.get('I', 'N/A')
        g_di = game.get('DI', 'N/A')
        return (
            f"🎮 ИГРА #N{g_i}    Display ID: {g_di}\n"
            f"──────────────────────────────\n"
        )
    except Exception as e:
        print(f"⚠️ fmt: {e}")
        return None


def send_to_channel(text):
    if not CHANNEL_ID:
        return False
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"⚠️ send: {e}")
        return False


def send_prediction(text):
    if not PREDICTION_CHANNEL_ID:
        return None
    try:
        return bot.send_message(PREDICTION_CHANNEL_ID, text)
    except Exception as e:
        print(f"⚠️ pred: {e}")
        return None


def parse_stats(text):
    if not text:
        return None
    m = re.search(r'#N(\d+)', text)
    if not m:
        return None
    return int(m.group(1)), bool(re.search(r'#R\b', text))


def finalize(pred, success, detail):
    """Финализация прогноза Натуралов с гарантированной очисткой"""
    try:
        mark = "✅" if success else "❌"
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID, 
            message_id=pred["msg_id"],
            text=(
                f"🎯 Игра #N{pred['first_n']}\n"
                f"Возможна Раздача (серия потока)\n"
                f"проверка {pred['label']}\n"
                f"{mark} {detail}"
            )
        )
    except ApiTelegramException as e:
        print(f"⚠️ edit err: {e.description}")
    except Exception as e: 
        print(f"⚠️ edit: {e}")
    finally:
        with lock:
            if pred in active_preds: 
                active_preds.remove(pred)


def _make_pred(first_n, second_n, label):
    with lock:
        if len(active_preds) >= MAX_ACTIVE:
            print(f"⛔ лимит {MAX_ACTIVE}, пропуск {label}")
            return False
            
    text = (
        f"🎯 Игра #N{first_n}\n"
        f"Возможна Раздача (серия потока)\n"
        f"проверка {label}\n"
        f"⏳ Ожидание #R..."
    )
    sent = send_prediction(text)
    if not sent:
        return False
        
    with lock:
        active_preds.append({
            "msg_id": sent.message_id,
            "first_n": first_n,
            "second_n": second_n,
            "checked": set(),
            "created_at": time.time(),
            "label": label
        })
    return True


# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
def process_stats_message(msg):
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID:
        return
    parsed = parse_stats(msg.text)
    if not parsed:
        return
    num, has_nat = parsed

    if num in processed_stats_nums:
        return

    if not is_final_result(msg.text):
        if ARROW_CHAR in msg.text:
            print(f"⏳ промежуточный #N{num} (стрелка), пропускаем")
        else:
            print(f"⏳ промежуточный #N{num}, текст: {msg.text[:100]!r}")
        return

    processed_stats_nums.add(num)
    print(f"🔎 ФИНАЛ #N{num} | #R={has_nat}")

    # Проверка прогнозов Натуралов
    with lock:
        preds_to_check = list(active_preds)
        
    for pred in preds_to_check:
        if num in {pred["first_n"], pred["second_n"]} and num not in pred["checked"]:
            pred["checked"].add(num)
            if has_nat:
                pos = "0️⃣" if num == pred["first_n"] else "1️⃣"
                finalize(pred, True, f"{pos} #N{num} → Натурал #R")
                print(f"🎉 НАТУРАЛ #N{num} ({pos})")
            elif len(pred["checked"]) >= 2:
                finalize(pred, False, "❌ не зашло")
                print(f"❌ нет #R в {pred['label']}")


@bot.channel_post_handler()
def on_stats(msg):
    print(f"📨 POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)


@bot.edited_channel_post_handler()
def on_stats_edited(msg):
    print(f"✏️ EDITED POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid, last_digit_5th
    games = fetch_data()
    if not games:
        return

    # Проверка таймаутов
    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, "⌛ таймаут ожидания #R")

    new_games = []
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or gid in sent_games:
            continue
        sent_games.add(gid)
        new_games.append(game)
        text = format_game_info(game)
        if text:
            send_to_channel(text)
            
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid:
            continue
        
        gid_num = int(gid)
        di_num = int(di)
        current_5th = get_5th_digit_from_end(gid_num)

        if current_5th is not None and last_digit_5th is not None and last_processed_gid is not None:
            # Условие: 5-я цифра с конца увеличилась строго на 1 (с учётом перехода 9 -> 0)
            if current_5th == (last_digit_5th + 1) % 10:
                delta = gid_num - last_processed_gid
                # Дополнительная проверка, что поток плотный
                if MIN_DELTA_ID <= delta <= MAX_DELTA_ID:
                    first_n = di_num
                    second_n = normalize(first_n + 1)
                    label = f"#N{first_n}/#N{second_n}"
                    
                    if _make_pred(first_n, second_n, label):
                        print(f"🔥 ИЗМЕНЕНИЕ ЦИФРЫ ({last_digit_5th} ➔ {current_5th})! Сформирован прогноз {label} (ΔID={delta})")
                else:
                    print(f"⚠️ Цифра изменилась ({last_digit_5th} ➔ {current_5th}), но дельта ID ({delta}) вне [{MIN_DELTA_ID}-{MAX_DELTA_ID}]")

        last_processed_gid = gid_num
        last_digit_5th = current_5th

    if new_games:
        print(f"✅ Новых игр: {len(new_games)} | Последняя 5-я цифра: {last_digit_5th}")
        
    if len(sent_games) > 300: 
        sent_games.clear()


def main():
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | ΔID=[{MIN_DELTA_ID}-{MAX_DELTA_ID}]")
    send_to_channel("🟢 <b>Бот запущен</b> | Отслеживание смены 5-й цифры ID (Баккара + 21)")
    
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    
    while True:
        try:
            api_cycle()
            time.sleep(15)
        except Exception as e:
            print(f"⚠️ ошибка цикла: {e}")
            time.sleep(30)


if __name__ == "__main__":
    main()
