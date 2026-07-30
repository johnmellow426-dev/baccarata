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

print(f"🔑 BOT_TOKEN: {'✅' if BOT_TOKEN else '❌ НЕ НАЙДЕН'}")
print(f"📢 CHANNEL_ID: {'✅' if CHANNEL_ID else '❌ НЕ НАЙДЕН'}")

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://melbet-5427.pro/",
}
NO_PROXY = {"http": None, "https": None}

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

BASE_PREDICTION_STRATEGY = {
    1: [(2, 60), (1, 40)], 2: [(3, 55), (2, 45)], 3: [(4, 50), (8, 50)],
    4: [(5, 55), (9, 45)], 5: [(6, 50), (10, 50)], 6: [(13, 85), (6, 15)],
    7: [(12, 80), (7, 20)], 8: [(11, 80), (8, 20)], 9: [(12, 65), (9, 35)],
    10: [(12, 60), (10, 40)], 11: [(1, 70), (11, 30)], 12: [(1, 70), (12, 30)],
    13: [(1, 75), (13, 25)], 14: [(2, 60), (1, 40)]
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ОБУЧЕНИЯ И СОСТОЯНИЯ ---
# Обучение карт на основе суммы цифр ID (id_sum % 13 + 1)
id_card_stats = defaultdict(lambda: defaultdict(int))  # id_mod -> {predicted_card: count}

# ЕДИНСТВЕННЫЙ АКТИВНЫЙ ПРОГНОЗ НА МАСТЬ В КАНАЛЕ
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

processed_game_ids = set()
logged_game_ids = set()
state_lock = threading.Lock()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И АНАЛИЗАТОР ID ---

def analyze_game_id(game_id):
    """Анализирует числовые закономерности ID раздачи."""
    gid_int = int(game_id)
    digits_sum = sum(int(d) for d in str(game_id) if d.isdigit())
    
    # 1. Определение масти игроку по ID
    player_suit_code = gid_int % 4
    
    # 2. Определение потенциального достоинства карты по модулю суммы цифр
    card_by_id = (digits_sum % 13) + 1
    
    return {
        "player_suit_code": player_suit_code,
        "player_suit": SUITS.get(player_suit_code, {}),
        "digits_sum": digits_sum,
        "card_by_id": card_by_id
    }

def normalize_game_num(num):
    while num > 1440: num -= 1440
    while num < 1: num += 1440
    return num

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def format_card(card_value):
    return CARD_SYMBOLS.get(card_value, str(card_value))

def format_card_full(card_value, suit_code):
    symbol = CARD_SYMBOLS.get(card_value, str(card_value))
    suit = SUITS.get(suit_code, {}).get("symbol", "❓")
    return f"{symbol}{suit}"

def parse_cards_from_api(cards_json):
    try:
        if not cards_json or str(cards_json).startswith("Win") or cards_json in ["Win1", "Win2", "Tie"]:
            return []
        cards = json.loads(cards_json) if isinstance(cards_json, str) else cards_json
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
    except Exception:
        return []

def get_all_game_cards(game_data):
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
            elif key == "B":
                result["dealer"] = cards
                result["all"].extend(cards)
        return result
    except Exception:
        return result

def fetch_game_details(game_id):
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=4, proxies=NO_PROXY)
        if resp.status_code == 200:
            return resp.json().get("Value", {})
        return None
    except Exception as e:
        print(f"⚠️ Ошибка сети при запросе игры {game_id}: {e}")
        return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=4, proxies=NO_PROXY)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
        return []
    except Exception as e:
        print(f"⚠️ Ошибка загрузки списка Live: {e}")
        return []

# --- ИЗУЧЕНИЕ ЗНАЧЕНИЙ ПО ID ---

