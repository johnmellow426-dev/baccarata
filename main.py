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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
logged_game_ids = set()
games_by_number = defaultdict(list)
analyzed_games = set()
prediction_created_for_game = set()

pattern_stats = {
    "all_4_suits": 0,
    "three_suits": 0,
    "two_suits": 0,
    "one_suit": 0,
    "total_analyzed": 0
}

game_history = []

active_suit_prediction = {
    "active": False, "message_id": None, "trigger_game_num": None,
    "trigger_game_id": None, "predicted_suit_code": None,
    "target_game_num": None, "checked_games_count": 0, "checked_game_ids": set()
}
state_lock = threading.Lock()

stats = {"total_seen": 0, "finished": 0, "with_cards": 0, "predictions": 0, "hits": 0, "misses": 0}

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize_game_num(num):
    while num > 1440: num -= 1440
    while num < 1: num += 1440
    return num

def fetch_game_details(game_id):
    try:
        resp = requests.get(DETAIL_URL_TEMPLATE.format(game_id=game_id), headers=HEADERS, timeout=5)
        if resp.status_code == 200: return resp.json().get("Value", {})
    except: pass
    return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=5)
        if resp.status_code == 200: return resp.json().get("Value", [])
    except: pass
    return []

def get_all_game_cards(game_data):
    result = {"player": [], "is_finished": False}
    try:
        sc = game_data.get("SC", {})
        cps = str(sc.get("CPS", "")).lower()
        result["is_finished"] = "завершена" in cps or "finished" in cps or sc.get("GE") == 1 or sc.get("IsFinished") == 1

        for item in sc.get("S", []):
            key = str(item.get("Key", "")).upper()
            value = item.get("Value", "")
            if key == "S": continue
            
            cards = []
            try:
                if value:
                    parsed = json.loads(value) if isinstance(value, str) else value
                    if isinstance(parsed, list):
                        for c in parsed:
                            val = c.get("R") or c.get("CV") or c.get("C", 0)
                            suit = c.get("S") or c.get("CS") or c.get("Suit", 0)
                            if val and int(val) > 0:
                                cards.append({"value": int(val), "suit": int(suit)})
            except: pass
            
            if key == "P": result["player"] = cards
    except: pass
    return result

def analyze_four_ids(game_num, four_ids):
    pattern_stats["total_analyzed"] += 1
    
    suits_map = {}
    for gid in four_ids:
        suit_code = int(gid) % 4
        suits_map[suit_code] = gid
    
    unique_suits = set(suits_map.keys())
    num_suits = len(unique_suits)
    
    if num_suits == 4:
        pattern_stats["all_4_suits"] += 1
        pattern_type = "ALL_4"
    elif num_suits == 3:
        pattern_stats["three_suits"] += 1
        pattern_type = "THREE"
    elif num_suits == 2:
        pattern_stats["two_suits"] += 1
        pattern_type = "TWO"
    else:
        pattern_stats["one_suit"] += 1
        pattern_type = "ONE"
    
    # Выбираем оптимальный ID (приоритет: ♠→♣→♦→♥)
    optimal_id = None
    optimal_suit = None
    
    for suit_code in [0, 1, 2, 3]:
        if suit_code in suits_map:
            optimal_id = suits_map[suit_code]
            optimal_suit = suit_code
            break
    
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    suits_str = ", ".join([f"{SUITS[s]['symbol']}(ID:{suits_map[s]})" for s in sorted(unique_suits)])
    print(f"🔍 [{timestamp}] #{game_num} | Паттерн: {pattern_type} | Масти: {suits_str}")
    print(f"   ✅ Оптимальный ID: {optimal_id} → {SUITS[optimal_suit]['symbol']} {SUITS[optimal_suit]['name']}")
    
    return optimal_id, optimal_suit, pattern_type

def send_prediction(suit_code, game_num, target_num, game_id):
    suit = SUITS.get(suit_code, {})
    msg = f"🎯 ПРОГНОЗ МАСТИ ИГРОКА\n\n Триггер: #{game_num} (ID: {game_id})\n♦️ Масть: {suit.get('symbol')} {suit.get('name')}\n🎯 Целевая игра: #{target_num}\n⏳ Ожидание..."
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg)
        return sent.message_id
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return None

def update_prediction(msg_id, success, details=""):
    try:
        emoji = "✅" if success else "❌"
        bot.edit_message_text(chat_id=PREDICTION_CHANNEL_ID, message_id=msg_id, text=f"🎯 ПРОГНОЗ МАСТИ ИГРОКА\n\n{emoji} {details}")
    except: pass

