import os
import time
import json
import datetime
import threading
import requests
import telebot

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
    0: {"name": "Пики", "symbol": "️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

processed_game_ids = set()
prediction_created_for_game = set()
game_history = []  # История всех игр

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

def log_game(game_num, gid, status, cards_info=None):
    """Логирование игры в историю и консоль"""
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    suit_code = int(gid) % 4
    suit = SUITS.get(suit_code, {})
    
    entry = {
        "time": timestamp,
        "game_num": game_num,
        "id": gid,
        "status": status,
        "suit": suit["symbol"],
        "cards": cards_info.get("player", []) if cards_info else []
    }
    game_history.append(entry)
    
    # Красивый вывод в консоль
    if status == "NEW":
        print(f" [{timestamp}] #{game_num} | ID: {gid} | Масть: {suit['symbol']} {suit['name']} | Статус: Ожидание")
    elif status == "FINISHED":
        cards_str = ", ".join([f"{c['value']}({c['suit']})" for c in cards_info.get("player", [])]) if cards_info.get("player") else "НЕТ КАРТ"
        print(f"✅ [{timestamp}] #{game_num} | ID: {gid} | Масть: {suit['symbol']} | Карты игрока: [{cards_str}]")
    elif status == "NO_CARDS":
        print(f"⚠️  [{timestamp}] #{game_num} | ID: {gid} | Масть: {suit['symbol']} | Карты не найдены")

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
        bot.edit_message_text(chat_id=PREDICTION_CHANNEL_ID, message_id=msg_id, text=f" ПРОГНОЗ МАСТИ ИГРОКА\n\n{emoji} {details}")
    except: pass

def print_stats():
    """Вывод статистики"""
    print(f"\n СТАТИСТИКА:")
    print(f"   Всего игр: {stats['total_seen']}")
    print(f"   Завершено: {stats['finished']}")
    print(f"   С картами: {stats['with_cards']}")
    print(f"   Прогнозов: {stats['predictions']}")
    print(f"   ✅ Зашло: {stats['hits']}")
    print(f"   ❌ Не зашло: {stats['misses']}")
    print(f"   В истории: {len(game_history)} записей\n")

def process_games():
    games = get_active_games()
    if not games: return

    game_num = get_utc_game_number()

    for g in games:
        gid = g.get("I")
        if not gid or gid in processed_game_ids:
            continue

        stats["total_seen"] += 1
        game_data = fetch_game_details(gid)
        if not game_data: continue

        cards_info = get_all_game_cards(game_data)
        is_finished = cards_info.get("is_finished", False)
        player_cards = cards_info.get("player", [])

        if not is_finished:
            log_game(game_num, gid, "NEW")
            continue

        processed_game_ids.add(gid)
        stats["finished"] += 1

        if not player_cards:
            log_game(game_num, gid, "NO_CARDS", cards_info)
            continue

        stats["with_cards"] += 1
        log_game(game_num, gid, "FINISHED", cards_info)

        suit_code = int(gid) % 4
        suit = SUITS.get(suit_code, {})

        with state_lock:
            if active_suit_prediction["active"]:
                target = active_suit_prediction["target_game_num"]
                diff = normalize_game_num(game_num - target)

                if diff > 2 and diff < 1430: 
                    update_prediction(active_suit_prediction["message_id"], False, "Время вышло.")
                    stats["misses"] += 1
                    active_suit_prediction["active"] = False

                elif 0 <= diff <= 2 and gid not in active_suit_prediction["checked_game_ids"]:
                    active_suit_prediction["checked_game_ids"].add(gid)
                    active_suit_prediction["checked_games_count"] += 1
                    
                    target_suit = active_suit_prediction["predicted_suit_code"]
                    hit = any(c["suit"] == target_suit for c in player_cards)

                    if hit:
                        update_prediction(active_suit_prediction["message_id"], True, f"ЗАШЁЛ на #{game_num}!")
                        stats["hits"] += 1
                        active_suit_prediction["active"] = False
                        print(f"🎉 ПРОГНОЗ ЗАШЁЛ!")
                    elif active_suit_prediction["checked_games_count"] >= 3:
                        update_prediction(active_suit_prediction["message_id"], False, "НЕ зашёл (3 попытки).")
                        stats["misses"] += 1
                        active_suit_prediction["active"] = False

            elif not active_suit_prediction["active"] and gid not in prediction_created_for_game:
                target_num = normalize_game_num(game_num + 3)
                msg_id = send_prediction(suit_code, game_num, target_num, gid)
                
                if msg_id:
                    stats["predictions"] += 1
                    active_suit_prediction.update({
                        "active": True, "message_id": msg_id,
                        "trigger_game_num": game_num, "trigger_game_id": gid,
                        "predicted_suit_code": suit_code, "target_game_num": target_num,
                        "checked_games_count": 0, "checked_game_ids": set()
                    })
                    prediction_created_for_game.add(gid)
                    print(f"🚀 Прогноз на #{target_num}")

def main():
    print("🚀 ЗАПУСК БОТА С ЛОГИРОВАНИЕМ")
    print("=" * 60)
    
    cycle = 0
    while True:
        try:
            process_games()
            cycle += 1
            if cycle % 20 == 0:  # Каждые 20 циклов выводим статистику
                print_stats()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
