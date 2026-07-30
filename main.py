import os
import time
import json
import datetime
import threading
import requests
import telebot
from telebot.apihelper import ApiTelegramException

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID", CHANNEL_ID)

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# --- КОНСТАНТЫ ---
SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
processed_game_ids = set()
logged_game_ids = set()
prediction_created_for_game = set()  # Чтобы не спамить прогнозами на одну и ту же игру

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
    except Exception as e:
        print(f"⚠️ Ошибка запроса деталей: {e}")
    return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ Ошибка запроса списка: {e}")
    return []

def get_all_game_cards(game_data):
    """Разделяем карты Игрока (P) и Дилера (B)"""
    result = {"player": [], "dealer": [], "result": None}
    try:
        sc = game_data.get("SC", {})
        s_list = sc.get("S", [])
        for item in s_list:
            key = item.get("Key", "")
            value = item.get("Value", "")
            
            if key == "S":
                result["result"] = value
                continue
            
            cards = []
            try:
                if value and not str(value).startswith("Win"):
                    parsed = json.loads(value) if isinstance(value, str) else value
                    for c in parsed:
                        val = c.get("R") or c.get("CV") or c.get("C", 0)
                        suit = c.get("S") or c.get("CS") or c.get("Suit", 0)
                        if val > 0:
                            cards.append({"value": int(val), "suit": int(suit)})
            except Exception as e:
                print(f"⚠️ Ошибка парсинга карт: {e}")

            if key == "P":
                result["player"] = cards
            elif key == "B":
                result["dealer"] = cards
    except Exception as e:
        print(f"⚠️ Ошибка получения карт: {e}")
    return result

def send_prediction(suit_code, game_num, target_num, game_id):
    suit = SUITS.get(suit_code, {})
    msg = (
        f"🎯 ПРОГНОЗ МАСТИ ИГРОКА\n\n"
        f"📌 Триггер: #{game_num} (ID: {game_id})\n"
        f"♦️ Масть: {suit.get('symbol')} {suit.get('name')}\n"
        f"🎯 Целевая игра: #{target_num}\n"
        f"⏳ Ожидание результата у игрока..."
    )
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
            text=f"🎯 ПРОГНОЗ МАСТИ ИГРОКА\n\n{emoji} {details}"
        )
    except Exception as e:
        print(f"⚠️ Ошибка редактирования: {e}")

def process_games():
    games = get_active_games()
    if not games:
        print("⚠️ Нет активных игр")
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
        
        # Получаем карты отдельно для игрока и дилера
        cards_info = get_all_game_cards(game_data)
        player_cards = cards_info.get("player", [])
        is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"

        # Масть по ID (для игрока)
        suit_code = int(gid) % 4
        suit = SUITS.get(suit_code, {})

        # Логирование (теперь видно карты именно игрока!)
        if gid not in logged_game_ids:
            logged_game_ids.add(gid)
            player_suits_str = ", ".join([f"{c['value']}({c['suit']})" for c in player_cards])
            print(f"🆕 #{game_num} ID:{gid} | Игрок карты: [{player_suits_str}] | Прогноз масти: {suit['symbol']}")

        # Проверка активного прогноза
        with state_lock:
            if active_suit_prediction["active"]:
                target = active_suit_prediction["target_game_num"]
                diff = normalize_game_num(game_num - target)

                if 0 <= diff <= 2 and gid not in active_suit_prediction["checked_game_ids"]:
                    active_suit_prediction["checked_game_ids"].add(gid)
                    active_suit_prediction["checked_games_count"] += 1

                    target_suit = active_suit_prediction["predicted_suit_code"]
                    
                    # 🔥 ПРОВЕРЯЕМ ТОЛЬКО У ИГРОКА ("P")
                    hit = any(c["suit"] == target_suit for c in player_cards)

                    if hit:
                        update_prediction(
                            active_suit_prediction["message_id"],
                            True,
                            f"ЗАШЁЛ на игре #{game_num}!\n🃏 У игрока выпала нужная масть!"
                        )
                        print(f"✅ Прогноз зашёл на игре #{game_num}!")
                        active_suit_prediction["active"] = False
                        
                    elif active_suit_prediction["checked_games_count"] >= 3 or (is_finished and diff == 2):
                        update_prediction(
                            active_suit_prediction["message_id"],
                            False,
                            f"НЕ зашёл (3 попытки исчерпаны)"
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
                    print(f"🎯 Создан прогноз на игру #{target_num}")

def main():
    print("🚀 ЗАПУСК БОТА (Проверка только карт ИГРОКА)")
    while True:
        try:
            process_games()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ Ошибка в главном цикле: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
