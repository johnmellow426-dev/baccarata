import os
import time
import re
import json
import datetime
import threading
import tempfile
import requests
import telebot

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHANNEL_ID              = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID   = os.getenv("PREDICTION_CHANNEL_ID")
MASTI_CHANNEL_ID        = os.getenv("MASTI_CHANNEL_ID")
STATS_SOURCE_CHANNEL_ID = int(os.getenv("STATS_SOURCE_CHANNEL_ID"))
DB_CHANNEL_ID           = os.getenv("DB_CHANNEL_ID")

DB_PATH        = os.getenv("DB_PATH", "/data/totals_db.json")
SERIES_TRIGGERS= set(int(x) for x in os.getenv("SERIES_TRIGGERS", "2,3,4,5").split(","))
PRED_TIMEOUT   = int(os.getenv("PRED_TIMEOUT", 720))
MAX_ACTIVE     = int(os.getenv("MAX_ACTIVE", 10))
MAX_MAST_ACTIVE= int(os.getenv("MAX_MAST_ACTIVE", 2))  # 🔥 лимит активных прогнозов мастей
DB_DUMP_MIN    = int(os.getenv("DB_DUMP_MIN", 60))

API_URL = "https://melbet-2814.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}
SUITS_RE = re.compile(r'[♠♥♦♣]')

ARROW_CHAR = '\U0001F448'

