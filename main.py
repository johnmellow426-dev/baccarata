import requests
import json
import time
import os
import datetime
import telebot
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID", CHANNEL_ID)

# ПРОВЕРЯЕМ, ЧТО ТОКЕН ЗАГРУЗИЛСЯ
print(f"🔑 BOT_TOKEN: {'✅' if BOT_TOKEN else '❌ НЕ НАЙДЕН'}")
print(f"📢 CHANNEL_ID: {'✅' if CHANNEL_ID else '❌ НЕ НАЙДЕН'}")

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://melbet-5427.pro/",
}
NO_PROXY = {"http": None, "https": None}

# --- ТЕСТОВЫЙ ЗАПРОС ПРИ СТАРТЕ ---
print("\n🔍 ТЕСТИРУЕМ API...")

try:
    test_resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
    print(f"📡 API ответ: {test_resp.status_code}")
    
    if test_resp.status_code == 200:
        test_data = test_resp.json()
        games_list = test_data.get("Value", [])
        print(f"📊 Найдено игр: {len(games_list)}")
    else:
        print(f"❌ Ошибка API: {test_resp.status_code}")
except Exception as e:
    print(f"❌ Ошибка тестового запроса: {e}")

print("\n" + "=" * 60)

# --- БОТ ---
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# --- КОНСТАНТЫ ---
CARD_SYMBOLS = {
    1: "A", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
    7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K", 14: "A"
}

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# СТРАТЕГИЯ ПРОГНОЗИРОВАНИЯ
PREDICTION_STRATEGY = {
    1: [(2, 60), (1, 40)],     # A -> 2 или A
    2: [(3, 55), (2, 45)],     # 2 -> 3 или 2
    3: [(4, 50), (8, 50)],     # 3 -> 4 или 8
    4: [(5, 55), (9, 45)],     # 4 -> 5 или 9
    5: [(6, 50), (10, 50)],    # 5 -> 6 или 10
    6: [(13, 85), (6, 15)],    # 6 -> K
    7: [(12, 80), (7, 20)],    # 7 -> Q
    8: [(11, 80), (8, 20)],    # 8 -> J
    9: [(12, 65), (9, 35)],    # 9 -> Q
    10: [(12, 60), (10, 40)],  # 10 -> Q
    11: [(1, 70), (11, 30)],   # J -> A
    12: [(1, 70), (12, 30)],   # Q -> A
    13: [(1, 75), (13, 25)],   # K -> A
    14: [(2, 60), (1, 40)],    # A -> 2 или A
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
stats = defaultdict(lambda: defaultdict(int))
history = []
game_details_cache = {}
processed_game_ids = set()
game_counter = 0

# Состояние прогноза
prediction = {
    "active": False,
    "trigger_game_num": None,
    "trigger_card": None,
    "trigger_suit": None,
    "predicted_value": None,
    "predicted_symbol": None,
    "target_game_num": None,
    "dogen_level": 1,
    "message_id": None,
    "checked": False
}

state_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=4)

# ============================================================
#   ФУНКЦИИ ДЛЯ РАБОТЫ С КАРТАМИ
# ============================================================

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize_game_num(num):
    while num > 1440:
        num -= 1440
    while num < 1:
        num += 1440
    return num

def format_card(card_value):
    return CARD_SYMBOLS.get(card_value, str(card_value))

def format_card_full(card_value, suit_code):
    symbol = CARD_SYMBOLS.get(card_value, str(card_value))
    suit = SUITS.get(suit_code, {}).get("symbol", "?")
    return f"{symbol}{suit}"

def parse_cards_from_api(cards_json):
    """
    Парсит карты из JSON-строки
    """
    try:
        if not cards_json or cards_json == "[]":
            return []
        
        if cards_json.startswith("Win") or cards_json in ["Win1", "Win2", "Tie"]:
            return []
        
        cards = json.loads(cards_json)
        parsed = []
        for c in cards:
            value = c.get("R") or c.get("CV") or c.get("C", 0)
            suit = c.get("S") or c.get("CS") or c.get("Suit", 0)
            
            if value > 0:
                parsed.append({
                    "value": value,
                    "suit": suit,
                    "symbol": format_card(value),
                    "full": format_card_full(value, suit)
                })
        return parsed
    except json.JSONDecodeError:
        return []
    except Exception as e:
        print(f"⚠️ Ошибка парсинга карт: {e}")
        return []

def get_all_game_cards(game_data):
    """Получает все карты из игры"""
    result = {"player": [], "dealer": [], "all": [], "result": None}
    
    try:
        sc = game_data.get("SC", {})
        s_list = sc.get("S", [])
        
        for item in s_list:
            key = item.get("Key", "")
            value = item.get("Value", "")
            
            if key == "S":
                result["result"] = value
                continue
            
            cards = parse_cards_from_api(value)
            
            if key == "P":
                result["player"] = cards
                result["all"].extend(cards)
            elif key == "B":  # Banker (Дилер)
                result["dealer"] = cards
                result["all"].extend(cards)
        
        return result
    except Exception as e:
        print(f"⚠️ Ошибка получения карт: {e}")
        return result

