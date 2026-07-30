import os
import time
import json
import datetime
import requests
import telebot
from collections import defaultdict

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID")

LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "️♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

# --- СОСТОЯНИЕ ---
logged_ids = set()
games_by_num = defaultdict(list)
predictions_made_for_num = set()

stats = {"total_seen": 0, "predictions": 0, "hits": 0, "misses": 0}
patterns = {"4_suits": 0, "3_suits": 0, "2_suits": 0, "1_suit": 0, "total_analyzed": 0}

active_pred = {
    "active": False, "msg_id": None, "trigger_num": 0, "target_num": 0,
    "suit_code": 0, "attempts": 0
}

# 🔥 НОВОЕ: Отслеживание проверенных игр (с очисткой)
checked_game_nums = set()
last_summary_time = time.time()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_game_num():
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now.hour * 60) + now.minute + 1

def normalize(num):
    while num > 1440: num -= 1440
    while num < 1: num += 1440
    return num

def fetch_details(gid):
    try:
        resp = requests.get(DETAIL_URL_TEMPLATE.format(game_id=gid), headers=HEADERS, timeout=5)
        if resp.status_code == 200: return resp.json().get("Value", {})
    except: pass
    return None

def get_active_games():
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=5)
        if resp.status_code == 200: return resp.json().get("Value", [])
    except: pass
    return []

def parse_cards(data):
    player_cards = []
    is_finished = False
    try:
        sc = data.get("SC", {})
        cps = str(sc.get("CPS", "")).lower()
        is_finished = "завершена" in cps or "finished" in cps or sc.get("GE") == 1
        
        for item in sc.get("S", []):
            if str(item.get("Key", "")).upper() != "P": continue
            value = item.get("Value", "")
            try:
                if value:
                    parsed = json.loads(value) if isinstance(value, str) else value
                    if isinstance(parsed, list):
                        for c in parsed:
                            val = c.get("R") or c.get("CV") or c.get("C", 0)
                            suit = c.get("S") or c.get("CS") or c.get("Suit", 0)
                            if val and int(val) > 0:
                                player_cards.append({"value": int(val), "suit": int(suit)})
            except: pass
    except: pass
    return player_cards, is_finished

# --- ТЕЛЕГРАМ ФУНКЦИИ ---
def send_to_channel(text, parse_mode="HTML"):
    try:
        bot.send_message(STATS_CHANNEL_ID, text, parse_mode=parse_mode)
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}")

def update_pred_message(success, details):
    if not active_pred["msg_id"]: return
    try:
        emoji = "✅" if success else ""
        text = (
            f"🎯 <b>ПРОГНОЗ МАСТИ ИГРОКА</b>\n"
            f"📌 Триггер: #{active_pred['trigger_num']}\n"
            f"🃏 Масть: {SUITS[active_pred['suit_code']]['symbol']} {SUITS[active_pred['suit_code']]['name']}\n"
            f"🎯 Цель: #{active_pred['target_num']}\n\n"
            f"{emoji} <b>Результат:</b>\n{details}"
        )
        bot.edit_message_text(chat_id=STATS_CHANNEL_ID, message_id=active_pred["msg_id"], text=text, parse_mode="HTML")
    except: pass

def reset_pred():
    active_pred.update({
        "active": False, "msg_id": None, "trigger_num": 0, 
        "target_num": 0, "suit_code": 0, "attempts": 0
    })
    print("🔄 Прогноз сброшен, готов к новому")

def send_summary():
    global last_summary_time
    hit_rate = (stats["hits"] / stats["predictions"] * 100) if stats["predictions"] > 0 else 0
    text = (
        f"📊 <b>СВОДНАЯ СТАТИСТИКА</b>\n"
        f"─────────────────\n"
        f"👁 Всего ID: {stats['total_seen']}\n"
        f"🎯 Прогнозов: {stats['predictions']}\n"
        f" Зашло: {stats['hits']} ({hit_rate:.1f}%)\n"
        f"🔴 Не зашло: {stats['misses']}\n"
        f"─────────────────\n"
        f"🔍 <b>Паттерны:</b> 4={patterns['4_suits']} 3={patterns['3_suits']} 2={patterns['2_suits']} 1={patterns['1_suit']}"
    )
    send_to_channel(text)
    last_summary_time = time.time()
    print(f"📊 Статистика отправлена")

