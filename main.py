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
STATS_CHANNEL_ID = os.getenv("STATS_CHANNEL_ID")  # Канал со статистикой

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

# ==================== НАСТРОЙКИ ФИЛЬТРА (СРАЗУ В ПРОГНОЗ) ====================
DB_NAME = "baccarat_history.db"
ENABLE_FILTER = True       # Фильтр ВКЛЮЧЕН
MIN_SAMPLES = 1            # Минимум 1 исторический случай для применения фильтра
MIN_WINRATE = 0.50         # 50% винрейт — если данные есть и винрейт >= 50%, сигнал проходит
API_FALLBACK_DELAY = 120   # Секунд до попытки получить исход через API

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
last_processed_gid = None
active_preds = []
test_active_preds = []
diff_stats = {}
test_diff_stats = {}
tracked_games = {}


# ==================== БАЗА ДАННЫХ ====================
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
def count_cards_in_string(cards_str):
    """Считает количество карт по символам мастей"""
    suit_chars = ['♠', '♥', '♦', '♣']
    count = 0
    for char in cards_str:
        if char in suit_chars:
            count += 1
    return count


def parse_stats_message(text):
    """
    Парсит сообщение из канала статистики.
    Пример: '#N611. ✅6(Q♣️6♥️) 1(2♠️Q♠️9♣️) #T7'
    Возвращает: (di, outcome) -> (611, '2/3')
    """
    if not text:
        return None, None
    
    di_match = re.search(r"#N(\d+)\.", text)
    if not di_match:
        return None, None
    di = int(di_match.group(1))
    
    parentheses = re.findall(r"\(([^)]+)\)", text)
    
    card_parentheses = []
    for p in parentheses:
        if count_cards_in_string(p) > 0:
            card_parentheses.append(p)
    
    if len(card_parentheses) >= 2:
        p1_count = count_cards_in_string(card_parentheses[0])
        p2_count = count_cards_in_string(card_parentheses[1])
        
        print(f"   🔍 DEBUG: DI={di}")
        print(f"   🔍 DEBUG: Игрок: '{card_parentheses[0]}' → {p1_count} карт")
        print(f"   🔍 DEBUG: Банкир: '{card_parentheses[1]}' → {p2_count} карт")
        
        outcome = f"{p1_count}/{p2_count}"
        print(f"   ✅ Результат: #N{di} → {outcome}")
        return di, outcome
    
    return None, None


def is_stats_channel(message):
    """Проверяет, что сообщение из канала статистики"""
    if not STATS_CHANNEL_ID:
        return False
    
    if STATS_CHANNEL_ID.startswith('@'):
        return message.chat.username and message.chat.username.lower() == STATS_CHANNEL_ID.lstrip('@').lower()
    else:
        return str(message.chat.id) == str(STATS_CHANNEL_ID)


def process_channel_stats(message):
    """Общая логика обработки сообщений канала статистики"""
    if not is_stats_channel(message):
        return
    
    text = message.text or message.caption or ""
    if not text:
        return
    
    post_type = "Новое" if not getattr(message, 'edit_date', None) else "Редактирование"
    print(f"📩 [КАНАЛ СТАТИСТИКИ] {post_type} (ID: {message.message_id})")
    
    di, outcome = parse_stats_message(text)
    
    if di and outcome:
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
                print(f"💾 [КАНАЛ] Сохранен исход игры #N{di} (ID: {gid_found}): {outcome}")
                finalize_predictions_for_game(di, outcome)
            else:
                print(f"ℹ️ [КАНАЛ] Игра #N{di} уже сохранена в БД")
        else:
            print(f"⚠️ [КАНАЛ] Игра #N{di} не найдена в трекере")
    else:
        print(f"⏭️ [КАНАЛ] Сообщение не содержит данных о картах")


@bot.channel_post_handler(content_types=['text'])
def handle_new_channel_post(message):
    process_channel_stats(message)


@bot.edited_channel_post_handler(content_types=['text'])
def handle_edited_channel_post(message):
    process_channel_stats(message)


# ==================== ФИНАЛИЗАЦИЯ ПРОГНОЗОВ ====================
def finalize_predictions_for_game(game_di, outcome):
    """Финализирует прогнозы для конкретной игры с известным исходом"""
    process_preds_list(active_preds, diff_stats, "ОСНОВНОЙ", game_di, outcome)
    process_preds_list(test_active_preds, test_diff_stats, "ТЕСТОВЫЙ", game_di, outcome)


