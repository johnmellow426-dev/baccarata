import os
import time
import re
import datetime
import threading
import requests
import telebot
from telebot.apihelper import ApiTelegramException

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHANNEL_ID              = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID   = os.getenv("PREDICTION_CHANNEL_ID")
MASTI_CHANNEL_ID        = os.getenv("MASTI_CHANNEL_ID")
STATS_SOURCE_CHANNEL_ID = int(os.getenv("STATS_SOURCE_CHANNEL_ID", "0"))

SERIES_TRIGGERS= set(int(x) for x in os.getenv("SERIES_TRIGGERS", "2,3,4,5").split(","))
PRED_TIMEOUT   = int(os.getenv("PRED_TIMEOUT", 720))
MAX_ACTIVE     = int(os.getenv("MAX_ACTIVE", 10))

# Параметры плотного потока генератора (delta ID)
MIN_DELTA_ID   = int(os.getenv("MIN_DELTA_ID", 100))
MAX_DELTA_ID   = int(os.getenv("MAX_DELTA_ID", 160))

# Сомнительные окончания game_id, которые нужно пропускать
FORBIDDEN_ENDINGS = ("62", "73", "51", "84")

API_URL = "https://melbet-2814.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}

ARROW_CHAR = '\U0001F448'

SUITS_MAP = {
    1: {"name": "Пики", "symbol": "♠️"},
    2: {"name": "Трефы", "symbol": "♣️"},
    3: {"name": "Бубны", "symbol": "♦️"},
    4: {"name": "Червы", "symbol": "♥️"}
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== СОСТОЯНИЕ ====================
sent_games   = set()
active_preds = []
current_series = {"pair": None, "dis": [], "ids": [], "published": False}
processed_stats_nums = set()
mast_preds = {}

# Флаг очереди для мастей (разрешено ли давать следующий прогноз)
can_predict_masti = True

# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
def normalize(n): 
    return ((n - 1) % 1440) + 1

def is_final_result(text):
    if not text: 
        return False
    if ARROW_CHAR in text: 
        return False
    return True

def get_suit_by_id(game_id):
    last_digit = int(str(game_id)[-1])
    if last_digit in (1, 5, 9):
        return 1  # ♠️ Пики
    elif last_digit in (2, 6):
        return 2  # ♣️ Трефы
    elif last_digit in (3, 7):
        return 3  # ♦️ Бубны
    else: # 4, 8, 0
        return 4  # ♥️ Червы

def parse_player_suits(text):
    groups = re.findall(r'\(([^)]*)\)', text or "")
    if not groups: 
        return set()
        
    player_cards_text = groups[0]
    found_suits = set()
    
    if any(s in player_cards_text for s in ['♠', '♠️']): 
        found_suits.add('♠️')
    if any(s in player_cards_text for s in ['♣', '♣️']): 
        found_suits.add('♣️')
    if any(s in player_cards_text for s in ['♦', '♦️']): 
        found_suits.add('♦️')
    if any(s in player_cards_text for s in ['♥', '♥️']): 
        found_suits.add('♥️')
        
    return found_suits

# ==================== API / TG ====================
def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200: return resp.json().get("Value", [])
    except Exception as e: print(f"⚠️ API: {e}")
    return []

def format_game_info(game):
    try:
        return (
            f"🎮 ИГРА #N{game.get('I','N/A')}   Display ID: {game.get('DI','N/A')}\n"
            f"──────────────────────────────\n")
    except Exception as e:
        print(f"⚠️ fmt: {e}"); return None

def send_to_channel(text):
    if not CHANNEL_ID: return False
    try: bot.send_message(CHANNEL_ID, text, parse_mode="HTML"); return True
    except Exception as e: print(f"⚠️ send: {e}"); return False

def send_prediction(text):
    if not PREDICTION_CHANNEL_ID: return None
    try: return bot.send_message(PREDICTION_CHANNEL_ID, text)
    except Exception as e: print(f"⚠️ pred: {e}"); return None

def send_masti_prediction(text):
    if not MASTI_CHANNEL_ID: return None
    try: return bot.send_message(MASTI_CHANNEL_ID, text)
    except Exception as e: print(f"⚠️ masti pred: {e}"); return None

def parse_stats(text):
    if not text: return None
    m = re.search(r'#N(\d+)', text)
    if not m: return None
    return int(m.group(1)), bool(re.search(r'#R\b', text))

def finalize(pred, success, detail):
    try:
        mark = "✅" if success else "❌"
        bot.edit_message_text(
            chat_id=PREDICTION_CHANNEL_ID, message_id=pred["msg_id"],
            text=(f"🎯 Игра #N{pred['first_n']}\nВозможна Раздача (серия потока)\n"
                  f"проверка {pred['label']}\n{mark} {detail}"))
    except ApiTelegramException as e:
        if "message is not modified" not in e.description and "message to edit not found" not in e.description:
            print(f"⚠️ edit err: {e}")
    except Exception as e: 
        print(f"⚠️ edit: {e}")
        
    with lock:
        if pred in active_preds: 
            active_preds.remove(pred)

def finalize_mast(pred_key, result_text):
    """Финализация прогноза масти и открытие очереди для следующего"""
    global can_predict_masti
    with lock:
        if pred_key not in mast_preds: return
        pred = mast_preds[pred_key]
        del mast_preds[pred_key]
        # Разрешаем создавать следующий прогноз после финализации текущего
        can_predict_masti = True
        
    try:
        text = f"🎯 Игра #N{pred['first_n']}\nИгрок {pred['suit_symbol']} {result_text}".strip()
        bot.edit_message_text(chat_id=MASTI_CHANNEL_ID, message_id=pred["msg_id"], text=text)
    except ApiTelegramException as e:
        if "message is not modified" not in e.description and "message to edit not found" not in e.description:
            print(f"⚠️ edit mast err: {e}")
    except Exception as e: 
        print(f"⚠️ edit mast: {e}")

def _make_pred(first_n, second_n, label):
    with lock:
        if len(active_preds) >= MAX_ACTIVE:
            print(f"⛔ лимит {MAX_ACTIVE}, пропуск {label}"); return False
    text = (f"🎯 Игра #N{first_n}\nВозможна Раздача (серия потока)\n"
            f"проверка {label}\n⏳ Ожидание #R...")
    sent = send_prediction(text)
    if not sent: return False
    with lock:
        active_preds.append({"msg_id": sent.message_id, "first_n": first_n,
                             "second_n": second_n, "checked": set(),
                             "created_at": time.time(), "label": label})
    return True

# ==================== ЛОГИКА ПРОГНОЗА НАТУРАЛОВ ====================
def is_hot_stream(id_list):
    if len(id_list) < 2:
        return True
    delta = id_list[-1] - id_list[-2]
    return MIN_DELTA_ID <= delta <= MAX_DELTA_ID

def flush_series(series):
    if series["published"]: return
    dis = series["dis"]
    ids = series["ids"]
    length = len(dis)
    if length < 2: return

    if not is_hot_stream(ids):
        print(f"⚠️ Пропуск серии #N{dis[-1]}: Дельта ID ({ids[-1] - ids[-2]}) вне диапазона [{MIN_DELTA_ID}-{MAX_DELTA_ID}]")
        return

    if length == 2:
        first_n = int(dis[-1])
        second_n = normalize(first_n + 1)
    elif length in (3, 4):
        first_n = int(dis[1])
        second_n = int(dis[2])
    elif length == 5:
        first_n = int(dis[2])
        second_n = int(dis[3])
    else:
        first_n = int(dis[-2])
        second_n = int(dis[-1])

    label = f"#N{first_n}/#N{second_n}"
    if _make_pred(first_n, second_n, label):
        series["published"] = True
        delta = ids[-1] - ids[-2] if len(ids) >= 2 else 0
        print(f"🔥 ГОРАЧИЙ ПОТОК! ПРОГНОЗ {label} (серия len={length}, ΔID={delta})")

# ==================== СОЗДАНИЕ ПРОГНОЗОВ МАСТЕЙ ====================
def create_mast_prediction(game_id, display_id):
    """Создаёт прогноз с соблюдением очереди и фильтром сомнительных ID"""
    global can_predict_masti
    
    # 1. Проверка очереди (прогноз каждые 3 игры — ждём закрытия предыдущего)
    if not can_predict_masti:
        return

    # 2. Фильтр сомнительных окончаний game_id (62, 73, 51, 84)
    gid_str = str(game_id)
    if gid_str.endswith(FORBIDDEN_ENDINGS):
        print(f"🚫 Пропуск сомнительного ID: {game_id} (оканчивается на {gid_str[-2:]})")
        return

    with lock:
        if display_id in mast_preds: 
            return
        # Блокируем создание новых прогнозов, пока этот не закроется
        can_predict_masti = False
    
    suit_code = get_suit_by_id(game_id)
    suit_info = SUITS_MAP[suit_code]
    
    text = f"🎯 Игра #N{display_id}\nИгрок {suit_info['symbol']}"
    sent = send_masti_prediction(text)
    
    if sent:
        with lock:
            mast_preds[display_id] = {
                "msg_id": sent.message_id,
                "suit_code": suit_code,
                "suit_symbol": suit_info['symbol'],
                "first_n": display_id,
                "second_n": normalize(display_id + 1),
                "checked": set(),
                "created_at": time.time()
            }
        print(f"🎯 МАСТЬ #N{display_id} → {suit_info['symbol']} (ID {game_id}) | Очередь заблокирована до закрытия")
    else:
        # Если не удалось отправить, возвращаем возможность дать прогноз
        with lock:
            can_predict_masti = True

# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
def process_stats_message(msg):
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID: return
    parsed = parse_stats(msg.text)
    if not parsed: return
    num, has_nat = parsed

    if num in processed_stats_nums: return

    if not is_final_result(msg.text):
        if ARROW_CHAR in msg.text:
            print(f"⏳ промежуточный #N{num} (стрелка), пропускаем")
        else:
            print(f"⏳ промежуточный #N{num}, текст: {msg.text[:100]!r}")
        return

    processed_stats_nums.add(num)
    print(f"🔎 ФИНАЛ #N{num} | #R={has_nat}")

    # 1. Проверка прогнозов натуралов
    with lock:
        preds_to_check = list(active_preds)
        
    for pred in preds_to_check:
        if num in {pred["first_n"], pred["second_n"]} and num not in pred["checked"]:
            pred["checked"].add(num)
            if has_nat:
                pos = "0️⃣" if num == pred["first_n"] else "1️⃣"
                finalize(pred, True, f"{pos} #N{num} → Натурал #R")
                print(f"🎉 НАТУРАЛ #N{num} ({pos})")
            elif len(pred["checked"]) >= 2:
                finalize(pred, False, "❌ не зашло")
                print(f"❌ нет #R в {pred['label']}")

    # 2. Проверка прогнозов мастей
    with lock:
        masti_to_check = list(mast_preds.items())

    for pred_key, pred in masti_to_check:
        if num in {pred["first_n"], pred["second_n"]} and num not in pred["checked"]:
            pred["checked"].add(num)
            
            player_suits = parse_player_suits(msg.text)
            
            if player_suits:
                if pred["suit_symbol"] in player_suits:
                    pos = "0️⃣" if num == pred["first_n"] else "1️⃣"
                    finalize_mast(pred_key, f"✅{pos}")
                    print(f"🎉 МАСТЬ УГАДАНА #N{num} (шаг {pos}) | Рука: {player_suits}")
                else:
                    if len(pred["checked"]) >= 2:
                        # Финализация проигрыша с красным крестом ❌
                        finalize_mast(pred_key, "❌")
                        print(f"❌ МАСТЬ НЕ УГАДАНА (#N{pred['first_n']}/#N{pred['second_n']})")
                    else:
                        print(f"⏳ Шаг 0️⃣ (#N{num}) не зашёл. Ждём 1️⃣ шаг (#N{pred['second_n']})")
            else:
                print(f"⚠️ Не удалось распарсить карты игрока для #N{num}")

@bot.channel_post_handler()
def on_stats(msg):
    print(f"📨 POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)

@bot.edited_channel_post_handler()
def on_stats_edited(msg):
    print(f"✏️ EDITED POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)

# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global current_series
    games = fetch_data()
    if not games: return

    # Таймауты активных прогнозов
    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, "⌛ таймаут ожидания #R")
                
        for pred_key, pred in list(mast_preds.items()):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize_mast(pred_key, "❌")
                print(f"⌛ таймаут масти #N{pred_key}")

    new_games = []
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or gid in sent_games: continue
        sent_games.add(gid)
        new_games.append(game)
        text = format_game_info(game)
        if text: send_to_channel(text)
        if di: 
            create_mast_prediction(int(gid), int(di))
            
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di: continue
        
        gid_num = int(gid)
        pair = (gid_num // 100) % 100
        
        if current_series["pair"] is not None and pair == (current_series["pair"] + 1) % 100:
            if current_series["ids"] and not (MIN_DELTA_ID <= (gid_num - current_series["ids"][-1]) <= MAX_DELTA_ID):
                current_series = {"pair": pair, "dis": [di], "ids": [gid_num], "published": False}
            else:
                current_series["dis"].append(di)
                current_series["ids"].append(gid_num)
        else:
            current_series = {"pair": pair, "dis": [di], "ids": [gid_num], "published": False}
            
        current_series["pair"] = pair

        if len(current_series["dis"]) in SERIES_TRIGGERS and not current_series["published"]:
            flush_series(current_series)

    if new_games:
        print(f"✅ Новых: {len(new_games)} | серия пара={current_series['pair']} len={len(current_series['dis'])}")
        
    if len(sent_games) > 300: 
        sent_games.clear()

def main():
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | ΔID=[{MIN_DELTA_ID}-{MAX_DELTA_ID}] | Запрет ID: {FORBIDDEN_ENDINGS}")
    send_to_channel(f"🟢 <b>Бот запущен</b> | ΔID: [{MIN_DELTA_ID}-{MAX_DELTA_ID}] | Запрет ID: {FORBIDDEN_ENDINGS}")
    
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    
    while True:
        try:
            api_cycle()
            time.sleep(15)
        except Exception as e:
            print(f"⚠️ ошибка цикла: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
