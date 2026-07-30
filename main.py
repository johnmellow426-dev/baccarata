import requests
import json
import time
import os
import datetime
import telebot
import threading
from collections import defaultdict
from statistics import mean, stdev
import math

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID", CHANNEL_ID)

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

# Базовая стратегия (на случай, если нет статистики)
BASE_PREDICTION_STRATEGY = {
    1: [(2, 60), (1, 40)], 2: [(3, 55), (2, 45)], 3: [(4, 50), (8, 50)],
    4: [(5, 55), (9, 45)], 5: [(6, 50), (10, 50)], 6: [(13, 85), (6, 15)],
    7: [(12, 80), (7, 20)], 8: [(11, 80), (8, 20)], 9: [(12, 65), (9, 35)],
    10: [(12, 60), (10, 40)], 11: [(1, 70), (11, 30)], 12: [(1, 70), (12, 30)],
    13: [(1, 75), (13, 25)], 14: [(2, 60), (1, 40)]
}

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
stats = defaultdict(lambda: defaultdict(int))  # Статистика: trigger_card -> {predicted_card: count}
success_history = []  # История успешных прогнозов
game_results = []  # История результатов игр
prediction_accuracy = defaultdict(float)  # Точность для каждой карты-триггера
card_correlation = defaultdict(dict)  # Корреляция между картами

game_counter = 0
processed_game_ids = set()

prediction = {
    "active": False,
    "trigger_game_num": None,
    "trigger_card": None,
    "trigger_suit": None,
    "predicted_value": None,
    "predicted_symbol": None,
    "target_game_num": None,
    "message_id": None,
    "checked": False,
    "games_checked": 0,
    "checked_game_ids": [],
    "strategy_used": "base"  # base, statistical, correlation, hybrid
}

state_lock = threading.Lock()

# --- УМНЫЕ ФУНКЦИИ СТАТИСТИКИ ---

def calculate_prediction_accuracy():
    """Рассчитывает точность прогнозов для каждой карты-триггера"""
    with state_lock:
        for trigger_card in stats:
            total = sum(stats[trigger_card].values())
            if total > 0:
                # Находим самую частую карту
                most_common = max(stats[trigger_card], key=stats[trigger_card].get)
                accuracy = stats[trigger_card][most_common] / total
                prediction_accuracy[trigger_card] = accuracy
                
                # Сохраняем корреляцию
                for card, count in stats[trigger_card].items():
                    if count > 0:
                        correlation = count / total
                        card_correlation[trigger_card][card] = correlation

def get_winning_streak():
    """Анализирует серию успешных прогнозов"""
    if len(success_history) < 3:
        return 0, 0
    
    # Считаем последние успехи
    recent_success = 0
    for i in range(len(success_history) - 1, -1, -1):
        if success_history[i]:
            recent_success += 1
        else:
            break
    
    # Считаем общий процент успеха
    total_success = sum(success_history[-50:])  # Последние 50 прогнозов
    total_attempts = min(len(success_history), 50)
    success_rate = total_success / total_attempts if total_attempts > 0 else 0
    
    return recent_success, success_rate

def get_best_predictions(trigger_card, min_accuracy=0.4):
    """Возвращает лучшие прогнозы для карты-триггера на основе статистики"""
    with state_lock:
        predictions = []
        
        # Проверяем статистику
        if trigger_card in stats and sum(stats[trigger_card].values()) > 5:
            total = sum(stats[trigger_card].values())
            for card, count in stats[trigger_card].items():
                accuracy = count / total
                if accuracy >= min_accuracy:
                    predictions.append((card, accuracy))
            
            # Сортируем по точности
            predictions.sort(key=lambda x: x[1], reverse=True)
            
            if predictions:
                return predictions
        
        # Если статистики нет, используем базовую стратегию
        if trigger_card in BASE_PREDICTION_STRATEGY:
            base = BASE_PREDICTION_STRATEGY[trigger_card]
            return [(card, prob/100) for card, prob in base]
        
        return [(1, 0.5)]  # Дефолтный прогноз

def analyze_trends():
    """Анализирует тренды в игре"""
    if len(game_results) < 10:
        return {}
    
    trends = {}
    # Анализируем последние N игр
    window = min(20, len(game_results))
    recent = game_results[-window:]
    
    # Считаем частоту каждой карты в последних играх
    card_freq = defaultdict(int)
    for cards in recent:
        for card in cards:
            card_freq[card] += 1
    
    # Находим карты, которые выпадают чаще обычного
    avg_freq = sum(card_freq.values()) / len(card_freq) if card_freq else 0
    for card, freq in card_freq.items():
        if freq > avg_freq * 1.5:  # На 50% выше среднего
            trends[card] = freq / window
    
    return trends