def update_diff_stats(stats_dict, label_tag, diff_val, is_win):
    with lock:
        if diff_val not in stats_dict:
            stats_dict[diff_val] = {"total": 0, "win": 0, "loss": 0}
        stats_dict[diff_val]["total"] += 1
        if is_win:
            stats_dict[diff_val]["win"] += 1
        else:
            stats_dict[diff_val]["loss"] += 1
        
        total = stats_dict[diff_val]["total"]
        wins = stats_dict[diff_val]["win"]
        winrate = round((wins / total) * 100, 1) if total > 0 else 0
        print(f"📊 [{label_tag} | Δ2D={diff_val}] Проходов: {wins}/{total} ({winrate}%)")


def process_preds_list(preds_list, stats_dict, label_tag, game_di, outcome):
    with lock:
        preds_to_remove = []
        for pred in list(preds_list):
            if game_di in pred["target_dis"]:
                pred["results"][game_di] = outcome
                
                has_hit = any(res in ["2/3", "3/2"] for res in pred["results"].values())
                all_finished = len(pred["results"]) >= len(pred["target_dis"])
                
                if has_hit or all_finished:
                    status_symbol = "✅" if has_hit else "❌"
                    res_str = ", ".join([f"#N{k}: {v}" for k, v in pred["results"].items()])
                    
                    updated_text = (
                        f"🎯 <b>РАССЧИТАН {'[ТЕСТ]' if pred['is_test'] else ''} | {status_symbol}</b>\n"
                        f"──────────────────────────────\n"
                        f"🎯 <b>Игры:</b> #N{pred['target_dis'][0]} - #N{pred['target_dis'][1]}\n"
                        f"🎲 <b>Цель:</b> <code>2/3</code> и <code>3/2</code>\n"
                        f"📐 <b>Δ2D:</b> {pred['diff_2d']}\n"
                        f"──────────────────────────────\n"
                        f"📊 <b>Результаты:</b> {res_str}\n"
                        f"Итог: <b>{'ВЫИГРЫШ' if has_hit else 'ПРОИГРЫШ'}</b>"
                    )
                    
                    try:
                        bot.edit_message_text(
                            chat_id=pred["chat_id"],
                            message_id=pred["msg_id"],
                            text=updated_text,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        print(f"⚠️ Ошибка редактирования прогноза: {e}")
                    
                    update_diff_stats(stats_dict, label_tag, pred["diff_2d"], is_win=has_hit)
                    preds_to_remove.append(pred)
        
        for p in preds_to_remove:
            if p in preds_list:
                preds_list.remove(p)


# ==================== API ФУНКЦИИ ====================
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
    """Fallback: получение карт через API"""
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
    except Exception as e:
        print(f"⚠️ fmt: {e}")
        return None


def send_to_channel(text):
    if not CHANNEL_ID:
        return False
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"⚠️ send main: {e}")
        return False


