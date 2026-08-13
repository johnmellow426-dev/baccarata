import os
import time
import json
import threading
import requests
import telebot
from collections import deque, Counter

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID")

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "melbet-4866.pro")
MODE = os.getenv("MODE", "INCREASE").upper()

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

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
last_processed_gid = None
active_preds = []
diff_stats = {}
di_to_gid = {}

# ==================== НАБЛЮДЕНИЕ И АНАЛИТИКА ====================
game_history = deque(maxlen=50)       # Последние 50 результатов игр
diff_outcome_map = {}                  # {Δ2D: [список исходов]} для корреляции
VALID_OUTCOMES = {"2/2", "3/2", "2/3", "3/3"}

# Статусы активной (незавершённой) игры
ACTIVE_STATUSES = {
    "Prematch", "PlayerMove", "BankerMove", "DealerMove",
    "Betting", "Pause", "Break", "Waiting", "Preparing",
    "Deal", "Dealing", "CardDeal", "CardsDeal"
}


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_last_2_digits(gid):
    try:
        return int(str(gid)[-2:])
    except (ValueError, TypeError):
        return None


def calculate_diff_2d(prev_2d, curr_2d, mode):
    if mode == "DECREASE":
        return (prev_2d - curr_2d) if prev_2d > curr_2d else None
    elif mode == "INCREASE":
        return (curr_2d - prev_2d) if curr_2d > prev_2d else None
    elif mode == "HYBRID":
        return abs(prev_2d - curr_2d)
    return None


def is_game_finished(data):
    """Проверяет завершённость игры"""
    stat = data.get("statistic", {})
    main_stat = stat.get("main", {}) if isinstance(stat, dict) else {}
    game_status = main_stat.get("S", "")

    if not game_status:
        return False
    if game_status in ACTIVE_STATUSES:
        return False

    timer = data.get("timer", {})
    time_run = timer.get("timeRun", True)
    if time_run:
        return False

    return True


def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ API LiveFeed: {e}")
    return []


def fetch_game_cards_outcome(game_id):
    """Возвращает результат ТОЛЬКО для завершённых игр"""
    url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=7)

        if resp.status_code == 204:
            return None
        if resp.status_code != 200:
            return None

        text = resp.text.strip()
        if not text or text.startswith("<"):
            return None

        data = resp.json()

        if not is_game_finished(data):
            return None

        stat = data.get("statistic", {})
        main_stat = stat.get("main", {}) if isinstance(stat, dict) else {}

        p1_raw = main_stat.get("P") or stat.get("P") or data.get("P")
        p2_raw = main_stat.get("B") or stat.get("B") or data.get("B")

        if p1_raw is None or p2_raw is None:
            return None

        try:
            p1_list = json.loads(p1_raw) if isinstance(p1_raw, str) else p1_raw
            p2_list = json.loads(p2_raw) if isinstance(p2_raw, str) else p2_raw
            p1_count = len(p1_list) if isinstance(p1_list, list) else int(p1_list)
            p2_count = len(p2_list) if isinstance(p2_list, list) else int(p2_list)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None

        if p1_count == 0 and p2_count == 0:
            return None

        return f"{p1_count}/{p2_count}"

    except Exception as e:
        print(f"⚠️ Ошибка запроса карт (Game ID {game_id}): {e}")
    return None


# ==================== АНАЛИТИКА: НАБЛЮДЕНИЕ И ПРОГНОЗ ====================
def record_result(diff_val, outcome):
    """Записывает результат игры в историю наблюдения"""
    if outcome not in VALID_OUTCOMES:
        return

    game_history.append(outcome)

    if diff_val is not None:
        if diff_val not in diff_outcome_map:
            diff_outcome_map[diff_val] = []
        diff_outcome_map[diff_val].append(outcome)
        # Ограничиваем историю по каждому Δ2D
        if len(diff_outcome_map[diff_val]) > 20:
            diff_outcome_map[diff_val] = diff_outcome_map[diff_val][-20:]

    print(f"📝 Записано: Δ2D={diff_val} → {outcome} | История: {list(game_history)[-5:]}")