def get_smart_prediction(trigger_card):
    """Умный выбор прогноза с учетом нескольких факторов"""
    with state_lock:
        # 1. Получаем статистические прогнозы
        stat_predictions = get_best_predictions(trigger_card, min_accuracy=0.35)
        
        # 2. Анализируем тренды
        trends = analyze_trends()
        
        # 3. Проверяем текущую серию
        streak, success_rate = get_winning_streak()
        
        # 4. Выбираем стратегию
        strategy = "hybrid"
        final_predictions = []
        
        for card, acc in stat_predictions:
            weight = acc  # Базовая точность
            
            # Если карта в тренде - увеличиваем вес
            if card in trends:
                weight += trends[card] * 0.3
            
            # Если есть успешная серия - немного увеличиваем уверенность
            if streak > 3 and success_rate > 0.6:
                weight *= 1.1
            
            # Проверяем корреляцию
            if trigger_card in card_correlation and card in card_correlation[trigger_card]:
                correlation = card_correlation[trigger_card][card]
                weight += correlation * 0.2
            
            final_predictions.append((card, weight))
        
        # Сортируем по весу
        final_predictions.sort(key=lambda x: x[1], reverse=True)
        
        # Выбираем лучший прогноз
        if final_predictions:
            best_card = final_predictions[0][0]
            best_score = final_predictions[0][1]
            
            # Если уверенность низкая, используем базовую стратегию
            if best_score < 0.3 and trigger_card in BASE_PREDICTION_STRATEGY:
                base = BASE_PREDICTION_STRATEGY[trigger_card]
                best_card = max(base, key=lambda x: x[1])[0]
                strategy = "base"
            else:
                strategy = "hybrid"
            
            return best_card, strategy
        
        return 1, "base"

def update_smart_statistics(trigger_card, actual_card, success):
    """Обновляет статистику с умным анализом"""
    with state_lock:
        # Обновляем базовую статистику
        stats[trigger_card][actual_card] += 1
        
        # Сохраняем историю успеха
        success_history.append(success)
        if len(success_history) > 100:  # Ограничиваем историю
            success_history.pop(0)
        
        # Пересчитываем точность
        calculate_prediction_accuracy()
        
        # Логируем улучшение
        total = sum(stats[trigger_card].values())
        if total > 20:
            accuracy = prediction_accuracy.get(trigger_card, 0)
            print(f"📊 Статистика для карты {format_card(trigger_card)}: {accuracy*100:.1f}% ({total} прогнозов)")

# --- ОСНОВНЫЕ ФУНКЦИИ ---

def get_utc_game_number():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize_game_num(num):
    while num > 1440: num -= 1440
    while num < 1: num += 1440
    return num

def format_card(card_value):
    return CARD_SYMBOLS.get(card_value, str(card_value))

def format_card_full(card_value, suit_code):
    symbol = CARD_SYMBOLS.get(card_value, str(card_value))
    suit = SUITS.get(suit_code, {}).get("symbol", "?")
    return f"{symbol}{suit}"

def parse_cards_from_api(cards_json):
    try:
        if not cards_json or cards_json == "[]" or cards_json.startswith("Win") or cards_json in ["Win1", "Win2", "Tie"]:
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
        resp = requests.get(url, headers=HEADERS, timeout=3, proxies=NO_PROXY)
        if resp.status_code == 200:
            return resp.json().get("Value", {})
        return None
    except Exception as e:
        print(f"⚠️ Ошибка сети при запросе игры {game_id}: {e}")
        return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=3, proxies=NO_PROXY)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
        return []
    except Exception as e:
        print(f"⚠️ Ошибка загрузки списка Live: {e}")
        return []

def get_game_card_info(game_id):
    game_data = fetch_game_details(game_id)
    if not game_data:
        return None, False
    cards = get_all_game_cards(game_data)
    is_finished = game_data.get("SC", {}).get("CPS") == "Игра завершена"
    return cards, is_finished

def check_prediction_for_game(cards_info, predicted_value):
    if not predicted_value:
        return False, None, None
    for card in cards_info.get("all", []):
        val = card["value"]
        if predicted_value == 1 and val in [1, 14]:
            return True, val, card["suit"]
        if val == predicted_value:
            return True, val, card["suit"]
    return False, None, None

