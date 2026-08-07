import os
import time
import re
import threading
import requests
import telebot
from telebot.apihelper import ApiTelegramException

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")

# Новые границы разницы последних 3-х цифр ID
MIN_DELTA_3D = int(os.getenv("MIN_DELTA_3D", 200))
MAX_DELTA_3D = int(os.getenv("MAX_DELTA_3D", 600))

# ID Спорта для Баккары
BACCARAT_SPORT_ID = 236

# Ссылка на API
API_URL = "https://melbet-4866.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
last_processed_gid = None


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_3_digits(gid):
    """Возвращает последние 3 цифры Game ID как число"""
    try:
        return int(str(gid)[-3:])
    except (ValueError, TypeError):
        return None


def calculate_delta_3d(prev_gid, curr_gid):
    """Вычисляет разницу последних 3-х цифр с учетом возможного перехода через 1000"""
    prev_3d = get_last_3_digits(prev_gid)
    curr_3d = get_last_3_digits(curr_gid)
    
    if prev_3d is None or curr_3d is None:
        return None
        
    delta = curr_3d - prev_3d
    if delta < 0:
        delta += 1000
    return delta


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


def _make_pred(di_num, delta):
    """Формирует и отправляет прогноз на текущую и следующую раздачу"""
    next_di_num = di_num + 1
    
    text = (
        f"🔥 <b>СИГНАЛ | БАККАРА</b>\n"
        f"──────────────────────────────\n"
        f"🎯 <b>Ожидаются игры:</b> #N{di_num} - #N{next_di_num}\n"
        f"📊 <b>Разница 3-х цифр ID:</b> {delta} (в интервале 200-600)\n"
        f"──────────────────────────────\n"
        f"⏳ <i>Вход на 1-2 шага (догон)</i>"
    )
    
    sent = send_prediction(text)
    return bool(sent)


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games:
        return

    # Фильтрация исключительно на Баккару (SI == 236)
    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]

    new_games = []
    for game in games:
        gid = game.get("I")
        if not gid or gid in sent_games:
            continue
        sent_games.add(gid)
        new_games.append(game)
        
        text = format_game_info(game)
        if text:
            send_to_channel(text)
            
    # Сортировка по возрастанию Display ID
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid:
            continue
        
        gid_num = int(gid)
        di_num = int(di)

        if last_processed_gid is not None:
            # Вычисляем разницу последних 3-х цифр ID
            delta_3d = calculate_delta_3d(last_processed_gid, gid_num)
            
            if delta_3d is not None:
                if MIN_DELTA_3D <= delta_3d <= MAX_DELTA_3D:
                    if _make_pred(di_num, delta_3d):
                        print(f"🔥 БАККАРА: Прогноз отправлен на игры #N{di_num}-#N{di_num + 1} (Δ3D: {delta_3d})")
                else:
                    print(f"ℹ️ [Баккара] #N{di_num} Δ3D={delta_3d} вне интервала [{MIN_DELTA_3D}-{MAX_DELTA_3D}]")

        last_processed_gid = gid_num

    if new_games:
        print(f"✅ Обработано новых игр Баккары: {len(new_games)} | Последний GID: {last_processed_gid}")
        
    if len(sent_games) > 300: 
        sent_games.clear()


def main():
    print(f"🚀 ЗАПУСК БОТА (СИГНАЛЫ РАЗНИЦЫ 3-х ЦИФР ID: {MIN_DELTA_3D}-{MAX_DELTA_3D})")
    send_to_channel("🟢 <b>Бот запущен</b> | Анализ разницы 3х цифр ID для прогноза раздач")
    
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