def _make_pred(target_chat_id, preds_list, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
    if not target_chat_id:
        print(f"   ⚠️ Канал для сигналов не указан (chat_id=None)")
        return False
    
    # --- ПРОВЕРКА ФИЛЬТРА ПО БД ---
    if ENABLE_FILTER:
        hits, total = get_transition_stats(prev_2d, curr_2d)
        direction = "УБЫВАНИЕ" if prev_2d > curr_2d else "УВЕЛИЧЕНИЕ"
        
        if total > 0:
            winrate = hits / total
            print(f"   🔍 БД: Переход {prev_2d} ➔ {curr_2d} ({direction}) | Исторически: {hits}/{total} ({winrate*100:.1f}%)")
            
            if total >= MIN_SAMPLES and winrate < MIN_WINRATE:
                print(f"   ⛔ ФИЛЬТР: Сигнал заблокирован (винрейт {winrate*100:.1f}% < {MIN_WINRATE*100}%)")
                return False
        else:
            print(f"   🔍 БД: Переход {prev_2d} ➔ {curr_2d} | Данных нет, отправляем для разведки.")
    
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
                    "chat_id": target_chat_id,
                    "msg_id": sent.message_id,
                    "target_dis": [di_num, next_di_num],
                    "diff_2d": diff_2d,
                    "results": {},
                    "is_test": is_test,
                    "created_at": time.time()
                })
            return True
    except Exception as e:
        print(f"⚠️ pred send: {e}")
    return False


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games:
        print("⚠️ API вернул пустой ответ")
        return
    
    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]
    
    if not games:
        print(f"ℹ️ Баккара не найдена в линии (всего игр: {len(raw_games)})")
        return
    
    print(f"📡 [API] Получено {len(games)} игр Баккары")
    
    new_games = []
    
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or not di:
            continue
        
        if gid not in tracked_games:
            tracked_games[gid] = {
                "di": int(di),
                "last_2d": get_last_2_digits(gid),
                "outcome_fetched": False,
                "added_at": time.time()
            }
        
        if gid in sent_games:
            continue
        
        sent_games.add(gid)
        new_games.append(game)
        
        text = format_game_info(game)
        if text:
            send_to_channel(text)
            print(f"📢 [АНОНС] Игра #N{di} (ID: {gid}) опубликована в канале")
    
    # Fallback: проверяем игры, которые давно в трекере но не получили исход
    for gid, info in list(tracked_games.items()):
        if not info["outcome_fetched"]:
            elapsed = time.time() - info.get("added_at", time.time())
            if elapsed > API_FALLBACK_DELAY:
                outcome = fetch_game_cards_outcome(gid)
                if outcome:
                    info["outcome_fetched"] = True
                    save_game_to_db(gid, info["di"], info["last_2d"], outcome)
                    print(f"🔄 [API FALLBACK] Игра #N{info['di']} (ID: {gid}): {outcome}")
                    finalize_predictions_for_game(info["di"], outcome)
    
    # Очистка памяти трекера
    if len(tracked_games) > 300:
        sorted_gids = sorted(tracked_games.keys(), key=lambda g: tracked_games[g]["di"])
        for old_gid in sorted_gids[:-200]:
            if tracked_games[old_gid]["outcome_fetched"]:
                del tracked_games[old_gid]
    
    new_games.sort(key=lambda g: int(g.get("DI") or 0))
    
    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid:
            continue
        
        gid_num = int(gid)
        di_num = int(di)
        curr_2d = get_last_2_digits(gid_num)
        
        print(f"🎮 [ОБРАБОТКА] Игра #N{di} (ID: {gid}) | last_2d: {curr_2d}")
        
        if last_processed_gid is None:
            print(f"   ⏸️ Это первая игра после запуска, ждем следующую для сравнения")
        else:
            prev_2d = get_last_2_digits(last_processed_gid)
            
            if prev_2d is not None and curr_2d is not None:
                print(f"   🔍 Сравнение: prev_2d={prev_2d} ➔ curr_2d={curr_2d}")
                
                if prev_2d > curr_2d:
                    diff_2d = prev_2d - curr_2d
                    print(f"   📉 УБЫВАНИЕ (Δ2D={diff_2d}) → Отправляем ОСНОВНОЙ сигнал")
                    
                    if _make_pred(PREDICTION_CHANNEL_ID, active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
                        print(f"   ✅ 🔥 ОСНОВНОЙ СИГНАЛ отправлен на #N{di_num}-#N{di_num+1}")
                    else:
                        print(f"   ❌ ОСНОВНОЙ сигнал НЕ отправлен")
                
                elif prev_2d < curr_2d:
                    diff_2d = curr_2d - prev_2d
                    print(f"   📈 УВЕЛИЧЕНИЕ (Δ2D={diff_2d}) → Отправляем ТЕСТОВЫЙ сигнал")
                    
                    if _make_pred(TEST_PREDICTION_CHANNEL_ID, test_active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=True):
                        print(f"   ✅ 🧪 ТЕСТОВЫЙ СИГНАЛ отправлен на #N{di_num}-#N{di_num+1}")
                    else:
                        print(f"   ❌ ТЕСТОВЫЙ сигнал НЕ отправлен")
                
                else:
                    print(f"   ⏭️ 2D равны ({prev_2d} == {curr_2d}) → Сигнал не нужен")
        
        last_processed_gid = gid_num
    
    if len(sent_games) > 300:
        sent_games.clear()


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
        text += f"Общий винрейт цели: <b>{(target_hits/total_games)*100:.1f}%</b>"
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['test_parse'])
def cmd_test_parse(message):
    """Тестирует парсинг на примерах из канала статистики"""
    test_messages = [
        "#N611. ✅6(Q♣️6♥️) 1(2♠️Q♠️9♣️) #T7",
        "#N607. ✅9(A♦️K♣️8♥️) 6(2♥️4♣️) #T15 🟩",
        "#N610. ❌4(7♥️8♦️) 4(J♣️9♠️) #T8",
        "#N645. ✅8(2♠️3♦️4♥️5♣️) 5(K♠️Q♠️2♥️3♣️7♦️) #T13",
    ]
    
    result = "🧪 <b>Тест парсинга:</b>\n\n"
    for msg in test_messages:
        di, outcome = parse_stats_message(msg)
        if di and outcome:
            result += f"✅ <code>#N{di}</code> → <b>{outcome}</b>\n"
        else:
            result += f"❌ Не распознано: <code>{msg[:40]}...</code>\n"
    
    bot.reply_to(message, result, parse_mode="HTML")


