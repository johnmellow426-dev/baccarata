import requests
import json
import time
import os
import datetime
import telebot
from concurrent.futures import ThreadPoolExecutor

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://melbet-5427.pro/",
}
NO_PROXY = {"http": None, "https": None}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

history = []
processed_game_ids = set()  # Игры, добавленные в историю
checked_game_ids = set()    # Игры, проверенные для прогноза
completed_count = 0

# Отслеживание количества карт по играм
game_card_counts = {}

prediction = {
    "active": False,
    "game_num": None,
    "base_count": None,
    "suit": None,
    "message_id": None,
    "checked": False
}

executor = ThreadPoolExecutor(max_workers=6)

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def fetch_game_details(game_id):
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=5, proxies=NO_PROXY)
        if resp.status_code != 200:
            return None, None
        data = resp.json().get("Value", {})
        
        player_suits = []
        for item in data.get("SC", {}).get("S", []):
            if item.get("Key") == "P":
                cards = json.loads(item.get("Value", "[]"))
                player_suits = [c.get("S") for c in cards if c.get("S") in SUITS]
        
        current_odds = {0: 1.90, 1: 1.90, 2: 1.90, 3: 1.90}
        for group in data.get("GE", []):
            if group.get("G") == 10185:
                for event in group.get("E", [[]])[0]:
                    name = event.get("PL", {}).get("N", "")
                    cf = event.get("C")
                    if "Пики" in name: current_odds[0] = cf
                    elif "Трефы" in name: current_odds[1] = cf
                    elif "Бубны" in name: current_odds[2] = cf
                    elif "Червы" in name: current_odds[3] = cf
                break
        return player_suits, current_odds
    except Exception as e:
        return None, None

def calculate_best_suit(current_odds):
    if len(history) < 3:
        return 0
    
    suit_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    suit_last_seen = {0: -1, 1: -1, 2: -1, 3: -1}
    
    for idx, suit in enumerate(history):
        suit_counts[suit] += 1
        suit_last_seen[suit] = idx
    
    scores = {}
    for suit in SUITS:
        streak = len(history) if suit_last_seen[suit] == -1 else (len(history) - 1) - suit_last_seen[suit]
        freq = suit_counts[suit] / len(history)
        odds_drop = 1.90 - current_odds[suit]
        scores[suit] = (streak * 0.4) + ((0.25 - freq) * 100 * 0.4) + (max(odds_drop, 0) * 10)
    
    return max(scores, key=scores.get)

def update_message(suffix=""):
    if not prediction["active"] or prediction["suit"] is None:
        return
    
    game_num = prediction["game_num"]
    suit = prediction["suit"]
    
    msg = f"БАККАРА #N{game_num}\n"
    msg += f" Масть: {SUITS[suit]['symbol']} {SUITS[suit]['name']}"
    if suffix:
        msg += f" {suffix}"
    
    try:
        if prediction["message_id"] is None:
            sent = bot.send_message(CHANNEL_ID, msg, parse_mode=None)
            prediction["message_id"] = sent.message_id
            print(f"📤 Отправлено: {msg}")
        else:
            bot.edit_message_text(chat_id=CHANNEL_ID, message_id=prediction["message_id"], text=msg)
            print(f"✏️ Обновлено: {msg}")
    except Exception as e:
        prediction["message_id"] = None

def reset_prediction():
    prediction["active"] = False
    prediction["game_num"] = None
    prediction["base_count"] = None
    prediction["suit"] = None
    prediction["message_id"] = None
    prediction["checked"] = False

def add_to_history(game_id, player_suits):
    """Добавляет карты в историю для прогноза (первые 2 карты)"""
    global completed_count
    
    if game_id in processed_game_ids:
        return
    
    processed_game_ids.add(game_id)
    if player_suits:
        # Берем только первые 2 карты для прогноза
        history.extend(player_suits[:2])
    completed_count += 1
    
    print(f"⚡ Добавлено в историю: Игра #{game_id} | Карт: {len(player_suits[:2])} | Масти: {[SUITS[s]['symbol'] for s in player_suits[:2]]} | Счетчик: {completed_count}")

