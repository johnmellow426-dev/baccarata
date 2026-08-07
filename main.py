import os
import time
import threading
import requests
import telebot

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")

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

# ==================== СОСТОЯНИЕ И СТАТИСТИКА ====================
sent_games = set()
last_processed_gid = None

# Учет статистики результатов по каждой разнице 2D: { diff_val: {"total": 0, "2/3": 0, "3/2": 0} }
diff_stats = {}


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_2_digits(gid):
    """Возвращает последние 2 цифры Game ID как число"""
    try:
        return int(str(gid)[-2:])
    except (ValueError, TypeError):
        return None


def log_diff_stat(diff_val, outcome=None):
    """Ведет учет вызовов и заходит в статистику по конкретной разнице"""
    with lock:
        if diff_val not in diff_stats:
            diff_stats[diff_val] = {"total": 0, "2/3": 0, "3/2": 0}
        
        diff_stats[diff_val]["total"] += 1
        if outcome in ["2/3", "3/2"]:
            diff_stats[diff_val][outcome] += 1

        print(f"📊 [УЧЕТ Δ2D={diff_val}] Всего сигналов: {diff_stats[diff_val]['total']} | 2/3: {diff_stats[diff_val]['2/3']} | 3/2: {diff_stats[diff_val]['3/2']}")


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


def _make_pred(di_num, prev_2d, curr_2d, diff_2d):
    """Формирует прогноз на исходы 2/3 и 3/2"""
    next_di_num = di_num + 1
    
    text = (
        f"🔥 <b>СИГНАЛ | БАККАРА</b>\n"
        f"──────────────────────────────\n"
        f"🎯 <b>Ожидаются игры:</b> #N{di_num} - #N{next_di_num}\n"
        f"🎲 <b>Прогноз исходов:</b> <code>2/3</code> и <code>3/2</code>\n"
        f"──────────────────────────────\n"
        f"📉 <b>2 последние цифры ID:</b> {prev_2d} ➔ {curr_2d}\n"
        f"📐 <b>Разница (Δ2D):</b> {diff_2d}\n"
        f"──────────────────────────────\n"
        f"⏳ <i>Вход на 1-2 шага (догон)</i>"
    )
    
    sent = send_prediction(text)
    if sent:
        log_diff_stat(diff_2d)
    return bool(sent)


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games:
        return

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
            
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid:
            continue
        
        gid_num = int(gid)
        di_num = int(di)

        if last_processed_gid is not None:
            prev_2d = get_last_2_digits(last_processed_gid)
            curr_2d = get_last_2_digits(gid_num)
            
            if prev_2d is not None and curr_2d is not None:
                # ГЛАВНОЕ УСЛОВИЕ: Предыдущие 2 цифры БОЛЬШЕ текущих
                if prev_2d > curr_2d:
                    diff_2d = prev_2d - curr_2d
                    if _make_pred(di_num, prev_2d, curr_2d, diff_2d):
                        print(f"🔥 БАККАРА: Сигнал #N{di_num}-#N{di_num+1} | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")
                else:
                    print(f"ℹ️ [Баккара] #N{di_num} Пропуск: 2D возрастают ({prev_2d} <= {curr_2d})")

        last_processed_gid = gid_num

    if new_games:
        print(f"✅ Обработано новых игр Баккары: {len(new_games)} | Последний GID: {last_processed_gid}")
        
    if len(sent_games) > 300: 
        sent_games.clear()


def main():
    print("🚀 ЗАПУСК БОТА (ФИЛЬТР: PREV_2D > CURR_2D | ПРОГНОЗ: 2/3 И 3/2)")
    send_to_channel("🟢 <b>Бот запущен</b> | Сигналы на исходы 2/3 и 3/2 при убывании 2-х цифр ID")
    
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
