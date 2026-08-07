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
MIN_DELTA_ID = int(os.getenv("MIN_DELTA_ID", 100))
MAX_DELTA_ID = int(os.getenv("MAX_DELTA_ID", 140))

# ID Спорта для Баккары
BACCARAT_SPORT_ID = 236

# Ссылка на API
API_URL = "https://melbet-4866.pro/service-api/LiveFeed/Get1x2_VZip?sports=146,236&champs=1643503,2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

ARROW_CHAR = '\U0001F448'

# ==================== МАППИНГ МАСТЕЙ ====================
SUIT_MAP = {
    0: "Черви ❤️",
    1: "Бубны 🔶",
    2: "Трефы ♣️",
    3: "Пики ♠️"
}

# Текстовые эквиваленты для распознавания масти из поста статистики
SUIT_KEYWORDS = {
    "черви": "Черви ❤️",
    "черва": "Черви ❤️",
    "❤️": "Черви ❤️",
    "бубны": "Бубны 🔶",
    "бубна": "Бубны 🔶",
    "🔶": "Бубны 🔶",
    "♦️": "Бубны 🔶",
    "трефы": "Трефы ♣️",
    "крести": "Трефы ♣️",
    "♣️": "Трефы ♣️",
    "пики": "Пики ♠️",
    "пика": "Пики ♠️",
    "♠️": "Пики ♠️"
}

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


def calculate_suit(id_value):
    """Возвращает масть по остатку от деления на 4"""
    try:
        val = int(id_value)
        return SUIT_MAP[val % 4]
    except (ValueError, TypeError):
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
        sport_name = game.get('SN', 'Баккара')
        return (
            f"🎴 <b>{sport_name}</b> | ИГРА #N{g_i}\n"
            f"Display ID: {g_di}\n"
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
        return bot.send_message(PREDICTION_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        print(f"⚠️ pred: {e}")
        return None


def parse_stats(text):
    """
    Парсит номер игры (#N) и выпавшую масть игрока из сообщения канала статистики.
    """
    if not text:
        return None, None
    m = re.search(r'#N(\d+)', text)
    if not m:
        return None, None
    
    num = int(m.group(1))
    found_suit = None
    
    text_lower = text.lower()
    for kw, suit_name in SUIT_KEYWORDS.items():
        if kw in text_lower:
            found_suit = suit_name
            break
            
    return num, found_suit


def finalize_suit_prediction(pred, actual_suit):
    """Финализация прогноза мастей с раздельным отчетом по GID и DI"""
    try:
        gid_win = (pred["gid_suit"] == actual_suit) if actual_suit else False
        di_win = (pred["di_suit"] == actual_suit) if actual_suit else False
        
        gid_mark = "✅" if gid_win else "❌"
        di_mark = "✅" if di_win else "❌"
        
        status_text = (
            f"🎯 <b>Прогноз масти | Баккара #N{pred['di']}</b>\n"
            f"Game ID: {pred['gid']}\n"
            f"──────────────────────────────\n"
            f"Выпавшая масть: <b>{actual_suit or 'Не определена'}</b>\n\n"
            f"1️⃣ <b>По Game ID (%4):</b> {pred['gid_suit']} {gid_mark}\n"
            f"2️⃣ <b>По Display ID (%4):</b> {pred['di_suit']} {di_mark}"
        )
        
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID, 
            message_id=pred["msg_id"],
            text=status_text,
            parse_mode="HTML"
        )
    except ApiTelegramException as e:
        print(f"⚠️ edit err: {e.description}")
    except Exception as e:
        print(f"⚠️ edit: {e}")
    finally:
        with lock:
            if pred in active_preds: 
                active_preds.remove(pred)


def _make_pred(gid, di):
    """Создает двойной прогноз по масти на основе GID % 4 и DI % 4"""
    with lock:
        if len(active_preds) >= MAX_ACTIVE:
            print(f"⛔ лимит {MAX_ACTIVE}, пропуск прогноза масти")
            return False
            
    gid_suit = calculate_suit(gid)
    di_suit = calculate_suit(di)
    
    text = (
        f"🎯 <b>Прогноз масти 1-й карты Игрока</b>\n"
        f"Игра: #N{di} (GID: {gid})\n"
        f"──────────────────────────────\n"
        f"1️⃣ <b>По Game ID (%4):</b> {gid_suit}\n"
        f"2️⃣ <b>По Display ID (%4):</b> {di_suit}\n"
        f"──────────────────────────────\n"
        f"⏳ Ожидание результата..."
    )
    sent = send_prediction(text)
    if not sent:
        return False
        
    with lock:
        active_preds.append({
            "msg_id": sent.message_id,
            "gid": gid,
            "di": di,
            "gid_suit": gid_suit,
            "di_suit": di_suit,
            "created_at": time.time()
        })
    return True


# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
def process_stats_message(msg):
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID:
        return
    num, actual_suit = parse_stats(msg.text)
    if not num:
        return

    if num in processed_stats_nums:
        return

    if not is_final_result(msg.text):
        return

    processed_stats_nums.add(num)
    print(f"🔎 ФИНАЛ #N{num} | Масть: {actual_suit}")

    # Проверка активных прогнозов по номеру Display ID (di) или Game ID (gid)
    with lock:
        preds_to_check = list(active_preds)
        
    for pred in preds_to_check:
        if num in (pred["di"], pred["gid"]):
            finalize_suit_prediction(pred, actual_suit)
            print(f"🎉 Прогноз для #N{num} финализирован! Фактическая масть: {actual_suit}")


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
    raw_games = fetch_data()
    if not raw_games:
        return

    # 🎯 ФИЛЬТРАЦИЯ: Оставляем ИСКЛЮЧИТЕЛЬНО игры Баккары (SI == 236)
    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]

    # Проверка таймаутов
    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize_suit_prediction(pred, None)

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
                # Проверка плотности потока Баккары
                if MIN_DELTA_ID <= delta <= MAX_DELTA_ID:
                    if _make_pred(gid_num, di_num):
                        print(f"🔥 БАККАРА: Прогноз масти отправлен для #N{di_num} (GID: {gid_num})")
                else:
                    print(f"⚠️ [Баккара] Цифра изменилась ({last_digit_5th} ➔ {current_5th}), но дельта ID ({delta}) вне [{MIN_DELTA_ID}-{MAX_DELTA_ID}]")

        last_processed_gid = gid_num
        last_digit_5th = current_5th

    if new_games:
        print(f"✅ Новых игр Баккары: {len(new_games)} | Последняя 5-я цифра: {last_digit_5th}")
        
    if len(sent_games) > 300: 
        sent_games.clear()


def main():
    print(f"🚀 ЗАПУСК БОТА (ПРОГНОЗ МАСТЕЙ GID % 4 И DI % 4) | ΔID=[{MIN_DELTA_ID}-{MAX_DELTA_ID}]")
    send_to_channel("🟢 <b>Бот запущен</b> | Отслеживание и двойная проверка мастей (Game ID % 4 и Display ID % 4)")
    
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
