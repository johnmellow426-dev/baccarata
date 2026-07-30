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

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
stats = defaultdict(lambda: defaultdict(int))
success_history = []
game_results = []
prediction_accuracy = defaultdict(float)
card_correlation = defaultdict(dict)

# Реестры для отслеживания
analyzed_trigger_ids = set()
active_predictions = {}  # {target_game_num: prediction_dict}
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

# --- АНАЛИТИКА И СТРАТЕГИИ ---

def get_winning_streak():
    if len(success_history) < 3:
        return 0, 0.0
    recent_success = 0
    for res in reversed(success_history):
        if res:
            recent_success += 1
        else:
            break
    total_attempts = min(len(success_history), 50)
    total_success = sum(success_history[-total_attempts:])
    success_rate = total_success / total_attempts if total_attempts > 0 else 0.0
    return recent_success, success_rate

def get_smart_prediction(trigger_card):
    if trigger_card in BASE_PREDICTION_STRATEGY:
        options = BASE_PREDICTION_STRATEGY[trigger_card]
        best_card = max(options, key=lambda x: x[1])[0]
        return best_card, "base"
    return 1, "base"

# --- ПУБЛИКАЦИЯ И ОБНОВЛЕНИЕ В TELEGRAM ---

def send_prediction_telegram(pred):
    trigger_full = format_card_full(pred["trigger_card"], pred["trigger_suit"])
    accuracy = prediction_accuracy.get(pred["trigger_card"], 0) * 100
    accuracy_text = f"{accuracy:.1f}%" if accuracy > 0 else "Накопление данных"
    
    streak, success_rate = get_winning_streak()
    streak_text = f"🔥 {streak} в ряд" if streak > 0 else "➖"
    
    msg = (
        f"🎯 **ПРОГНОЗ БАККАРА / 21**\n"
        f"─────────────────\n"
        f"📌 Триггер: Игра **#{pred['trigger_game_num']}**\n"
        f"🃏 Первая карта: **{trigger_full}**\n"
        f"─────────────────\n"
        f"🎯 Прогноз карт(ы): **{pred['predicted_symbol']}**\n"
        f"🎯 Целевая игра: **#{pred['target_game_num']}** (до +2 итераций)\n"
        f"🧠 Стратегия: {pred['strategy'].capitalize()}\n"
        f"📈 Точность триггера: {accuracy_text}\n"
        f"🔥 Серия: {streak_text}\n"
        f"─────────────────\n"
        f"⏳ Ожидаем результат..."
    )
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg, parse_mode="Markdown")
        pred["message_id"] = sent.message_id
        print(f"📢 Прогноз опубликован в канал (ID: {sent.message_id})")
    except Exception as e:
        print(f"❌ Ошибка отправки прогноза в Telegram: {e}")

def update_prediction_telegram(pred, result_text, success):
    if not pred.get("message_id"):
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

# --- ОСНОВНОЙ ЦИКЛ ОБРАБОТКИ ---

def process_live_games():
    games = get_active_games()
    if not games:
        return

    for g in games:
        gid = g.get("I")
        if not gid:
            continue

        # Извлекаем подробную информацию об игре
        game_data = fetch_game_details(gid)
        if not game_data:
            continue

        cards_info = get_all_game_cards(game_data)
        is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"

        # Извлекаем номер игры из структуры BК
        game_num = get_utc_game_number()

        player_cards = cards_info.get("player", [])
        if not player_cards:
            continue

        first_card = player_cards[0]
        first_val = first_card["value"]
        first_suit = first_card["suit"]

        with state_lock:
            # 1. ОБРАБОТКА И СОЗДАНИЕ НОВОГО ПРОГНОЗА
            if gid not in analyzed_trigger_ids:
                analyzed_trigger_ids.add(gid)
                
                # Генерация прогноза на N+3 игру
                target_num = normalize_game_num(game_num + 3)
                pred_val, strategy = get_smart_prediction(first_val)
                pred_symbol = format_card(pred_val)

                new_pred = {
                    "trigger_game_num": game_num,
                    "trigger_card": first_val,
                    "trigger_suit": first_suit,
                    "predicted_value": pred_val,
                    "predicted_symbol": pred_symbol,
                    "target_game_num": target_num,
                    "strategy": strategy,
                    "message_id": None,
                    "checked_games_count": 0,
                    "checked_game_ids": set()
                }

                active_predictions[target_num] = new_pred
                send_prediction_telegram(new_pred)
                print(f"🎯 Сгенерирован прогноз: Игра #{game_num} ({format_card_full(first_val, first_suit)}) -> На цель #{target_num} [{pred_symbol}]")

            # 2. ПРОВЕРКА АКТИВНЫХ ПРОГНОЗОВ В ТЕКУЩЕЙ ИГРЕ
            to_remove = []
            for target_num, pred in active_predictions.items():
                # Проверяем диапазон: целевая игра и 2 последующие догоном
                diff = normalize_game_num(game_num - target_num)
                if 0 <= diff <= 2:
                    if gid not in pred["checked_game_ids"]:
                        pred["checked_game_ids"].add(gid)
                        pred["checked_games_count"] += 1

                        # Проверка наличия угаданной карты
                        all_game_cards = cards_info.get("all", [])
                        hit_card = next((c for c in all_game_cards if c["value"] == pred["predicted_value"] or (pred["predicted_value"] == 1 and c["value"] in [1, 14])), None)

                        if hit_card:
                            res_str = f"✅ УГАДАНО на {diff + 1}-м шаге!\n🃏 Найдена: **{hit_card['full']}**"
                            update_prediction_telegram(pred, res_str, True)
                            success_history.append(True)
                            stats[pred["trigger_card"]][pred["predicted_value"]] += 1
                            to_remove.append(target_num)
                            print(f"✅ Прогноз #{target_num} ЗАШЕЛ!")
                        elif pred["checked_games_count"] >= 3 or is_finished:
                            if pred["checked_games_count"] >= 3:
                                actual_str = ", ".join([c["full"] for c in all_game_cards[:4]])
                                res_str = f"❌ НЕ СБЫЛСЯ (3 попытки исчерпаны)\n🃏 Карты матча: {actual_str}"
                                update_prediction_telegram(pred, res_str, False)
                                success_history.append(False)
                                to_remove.append(target_num)
                                print(f"❌ Прогноз #{target_num} НЕ зашел.")

            for t_num in to_remove:
                del active_predictions[t_num]

def main():
    print("\n🚀 ЗАПУСК МОНИТОРИНГА LIVE И АВТО-ПУБЛИКАЦИИ")
    print("=" * 60)
    
    while True:
        try:
            process_live_games()
            time.sleep(2.5)  # Пауза между итерациями опроса
        except Exception as e:
            print(f"⚠️ Ошибка в основном цикле: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