def print_stats():
    print(f"\n📊 СТАТИСТИКА:")
    print(f"   Всего игр: {stats['total_seen']}")
    print(f"   Завершено: {stats['finished']}")
    print(f"   Прогнозов: {stats['predictions']}")
    print(f"   ✅ Зашло: {stats['hits']}")
    print(f"   ❌ Не зашло: {stats['misses']}")
    print(f"\n🔍 ПАТТЕРНЫ:")
    print(f"   Проанализировано: {pattern_stats['total_analyzed']}")
    print(f"   Все 4 масти: {pattern_stats['all_4_suits']}")
    print(f"   3 масти: {pattern_stats['three_suits']}")
    print(f"   2 масти: {pattern_stats['two_suits']}")
    print(f"   1 масть: {pattern_stats['one_suit']}\n")

def process_games():
    games = get_active_games()
    if not games: return

    game_num = get_utc_game_number()
    
    # Собираем ID по номеру игры
    for g in games:
        gid = g.get("I")
        if not gid or gid in logged_game_ids:
            continue
        
        logged_game_ids.add(gid)
        stats["total_seen"] += 1
        
        if game_num not in games_by_number:
            games_by_number[game_num] = []
        
        if gid not in games_by_number[game_num]:
            games_by_number[game_num].append(gid)
    
    # 🔥 БЫСТРЫЙ ПРОГНОЗ: анализируем игры с 3+ ID
    for gnum, ids in list(games_by_number.items()):
        if gnum in analyzed_games or len(ids) < 3:
            continue
        
        analyzed_games.add(gnum)
        optimal_id, optimal_suit, pattern_type = analyze_four_ids(gnum, ids)
        
        # Создаём прогноз СРАЗУ, не ждём завершения
        with state_lock:
            if not active_suit_prediction["active"] and gnum not in prediction_created_for_game:
                target_num = normalize_game_num(gnum + 3)
                msg_id = send_prediction(optimal_suit, gnum, target_num, optimal_id)
                
                if msg_id:
                    stats["predictions"] += 1
                    active_suit_prediction.update({
                        "active": True, "message_id": msg_id,
                        "trigger_game_num": gnum, "trigger_game_id": optimal_id,
                        "predicted_suit_code": optimal_suit, "target_game_num": target_num,
                        "checked_games_count": 0, "checked_game_ids": set()
                    })
                    prediction_created_for_game.add(gnum)
                    print(f"🚀 Прогноз на #{target_num} (ID: {optimal_id})")
        
        # Проверяем завершение для результата
        game_data = fetch_game_details(optimal_id)
        if not game_data: continue
        
        cards_info = get_all_game_cards(game_data)
        is_finished = cards_info.get("is_finished", False)
        player_cards = cards_info.get("player", [])
        
        if not is_finished:
            continue
        
        stats["finished"] += 1
        
        if not player_cards:
            continue
        
        with state_lock:
            if active_suit_prediction["active"]:
                target = active_suit_prediction["target_game_num"]
                diff = normalize_game_num(gnum - target)
                
                if diff > 2 and diff < 1430:
                    update_prediction(active_suit_prediction["message_id"], False, "Время вышло.")
                    stats["misses"] += 1
                    active_suit_prediction["active"] = False
                elif 0 <= diff <= 2 and optimal_id not in active_suit_prediction["checked_game_ids"]:
                    active_suit_prediction["checked_game_ids"].add(optimal_id)
                    active_suit_prediction["checked_games_count"] += 1
                    
                    target_suit = active_suit_prediction["predicted_suit_code"]
                    hit = any(c["suit"] == target_suit for c in player_cards)
                    
                    if hit:
                        update_prediction(active_suit_prediction["message_id"], True, f"ЗАШЁЛ на #{gnum}!")
                        stats["hits"] += 1
                        active_suit_prediction["active"] = False
                        print(f"🎉 ПРОГНОЗ ЗАШЁЛ!")
                    elif active_suit_prediction["checked_games_count"] >= 3:
                        update_prediction(active_suit_prediction["message_id"], False, "НЕ зашёл (3 попытки).")
                        stats["misses"] += 1
                        active_suit_prediction["active"] = False

def main():
    print("🚀 ЗАПУСК БОТА (БЫСТРЫЙ ПРОГНОЗ)")
    print("=" * 60)
    
    cycle = 0
    while True:
        try:
            process_games()
            cycle += 1
            if cycle % 20 == 0:
                print_stats()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