def process_game_step(game_num, game_id, first_card, first_suit, cards_info, is_finished):
    global game_counter, game_results
    
    with state_lock:
        # Сохраняем результат игры для анализа трендов
        all_card_values = [c["value"] for c in cards_info.get("all", [])]
        if all_card_values:
            game_results.append(all_card_values)
            if len(game_results) > 200:  # Ограничиваем историю
                game_results.pop(0)
        
        # Если есть активный прогноз и он еще не проверен полностью
        if prediction["active"] and not prediction["checked"]:
            target_num = prediction["target_game_num"]
            
            # Проверяем целевую игру и 2 следующие
            if game_num == target_num or (game_num > target_num and game_num - target_num <= 2):
                if game_id not in prediction["checked_game_ids"]:
                    prediction["checked_game_ids"].append(game_id)
                    prediction["games_checked"] += 1
                    
                    is_hit, hit_value, hit_suit = check_prediction_for_game(cards_info, prediction["predicted_value"])
                    
                    if is_hit:
                        offset = game_num - target_num
                        emoji_map = {0: "✅0️⃣", 1: "✅1️⃣", 2: "✅2️⃣"}
                        result_text = f"{emoji_map[offset]} (на {offset+1}-й игре)\n🃏 Найдена: {format_card_full(hit_value, hit_suit)}"
                        update_prediction_message(result_text, True)
                        print(f"🎯 ПРОГНОЗ СБЫЛСЯ! Игра #{offset+1}")
                        
                        # Обновляем статистику с успехом
                        update_smart_statistics(prediction["trigger_card"], prediction["predicted_value"], True)
                        prediction["checked"] = True
                        reset_prediction()
                        
                    elif prediction["games_checked"] >= 3 or (prediction["games_checked"] >= 2 and is_finished):
                        actual_cards = ", ".join([c["full"] for c in cards_info.get("all", [])[:3]])
                        result_text = f"❌ НЕ СБЫЛСЯ ({prediction['games_checked']} попыток)\n🃏 Выпали: {actual_cards}"
                        update_prediction_message(result_text, False)
                        print(f"❌ ПРОГНОЗ НЕ СБЫЛСЯ")
                        
                        # Обновляем статистику с неудачей
                        update_smart_statistics(prediction["trigger_card"], prediction["predicted_value"], False)
                        prediction["checked"] = True
                        reset_prediction()

        # Создаем новый прогноз, если нет активного
        if not prediction["active"] and first_card:
            create_smart_prediction(game_num, first_card, first_suit)

def create_smart_prediction(trigger_num, trigger_card, trigger_suit):
    if prediction["active"]:
        return
    
    # Используем умную стратегию
    pred_value, strategy = get_smart_prediction(trigger_card)
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
    prediction["games_checked"] = 0
    prediction["checked_game_ids"] = []
    prediction["strategy_used"] = strategy
    
    send_smart_prediction_message(trigger_num, trigger_card, trigger_suit, pred_symbol, target_num, strategy)
    print(f"🎯 НОВЫЙ ПРОГНОЗ: #{trigger_num} ({format_card_full(trigger_card, trigger_suit)}) -> #{target_num} ({pred_symbol}) [Стратегия: {strategy}]")

def send_smart_prediction_message(trigger_num, trigger_card, trigger_suit, pred_symbol, target_num, strategy):
    trigger_full = format_card_full(trigger_card, trigger_suit)
    
    # Добавляем информацию о стратегии и уверенности
    strategy_emoji = {
        "base": "📊",
        "statistical": "📈",
        "correlation": "🔗",
        "hybrid": "🧠"
    }
    strategy_names = {
        "base": "Базовая",
        "statistical": "Статистическая",
        "correlation": "Корреляционная",
        "hybrid": "Гибридная (умная)"
    }
    
    # Показываем точность для этой карты
    accuracy = prediction_accuracy.get(trigger_card, 0) * 100
    accuracy_text = f"{accuracy:.1f}%" if accuracy > 0 else "Нет данных"
    
    # Показываем серию успехов
    streak, success_rate = get_winning_streak()
    streak_text = f"🔥 {streak} подряд" if streak > 0 else "🔄 Нет серии"
    success_rate_text = f"{(success_rate*100):.1f}%" if success_rate > 0 else "Нет данных"
    
    msg = (
        f"🎯 ПРОГНОЗ БАККАРА\n"
        f"─────────────────\n"
        f"📌 Триггер: игра #{trigger_num}\n"
        f"🃏 Первая карта: {trigger_full}\n"
        f"─────────────────\n"
        f"🎯 Прогноз: {pred_symbol}\n"
        f"🎯 Игра: #{target_num}\n"
        f"📊 Проверка: 3 игры\n"
        f"─────────────────\n"
        f"🧠 Стратегия: {strategy_names.get(strategy, 'Гибридная')}\n"
        f"📈 Точность карты: {accuracy_text}\n"
        f"🔥 Серия: {streak_text}\n"
        f"📊 Успешность: {success_rate_text}\n"
        f"─────────────────\n"
        f"⏳ Ожидание результата..."
    )
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, msg)
        prediction["message_id"] = sent.message_id
    except Exception as e:
        print(f"❌ Ошибка отправки сообщения: {e}")

