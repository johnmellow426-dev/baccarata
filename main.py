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
processed_game_ids = set()
completed_count = 0

prediction = {
    "active": False,
    "game_num": None,
    "base_count": None,
    "suit": None,
    "message_id": None,
    "checked": False
}

executor = ThreadPoolExecutor(max_workers=4)

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
        else:
            bot.edit_message_text(chat_id=CHANNEL_ID, message_id=prediction["message_id"], text=msg)
    except Exception as e:
        prediction["message_id"] = None

def reset_prediction():
    prediction["active"] = False
    prediction["game_num"] = None
    prediction["base_count"] = None
    prediction["suit"] = None
    prediction["message_id"] = None
    prediction["checked"] = False

def process_finished_game(game_id, player_suits):
    global completed_count
    
    if game_id in processed_game_ids:
        return
    
    processed_game_ids.add(game_id)
    if player_suits:
        history.extend(player_suits)
    completed_count += 1
    
    if prediction["active"] and not prediction["checked"] and prediction["base_count"] is not None:
        offset = completed_count - prediction["base_count"] - 1
        
        if 0 <= offset <= 2:
            if prediction["suit"] in (player_suits or []):
                emoji_map = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
                update_message(emoji_map[offset])
                prediction["checked"] = True
                time.sleep(1)
                reset_prediction()
            elif offset == 2:
                update_message("❌")
                prediction["checked"] = True
                time.sleep(1)
                reset_prediction()

def main():
    global completed_count
    
    print("🚀 Запуск бота БАККАРА (ускоренный)...")
    
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
                history.extend(suits)
                processed_game_ids.add(gid)
                completed_count += 1
        
        print(f"📊 Начальная история: {len(history)} карт, {completed_count} игр")
    except Exception as e:
        print(f"⚠️ Ошибка начального сбора: {e}")
    
    while True:
        try:
            resp = requests.get(LIST_URL, headers=HEADERS, timeout=5, proxies=NO_PROXY)
            games = resp.json().get("Value", [])
            
            # Параллельная обработка завершенных игр
            futures = []
            for g in games:
                gid = g.get("I")
                if g.get("SC", {}).get("CPS") == "Игра завершена" and gid not in processed_game_ids:
                    futures.append((gid, executor.submit(fetch_game_details, gid)))
            
            for gid, future in futures:
                suits, _ = future.result(timeout=5)
                process_finished_game(gid, suits)
            
            # Создание нового прогноза
            if not prediction["active"] or prediction["checked"]:
                next_game = None
                for g in games:
                    if g.get("SC", {}).get("I") == "Ставки до начала игры":
                        next_game = g
                        break
                
                if next_game:
                    next_id = next_game.get("I")
                    game_num = get_utc_game_number()
                    
                    _, odds = fetch_game_details(next_id)
                    if odds:
                        best_suit = calculate_best_suit(odds)
                        
                        prediction["active"] = True
                        prediction["game_num"] = game_num
                        prediction["base_count"] = completed_count
                        prediction["suit"] = best_suit
                        prediction["message_id"] = None
                        prediction["checked"] = False
                        
                        update_message()
            
            time.sleep(1)  # Ускорено с 5 до 1 секунды
            
        except Exception as e:
            time.sleep(1)

if __name__ == "__main__":
    main()