# ============================================================
#   ФУНКЦИИ ДЛЯ РАБОТЫ С API
# ============================================================

def fetch_game_details(game_id):
    """Получает детали игры без агрессивного кэширования для Live-игр"""
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=5, proxies=NO_PROXY)
        
        if resp.status_code != 200:
            return None
        
        data = resp.json().get("Value", {})
        return data
    except Exception as e:
        print(f"⚠️ Ошибка получения деталей игры {game_id}: {e}")
        return None

def get_active_games():
    """Получает список всех текущих и завершенных игр из LiveFeed"""
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=5, proxies=NO_PROXY)
        if resp.status_code != 200:
            return []
        games = resp.json().get("Value", [])
        return games
    except Exception as e:
        print(f"⚠️ Ошибка получения списка игр: {e}")
        return []

def get_game_card_info(game_id):
    """Получает карты и статус завершения игры"""
    game_data = fetch_game_details(game_id)
    if not game_data:
        return None, False
    
    cards = get_all_game_cards(game_data)
    is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"
    
    return cards, is_finished

# ============================================================
#   ЛОГИКА ПРОГНОЗИРОВАНИЯ
# ============================================================

def get_prediction_with_stats(trigger_card):
    """Возвращает прогноз на основе статистики и стратегии"""
    if trigger_card in stats and sum(stats[trigger_card].values()) > 10:
        predictions = stats[trigger_card]
        best_pred = max(predictions, key=predictions.get)
        return best_pred
    
    if trigger_card in PREDICTION_STRATEGY:
        options = PREDICTION_STRATEGY[trigger_card]
        return max(options, key=lambda x: x[1])[0]
    
    return 1

def update_statistics(trigger_card, actual_card):
    """Обновляет статистику"""
    with state_lock:
        stats[trigger_card][actual_card] += 1

def check_prediction_for_game(cards_info, predicted_value):
    """Проверяет, есть ли прогнозируемая карта в игре"""
    if not predicted_value:
        return False, None, None
    
    for card in cards_info.get("all", []):
        val = card["value"]
        if predicted_value == 1 and val in [1, 14]:
            return True, val, card["suit"]
        if val == predicted_value:
            return True, val, card["suit"]
    
    return False, None, None

def process_game_step(game_num, first_card, first_suit, cards_info, is_finished):
    """Обрабатывает игру на любом этапе (Live или Завершена)"""
    global game_counter
    
    with state_lock:
        if prediction["active"] and not prediction["checked"]:
            target_num = prediction["target_game_num"]
            offset = game_num - target_num
            
            if 0 <= offset <= 2:
                is_hit, hit_value, hit_suit = check_prediction_for_game(
                    cards_info, 
                    prediction["predicted_value"]
                )
                
                # Если карта уже выпала
                if is_hit:
                    emoji_map = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
                    result_text = f"{emoji_map[offset]} (на {offset+1}-й игре)"
                    
                    if hit_value:
                        result_text += f"\n🃏 Найдена: {format_card_full(hit_value, hit_suit)}"
                    
                    update_prediction_message(result_text)
                    print(f"🎯 ПРОГНОЗ СБЫЛСЯ! Попытка {offset+1}")
                    
                    update_statistics(prediction["trigger_card"], prediction["predicted_value"])
                    
                    prediction["checked"] = True
                    prediction["dogen_level"] = 1
                    reset_prediction()
                    
                # Если пройдено 3 попытки и текущая 3-я игра завершилась без попадания
                elif offset == 2 and is_finished:
                    actual_cards = ", ".join([c["full"] for c in cards_info.get("all", [])[:3]])
                    result_text = f"❌ НЕ СБЫЛСЯ (3 попытки)\n🃏 Выпали: {actual_cards}"
                    
                    update_prediction_message(result_text)
                    print(f"❌ ПРОГНОЗ НЕ СБЫЛСЯ")
                    
                    for card in cards_info.get("all", []):
                        if card["value"] != prediction["predicted_value"]:
                            update_statistics(prediction["trigger_card"], card["value"])
                            break
                    
                    prediction["checked"] = True
                    prediction["dogen_level"] = min(prediction["dogen_level"] + 1, 3)
                    reset_prediction()

        # Если прогноза нет — создаем его по первой разданной карте
        if not prediction["active"] and first_card:
            create_new_prediction(game_num, first_card, first_suit)

def create_new_prediction(trigger_num, trigger_card, trigger_suit):
    """Создает новый прогноз"""
    if prediction["active"]:
        return
    
    pred_value = get_prediction_with_stats(trigger_card)
    pred_symbol = format_card(pred_value)
    
    target_num = normalize_game_num(trigger_num + 3)
    
    prediction["active"] = True
    prediction["trigger_game_num"] = trigger_num
    prediction["trigger_card"] = trigger_card
    prediction["trigger_suit"] = trigger_suit
    prediction["predicted_value"] = pred_value
    prediction["predicted_symbol"] = pred_symbol
    prediction["target_game_num"] = target_num
    prediction["message_id"] = None
    prediction["checked"] = False
    
    send_prediction_message(trigger_num, trigger_card, trigger_suit, pred_symbol, target_num)
    
    print(f"🎯 НОВЫЙ ПРОГНОЗ: #{trigger_num} ({format_card_full(trigger_card, trigger_suit)}) -> #{target_num} ({pred_symbol})")

