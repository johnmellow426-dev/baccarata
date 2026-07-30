import os
import time
import json
import datetime
import threading
import requests
from collections import defaultdict
import telebot
from telebot.apihelper import ApiTelegramException

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID", CHANNEL_ID)

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true&virtualSports=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# --- КОНСТАНТЫ ---
SUITS = {
    0: {"name": "Пики", "symbol": "️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
processed_game_ids = set()
logged_game_ids = set()
prediction_created_for_game = set()  # НОВОЕ: отслеживаем созданные прогнозы

active_suit_prediction = {
    "active": False,
    "message_id": None,
    "trigger_game_num": None,
    "trigger_game_id": None,
    "predicted_suit_code": None,
    "target_game_num": None,
    "checked_games_count": 0,
    "checked_game_ids": set()
}

state_lock = threading.Lock()

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize_game_num(num):
    while num > 1440: num -= 1440
    while num < 1: num += 1440
    return num

def fetch_game_details(game_id):
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("Value", {})
    except:
        pass
    return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except:
        pass
    return []

def parse_cards_from_api(cards_json):
    try:
        if not cards_json or str(cards_json).startswith("Win"):
            return []
        cards = json.loads(cards_json) if isinstance(cards_json, str) else cards_json
        return [{"value": c.get("R", 0), "suit": c.get("S", 0)} for c in cards if c.get("R", 0) > 0]
    except:
        return []

def get_all_game_cards(game_data):
    result = []
    try:
        for item in game_data.get("SC", {}).get("S", []):
            if item.get("Key") in ["P", "B"]:
                result.extend(parse_cards_from_api(item.get("Value")))
    except:
        pass
    return result

def send_prediction(suit_code, game_num, target_num, game_id):
    suit = SUITS.get(suit_code, {})
    msg = f"🎯 ПРОГНОЗ МАСТИ\n\n📌 Триггер: #{game_num} (ID: {game_id})\n♦️ Масть: {suit.get('symbol')} {suit.get('name')}\n🎯 Целевая: #{target_num}\n⏳ Ожидание..."
    
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg)
        return sent.message_id
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None

def update_prediction(msg_id, success, details=""):
    try:
        emoji = "✅" if success else "❌"
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID,
            message_id=msg_id,
            text=f"🎯 ПРОГНОЗ МАСТИ\n\n{emoji} {details}"
        )
    except:
        pass

def process_games():
    games = get_active_games()
    if not games:
        print("⚠️ Нет игр")
        return

    game_num = get_utc_game_number()
    print(f"🔄 Цикл | Игра #{game_num} | Загружено: {len(games)}")

    for g in games:
        gid = g.get("I")
        if not gid or gid in processed_game_ids:
            continue

        game_data = fetch_game_details(gid)
        if not game_data:
            continue

        processed_game_ids.add(gid)
        all_cards = get_all_game_cards(game_data)
        is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"

        # Масть по ID
        suit_code = int(gid) % 4
        suit = SUITS.get(suit_code, {})

        # Логирование
        if gid not in logged_game_ids:
            logged_game_ids.add(gid)
            print(f"🆕 #{game_num} ID:{gid} Масть:{suit['symbol']}")

        # Проверка активного прогноза
        with state_lock:
            if active_suit_prediction["active"]:
                target = active_suit_prediction["target_game_num"]
                diff = normalize_game_num(game_num - target)

                if 0 <= diff <= 2 and gid not in active_suit_prediction["checked_game_ids"]:
                    active_suit_prediction["checked_game_ids"].add(gid)
                    active_suit_prediction["checked_games_count"] += 1

                    target_suit = active_suit_prediction["predicted_suit_code"]
                    hit = any(c["suit"] == target_suit for c in all_cards)

                    if hit:
                        update_prediction(
                            active_suit_prediction["message_id"],
                            True,
                            f"ЗАШЁЛ на игре #{game_num}!"
                        )
                        print(f"✅ Прогноз зашёл!")
                        active_suit_prediction["active"] = False
                    elif active_suit_prediction["checked_games_count"] >= 3 or (is_finished and diff == 2):
                        update_prediction(
                            active_suit_prediction["message_id"],
                            False,
                            f"НЕ зашёл (3 попытки)"
                        )
                        print(f"❌ Прогноз не зашёл")
                        active_suit_prediction["active"] = False

            # Создание нового прогноза (ТОЛЬКО ОДИН РАЗ на игру)
            elif not active_suit_prediction["active"] and gid not in prediction_created_for_game:
                target_num = normalize_game_num(game_num + 3)
                msg_id = send_prediction(suit_code, game_num, target_num, gid)

                if msg_id:
                    active_suit_prediction.update({
                        "active": True,
                        "message_id": msg_id,
                        "trigger_game_num": game_num,
                        "trigger_game_id": gid,
                        "predicted_suit_code": suit_code,
                        "target_game_num": target_num,
                        "checked_games_count": 0,
                        "checked_game_ids": set()
                    })
                    prediction_created_for_game.add(gid)
                    print(f"🎯 Прогноз на #{target_num}")

def main():
    print("🚀 ЗАПУСК БОТА")
    while True:
        try:
            process_games()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
