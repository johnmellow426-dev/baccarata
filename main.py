import os
import time
import json
import threading
import requests
import telebot

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")                     # Канал анонсов будущих игр
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID") # Канал прогнозов

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "melbet-4866.pro")

# РЕЖИМ РАБОТЫ: "DECREASE" (Убывание), "INCREASE" (Возрастание), "HYBRID" (Гибрид)
MODE = os.getenv("MODE", "INCREASE").upper()

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

active_preds = []   # Активные прогнозы
diff_stats = {}     # Статистика: { diff_val: {"total": 0, "win": 0, "loss": 0} }
di_to_gid = {}      # Реестр карт: Display ID (DI) -> Game ID (GID)


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_2_digits(gid):
    """Возвращает последние 2 цифры Game ID как число"""
    try:
        return int(str(gid)[-2:])
    except (ValueError, TypeError):
        return None


def calculate_diff_2d(prev_2d, curr_2d, mode):
    """Рассчитывает разницу Δ2D в зависимости от выбранного режима"""
    if mode == "DECREASE":
        # Только убывание: prev > curr
        return (prev_2d - curr_2d) if prev_2d > curr_2d else None
    elif mode == "INCREASE":
        # Только возрастание: curr > prev
        return (curr_2d - prev_2d) if curr_2d > prev_2d else None
    elif mode == "HYBRID":
        # Гибрид: любая абсолютная разница
        return abs(prev_2d - curr_2d)
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
    """Запрашивает детальную статистику с обработкой 204 No Content"""
    url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=7)
        
        # 204 No Content — игра еще идет или статистика формируется (просто ждем)
        if resp.status_code == 204:
            return None

        if resp.status_code != 200:
            print(f"⚠️ API вернул код {resp.status_code} для Game ID {game_id}")
            return None

        text = resp.text.strip()
        if not text:
            return None

        if text.startswith("<"):
            print(f"🚫 API заблокировал запрос (HTML вместо JSON) для Game ID {game_id}")
            return None

        data = resp.json()
        stat = data.get("statistic", {})
        main_stat = stat.get("main", {}) if isinstance(stat, dict) else {}
        
        p1_raw = main_stat.get("P1") or stat.get("P1") or data.get("P1")
        p2_raw = main_stat.get("P2") or stat.get("P2") or data.get("P2")
        
        if p1_raw is None or p2_raw is None:
            return None

        p1_count = len(json.loads(p1_raw)) if isinstance(p1_raw, str) and p1_raw.startswith("[") else (len(p1_raw) if isinstance(p1_raw, list) else p1_raw)
        p2_count = len(json.loads(p2_raw)) if isinstance(p2_raw, str) and p2_raw.startswith("[") else (len(p2_raw) if isinstance(p2_raw, list) else p2_raw)

        return f"{p1_count}/{p2_count}"

    except Exception as e:
        print(f"⚠️ Ошибка запроса карт (Game ID {game_id}): {e}")
    return None

def update_diff_stats(diff_val, is_win):
    """Обновляет и выводит статистику побед по разнице Δ2D"""
    with lock:
        if diff_val not in diff_stats:
            diff_stats[diff_val] = {"total": 0, "win": 0, "loss": 0}
        
        diff_stats[diff_val]["total"] += 1
        if is_win:
            diff_stats[diff_val]["win"] += 1
        else:
            diff_stats[diff_val]["loss"] += 1

        total = diff_stats[diff_val]["total"]
        wins = diff_stats[diff_val]["win"]
        winrate = round((wins / total) * 100, 1) if total > 0 else 0
        
        print(f"📊 [Δ2D={diff_val}] Проходов: {wins}/{total} ({winrate}%) | Промахов: {diff_stats[diff_val]['loss']}")


# ==================== АНОНСЫ БУДУЩИХ ИГР ====================
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


# ==================== ФУНКЦИИ ПРОГНОЗИРОВАНИЯ ====================
def make_prediction(di_num, prev_2d, curr_2d, diff_2d):
    """Отправляет сигнал в канал прогнозов"""
    if not PREDICTION_CHANNEL_ID:
        return False

    target_dis = [di_num, di_num + 1, di_num + 2]
    
    text = (
        f"🎰 <b>Игра № {di_num}</b>\n"
        f"🃏 <b>Прогноз:</b> 3/2\n"
        f"🔄 <b>Догонов:</b> 2\n"
        f"📌 <b>Результат:</b> ⏳ <i>В игре...</i>"
    )
    
    try:
        sent = bot.send_message(PREDICTION_CHANNEL_ID, text, parse_mode="HTML")
        if sent:
            with lock:
                active_preds.append({
                    "chat_id": PREDICTION_CHANNEL_ID,
                    "msg_id": sent.message_id,
                    "target_dis": target_dis,
                    "diff_2d": diff_2d,
                    "results": {},
                    "created_at": time.time()
                })
            return True
    except Exception as e:
        print(f"⚠️ Ошибка отправки прогноза: {e}")
    return False