SUITS_MAP = {
    0: {"name": "Пики", "symbol": "️♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== СОСТОЯНИЕ ====================
sent_games   = set()
active_preds = []
current_series = {"pair": None, "dis": [], "published": False}
processed_stats_nums = set()
mast_preds = {}

totals_db = {}
di_to_pair = {}
last_dump = 0.0

# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
def normalize(n): return ((n - 1) % 1440) + 1
def pair_key(p): return f"{p:02d}"

def is_final_result(text):
    if not text: return False
    if ARROW_CHAR in text: return False
    return True

def get_suit_by_id(game_id):
    last_digit = int(str(game_id)[-1])
    if last_digit % 3 == 0 and last_digit != 0:
        return 3
    return last_digit % 3

# ==================== БД ====================
def load_db():
    global totals_db, di_to_pair
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        totals_db  = data.get("totals", {})
        di_to_pair = {int(k): v for k, v in data.get("di_to_pair", {}).items()}
        print(f"💾 БД загружена: пар={len(totals_db)}, маппингов={len(di_to_pair)}")
    except FileNotFoundError:
        print("💾 БД не найдена, начинаем с нуля")
    except Exception as e:
        print(f"⚠️ load_db: {e}")

def save_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        data = {"totals": totals_db, "di_to_pair": {str(k): v for k, v in di_to_pair.items()}}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(DB_PATH) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DB_PATH)
    except Exception as e:
        print(f"⚠️ save_db: {e}")

def record_total(pair, outcome):
    k = pair_key(pair)
    with lock:
        totals_db.setdefault(k, {})
        totals_db[k][outcome] = totals_db[k].get(outcome, 0) + 1
        save_db()
    print(f"📊 DB[{k}][{outcome}] = {totals_db[k][outcome]}")

def parse_totals(text):
    groups = re.findall(r'\(([^)]*)\)', text or "")
    if len(groups) < 2: return None
    p = len(SUITS_RE.findall(groups[0]))
    d = len(SUITS_RE.findall(groups[1]))
    if p == 0 or d == 0: return None
    return f"{p}/{d}"

def parse_player_suit(text):
    groups = re.findall(r'\(([^)]*)\)', text or "")
    if len(groups) < 1: return None
    suits = SUITS_RE.findall(groups[0])
    return suits[0] if suits else None

def build_dump():
    lines = ["📚 <b>БД ТОТАЛОВ (пара → исходы)</b>", "формат: пара | 2/2 2/3 3/2 3/3"]
    order = ["2/2", "2/3", "3/2", "3/3"]
    for k in sorted(totals_db.keys()):
        row = totals_db[k]
        if not row: continue
        main = " ".join(f"{o}:{row[o]}" for o in order if o in row)
        extra = " ".join(f"{o}:{row[o]}" for o in sorted(row) if o not in order)
        lines.append(f"<code>{k}</code> → {main}{('  '+extra) if extra else ''}  [всего {sum(row.values())}]")
    if len(lines) == 2: lines.append("(пока пусто)")
    return "\n".join(lines)

def send_dump():
    if not DB_CHANNEL_ID: return
    txt = build_dump()
    for i in range(0, len(txt), 4000):
        try: bot.send_message(DB_CHANNEL_ID, txt[i:i+4000], parse_mode="HTML")
        except Exception as e: print(f"⚠️ dump send: {e}")
    send_db_file()

def send_db_file(chat_id=None):
    cid = chat_id or DB_CHANNEL_ID
    if not cid: return
    try:
        if not os.path.exists(DB_PATH):
            if chat_id: bot.send_message(cid, "⚠️ Файл БД ещё не создан")
            return
        with open(DB_PATH, "rb") as f:
            bot.send_document(cid, f, caption="📦 Бэкап totals_db.json", visible_file_name="totals_db.json")
        print(f"📤 Файл БД отправлен в {cid}")
    except Exception as e:
        print(f"⚠️ send_db_file: {e}")

# ==================== API / TG ====================
def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200: return resp.json().get("Value", [])
    except Exception as e: print(f"️ API: {e}")
    return []

def format_game_info(game):
    try:
        return (
            f"🎮 ИГРА #N{game.get('I','N/A')}   Display ID: {game.get('DI','N/A')}\n"
            f"──────────────────────────────\n")
    except Exception as e:
        print(f"⚠️ fmt: {e}"); return None

def send_to_channel(text):
    try: bot.send_message(CHANNEL_ID, text, parse_mode="HTML"); return True
    except Exception as e: print(f"⚠️ send: {e}"); return False

def send_prediction(text):
    if not PREDICTION_CHANNEL_ID: return None
    try: return bot.send_message(PREDICTION_CHANNEL_ID, text)
    except Exception as e: print(f"️ pred: {e}"); return None

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
    except Exception as e: print(f"⚠️ edit: {e}")
    if pred in active_preds: active_preds.remove(pred)

def finalize_mast(display_id, result_text):
    if display_id not in mast_preds: return
    pred = mast_preds[display_id]
    try:
        text = f"🎯 Игра #N{display_id}\nИгрок {pred['suit_symbol']} {result_text}"
        bot.edit_message_text(chat_id=MASTI_CHANNEL_ID, message_id=pred["msg_id"], text=text)
    except Exception as e: print(f"⚠️ edit mast: {e}")
    del mast_preds[display_id]

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

def flush_series(series):
    if series["published"]: return
    dis = series["dis"]
    length = len(dis)
    if length < 2: return

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
        print(f"🚀 ПРОГНОЗ {label} (серия len={length})")

def create_mast_prediction(game_id, display_id):
    """🔥 Создаёт прогноз масти с лимитом активных"""
    if display_id in mast_preds: return
    
    # Проверяем лимит активных прогнозов мастей
    with lock:
        if len(mast_preds) >= MAX_MAST_ACTIVE:
            print(f"⛔ лимит мастей {MAX_MAST_ACTIVE}, пропуск #N{display_id} (активных: {len(mast_preds)})")
            return
    
    suit_code = get_suit_by_id(game_id)
    suit_info = SUITS_MAP[suit_code]
    
    text = f"🎯 Игра #N{display_id}\nИгрок {suit_info['symbol']}"
    sent = send_masti_prediction(text)
    
    if sent:
        mast_preds[display_id] = {
            "msg_id": sent.message_id,
            "suit_code": suit_code,
            "suit_symbol": suit_info['symbol'],
            "first_n": display_id,
            "second_n": normalize(display_id + 1),
            "checked": set(),
            "created_at": time.time()
        }
        print(f"🎯 МАСТЬ #N{display_id} → {suit_info['symbol']} (ID {game_id} mod3={suit_code}) | активных: {len(mast_preds)}")

# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
def process_stats_message(msg):
    if msg.chat.id != STATS_SOURCE_CHANNEL_ID: return
    parsed = parse_stats(msg.text)
    if not parsed: return
    num, has_nat = parsed

    if num in processed_stats_nums: return

    if not is_final_result(msg.text):
        if ARROW_CHAR in msg.text:
            print(f" промежуточный #N{num} (стрелка), пропускаем")
        else:
            print(f"⏳ промежуточный #N{num}, текст: {msg.text[:100]!r}")
        return

    pair = di_to_pair.get(num)
    outcome = parse_totals(msg.text)
    print(f"🔎 ФИНАЛ #N{num} | пара={pair} | тотал={outcome} | #R={has_nat}")

    if pair is not None and outcome:
        record_total(pair, outcome)
        processed_stats_nums.add(num)
    elif pair is None:
        print(f"️ DB: пара для #N{num} неизвестна")
    elif outcome is None:
        print(f"⚠️ DB: не удалось распарсить тотал для #N{num}")

    # 1. Проверка прогнозов натуралов
    with lock:
        for pred in list(active_preds):
            if num in {pred["first_n"], pred["second_n"]} and num not in pred["checked"]:
                pred["checked"].add(num)
                if has_nat:
                    pos = "0️" if num == pred["first_n"] else "1️⃣"
                    finalize(pred, True, f"{pos} #N{num} → Натурал #R")
                    print(f" НАТУРАЛ #N{num} ({pos})")
                elif len(pred["checked"]) >= 2:
                    finalize(pred, False, "❌ не зашло")
                    print(f"❌ нет #R в {pred['label']}")

    # 2. Проверка прогнозов мастей
    if num in mast_preds:
        pred = mast_preds[num]
        actual_suit = parse_player_suit(msg.text)
        
        if actual_suit:
            if actual_suit == pred["suit_symbol"]:
                pos = "0️⃣" if num == pred["first_n"] else "1️⃣"
                finalize_mast(num, f"✅{pos}")
                print(f"🎉 МАСТЬ УГАДАНА #N{num} ({pos})")
            else:
                pred["checked"].add(num)
                if len(pred["checked"]) >= 2:
                    finalize_mast(num, "")
                    print(f"❌ МАСТЬ НЕ УГАДАНА #N{num}")
        else:
            print(f"⚠️ Не удалось распарсить масть для #N{num}")

@bot.channel_post_handler()
def on_stats(msg):
    print(f"📨 POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)

@bot.edited_channel_post_handler()
def on_stats_edited(msg):
    print(f"✏️ EDITED POST chat.id={msg.chat.id} | {(msg.text or '')[:80]!r}")
    process_stats_message(msg)

@bot.message_handler(commands=["db"])
def cmd_db(m):
    try: bot.send_message(m.chat.id, build_dump(), parse_mode="HTML")
    except Exception as e: print(f"⚠️ cmd_db: {e}")

@bot.message_handler(commands=["dbfile"])
def cmd_dbfile(m):
    send_db_file(m.chat.id)

# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global current_series, last_dump
    games = fetch_data()
    if not games: return

    with lock:
        for pred in list(active_preds):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize(pred, False, " таймаут ожидания #R")
        for display_id, pred in list(mast_preds.items()):
            if time.time() - pred["created_at"] > PRED_TIMEOUT:
                finalize_mast(display_id, "⏰")
                print(f" таймаут масти #N{display_id}")

    new_games = []
    for game in games:
        gid = game.get("I"); di = game.get("DI")
        if not gid or gid in sent_games: continue
        sent_games.add(gid)
        new_games.append(game)
        text = format_game_info(game)
        if text: send_to_channel(text)
        if di: 
            di_to_pair[int(di)] = (int(gid) // 100) % 100
            create_mast_prediction(int(gid), int(di))
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I"); di = game.get("DI")
        if not di: continue
        pair = (int(gid) // 100) % 100
        
        if current_series["pair"] is not None and pair == (current_series["pair"] + 1) % 100:
            current_series["dis"].append(di)
        else:
            current_series = {"pair": pair, "dis": [di], "published": False}
        current_series["pair"] = pair

        if len(current_series["dis"]) in SERIES_TRIGGERS and not current_series["published"]:
            flush_series(current_series)

    if new_games:
        print(f"✅ Новых: {len(new_games)} | серия пара={current_series['pair']} len={len(current_series['dis'])}")
        save_db()
    if len(sent_games) > 300: sent_games.clear()

    if DB_CHANNEL_ID and time.time() - last_dump > DB_DUMP_MIN * 60:
        send_dump(); last_dump = time.time()

def main():
    load_db()
    print(f"🚀 ЗАПУСК | STATS_ID={STATS_SOURCE_CHANNEL_ID} | triggers={sorted(SERIES_TRIGGERS)} | db={DB_PATH}")
    send_to_channel(f"🟢 <b>Бот запущен</b> | триггеры серии: {sorted(SERIES_TRIGGERS)} | макс. мастей: {MAX_MAST_ACTIVE}")
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    while True:
        try:
            api_cycle(); time.sleep(15)
        except Exception as e:
            print(f"⚠️ цикл: {e}"); time.sleep(30)

if __name__ == "__main__":
    main()