def analyze_streak():
    """Определяет серию из 3+ одинаковых исходов подряд"""
    if len(game_history) < 3:
        return None
    recent = list(game_history)
    last = recent[-1]
    streak = 0
    for r in reversed(recent):
        if r == last:
            streak += 1
        else:
            break
    if streak >= 3:
        return last  # Прогнозируем продолжение серии
    return None


def analyze_alternation():
    """Определяет чередование: A, B, A, B → следующий A"""
    if len(game_history) < 4:
        return None
    recent = list(game_history)[-4:]
    if recent[0] == recent[2] and recent[1] == recent[3] and recent[0] != recent[1]:
        return recent[0]
    return None


def analyze_frequency():
    """Самый частый исход в последних 10 играх"""
    if len(game_history) < 5:
        return None
    recent = list(game_history)[-10:]
    counts = Counter(recent)
    most_common = counts.most_common(1)[0]
    # Возвращаем только если исход встречается > 40% случаев
    if most_common[1] / len(recent) > 0.4:
        return most_common[0]
    return None


def analyze_diff_correlation(diff_2d):
    """Анализ корреляции: какой исход чаще всего был при данном Δ2D"""
    if diff_2d not in diff_outcome_map:
        return None
    outcomes = diff_outcome_map[diff_2d]
    if len(outcomes) < 3:
        return None
    counts = Counter(outcomes)
    most_common = counts.most_common(1)[0]
    if most_common[1] / len(outcomes) > 0.4:
        return most_common[0]
    return None


def determine_prediction(diff_2d):
    """
    Определяет прогноз на основе наблюдения.
    Приоритет:
      1. Серия (3+ одинаковых подряд)
      2. Чередование (A,B,A,B)
      3. Корреляция с Δ2D
      4. Частотный анализ
      5. Фолбэк по чётности Δ2D
    """
    # 1. Серия
    streak_pred = analyze_streak()
    if streak_pred:
        print(f"🧠 [СЕРИЯ] Обнаружена серия → {streak_pred}")
        return streak_pred, "серия"

    # 2. Чередование
    alt_pred = analyze_alternation()
    if alt_pred:
        print(f"🧠 [ЧЕРЕДОВАНИЕ] Обнаружено чередование → {alt_pred}")
        return alt_pred, "чередование"

    # 3. Корреляция с Δ2D
    corr_pred = analyze_diff_correlation(diff_2d)
    if corr_pred:
        print(f"🧠 [КОРРЕЛЯЦИЯ Δ2D={diff_2d}] → {corr_pred}")
        return corr_pred, "корреляция"

    # 4. Частотный анализ
    freq_pred = analyze_frequency()
    if freq_pred:
        print(f"🧠 [ЧАСТОТА] Наиболее вероятный → {freq_pred}")
        return freq_pred, "частота"

    # 5. Фолбэк по чётности
    fallback = "2/3" if diff_2d % 2 == 0 else "3/2"
    print(f"🧠 [ФОЛБЭК] Чётность Δ2D={diff_2d} → {fallback}")
    return fallback, "фолбэк"


def get_active_prediction_dis():
    """Возвращает множество DI, на которые уже есть активные прогнозы"""
    active_dis = set()
    with lock:
        for pred in active_preds:
            for di in pred["target_dis"]:
                active_dis.add(int(di))
    return active_dis


# ==================== СТАТИСТИКА ====================
def update_diff_stats(diff_val, is_win):
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


# ==================== АНОНСЫ ====================
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


