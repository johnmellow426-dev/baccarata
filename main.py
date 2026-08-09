import os
import time
import json
import re
import threading
import requests
import telebot
import sqlite3

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")
TEST_PREDICTION_CHANNEL_ID = os.getenv("TEST_PREDICTION_CHANNEL_ID")
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID")  # ID канала со статистикой (#N611. ✅6(...) 1(...))

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "melbet-4866.pro")
BACCARAT_SPORT_ID = 236

API_URL = f"https://{BASE_DOMAIN}/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
STATISTIC_URL_TEMPLATE = f"https://{BASE_DOMAIN}/cyber-api/mainfeedlive/web/cyber/v3/statistic?country=192&fcountry=192&gameId={{game_id}}&gr=1521&lng=ru&ref=8"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://{BASE_DOMAIN}/",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== НАСТРОЙКИ ФИЛЬТРА БД ====================
DB_NAME = "baccarat_history.db"
ENABLE_FILTER = True       # Включить проверку по базе данных
MIN_SAMPLES = 5            # Минимум исторических случаев для включения фильтра
MIN_WINRATE = 0.55         # Минимальный винрейт (55%) для пропуска сигнала

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
last_processed_gid = None

active_preds = []       
test_active_preds = []  

diff_stats = {}         
test_diff_stats = {}    

# Трекер игр: { gid: {"di": int, "last_2d": int, "outcome_fetched": bool} }
tracked_games = {}


# ==================== БАЗА ДАННЫХ (SQLite) ====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS games (
                 gid TEXT PRIMARY KEY,
                 di INTEGER,
                 last_2d INTEGER,
                 outcome TEXT,
                 timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
    print(f"💾 База данных '{DB_NAME}' инициализирована.")

def save_game_to_db(gid, di, last_2d, outcome):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO games (gid, di, last_2d, outcome)
                 VALUES (?, ?, ?, ?)''', (str(gid), di, last_2d, outcome))
    conn.commit()
    conn.close()

def get_transition_stats(prev_2d, curr_2d):
    """Анализирует БД: как часто после перехода prev_2d -> curr_2d следующие игры давали 2/3 или 3/2"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        SELECT g1.di 
        FROM games g1
        JOIN games g2 ON g2.di = g1.di + 1
        WHERE g1.last_2d = ? AND g2.last_2d = ?
    ''', (prev_2d, curr_2d))
    
    transitions = c.fetchall()
    hits = 0
    total = 0
    
    for (g1_di,) in transitions:
        c.execute('''SELECT outcome FROM games WHERE di IN (?, ?)''', (g1_di + 1, g1_di + 2))
        outcomes = [row[0] for row in c.fetchall()]
        
        if len(outcomes) == 2 and None not in outcomes:
            total += 1
            if any(o in ["2/3", "3/2"] for o in outcomes):
                hits += 1
    conn.close()
    return hits, total


# ==================== ПАРСИНГ КАНАЛА СТАТИСТИКИ ====================
def parse_stats_message(text):
    """
    Парсит сообщение из канала статистики.
    Пример: '#N611. ✅6(Q♣️6♥️) 1(2♠️Q♠️9♣️) #T7'
    Возвращает: (di, outcome) -> (611, '6/1')
    """
    if not text:
        return None, None
    
    # Регулярное выражение ищет: #N{число}. ... {число}(скобки) {число}(скобки)
    match = re.search(r"#N(\d+)\..*?(\d+)\([^)]*\)\s+(\d+)\([^)]*\)", text, re.DOTALL)
    if match:
        di = int(match.group(1))
        p1 = int(match.group(2))
        p2 = int(match.group(3))
        return di, f"{p1}/{p2}"
    return None, None

@bot.channel_post_handler(content_types=['text'])
@bot.edited_channel_post_handler(content_types=['text'])
def handle_stats_channel(message):
    """Обработчик сообщений из канала со статистикой"""
    if not STATS_CHANNEL_ID:
        return
    
    if str(message.chat.id) != str(STATS_CHANNEL_ID):
        return
        
    di, outcome = parse_stats_message(message.text)
    
    if di and outcome:
        # Ищем игру в нашем трекере по Display ID
        gid_found = None
        info_found = None
        for g_id, info in tracked_games.items():
            if info["di"] == di:
                gid_found = g_id
                info_found = info
                break
                
        if gid_found:
            if not info_found["outcome_fetched"]:
                info_found["outcome_fetched"] = True
                save_game_to_db(gid_found, di, info_found["last_2d"], outcome)
                print(f"📥 [КАНАЛ СТАТИСТИКИ] Сохранен исход игры #N{di} (ID: {gid_found}): {outcome}")
                
                # Финализируем прогнозы
                check_and_finalize_predictions(di, gid_found)
        else:
            print(f"ℹ️ [КАНАЛ СТАТИСТИКИ] Игра #N{di} не найдена в трекере (возможно, анонс был до запуска бота).")


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_2_digits(gid):
    try:
        return int(str(gid)[-2:])
    except (ValueError, TypeError):
        return None

