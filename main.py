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
# Обучение (заполняется ВСЕГДА, независимо от активного прогноза)
stats = defaultdict(lambda: defaultdict(int))  # trigger_card -> {predicted_card: count}
prediction_accuracy = defaultdict(float)
success_history = []

# ЕДИНСТВЕННЫЙ АКТИВНЫЙ ПРОГНОЗ В КАНАЛЕ
active_prediction = {
    "active": False,
    "message_id": None,
    "trigger_game_num": None,
    "trigger_card": None,
    "trigger_suit": None,
    "predicted_value": None,
    "predicted_symbol": None,
    "target_game_num": None,
    "strategy": "base",
    "checked_games_count": 0,
    "checked_game_ids": set()
}

processed_game_ids = set()
state_lock = threading.Lock()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

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

# --- УМНЫЙ ВЫБОР ПРОГНОЗА С ОБУЧЕНИЕМ ---

def get_smart_prediction(trigger_card):
    """Выбирает карту с опорой на накопленную статистику. Если данных мало — берёт базовую."""
    with state_lock:
        if trigger_card in stats and sum(stats[trigger_card].values()) >= 10:
            total = sum(stats[trigger_card].values())
            best_card = max(stats[trigger_card], key=stats[trigger_card].get)
            accuracy = stats[trigger_card][best_card] / total
            if accuracy >= 0.35:
                return best_card, "обученная (AI)"

        if trigger_card in BASE_PREDICTION_STRATEGY:
            options = BASE_PREDICTION_STRATEGY[trigger_card]
            best_card = max(options, key=lambda x: x[1])[0]
            return best_card, "базовая"

        return 1, "базовая"

def update_statistics(trigger_card, actual_cards):
    """Постоянно накапливает статистику по картам для обучения."""
    with state_lock:
        for c in actual_cards:
            val = c["value"]
            stats[trigger_card][val] += 1
            
        total = sum(stats[trigger_card].values())
        if total > 0:
            most_common = max(stats[trigger_card], key=stats[trigger_card].get)
            prediction_accuracy[trigger_card] = stats[trigger_card][most_common] / total

# --- ТЕЛЕГРАМ УВЕДОМЛЕНИЯ ---

def send_prediction_telegram():
    pred = active_prediction
    trigger_full = format_card_full(pred["trigger_card"], pred["trigger_suit"])
    
    total_samples = sum(stats[pred["trigger_card"]].values())
    acc = prediction_accuracy.get(pred["trigger_card"], 0) * 100
    acc_str = f"{acc:.1f}% ({total_samples} игр в базе)" if total_samples > 0 else "Сбор первичных данных"

    msg = (
        f"🎯 **ПРОГНОЗ БАККАРА / 21**\n"
        f"─────────────────\n"
        f"📌 Триггер: Игра **#{pred['trigger_game_num']}**\n"
        f"🃏 Первая карта: **{trigger_full}**\n"
        f"─────────────────\n"
        f"🎯 Ждем карту: **{pred['predicted_symbol']}**\n"
        f"🎯 Целевая игра: **#{pred['target_game_num']}** (до 3 итераций)\n"
        f"🧠 Модель: {pred['strategy'].capitalize()}\n"
        f"📈 Точность связки: {acc_str}\n"
        f"─────────────────\n"
        f"⏳ Статус: Ожидание входа в игру..."
    )
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg, parse_mode="Markdown")
        pred["message_id"] = sent.message_id
        print(f"📢 [КАНАЛ] Опубликован единственный активный прогноз #{pred['target_game_num']}")
    except Exception as e:
        print(f"❌ Ошибка отправки прогноза в Telegram: {e}")

