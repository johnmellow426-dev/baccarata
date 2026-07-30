import os
import time
import json
import datetime
import requests
import telebot
from collections import defaultdict

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID")  # ID канала для трансляции

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

# --- СОСТОЯНИЕ И СТАТИСТИКА ---
logged_ids = set()
finished_ids = set()
games_by_num = defaultdict(list)
predictions_made_for_num = set()

stats = {"total_seen": 0, "finished": 0, "predictions": 0, "hits": 0, "misses": 0}
patterns = {"4_suits": 0, "3_suits": 0, "2_suits": 0, "1_suit": 0, "total_analyzed": 0}

active_pred = {
    "active": False, "msg_id": None, "trigger_num": 0, "target_num": 0,
    "suit_code": 0, "attempts": 0, "checked_nums": set()
}

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

# --- ФУНКЦИИ ТЕЛЕГРАМ ---
def send_to_channel(text):
    try:
        bot.send_message(STATS_CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        print(f"❌ Ошибка Telegram: {e}")

def update_pred_message(success, details):
    if not active_pred["msg_id"]: return
    try:
        emoji = "✅" if success else "❌"
        text = (
            f"🎯 <b>ПРОГНОЗ МАСТИ ИГРОКА</b>\n"
            f"📌 Триггер: #{active_pred['trigger_num']}\n"
            f"🃏 Масть: {SUITS[active_pred['suit_code']]['symbol']} {SUITS[active_pred['suit_code']]['name']}\n"
            f"🎯 Цель: #{active_pred['target_num']} (диапазон)\n\n"
            f"{emoji} <b>Результат:</b> {details}"
        )
        bot.edit_message_text(chat_id=STATS_CHANNEL_ID, message_id=active_pred["msg_id"], text=text, parse_mode="HTML")
    except: pass

def reset_pred():
    active_pred.update({"active": False, "msg_id": None, "trigger_num": 0, "target_num": 0, "suit_code": 0, "attempts": 0, "checked_nums": set()})

def send_summary():
    hit_rate = (stats["hits"] / stats["predictions"] * 100) if stats["predictions"] > 0 else 0
    text = (
        f"📊 <b>СВОДНАЯ СТАТИСТИКА</b>\n"
        f"─────────────────\n"
        f"👁 Всего увидено игр: {stats['total_seen']}\n"
        f"✅ Завершено с картами: {stats['finished']}\n"
        f"🎯 Всего прогнозов: {stats['predictions']}\n"
        f"🟢 Зашло: {stats['hits']} ({hit_rate:.1f}%)\n"
        f"🔴 Не зашло: {stats['misses']}\n"
        f"─────────────────\n"
        f"🔍 <b>Анализ паттернов ID:</b>\n"
        f"   4 масти: {patterns['4_suits']}\n"
        f"   3 масти: {patterns['3_suits']}\n"
        f"   2 масти: {patterns['2_suits']}\n"
        f"   1 масть: {patterns['1_suit']}"
    )
    send_to_channel(text)

# --- ОСНОВНАЯ ЛОГИКА ---
def process_games():
    games = get_active_games()
    if not games: return
    
    current_num = get_game_num()
    
    # 1. Сбор ID
    for g in games:
        gid = g.get("I")
        if not gid or gid in logged_ids: continue
        logged_ids.add(gid)
        stats["total_seen"] += 1
        if gid not in games_by_num[current_num]:
            games_by_num[current_num].append(gid)

    # Очистка старых данных из памяти (храним только последние 10 номеров игр)
    keys_to_delete = [k for k in games_by_num if k < normalize(current_num - 10)]
    for k in keys_to_delete: del games_by_num[k]

    # 2. Создание прогноза (при 3+ ID)
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

        # Выбор оптимального ID (приоритет ♠→♣→♦→♥)
        optimal_id = ids[0]
        optimal_suit = int(ids[0]) % 4
        for suit_code in [0, 1, 2, 3]:
            if any(int(i) % 4 == suit_code for i in ids):
                optimal_id = next(i for i in ids if int(i) % 4 == suit_code)
                optimal_suit = suit_code
                break

        target_num = normalize(gnum + 3)
        predictions_made_for_num.add(gnum)
        
        # Отправка в канал
        pattern_text = "Все 4 масти" if n_suits == 4 else f"{n_suits} масти"
        msg = (
            f"🎯 <b>НОВЫЙ ПРОГНОЗ</b>\n"
            f"📌 Триггер: #{gnum} (ID: <code>{optimal_id}</code>)\n"
            f"🃏 Масть: {SUITS[optimal_suit]['symbol']} {SUITS[optimal_suit]['name']}\n"
            f"🎯 Цель: #{target_num} (диапазон {target_num}-{normalize(target_num+2)})\n"
            f"📊 Паттерн: доступно {pattern_text}\n"
            f"⏳ <i>Ожидание результата...</i>"
        )
        sent = bot.send_message(STATS_CHANNEL_ID, msg, parse_mode="HTML")
        
        stats["predictions"] += 1
        active_pred.update({
            "active": True, "msg_id": sent.message_id, "trigger_num": gnum,
            "target_num": target_num, "suit_code": optimal_suit, "attempts": 0, "checked_nums": set()
        })
        print(f"🚀 Прогноз создан: #{gnum} -> #{target_num}")

    # 3. Проверка результатов
    if not active_pred["active"]: return
    
    target = active_pred["target_num"]
    check_range = {normalize(target + i) for i in range(3)}
    
    for gnum in check_range:
        if gnum in active_pred["checked_nums"]: continue
        
        ids = games_by_num.get(gnum, [])
        if not ids: continue
        
        # Ищем завершенную игру среди ID этого номера
        player_cards = []
        is_finished = False
        
        for gid in ids:
            if gid in finished_ids: continue
            data = fetch_details(gid)
            if not data: continue
            
            cards, finished = parse_cards(data)
            if finished:
                finished_ids.add(gid)
                is_finished = True
                if cards:
                    player_cards = cards
                    break # Нашли карты, дальше не ищем
        
        if not is_finished: continue
        
        stats["finished"] += 1
        active_pred["checked_nums"].add(gnum)
        active_pred["attempts"] += 1
        
        if not player_cards:
            print(f"⚠️ #{gnum} завершена, карт нет")
            continue
        
        cards_str = " ".join([f"{c['value']}{SUITS[c['suit']]['symbol']}" for c in player_cards])
        send_to_channel(f"📝 <b>Игра #{gnum} завершена:</b> Игрок получил <code>{cards_str}</code>")
        
        # Проверка попадания
        if any(c["suit"] == active_pred["suit_code"] for c in player_cards):
            update_pred_message(True, f"Зашло на игре #{gnum}!\nКарты: {cards_str}")
            stats["hits"] += 1
            print(f"🎉 ПРОГНОЗ ЗАШЁЛ!")
            reset_pred()
            return
        elif active_pred["attempts"] >= 3:
            update_pred_message(False, f"Не зашло (3 попытки).\nПоследняя игра #{gnum}: {cards_str}")
            stats["misses"] += 1
            print(f"❌ Прогноз не зашёл")
            reset_pred()
            return

# --- ЗАПУСК ---
def main():
    print("🚀 ЗАПУСК БОТА-СТАТИСТА")
    send_to_channel("🟢 <b>Бот-статист запущен и начал мониторинг...</b>")
    
    cycle = 0
    while True:
        try:
            process_games()
            cycle += 1
            # Сводка каждые 50 циклов (примерно каждые 2.5 минуты)
            if cycle % 50 == 0:
                send_summary()
            time.sleep(3)
        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