def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ API LiveFeed: {e}")
    return []

def fetch_game_cards_outcome(game_id):
    """Fallback: получение карт через API, если канал статистики не ответил"""
    url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=5)
        if resp.status_code == 200 and resp.text.strip():
            data = resp.json()
            stat = data.get("statistic", {}).get("main", {})
            p1_raw = stat.get("P1", "[]")
            p2_raw = stat.get("P2", "[]")
            p1_cards = json.loads(p1_raw) if isinstance(p1_raw, str) else p1_raw
            p2_cards = json.loads(p2_raw) if isinstance(p2_raw, str) else p2_raw
            if p1_cards and p2_cards:
                return f"{len(p1_cards)}/{len(p2_cards)}"
    except Exception:
        pass
    return None

def update_diff_stats(stats_dict, label_tag, diff_val, is_win):
    with lock:
        if diff_val not in stats_dict:
            stats_dict[diff_val] = {"total": 0, "win": 0, "loss": 0}
        stats_dict[diff_val]["total"] += 1
        if is_win: stats_dict[diff_val]["win"] += 1
        else: stats_dict[diff_val]["loss"] += 1


# ==================== TELEGRAM ====================
def format_game_info(game):
    try:
        g_i = game.get('I', 'N/A')
        g_di = game.get('DI', 'N/A')
        sport_name = game.get('SN', 'Баккара')
        return (
            f"🎴 <b>{sport_name}</b> | ИГРА #N{g_i}\n"
            f"Display ID: {g_di}\n"
            f"──────────────────────────────\n"
        )
    except: return None

def send_to_channel(text):
    if not CHANNEL_ID: return False
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except: return False

