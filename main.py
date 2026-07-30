import os
import time
import json
import datetime
import threading
import requests
import telebot
from collections import defaultdict

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID", CHANNEL_ID)

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
})

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

STEP_EMOJIS = {0: "0️⃣", 1: "1️⃣", 2: "2️⃣"}

# --- ХРАНИЛИЩЕ В ПАМЯТИ ---
logged_game_ids = set()
games_by_number = defaultdict(list)
analyzed_games = set()
prediction_created_for_game = set()

active_prediction = {
    "active": False,
    "message_id": None,
    "predicted_suit": None,
    "target_game_num": None,
    "checked_game_ids": set(),
    "step": 0
}
state_lock = threading.Lock()

stats = {"total": 0, "hits": 0, "misses": 0}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize_game_num(num):
    return ((num - 1) % 1440) + 1

def fetch_game_details(game_id):
    try:
        resp = session.get(DETAIL_URL_TEMPLATE.format(game_id=game_id), timeout=4)
        if resp.status_code == 200:
            return resp.json().get("Value", {})
    except Exception:
        pass
    return None

def get_active_games():
    try:
        resp = session.get(LIST_URL, timeout=4)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception:
        pass
    return []

def get_player_cards_and_status(game_data):
    result = {"player": [], "is_finished": False}
    try:
        sc = game_data.get("SC", {})
        cps = str(sc.get("CPS", "")).lower()
        result["is_finished"] = any(w in cps for w in ["завершена", "finished"]) or sc.get("GE") == 1 or sc.get("IsFinished") == 1

        for item in sc.get("S", []):
            if str(item.get("Key", "")).upper() == "P":
                value = item.get("Value", "")
                parsed = json.loads(value) if isinstance(value, str) else value
                if isinstance(parsed, list):
                    for c in parsed:
                        val = c.get("R") or c.get("CV") or c.get("C", 0)
                        suit = c.get("S") or c.get("CS") or c.get("Suit", 0)
                        if val and int(val) > 0:
                            result["player"].append({"value": int(val), "suit": int(suit)})
                break
    except Exception:
        pass
    return result

def select_optimal_suit_and_id(four_ids):
    suits_map = {int(gid) % 4: gid for gid in four_ids}
    for suit_code in [0, 1, 2, 3]:  # Приоритет: ♠ → ♣ → ♦ → ♥
        if suit_code in suits_map:
            return suits_map[suit_code], suit_code
    return four_ids[0], int(four_ids[0]) % 4

# --- РАБОТА С TELEGRAM ---
def send_prediction_msg(suit_code, target_num):
    suit = SUITS.get(suit_code, {})
    text = f"⚡️ #{target_num} ➔ {suit['symbol']} {suit['name']}\n⏳ Ожидание..."
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, text)
        return sent.message_id
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None

def update_prediction_msg(msg_id, suit_code, target_num, is_win, step=0):
    suit = SUITS.get(suit_code, {})
    if is_win:
        step_str = STEP_EMOJIS.get(step, "")
        text = f"✅ #{target_num} ➔ {suit['symbol']} {suit['name']} {step_str}"
    else:
        text = f"❌ #{target_num} ➔ {suit['symbol']} {suit['name']}"
        
    try:
        bot.edit_message_text(chat_id=PREDICTION_CHANNEL_ID, message_id=msg_id, text=text)
    except Exception:
        pass

# --- ОСНОВНАЯ ЛОГИКА ---
def process_games():
    games = get_active_games()
    if not games:
        return

    current_gnum = get_utc_game_number()

    # Группировка игр по номеру
    for g in games:
        gid = g.get("I")
        if not gid or gid in logged_game_ids:
            continue
        
        logged_game_ids.add(gid)
        games_by_number[current_gnum].append(gid)

    # 1. СОЗДАНИЕ НОВОГО ПРОГНОЗА
    for gnum, ids in list(games_by_number.items()):
        if gnum in analyzed_games or len(ids) < 3:
            continue

        analyzed_games.add(gnum)
        optimal_id, optimal_suit = select_optimal_suit_and_id(ids)

        with state_lock:
            if not active_prediction["active"] and gnum not in prediction_created_for_game:
                target_num = normalize_game_num(gnum + 3)
                msg_id = send_prediction_msg(optimal_suit, target_num)

                if msg_id:
                    stats["total"] += 1
                    active_prediction.update({
                        "active": True,
                        "message_id": msg_id,
                        "predicted_suit": optimal_suit,
                        "target_game_num": target_num,
                        "checked_game_ids": set(),
                        "step": 0
                    })
                    prediction_created_for_game.add(gnum)
                    print(f"🚀 Прогноз выложен: #{target_num} ➔ {SUITS[optimal_suit]['symbol']}")

        # 2. ПРОВЕРКА ИСХОДОВ ДЛЯ АКТИВНОГО ПРОГНОЗА
        with state_lock:
            if not active_prediction["active"]:
                continue

            target_num = active_prediction["target_game_num"]
            target_suit = active_prediction["predicted_suit"]

            # Проверяем целевую игру и 2 следующих догона
            for check_offset in range(3):
                check_num = normalize_game_num(target_num + check_offset)
                check_ids = games_by_number.get(check_num, [])

                for gid in check_ids:
                    if gid in active_prediction["checked_game_ids"]:
                        continue

                    game_data = fetch_game_details(gid)
                    if not game_data:
                        continue

                    cards_info = get_player_cards_and_status(game_data)
                    if not cards_info["is_finished"]:
                        continue

                    active_prediction["checked_game_ids"].add(gid)
                    player_cards = cards_info["player"]

                    # Условие захода
                    is_hit = any(c["suit"] == target_suit for c in player_cards)

                    if is_hit:
                        update_prediction_msg(
                            active_prediction["message_id"], 
                            target_suit, 
                            target_num, 
                            is_win=True, 
                            step=check_offset
                        )
                        stats["hits"] += 1
                        active_prediction["active"] = False
                        print(f"🎉 ЗАШЕЛ на шаг +{check_offset} (#{check_num})")
                        break

            # Если проверены все 3 игры (основная + 2 догона) и захода нет — оформляем минус
            if active_prediction["active"]:
                last_check_num = normalize_game_num(target_num + 2)
                # Если текущая игра ушла дальше 3-х целевых игр
                if (current_gnum - target_num) % 1440 > 3 and len(active_prediction["checked_game_ids"]) >= 3:
                    update_prediction_msg(
                        active_prediction["message_id"], 
                        target_suit, 
                        target_num, 
                        is_win=False
                    )
                    stats["misses"] += 1
                    active_prediction["active"] = False
                    print(f"❌ НЕ ЗАШЕЛ на #{target_num}")

    # Чистка старых данных из памяти каждые 100 игр
    if len(logged_game_ids) > 1000:
        logged_game_ids.clear()
        analyzed_games.clear()

def main():
    print("🚀 БОТ УСПЕШНО ЗАПУЩЕН")
    print("=" * 40)
    
    while True:
        try:
            process_games()
            time.sleep(2.5)
        except Exception as e:
            print(f"⚠️ Ошибка цикла: {e}")
            time.sleep(4)

if __name__ == "__main__":
    main()