def send_prediction_message(trigger_num, trigger_card, trigger_suit, pred_symbol, target_num):
    """Отправляет прогноз в канал"""
    dogen = prediction["dogen_level"]
    trigger_full = format_card_full(trigger_card, trigger_suit)
    
    msg = (
        f"🎯 ПРОГНОЗ БАККАРА\n"
        f"─────────────────\n"
        f"📌 Триггер: игра #{trigger_num}\n"
        f"🃏 Первая карта: {trigger_full}\n"
        f"─────────────────\n"
        f"🎯 Прогноз: {pred_symbol}\n"
        f"🎯 Игра: #{target_num}\n"
        f"📊 Догон: {dogen}/3\n"
        f"─────────────────\n"
        f"⏳ Ожидание результата..."
    )
    
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg)
        prediction["message_id"] = sent.message_id
        print(f"📤 Прогноз отправлен в канал")
    except Exception as e:
        print(f"❌ Ошибка отправки прогноза: {e}")

def update_prediction_message(result_text):
    """Обновляет сообщение с прогнозом"""
    if not prediction["message_id"]:
        return
    
    trigger_num = prediction["trigger_game_num"]
    trigger_card = prediction["trigger_card"]
    trigger_suit = prediction["trigger_suit"]
    pred_symbol = prediction["predicted_symbol"]
    target_num = prediction["target_game_num"]
    dogen = prediction["dogen_level"]
    
    trigger_full = format_card_full(trigger_card, trigger_suit)
    
    msg = (
        f"🎯 ПРОГНОЗ БАККАРА\n"
        f"─────────────────\n"
        f"📌 Триггер: игра #{trigger_num}\n"
        f"🃏 Первая карта: {trigger_full}\n"
        f"─────────────────\n"
        f"🎯 Прогноз: {pred_symbol}\n"
        f"🎯 Игра: #{target_num}\n"
        f"📊 Догон: {dogen}/3\n"
        f"─────────────────\n"
        f"📊 Результат: {result_text}"
    )
    
    try:
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID,
            message_id=prediction["message_id"],
            text=msg
        )
        print(f"✏️ Прогноз обновлен")
    except Exception as e:
        print(f"❌ Ошибка обновления прогноза: {e}")

def reset_prediction():
    """Сбрасывает прогноз"""
    dogen = prediction["dogen_level"]
    for key in prediction:
        if key != "dogen_level":
            prediction[key] = None if key not in ["active", "checked"] else False
    prediction["active"] = False
    prediction["checked"] = False
    prediction["dogen_level"] = dogen

# ============================================================
#   ОСНОВНОЙ ЦИКЛ
# ============================================================

def main():
    global game_counter, processed_game_ids, history
    
    print("\n🚀 ЗАПУСК ОСНОВНОГО ЦИКЛА МОНИТОРИНГА")
    print("=" * 60)
    print("📡 Мониторинг всех текущих (Live) и завершенных игр")
    print("=" * 60)
    
    # Первичная загрузка
    try:
        print("\n📡 Сбор начального списка игр...")
        games = get_active_games()
        print(f"📊 Найдено {len(games)} игр в списке")
    except Exception as e:
        print(f"⚠️ Ошибка начальной загрузки: {e}")
    
    print("\n🔄 Основной цикл активен (каждые 2 секунды)...\n")
    
    # Основной цикл
    while True:
        try:
            games = get_active_games()
            
            for g in games:
                gid = g.get("I")
                if not gid:
                    continue
                
                # Запрашиваем детали текущей игры
                cards_info, is_finished = get_game_card_info(gid)
                
                if not cards_info or not cards_info.get("player"):
                    # Игра еще не началась или карты еще не разданы
                    continue
                
                first_card = cards_info["player"][0]
                first_value = first_card["value"]
                first_suit = first_card["suit"]
                
                # Если эту конкретную раздачу ещё не обрабатывали
                if gid not in processed_game_ids:
                    with state_lock:
                        history.append(first_value)
                        game_counter += 1
                        
                        game_num = get_utc_game_number()
                        all_cards = ", ".join([c["full"] for c in cards_info.get("all", [])])
                        
                        status_str = "Завершена" if is_finished else "ИДЕТ (LIVE)"
                        print(f"🎮 Игра {gid} (#{game_num}) [{status_str}] | Первая: {first_card['full']} | Карты: {all_cards}")
                        
                        # Если игра завершилась, помечаем id как полностью обработанный
                        if is_finished:
                            processed_game_ids.add(gid)
                        
                        process_game_step(game_num, first_value, first_suit, cards_info, is_finished)
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Ошибка в основном цикле: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