def update_id_statistics(game_id, actual_cards):
    """Обучение связи суммы цифр ID с выпадающими картами."""
    id_analysis = analyze_game_id(game_id)
    id_mod = id_analysis["card_by_id"]
    
    with state_lock:
        for c in actual_cards:
            val = c["value"]
            id_card_stats[id_mod][val] += 1

def predict_card_by_id(game_id):
    """Прогноз карты на основе ID."""
    id_analysis = analyze_game_id(game_id)
    id_mod = id_analysis["card_by_id"]
    
    with state_lock:
        if id_mod in id_card_stats and sum(id_card_stats[id_mod].values()) >= 10:
            best_card = max(id_card_stats[id_mod], key=id_card_stats[id_mod].get)
            return best_card, "обучение по ID"
    
    return id_mod, "гипотеза по ID"

# --- ТЕЛЕГРАМ УВЕДОМЛЕНИЯ ПРОГНОЗА МАСТИ ---

def send_suit_prediction_telegram():
    pred = active_suit_prediction
    suit_info = SUITS.get(pred["predicted_suit_code"], {})
    suit_str = f"{suit_info.get('symbol', '')} {suit_info.get('name', '')}"

    msg = (
        f"🎯 **ПРОГНОЗ МАСТИ ПО ID (БАККАРА / 21)**\n"
        f"─────────────────\n"
        f"📌 Триггер: Игра **#{pred['trigger_game_num']}** (ID: `{pred['trigger_game_id']}`)\n"
        f"─────────────────\n"
        f"♦️ Прогнозируемая масть: **{suit_str}**\n"
        f"🎯 Целевая игра: **#{pred['target_game_num']}** (до 3 итераций)\n"
        f"🧠 Модель: Анализ Hash/ID\n"
        f"─────────────────\n"
        f"⏳ Статус: Ожидание входа в игры..."
    )
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg, parse_mode="Markdown")
        pred["message_id"] = sent.message_id
        print(f"📢 [КАНАЛ] Опубликован прогноз масти {suit_str} на целевую игру #{pred['target_game_num']}")
    except Exception as e:
        print(f"❌ Ошибка отправки прогноза в Telegram: {e}")

def update_suit_prediction_telegram(result_text, success):
    pred = active_suit_prediction
    if not pred["message_id"]:
        return

    suit_info = SUITS.get(pred["predicted_suit_code"], {})
    suit_str = f"{suit_info.get('symbol', '')} {suit_info.get('name', '')}"
    result_emoji = "✅" if success else "❌"

    msg = (
        f"🎯 **ПРОГНОЗ МАСТИ ПО ID (БАККАРА / 21)**\n"
        f"─────────────────\n"
        f"📌 Триггер: Игра **#{pred['trigger_game_num']}**\n"
        f"♦️ Прогноз масти: **{suit_str}**\n"
        f"🎯 Целевая игра: **#{pred['target_game_num']}**\n"
        f"─────────────────\n"
        f"{result_emoji} **Результат:** {result_text}"
    )
    try:
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID,
            message_id=pred["message_id"],
            text=msg,
            parse_mode="Markdown"
        )
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            print(f"⚠️ Ошибка Telegram API при обновлении: {e}")
    except Exception as e:
        print(f"❌ Непредвиденная ошибка редактирования сообщения: {e}")

def reset_active_suit_prediction():
    with state_lock:
        active_suit_prediction["active"] = False
        active_suit_prediction["message_id"] = None
        active_suit_prediction["trigger_game_num"] = None
        active_suit_prediction["trigger_game_id"] = None
        active_suit_prediction["predicted_suit_code"] = None
        active_suit_prediction["target_game_num"] = None
        active_suit_prediction["checked_games_count"] = 0
        active_suit_prediction["checked_game_ids"] = set()

# --- ОСНОВНАЯ ЛОГИКА ---