def check_prediction(game_id, player_suits):
    """Проверяет прогноз по всем картам игры (до 3)"""
    if game_id in checked_game_ids:
        return
    
    if not prediction["active"] or prediction["checked"] or prediction["base_count"] is None:
        return
    
    offset = completed_count - prediction["base_count"]
    
    if 1 <= offset <= 3:
        # Проверяем по ВСЕМ картам (до 3)
        all_suits = player_suits[:3] if player_suits else []
        
        if prediction["suit"] in all_suits:
            emoji_map = {1: "✅0️⃣", 2: "✅1️⃣", 3: "✅2️⃣"}
            update_message(emoji_map[offset])
            print(f"✅ Успех на позиции {offset-1} (игра #{game_id}, карты: {[SUITS[s]['symbol'] for s in all_suits]})")
            prediction["checked"] = True
            checked_game_ids.add(game_id)
            reset_prediction()
        elif offset == 3:
            update_message("❌")
            print(f"❌ Провал (игра #{game_id}, карты: {[SUITS[s]['symbol'] for s in all_suits]})")
            prediction["checked"] = True
            checked_game_ids.add(game_id)
            reset_prediction()

def create_prediction():
    global prediction
    
    next_game_num = get_utc_game_number() + 1
    
    resp = requests.get(LIST_URL, headers=HEADERS, timeout=5, proxies=NO_PROXY)
    games = resp.json().get("Value", [])
    
    next_game = None
    for g in games:
        scores = g.get("SC", {})
        fs = scores.get("FS", {})
        s1 = fs.get("S1", 0)
        s2 = fs.get("S2", 0)
        if s1 == 0 and s2 == 0 and scores.get("CPS") != "Игра завершена":
            next_game = g
            break
    
    if not next_game:
        return False
    
    next_id = next_game.get("I")
    _, odds = fetch_game_details(next_id)
    
    if not odds:
        return False
    
    best_suit = calculate_best_suit(odds)
    
    prediction["active"] = True
    prediction["game_num"] = next_game_num
    prediction["base_count"] = completed_count
    prediction["suit"] = best_suit
    prediction["message_id"] = None
    prediction["checked"] = False
    
    update_message()
    print(f"🎯 Прогноз на БАККАРА #N{next_game_num}, масть {SUITS[best_suit]['name']}, база: {completed_count}")
    return True

def main():
    global completed_count
    
    print("🚀 Запуск бота БАККАРА (2 карты для прогноза, 3 для проверки)...")
    
    # Начальный сбор истории
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
        games = resp.json().get("Value", [])
        
        futures = []
        for g in games:
            if g.get("SC", {}).get("CPS") == "Игра завершена":
                gid = g.get("I")
                if gid not in processed_game_ids:
                    futures.append((gid, executor.submit(fetch_game_details, gid)))
        
        for gid, future in futures:
            suits, _ = future.result(timeout=10)
            if suits:
                add_to_history(gid, suits)
                checked_game_ids.add(gid)  # Завершенные игры сразу помечаем как проверенные
        
        print(f"📊 Начальная история: {len(history)} карт, {completed_count} игр")
    except Exception as e:
        print(f"️ Ошибка начального сбора: {e}")
    
    while True:
        try:
            resp = requests.get(LIST_URL, headers=HEADERS, timeout=5, proxies=NO_PROXY)
            games = resp.json().get("Value", [])
            
            # Обработка игр
            futures = []
            for g in games:
                gid = g.get("I")
                scores = g.get("SC", {})
                fs = scores.get("FS", {})
                s1 = fs.get("S1", 0)
                s2 = fs.get("S2", 0)
                is_finished = scores.get("CPS") == "Игра завершена"
                
                # Игра идет или завершена
                if (is_finished or (s1 > 0 or s2 > 0)):
                    # Проверяем, изменилось ли количество карт
                    _, current_suits = fetch_game_details(gid)
                    if current_suits:
                        card_count = len(current_suits)
                        prev_count = game_card_counts.get(gid, 0)
                        game_card_counts[gid] = card_count
                        
                        # Если карт стало больше или игра завершена
                        if card_count > prev_count or is_finished:
                            futures.append((gid, current_suits, is_finished))
            
            for gid, suits, is_finished in futures:
                # Добавляем в историю если еще не добавлено (первые 2 карты)
                if gid not in processed_game_ids and len(suits) >= 2:
                    add_to_history(gid, suits)
                
                # Проверяем прогноз если карт 3 или игра завершена
                if (len(suits) >= 3 or is_finished) and gid not in checked_game_ids:
                    check_prediction(gid, suits)
            
            # Создание прогноза
            if not prediction["active"] or prediction["checked"]:
                create_prediction()
            
            time.sleep(1)
            
        except Exception as e:
            time.sleep(1)

if __name__ == "__main__":
    main()