@bot.message_handler(commands=['debug'])
def cmd_debug(message):
    """Показать текущее состояние трекера игр"""
    text = "🔍 <b>Состояние трекера:</b>\n\n"
    text += f"Игр в памяти: <b>{len(tracked_games)}</b>\n"
    text += f"Активных прогнозов (осн): <b>{len(active_preds)}</b>\n"
    text += f"Активных прогнозов (тест): <b>{len(test_active_preds)}</b>\n\n"
    
    sorted_games = sorted(tracked_games.items(), key=lambda x: x[1]["di"], reverse=True)[:10]
    text += "<b>Последние 10 игр:</b>\n"
    for gid, info in sorted_games:
        status = "✅" if info["outcome_fetched"] else "⏳"
        text += f"{status} DI: {info['di']} | 2D: {info['last_2d']}\n"
    
    bot.reply_to(message, text, parse_mode="HTML")


@bot.message_handler(commands=['clear_db'])
def cmd_clear(message):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("DELETE FROM games")
    conn.commit()
    conn.close()
    bot.reply_to(message, "🗑 База данных полностью очищена.")


# ==================== ЗАПУСК ====================
def polling_wrapper():
    """Обертка для polling с обработкой критических ошибок"""
    try:
        bot.infinity_polling(timeout=60, long_polling_timeout=120)
    except Exception as e:
        print(f"❌ Критическая ошибка polling: {e}")
        os._exit(1)


def main():
    print("🚀 ЗАПУСК БОТА (ПАРСИНГ КАНАЛА СТАТИСТИКИ + БД + ФИЛЬТР)")
    init_db()
    
    # Проверяем все каналы
    channels = {
        "CHANNEL_ID": CHANNEL_ID,
        "STATS_CHANNEL_ID": STATS_CHANNEL_ID,
        "PREDICTION_CHANNEL_ID": PREDICTION_CHANNEL_ID,
        "TEST_PREDICTION_CHANNEL_ID": TEST_PREDICTION_CHANNEL_ID,
    }
    
    for name, chat_id in channels.items():
        if not chat_id:
            print(f"❌ {name} НЕ УКАЗАН!")
        else:
            print(f"✅ {name} = {chat_id}")
            try:
                chat_info = bot.get_chat(chat_id)
                print(f"   📎 Доступен: {chat_info.title}")
            except Exception as e:
                print(f"   ⚠️ Ошибка доступа: {e}")
    
    if STATS_CHANNEL_ID:
        print(f"\n📊 Канал статистики подключен. Мониторинг новых и отредактированных сообщений.")
    else:
        print(f"\n⚠️ STATS_CHANNEL_ID не указан! Парсинг канала статистики ОТКЛЮЧЕН.")
    
    send_to_channel("🟢 <b>Бот запущен</b> | Фильтр: MIN_SAMPLES=1, MIN_WINRATE=50%\nКоманды: /stats /test_parse /debug /clear_db")
    
    threading.Thread(target=polling_wrapper, daemon=True).start()
    
    while True:
        try:
            api_cycle()
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ ошибка цикла: {e}")
            time.sleep(20)


if __name__ == "__main__":
    main()