def update_prediction_message(result_text, success):
    if not prediction["message_id"]: return
    
    trigger_num = prediction["trigger_game_num"]
    trigger_card = prediction["trigger_card"]
    trigger_suit = prediction["trigger_suit"]
    pred_symbol = prediction["predicted_symbol"]
    target_num = prediction["target_game_num"]
    trigger_full = format_card_full(trigger_card, trigger_suit)
    strategy = prediction.get("strategy_used", "hybrid")
    
    strategy_names = {
        "base": "Базовая",
        "statistical": "Статистическая",
        "correlation": "Корреляционная",
        "hybrid": "Гибридная (умная)"
    }
    
    accuracy = prediction_accuracy.get(trigger_card, 0) * 100
    accuracy_text = f"{accuracy:.1f}%" if accuracy > 0 else "Нет данных"
    
    streak, success_rate = get_winning_streak()
    streak_text = f"🔥 {streak} подряд" if streak > 0 else "🔄 Нет серии"
    success_rate_text = f"{(success_rate*100):.1f}%" if success_rate > 0 else "Нет данных"
    
    result_emoji = "✅" if success else "❌"
    
    msg = (
        f"🎯 ПРОГНОЗ БАККАРА\n"
        f"─────────────────\n"
        f"📌 Триггер: игра #{trigger_num}\n"
        f"🃏 Первая карта: {trigger_full}\n"
        f"─────────────────\n"
        f"🎯 Прогноз: {pred_symbol}\n"
        f"🎯 Игра: #{target_num}\n"
        f"📊 Проверка: 3 игры\n"
        f"─────────────────\n"
        f"🧠 Стратегия: {strategy_names.get(strategy, 'Гибридная')}\n"
        f"📈 Точность карты: {accuracy_text}\n"
        f"🔥 Серия: {streak_text}\n"
        f"📊 Успешность: {success_rate_text}\n"
        f"─────────────────\n"
        f"{result_emoji} Результат: {result_text}"
    )
    try:
        bot.edit_message_text(chat_id=PREDICTION_CHANNEL_ID, message_id=prediction["message_id"], text=msg)
    except Exception as e:
        print(f"❌ Ошибка обновления сообщения: {e}")

def reset_prediction():
    for key in prediction:
        if key in ["active", "checked"]:
            prediction[key] = False
        elif key in ["games_checked"]:
            prediction[key] = 0
        elif key in ["checked_game_ids"]:
            prediction[key] = []
        else:
            prediction[key] = None

def main():
    global game_counter, processed_game_ids
    print("\n🚀 ЗАПУСК УМНОГО МОНИТОРИНГА LIVE")
    print("=" * 60)
    print("🧠 Стратегии:")
    print("  - Базовая: начальная стратегия")
    print("  - Статистическая: на основе накопленных данных")
    print("  - Корреляционная: анализ связей между картами")
    print("  - Гибридная: комбинация всех стратегий")
    print("=" * 60)
    
    # Инициализируем статистику
    calculate_prediction_accuracy()
    
    while True:
        try:
            games = get_active_games()
            
            for g in games:
                gid = g.get("I")
                if not gid:
                    continue
                
                cards_info, is_finished = get_game_card_info(gid)
                
                if not cards_info or not cards_info.get("player"):
                    continue
                
                first_card = cards_info["player"][0]
                first_value = first_card["value"]
                first_suit = first_card["suit"]
                
                if gid not in processed_game_ids:
                    game_counter += 1
                    game_num = get_utc_game_number()
                    
                    all_cards = ", ".join([c["full"] for c in cards_info.get("all", [])])
                    status_str = "Завершена" if is_finished else "ИДЕТ (LIVE)"
                    print(f"🎮 Игра {gid} (#{game_num}) [{status_str}] | Первая: {first_card['full']} | Карты: {all_cards}")
                    
                    if is_finished:
                        processed_game_ids.add(gid)
                    
                    process_game_step(game_num, gid, first_value, first_suit, cards_info, is_finished)
            
            time.sleep(2)
            
        except Exception as e:
            print(f"⚠️ Ошибка главного цикла: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
