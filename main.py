import requests
import json
import time
import os
import datetime
import telebot

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
    1: {"name": "Трефы", "symbol": "️♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# История мастей Игрока
history = []
processed_game_ids = set()
completed_count = 0  # Счетчик завершенных игр

# Состояние прогноза
prediction = {
    "active": False,
    "game_num": None,       # Номер игры для отображения (UTC)
    "base_count": None,     # Счетчик на момент прогноза
    "suit": None,
    "message_id": None,
    "checked": False        # Прогноз проверен (успех/провал)
}

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def fetch_game_details(game_id):
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=10, proxies=NO_PROXY)
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
        print(f"⚠️ Ошибка деталей #{game_id}: {e}")
        return None, None

def calculate_best_suit(current_odds):
    """Выбирает масть с наибольшей аномалией"""
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
    """Отправляет или редактирует сообщение"""
    if not prediction["active"] or prediction["suit"] is None:
        return
    
    game_num = prediction["game_num"]
    suit = prediction["suit"]
    
    msg = f"БАККАРА #N{game_num}\n"
    msg += f"🂠 Масть: {SUITS[suit]['symbol']} {SUITS[suit]['name']}"
    if suffix:
        msg += f" {suffix}"
    
    try:
        if prediction["message_id"] is None:
            sent = bot.send_message(CHANNEL_ID, msg, parse_mode=None)
            prediction["message_id"] = sent.message_id
            print(f" Отправлено: {msg}")
        else:
            bot.edit_message_text(chat_id=CHANNEL_ID, message_id=prediction["message_id"], text=msg)
            print(f"✏️ Обновлено: {msg}")
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}")
        prediction["message_id"] = None

def reset_prediction():
    """Сброс для нового прогноза"""
    prediction["active"] = False
    prediction["game_num"] = None
    prediction["base_count"] = None
    prediction["suit"] = None
    prediction["message_id"] = None
    prediction["checked"] = False

def process_finished_game(game_id, player_suits):
    """Обработка завершенной игры"""
    global completed_count
    
    if game_id in processed_game_ids:
        return
    
    processed_game_ids.add(game_id)
    if player_suits:
        history.extend(player_suits)
    completed_count += 1
    
    print(f"📥 Игра завершена #{game_id} | Счетчик: {completed_count} | Масти: {[SUITS[s]['symbol'] for s in (player_suits or [])]}")
    
    # Проверяем прогноз
    if prediction["active"] and not prediction["checked"] and prediction["base_count"] is not None:
        offset = completed_count - prediction["base_count"] - 1  # 0, 1 или 2
        
        if 0 <= offset <= 2:
            if prediction["suit"] in (player_suits or []):
                # Успех!
                emoji_map = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
                update_message(emoji_map[offset])
                print(f"✅ Успех на позиции {offset} (игра #{completed_count})")
                prediction["checked"] = True
                time.sleep(3)
                reset_prediction()
            elif offset == 2:
                # Последняя проверка, масти нет - провал
                update_message("❌")
                print(f" Провал (игра #{completed_count})")
                prediction["checked"] = True
                time.sleep(3)
                reset_prediction()

def main():
    global completed_count
    
    print("🚀 Запуск бота БАККАРА (диапазон N, N+1, N+2)...")
    
    # Начальный сбор истории
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
        games = resp.json().get("Value", [])
        for g in games:
            if g.get("SC", {}).get("CPS") == "Игра завершена":
                gid = g.get("I")
                if gid not in processed_game_ids:
                    suits, _ = fetch_game_details(gid)
                    if suits:
                        history.extend(suits)
                        processed_game_ids.add(gid)
                        completed_count += 1
        print(f"📊 Начальная история: {len(history)} карт, {completed_count} игр")
    except Exception as e:
        print(f"️ Ошибка начального сбора: {e}")
    
    while True:
        try:
            resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
            games = resp.json().get("Value", [])
            
            # 1. Обработка завершенных игр
            for g in games:
                gid = g.get("I")
                if g.get("SC", {}).get("CPS") == "Игра завершена" and gid not in processed_game_ids:
                    suits, _ = fetch_game_details(gid)
                    process_finished_game(gid, suits)
            
            # 2. Если прогноз проверен или не активен - создаем новый
            if not prediction["active"] or prediction["checked"]:
                # Ищем следующую игру
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
                        print(f"🎯 Новый прогноз: БАККАРА #{game_num}, масть {SUITS[best_suit]['name']}, база счетчика: {completed_count}")
            
            time.sleep(5)
            
        except Exception as e:
            print(f"❌ Ошибка цикла: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