def update_prediction_telegram(result_text, success):
    pred = active_prediction
    if not pred["message_id"]:
        return

    trigger_full = format_card_full(pred["trigger_card"], pred["trigger_suit"])
    result_emoji = "✅" if success else "❌"

    msg = (
        f"🎯 **ПРОГНОЗ БАККАРА / 21**\n"
        f"─────────────────\n"
        f"📌 Триггер: Игра **#{pred['trigger_game_num']}**\n"
        f"🃏 Первая карта: **{trigger_full}**\n"
        f"─────────────────\n"
        f"🎯 Прогноз: **{pred['predicted_symbol']}**\n"
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

def reset_active_prediction():
    """Сбрасывает состояние активного прогноза для готовности к следующему."""
    with state_lock:
        active_prediction["active"] = False
        active_prediction["message_id"] = None
        active_prediction["trigger_game_num"] = None
        active_prediction["trigger_card"] = None
        active_prediction["trigger_suit"] = None
        active_prediction["predicted_value"] = None
        active_prediction["predicted_symbol"] = None
        active_prediction["target_game_num"] = None
        active_prediction["checked_games_count"] = 0
        active_prediction["checked_game_ids"] = set()

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

        player_cards = cards_info.get("player", [])
        all_cards = cards_info.get("all", [])

        if not player_cards:
            continue

        first_card = player_cards[0]
        first_val = first_card["value"]
        first_suit = first_card["suit"]

        with state_lock:
            # 1. ФОНОВОЕ ОБУЧЕНИЕ: Каждая завершенная игра питает статистику
            if is_finished and gid not in processed_game_ids:
                processed_game_ids.add(gid)
                update_statistics(first_val, all_cards)

            # 2. ПРОВЕРКА ТЕКУЩЕГО АКТИВНОГО ПРОГНОЗА (если он есть)
            if active_prediction["active"]:
                target_num = active_prediction["target_game_num"]
                diff = normalize_game_num(game_num - target_num)

                # Проверяем 3 шага (целевая игра, +1, +2)
                if 0 <= diff <= 2:
                    if gid not in active_prediction["checked_game_ids"]:
                        active_prediction["checked_game_ids"].add(gid)
                        active_prediction["checked_games_count"] += 1

                        # Проверяем выпадение нужной карты
                        hit_card = next((c for c in all_cards if c["value"] == active_prediction["predicted_value"] or (active_prediction["predicted_value"] == 1 and c["value"] in [1, 14])), None)

                        if hit_card:
                            res_str = f"✅ ЗАШЕЛ на {diff + 1}-й игре!\n🃏 Выпала карта: **{hit_card['full']}**"
                            update_prediction_telegram(res_str, True)
                            success_history.append(True)
                            print(f"🎯 Прогноз #{target_num} ЗАШЕЛ! Канал свободен для нового прогноза.")
                            reset_active_prediction()

                        elif active_prediction["checked_games_count"] >= 3 or (is_finished and diff == 2):
                            actual_str = ", ".join([c["full"] for c in all_cards[:4]])
                            res_str = f"❌ НЕ СБЫЛСЯ (3 попытки окончены)\n🃏 Карты последней игры: {actual_str}"
                            update_prediction_telegram(res_str, False)
                            success_history.append(False)
                            print(f"❌ Прогноз #{target_num} НЕ зашел. Канал свободен для нового прогноза.")
                            reset_active_prediction()

            # 3. СОЗДАНИЕ НОВОГО ПРОГНОЗА (Только если канал пуст!)
            elif not active_prediction["active"] and gid not in processed_game_ids:
                target_num = normalize_game_num(game_num + 3)
                pred_val, strategy = get_smart_prediction(first_val)
                pred_symbol = format_card(pred_val)

                active_prediction["active"] = True
                active_prediction["trigger_game_num"] = game_num
                active_prediction["trigger_card"] = first_val
                active_prediction["trigger_suit"] = first_suit
                active_prediction["predicted_value"] = pred_val
                active_prediction["predicted_symbol"] = pred_symbol
                active_prediction["target_game_num"] = target_num
                active_prediction["strategy"] = strategy
                active_prediction["checked_games_count"] = 0
                active_prediction["checked_game_ids"] = set()

                send_prediction_telegram()

def main():
    print("\n🚀 ЗАПУСК МОНИТОРИНГА (РЕЖИМ: 1 АКТИВНЫЙ ПРОГНОЗ + ФОНОВОЕ ОБУЧЕНИЕ)")
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