def check_active_predictions():
    """Проверяет карты для активных прогнозов с обновлением в Telegram"""
    with lock:
        if not active_preds:
            return

        preds_to_remove = []
        for pred in list(active_preds):
            target_dis = [int(x) for x in pred["target_dis"]]
            
            for target_di in target_dis:
                if target_di not in pred["results"]:
                    game_gid = di_to_gid.get(target_di)
                    
                    if not game_gid:
                        continue

                    outcome = fetch_game_cards_outcome(game_gid)
                    if outcome:
                        print(f"🎯 Карта получена для игры № {target_di}: {outcome}")
                        pred["results"][target_di] = outcome
            
            # Расчет и закрытие прогноза
            has_hit = any(res == "3/2" for res in pred["results"].values())
            all_finished = len(pred["results"]) >= len(target_dis)

            if has_hit or all_finished:
                status_symbol = "✅" if has_hit else "❌"
                status_badge = "🟩 <b>ПРОХОД</b>" if has_hit else "🟥 <b>ПРОМАХ</b>"
                res_str = ", ".join([f"{v}" for k, v in pred["results"].items()])
                
                updated_text = (
                    f"🎰 <b>Игра № {pred['target_dis'][0]}</b>\n"
                    f"🃏 <b>Прогноз:</b> 3/2\n"
                    f"🔄 <b>Догонов:</b> 2\n"
                    f"📌 <b>Результат:</b> {res_str} {status_symbol}\n"
                    f"Итог: {status_badge}"
                )

                try:
                    bot.edit_message_text(
                        chat_id=pred["chat_id"],
                        message_id=pred["msg_id"],
                        text=updated_text,
                        parse_mode="HTML"
                    )
                    print(f"🎉 Прогноз на № {pred['target_dis'][0]} ЗАКРЫТ!")
                except Exception as e:
                    print(f"⚠️ Ошибка редактирования сообщения: {e}")

                update_diff_stats(pred["diff_2d"], is_win=has_hit)
                preds_to_remove.append(pred)

        for p in preds_to_remove:
            if p in active_preds:
                active_preds.remove(p)


# ==================== ГЛАВНЫЙ ЦИКЛ ====================
def api_cycle():
    global last_processed_gid
    raw_games = fetch_data()
    if not raw_games:
        return

    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]

    # Обновляем реестр соответствия Display ID -> Game ID
    for game in games:
        di = game.get("DI")
        gid = game.get("I")
        if di and gid:
            di_to_gid[int(di)] = int(gid)

    # Проверяем карточные результаты открытых сигналов
    check_active_predictions()

    new_games = []
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or gid in sent_games:
            continue
        
        sent_games.add(gid)
        new_games.append(game)

        # Анонс будущей игры в CHANNEL_ID
        text = format_game_info(game)
        if text:
            send_to_channel(text)
            
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
                # Вычисление разницы согласно режиму MODE
                diff_2d = calculate_diff_2d(prev_2d, curr_2d, MODE)
                
                # Проверяем условия диапазона (12 < Δ2D < 67)
                if diff_2d is not None and 12 < diff_2d < 67:
                    if make_prediction(di_num, prev_2d, curr_2d, diff_2d):
                        print(f"🔥 Прогноз отправлен на #N{di_num} [{MODE}] | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

        last_processed_gid = gid_num

    # Очистка реестра
    if len(sent_games) > 300: 
        sent_games.clear()
    if len(di_to_gid) > 500:
        di_to_gid.clear()


def main():
    print(f"🚀 ЗАПУСК БОТА ПРОГНОЗОВ | РЕЖИМ: {MODE}")
    
    try:
        bot.remove_webhook()
    except Exception as e:
        print(f"⚠️ Сброс webhook: {e}")

    while True:
        try:
            api_cycle()
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ Ошибка цикла: {e}")
            time.sleep(20)


if __name__ == "__main__":
    main()