def process_live_games():
    games = get_active_games()
    if not games:
        return

    for g in games:
        gid = g.get("I")
        if not gid:
            continue

        game_data = fetch_game_details(gid)
        if not game_data:
            continue

        cards_info = get_all_game_cards(game_data)
        is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"
        game_num = get_utc_game_number()
        all_cards = cards_info.get("all", [])

        # Анализ ID текущей игры
        id_analysis = analyze_game_id(gid)

        with state_lock:
            # 1. ЛОГИРОВАНИЕ И АНАЛИЗ В КОНСОЛЬ
            if gid not in logged_game_ids:
                logged_game_ids.add(gid)
                pred_card, model_type = predict_card_by_id(gid)
                print(
                    f"🆕 Начата игра #{game_num} (ID: {gid}) | "
                    f"Прогноз масти ID: {id_analysis['player_suit']['symbol']} {id_analysis['player_suit']['name']} | "
                    f"Прогноз карты по ID: {format_card(pred_card)} ({model_type})"
                )

            # 2. ФОНОВОЕ ОБУЧЕНИЕ ПО ID
            if is_finished and gid not in processed_game_ids:
                processed_game_ids.add(gid)
                update_id_statistics(gid, all_cards)

            # 3. ПРОВЕРКА АКТИВНОГО ПРОГНОЗА МАСТИ
            if active_suit_prediction["active"]:
                target_num = active_suit_prediction["target_game_num"]
                diff = normalize_game_num(game_num - target_num)

                # Проверяем 3 игры подряд (целевая, +1, +2)
                if 0 <= diff <= 2:
                    if gid not in active_suit_prediction["checked_game_ids"]:
                        active_suit_prediction["checked_game_ids"].add(gid)
                        active_suit_prediction["checked_games_count"] += 1

                        # Ищем совпадение нужной масти в картах
                        target_suit = active_suit_prediction["predicted_suit_code"]
                        hit_card = next((c for c in all_cards if c["suit"] == target_suit), None)

                        if hit_card:
                            res_str = f"✅ ЗАШЕЛ на {diff + 1}-й игре!\n🃏 Выпала карта: **{hit_card['full']}**"
                            update_suit_prediction_telegram(res_str, True)
                            print(f"🎯 Прогноз масти #{target_num} ЗАШЕЛ! Канал свободен.")
                            reset_active_suit_prediction()

                        elif active_suit_prediction["checked_games_count"] >= 3 or (is_finished and diff == 2):
                            actual_str = ", ".join([c["full"] for c in all_cards[:4]]) if all_cards else "Карты не показаны"
                            res_str = f"❌ НЕ СБЫЛСЯ (3 попытки окончены)\n🃏 Карты: {actual_str}"
                            update_suit_prediction_telegram(res_str, False)
                            print(f"❌ Прогноз масти #{target_num} НЕ зашел. Канал свободен.")
                            reset_active_suit_prediction()

            # 4. СОЗДАНИЕ НОВОГО ПРОГНОЗА (Только если активных прогнозов НЕТ)
            elif not active_suit_prediction["active"] and gid not in processed_game_ids:
                target_num = normalize_game_num(game_num + 3)

                active_suit_prediction["active"] = True
                active_suit_prediction["trigger_game_num"] = game_num
                active_suit_prediction["trigger_game_id"] = gid
                active_suit_prediction["predicted_suit_code"] = id_analysis["player_suit_code"]
                active_suit_prediction["target_game_num"] = target_num
                active_suit_prediction["checked_games_count"] = 0
                active_suit_prediction["checked_game_ids"] = set()

                send_suit_prediction_telegram()

def main():
    print("\n🚀 ЗАПУСК МОНИТОРИНГА (РЕЖИМ: 1 АКТИВНЫЙ ПРОГНОЗ МАСТИ В КАНАЛЕ + ИЗУЧЕНИЕ ID)")
    print("=" * 65)
    
    while True:
        try:
            process_live_games()
            time.sleep(2.5)
        except Exception as e:
            print(f"⚠️ Ошибка в главном цикле: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