def _make_pred(target_chat_id, preds_list, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
    if not target_chat_id: return False

    # --- ПРОВЕРКА ФИЛЬТРА ПО БД ---
    if ENABLE_FILTER:
        hits, total = get_transition_stats(prev_2d, curr_2d)
        direction = "УБЫВАНИЕ" if prev_2d > curr_2d else "УВЕЛИЧЕНИЕ"
        
        if total > 0:
            winrate = hits / total
            print(f"🔍 БД: Переход {prev_2d} ➔ {curr_2d} ({direction}) | Исторически: {hits}/{total} ({winrate*100:.1f}%)")
            if total >= MIN_SAMPLES and winrate < MIN_WINRATE:
                print(f"⛔ ФИЛЬТР: Сигнал заблокирован (винрейт {winrate*100:.1f}% < {MIN_WINRATE*100}%)")
                return False

    next_di_num = di_num + 1
    tag = "🧪 ТЕСТОВЫЙ (УВЕЛИЧЕНИЕ)" if is_test else "🔥 ОСНОВНОЙ (УБЫВАНИЕ)"
    
    text = (
        f"<b>{tag} | БАККАРА</b>\n"
        f"──────────────────────────────\n"
        f"🎯 <b>Ожидаются:</b> #N{di_num} - #N{next_di_num}\n"
        f"🎲 <b>Цель:</b> <code>2/3</code> и <code>3/2</code>\n"
        f"📉 <b>2D:</b> {prev_2d} ➔ {curr_2d} | <b>Δ2D:</b> {diff_2d}\n"
        f"──────────────────────────────\n"
        f"⏳ <i>Ожидание результатов...</i>"
    )
    
    try:
        sent = bot.send_message(target_chat_id, text, parse_mode="HTML")
        if sent:
            with lock:
                preds_list.append({
                    "chat_id": target_chat_id, "msg_id": sent.message_id,
                    "target_dis": [di_num, next_di_num], "diff_2d": diff_2d,
                    "results": {}, "is_test": is_test
                })
            return True
    except Exception as e:
        print(f"⚠️ pred send: {e}")
    return False

def process_preds_list(preds_list, stats_dict, label_tag, game_di, outcome):
    with lock:
        for pred in list(preds_list):
            if game_di in pred["target_dis"]:
                pred["results"][game_di] = outcome
                has_hit = any(res in ["2/3", "3/2"] for res in pred["results"].values())
                all_finished = len(pred["results"]) >= len(pred["target_dis"])

                if has_hit or all_finished:
                    status_symbol = "✅" if has_hit else "❌"
                    res_str = ", ".join([f"#N{k}: {v}" for k, v in pred["results"].items()])
                    updated_text = (
                        f"🎯 <b>РАССЧИТАН {'[ТЕСТ]' if pred['is_test'] else ''} {status_symbol}</b>\n"
                        f"Игры: #N{pred['target_dis'][0]} - #N{pred['target_dis'][1]}\n"
                        f"Результаты: {res_str}\n"
                        f"Итог: <b>{'ВЫИГРЫШ' if has_hit else 'ПРОИГРЫШ'}</b>"
                    )
                    try:
                        bot.edit_message_text(chat_id=pred["chat_id"], message_id=pred["msg_id"], text=updated_text, parse_mode="HTML")
                    except: pass
                    update_diff_stats(stats_dict, label_tag, pred["diff_2d"], is_win=has_hit)
                    preds_list.remove(pred)

def check_and_finalize_predictions(game_di, game_gid):
    outcome = None
    # Сначала проверяем, не сохранился ли исход уже из канала статистики
    for g_id, info in tracked_games.items():
        if g_id == game_gid and info["outcome_fetched"]:
            # Исход уже сохранен в БД через handle_stats_channel
            pass

    # Если канал статистики еще не ответил, пробуем API (Fallback)
    if not tracked_games.get(game_gid, {}).get("outcome_fetched"):
        outcome = fetch_game_cards_outcome(game_gid)
        if outcome:
            tracked_games[game_gid]["outcome_fetched"] = True
            save_game_to_db(game_gid, game_di, tracked_games[game_gid]["last_2d"], outcome)
            print(f"🔎 [API FALLBACK] Игра #N{game_di} завершена: {outcome}")

    process_preds_list(active_preds, diff_stats, "ОСНОВНОЙ", game_di, outcome)
    process_preds_list(test_active_preds, test_diff_stats, "ТЕСТОВЫЙ", game_di, outcome)


# ==================== КОМАНДЫ БОТА ====================
@bot.message_handler(commands=['stats'])
def cmd_stats(message):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM games")
    total_games = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM games WHERE outcome IN ('2/3', '3/2')")
    target_hits = c.fetchone()[0]
    conn.close()
    
    text = f"📊 <b>Статистика БД</b>\n"
    text += f"Всего сохранено игр: <b>{total_games}</b>\n"
    text += f"Попаданий в цель (2/3 и 3/2): <b>{target_hits}</b>\n"
    if total_games > 0:
        text += f"Общий винрейт: <b>{(target_hits/total_games)*100:.1f}%</b>"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(commands=['clear_db'])
def cmd_clear(message):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("DELETE FROM games")
    conn.commit()
    conn.close()
    bot.reply_to(message, "🗑 База данных полностью очищена.")


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games: return

    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]
    new_games = []
    
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or not di: continue
            
        if gid not in tracked_games:
            tracked_games[gid] = {
                "di": int(di),
                "last_2d": get_last_2_digits(gid),
                "outcome_fetched": False
            }

        if gid in sent_games: continue
            
        sent_games.add(gid)
        new_games.append(game)
        text = format_game_info(game)
        if text: send_to_channel(text)
            
    # Очистка памяти трекера
    if len(tracked_games) > 200:
        sorted_gids = sorted(tracked_games.keys(), key=lambda g: tracked_games[g]["di"])
        for old_gid in sorted_gids[:-200]:
            if tracked_games[old_gid]["outcome_fetched"]:
                del tracked_games[old_gid]

    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid: continue
        
        gid_num = int(gid)
        di_num = int(di)

        if last_processed_gid is not None:
            prev_2d = get_last_2_digits(last_processed_gid)
            curr_2d = get_last_2_digits(gid_num)
            
            if prev_2d is not None and curr_2d is not None:
                if prev_2d > curr_2d:
                    diff_2d = prev_2d - curr_2d
                    if _make_pred(PREDICTION_CHANNEL_ID, active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
                        print(f"🔥 ОСНОВНОЙ | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

                elif prev_2d < curr_2d:
                    diff_2d = curr_2d - prev_2d
                    if _make_pred(TEST_PREDICTION_CHANNEL_ID, test_active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=True):
                        print(f"🧪 ТЕСТОВЫЙ | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

        last_processed_gid = gid_num

    if len(sent_games) > 300: sent_games.clear()


def main():
    print("🚀 ЗАПУСК БОТА (ПАРСИНГ КАНАЛА СТАТИСТИКИ + БД)")
    init_db()
    
    if not STATS_CHANNEL_ID:
        print("⚠️ STATS_CHANNEL_ID не указан. Сбор статистики из канала отключен.")
    else:
        print(f"✅ Канал статистики подключен: {STATS_CHANNEL_ID}")
        
    send_to_channel("🟢 <b>Бот запущен</b> | Режим сбора БД из канала статистики активирован.\nКоманды: /stats, /clear_db")
    
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    
    while True:
        try:
            api_cycle()
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ ошибка цикла: {e}")
            time.sleep(20)

if __name__ == "__main__":
    main()
