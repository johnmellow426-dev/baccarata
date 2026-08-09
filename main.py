import os
import time
import json
import threading
import requests
import telebot

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")
TEST_PREDICTION_CHANNEL_ID = os.getenv("TEST_PREDICTION_CHANNEL_ID")  # Дополнительный тестовый канал

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "melbet-4866.pro")

# ID Спорта для Баккары
BACCARAT_SPORT_ID = 236

# Ссылки на API
API_URL = f"https://{BASE_DOMAIN}/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
STATISTIC_URL_TEMPLATE = f"https://{BASE_DOMAIN}/cyber-api/mainfeedlive/web/cyber/v3/statistic?country=192&fcountry=192&gameId={{game_id}}&gr=1521&lng=ru&ref=8"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://{BASE_DOMAIN}/",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

# ==================== СОСТОЯНИЕ И СТАТИСТИКА ====================
sent_games = set()
last_processed_gid = None

active_preds = []       # Активные прогнозы (ОСНОВНОЙ КАНАЛ)
test_active_preds = []  # Активные прогнозы (ТЕСТОВЫЙ КАНАЛ)

diff_stats = {}         # Статистика основного канала: { diff_val: {"total": 0, "win": 0, "loss": 0} }
test_diff_stats = {}    # Статистика тестового канала: { diff_val: {"total": 0, "win": 0, "loss": 0} }


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_2_digits(gid):
    """Возвращает последние 2 цифры Game ID как число"""
    try:
        return int(str(gid)[-2:])
    except (ValueError, TypeError):
        return None


def fetch_data():
    """Получение списка текущих/предстоящих игр в линии"""
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ API LiveFeed: {e}")
    return []


def fetch_game_cards_outcome(game_id):
    """Запрашивает детальную статистику завершенной игры (количество карт P1/P2)"""
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
    except Exception as e:
        print(f"⚠️ Ошибка получения карт (ID {game_id}): {e}")
    return None


def update_diff_stats(stats_dict, label_tag, diff_val, is_win):
    """Обновляет и выводит статистику побед по разнице Δ2D"""
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
        
        print(f"📊 [{label_tag} | Δ2D={diff_val}] Проходов: {wins}/{total} ({winrate}%) | Промахов: {stats_dict[diff_val]['loss']}")


# ==================== РАБОТА С TELEGRAM ====================
def format_game_info(game):
    """Форматирует сообщение анонса будущей игры"""
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
    """Отправка анонса в основной канал анонсов"""
    if not CHANNEL_ID:
        return False
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        return True
    except Exception as e:
        print(f"⚠️ send main: {e}")
        return False


def _make_pred(target_chat_id, preds_list, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
    """Формирует и отправляет пост-сигнал в указанный канал"""
    if not target_chat_id:
        return False

    next_di_num = di_num + 1
    tag = "🧪 ТЕСТОВЫЙ СИГНАЛ (УВЕЛИЧЕНИЕ)" if is_test else "🔥 СИГНАЛ (УБЫВАНИЕ)"
    
    text = (
        f"<b>{tag} | БАККАРА</b>\n"
        f"──────────────────────────────\n"
        f"🎯 <b>Ожидаются игры:</b> #N{di_num} - #N{next_di_num}\n"
        f"🎲 <b>Прогноз исходов:</b> <code>2/3</code> и <code>3/2</code>\n"
        f"──────────────────────────────\n"
        f"📉 <b>2 последние цифры ID:</b> {prev_2d} ➔ {curr_2d}\n"
        f"📐 <b>Разница (Δ2D):</b> {diff_2d}\n"
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
        print(f"⚠️ pred send (chat={target_chat_id}): {e}")
    return False


def process_preds_list(preds_list, stats_dict, label_tag, game_di, outcome):
    """Универсальная обработка и финализация списка прогнозов"""
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
                        f"🎯 <b>СИГНАЛ РАССЧИТАН {'[ТЕСТ]' if pred['is_test'] else ''} | БАККАРА {status_symbol}</b>\n"
                        f"──────────────────────────────\n"
                        f"🎯 <b>Игры:</b> #N{pred['target_dis'][0]} - #N{pred['target_dis'][1]}\n"
                        f"🎲 <b>Цель:</b> <code>2/3</code> и <code>3/2</code>\n"
                        f"📐 <b>Разница (Δ2D):</b> {pred['diff_2d']}\n"
                        f"──────────────────────────────\n"
                        f"📊 <b>Результаты карт:</b> {res_str}\n"
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


def check_and_finalize_predictions(game_di, game_gid):
    """Проверяет результат завершенной игры и финализирует основные и тестовые сигналы"""
    outcome = fetch_game_cards_outcome(game_gid)
    if not outcome:
        return

    print(f"🔎 Игра #N{game_di} (ID: {game_gid}) завершена с исходом карт: {outcome}")

    # Проверяем основной и тестовый списки
    process_preds_list(active_preds, diff_stats, "ОСНОВНОЙ", game_di, outcome)
    process_preds_list(test_active_preds, test_diff_stats, "ТЕСТОВЫЙ", game_di, outcome)


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games:
        return

    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]

    new_games = []
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or gid in sent_games:
            continue
        
        sent_games.add(gid)
        new_games.append(game)
        
        # 1. Отправляем анонс будущей игры в основной канал
        text = format_game_info(game)
        if text:
            send_to_channel(text)

        # 2. Передаем игру на финализацию открытых сигналов
        if di:
            check_and_finalize_predictions(int(di), gid)
            
    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    for game in new_games:
        gid = game.get("I")
        di = game.get("DI")
        if not di or not gid:
            continue
        
        gid_num = int(gid)
        di_num = int(di)

        if last_processed_gid is not None:
            prev_2d = get_last_2_digits(last_processed_gid)
            curr_2d = get_last_2_digits(gid_num)
            
            if prev_2d is not None and curr_2d is not None:
                # 1. Основное условие: Предыдущие 2 цифры БОЛЬШЕ текущих (УБЫВАНИЕ)
                if prev_2d > curr_2d:
                    diff_2d = prev_2d - curr_2d
                    if _make_pred(PREDICTION_CHANNEL_ID, active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=False):
                        print(f"🔥 ОСНОВНОЙ Сигнал отправлен на #N{di_num}-#N{di_num+1} | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

                # 2. Тестовое условие: Предыдущие 2 цифры МЕНЬШЕ текущих (УВЕЛИЧЕНИЕ)
                elif prev_2d < curr_2d:
                    diff_2d = curr_2d - prev_2d
                    if _make_pred(TEST_PREDICTION_CHANNEL_ID, test_active_preds, di_num, prev_2d, curr_2d, diff_2d, is_test=True):
                        print(f"🧪 ТЕСТОВЫЙ Сигнал отправлен на #N{di_num}-#N{di_num+1} | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

        last_processed_gid = gid_num

    if len(sent_games) > 300: 
        sent_games.clear()


def main():
    print("🚀 ЗАПУСК БОТА (ОСНОВНОЙ КАНАЛ + ТЕСТОВЫЙ КАНАЛ ДЛЯ PREV < CURR)")
    send_to_channel("🟢 <b>Бот Баккары запущен</b> | Мониторинг основных и тестовых сигналов 2/3 и 3/2")
    
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