# --- ОСНОВНАЯ ЛОГИКА ---
def process_games():
    games = get_active_games()
    if not games: return
    
    current_num = get_game_num()
    
    # 🔥 1. ОЧИСТКА СТАРЫХ ДАННЫХ (каждые 100 циклов)
    if stats["total_seen"] % 100 == 0:
        old_keys = [k for k in list(games_by_num.keys()) if k < normalize(current_num - 20)]
        for k in old_keys:
            del games_by_num[k]
        # Очищаем старые проверенные номера (оставляем только последние 50)
        if len(checked_game_nums) > 50:
            checked_game_nums.clear()
        print(f" Очистка памяти. Текущий номер: {current_num}")
    
    # 2. СБОР НОВЫХ ID
    new_ids_count = 0
    for g in games:
        gid = g.get("I")
        if not gid or gid in logged_ids: continue
        logged_ids.add(gid)
        stats["total_seen"] += 1
        new_ids_count += 1
        if gid not in games_by_num[current_num]:
            games_by_num[current_num].append(gid)
    
    if new_ids_count > 0:
        print(f" Найдено {new_ids_count} новых ID. Всего в памяти: {len(games_by_num)} игр")

    #  3. СОЗДАНИЕ ПРОГНОЗА (если нет активного и есть 3+ ID)
    if not active_pred["active"]:
        for gnum, ids in list(games_by_num.items()):
            if gnum in predictions_made_for_num or len(ids) < 3: continue
            
            # Анализ паттерна
            suits_found = set(int(i) % 4 for i in ids)
            n_suits = len(suits_found)
            patterns["total_analyzed"] += 1
            if n_suits == 4: patterns["4_suits"] += 1
            elif n_suits == 3: patterns["3_suits"] += 1
            elif n_suits == 2: patterns["2_suits"] += 1
            else: patterns["1_suit"] += 1

            # Выбор оптимального ID
            optimal_id = ids[0]
            optimal_suit = int(ids[0]) % 4
            for suit_code in [0, 1, 2, 3]:
                if any(int(i) % 4 == suit_code for i in ids):
                    optimal_id = next(i for i in ids if int(i) % 4 == suit_code)
                    optimal_suit = suit_code
                    break

            target_num = normalize(gnum + 3)
            predictions_made_for_num.add(gnum)
            
            pattern_text = "Все 4 масти" if n_suits == 4 else f"{n_suits} масти"
            msg = (
                f"🎯 <b>НОВЫЙ ПРОГНОЗ</b>\n"
                f"📌 Триггер: #{gnum}\n"
                f"🃏 Масть: {SUITS[optimal_suit]['symbol']} {SUITS[optimal_suit]['name']}\n"
                f"🎯 Цель: #{target_num}\n"
                f"📊 Паттерн: {pattern_text}\n"
                f" <i>Ожидание...</i>"
            )
            sent = bot.send_message(STATS_CHANNEL_ID, msg, parse_mode="HTML")
            
            stats["predictions"] += 1
            active_pred.update({
                "active": True, "msg_id": sent.message_id, "trigger_num": gnum,
                "target_num": target_num, "suit_code": optimal_suit, "attempts": 0
            })
            print(f"🚀 ПРОГНОЗ СОЗДАН: #{gnum} -> #{target_num} ({SUITS[optimal_suit]['symbol']}) | ID: {len(ids)} шт")
            return  # 🔥 ВАЖНО: создаем только 1 прогноз за раз

    # 🔥 4. ПРОВЕРКА РЕЗУЛЬТАТОВ (если есть активный прогноз)
    if active_pred["active"]:
        target = active_pred["target_num"]
        check_range = [normalize(target + i) for i in range(3)]
        
        for gnum in check_range:
            if gnum in checked_game_nums: continue
            
            ids_to_check = games_by_num.get(gnum, [])
            if not ids_to_check: continue
            
            print(f" Проверка игры #{gnum} (попытка {active_pred['attempts']+1}/3)")
            active_pred["attempts"] += 1
            checked_game_nums.add(gnum)
            
            results_text = f"📝 <b>Игра #{gnum} (все потоки):</b>\n"
            is_hit = False
            all_finished = True
            
            for gid in ids_to_check:
                data = fetch_details(gid)
                if not data: 
                    results_text += f"🆔 <code>{gid[-6:]}</code>: ⚠️ Ошибка\n"
                    continue
                    
                cards, is_finished = parse_cards(data)
                
                if not is_finished:
                    all_finished = False
                    results_text += f"🆔 <code>{gid[-6:]}</code>: ⏳ Идет\n"
                    continue
                
                if cards:
                    cards_str = " ".join([f"{c['value']}{SUITS[c['suit']]['symbol']}" for c in cards])
                    results_text += f"🆔 <code>{gid[-6:]}</code>: {cards_str}\n"
                    if any(c["suit"] == active_pred["suit_code"] for c in cards):
                        is_hit = True
                else:
                    results_text += f"🆔 <code>{gid[-6:]}</code>: ⚠️ Пусто\n"
            
            # Если не все игры завершились, ждем
            if not all_finished:
                print(f"⏳ Игра #{gnum} еще не все потоки завершились, ждем...")
                continue
            
            # Финальный результат
            if is_hit:
                update_pred_message(True, results_text)
                stats["hits"] += 1
                print(f"🎉 ПРОГНОЗ ЗАШЁЛ на #{gnum}!")
                reset_pred()
                return
            elif active_pred["attempts"] >= 3:
                update_pred_message(False, results_text + "\n<i>(3 попытки)</i>")
                stats["misses"] += 1
                print(f"❌ Прогноз не зашёл (3 попытки)")
                reset_pred()
                return
            else:
                print(f"⏳ Продолжаем проверку (попыток: {active_pred['attempts']}/3)")

# --- ЗАПУСК ---
def main():
    print("🚀 ЗАПУСК БОТА (ОПТИМИЗИРОВАННЫЙ)")
    print("=" * 60)
    send_to_channel("🟢 <b>Бот запущен (оптимизированная версия)</b>")
    
    cycle = 0
    while True:
        try:
            process_games()
            cycle += 1
            
            #  Статистика раз в ЧАС (3600 секунд)
            if time.time() - last_summary_time >= 3600:
                send_summary()
            
            if cycle % 20 == 0:
                print(f"⏱ Цикл {cycle} | Активен: {active_pred['active']} | Прогнозов: {stats['predictions']}")
            
            time.sleep(3)
        except Exception as e:
            print(f"️ Ошибка: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(5)

if __name__ == "__main__":
    main()