# ==================== ПРОГНОЗИРОВАНИЕ ====================
def make_prediction(di_num, prev_2d, curr_2d, diff_2d):
    """Отправляет прогноз. Не создаёт дублей на одну игру."""
    if not PREDICTION_CHANNEL_ID:
        return False

    # ✅ ЗАЩИТА ОТ ДУБЛЕЙ: проверяем, нет ли уже прогноза на эти DI
    active_dis = get_active_prediction_dis()
    target_dis = [di_num, di_num + 1, di_num + 2]

    # Если хотя бы один из целевых DI уже в активном прогнозе — пропускаем
    if any(di in active_dis for di in target_dis):
        print(f"⏭️ Пропуск: игра № {di_num} уже в активном прогнозе")
        return False

    # Определяем прогноз на основе наблюдения
    prediction_val, method = determine_prediction(diff_2d)

    text = (
        f"🎰 <b>Игра № {di_num}</b>\n"
        f"🃏 <b>Прогноз:</b> {prediction_val}\n"
        f"🧠 <b>Метод:</b> {method}\n"
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
                    "prediction": prediction_val,
                    "method": method,
                    "results": {},
                    "created_at": time.time()
                })
            return True
    except Exception as e:
        print(f"⚠️ Ошибка отправки прогноза: {e}")
    return False


def check_active_predictions():
    """Проверяет результаты только завершённых игр"""
    with lock:
        if not active_preds:
            return

        preds_to_remove = []
        for pred in list(active_preds):
            target_dis = [int(x) for x in pred["target_dis"]]
            prediction_val = pred.get("prediction", "3/2")

            for target_di in target_dis:
                if target_di not in pred["results"]:
                    game_gid = di_to_gid.get(target_di)

                    if not game_gid:
                        continue

                    outcome = fetch_game_cards_outcome(game_gid)
                    if outcome:
                        print(f"🎯 Результат игры № {target_di}: {outcome}")
                        pred["results"][target_di] = outcome

                        # ✅ Записываем результат в историю наблюдения
                        record_result(pred["diff_2d"], outcome)

            has_hit = any(res == prediction_val for res in pred["results"].values())
            all_finished = len(pred["results"]) >= len(target_dis)

            # Таймаут 15 минут
            is_timeout = (time.time() - pred.get("created_at", 0)) > 900
            if is_timeout and not all_finished:
                all_finished = True
                has_hit = False

            if has_hit or all_finished:
                if is_timeout:
                    status_symbol = "⚪"
                    status_badge = "⚪ <b>ОТМЕНЕН</b>"
                else:
                    status_symbol = "✅" if has_hit else "❌"
                    status_badge = "🟩 <b>ПРОХОД</b>" if has_hit else "🟥 <b>ПРОМАХ</b>"

                res_str = ", ".join([f"{v}" for k, v in pred["results"].items()]) if pred["results"] else "Нет данных"

                updated_text = (
                    f"🎰 <b>Игра № {pred['target_dis'][0]}</b>\n"
                    f"🃏 <b>Прогноз:</b> {prediction_val}\n"
                    f"🧠 <b>Метод:</b> {pred.get('method', '?')}\n"
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
                    print(f"⚠️ Ошибка редактирования: {e}")

                if not is_timeout:
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

    for game in games:
        di = game.get("DI")
        gid = game.get("I")
        if di and gid:
            di_to_gid[int(di)] = int(gid)

    check_active_predictions()

    new_games = []
    for game in games:
        gid = game.get("I")
        di = game.get("DI")
        if not gid or gid in sent_games:
            continue

        sent_games.add(gid)
        new_games.append(game)

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
                diff_2d = calculate_diff_2d(prev_2d, curr_2d, MODE)

                if diff_2d is not None and 12 < diff_2d < 67:
                    if make_prediction(di_num, prev_2d, curr_2d, diff_2d):
                        print(f"🔥 Прогноз на #N{di_num} [{MODE}] | 2D: {prev_2d} ➔ {curr_2d} | Δ2D = {diff_2d}")

        last_processed_gid = gid_num

    if len(sent_games) > 300:
        sent_games.clear()
    if len(di_to_gid) > 500:
        di_to_gid.clear()


def main():
    print(f"🚀 ЗАПУСК БОТА | РЕЖИМ: {MODE}")
    print(f"🧠 Аналитика: наблюдение за последними {game_history.maxlen} играми")

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
