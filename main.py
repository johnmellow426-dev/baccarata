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

MAX_ACTIVE_PREDS = 5
MAX_CHECKS_PER_CYCLE = 6
PRED_TIMEOUT = 600
STAT_TIMEOUT = 5

API_URL = f"https://{BASE_DOMAIN}/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
STATISTIC_URL_TEMPLATE = f"https://{BASE_DOMAIN}/cyber-api/mainfeedlive/web/cyber/v3/statistic?country=192&fcountry=192&gameId={{game_id}}&gr=1521&lng=ru&ref=8"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://{BASE_DOMAIN}/",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
lock = threading.Lock()

session = requests.Session()
session.headers.update(HEADERS)

# ==================== СОСТОЯНИЕ ====================
sent_games = set()
last_processed_gid = None
active_preds = []
diff_stats = {}
di_to_gid = {}

game_history = deque(maxlen=50)
diff_outcome_map = {}
VALID_OUTCOMES = {"2/2", "3/2", "2/3", "3/3"}

ACTIVE_STATUSES = {
    "Prematch", "PlayerMove", "BankerMove", "DealerMove",
    "Betting", "Pause", "Break", "Waiting", "Preparing",
    "Deal", "Dealing", "CardDeal", "CardsDeal"
}


# ==================== ВСПОМОГАТЕЛЬНЫЕ ====================
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
    stat = data.get("statistic", {})
    main_stat = stat.get("main", {}) if isinstance(stat, dict) else {}
    game_status = main_stat.get("S", "")
    if not game_status:
        return False
    if game_status in ACTIVE_STATUSES:
        return False
    timer = data.get("timer", {})
    if timer.get("timeRun", True):
        return False
    return True


def fetch_data():
    try:
        resp = session.get(API_URL, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ LiveFeed: {e}")
    return []


def fetch_game_cards_outcome(game_id):
    url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
    try:
        resp = session.get(url, timeout=STAT_TIMEOUT)
        if resp.status_code in (204, 404):
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

    except requests.exceptions.Timeout:
        pass
    except Exception as e:
        print(f"⚠️ Карты GID {game_id}: {e}")
    return None


# ==================== НАБЛЮДЕНИЕ ====================
def record_result(diff_val, outcome, is_first_game=False):
    if outcome not in VALID_OUTCOMES:
        return
    game_history.append(outcome)
    if is_first_game and diff_val is not None:
        if diff_val not in diff_outcome_map:
            diff_outcome_map[diff_val] = []
        diff_outcome_map[diff_val].append(outcome)
        if len(diff_outcome_map[diff_val]) > 20:
            diff_outcome_map[diff_val] = diff_outcome_map[diff_val][-20:]


def analyze_streak():
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
    return last if streak >= 3 else None


def analyze_alternation():
    if len(game_history) < 4:
        return None
    recent = list(game_history)[-4:]
    if recent[0] == recent[2] and recent[1] == recent[3] and recent[0] != recent[1]:
        return recent[0]
    return None


def analyze_frequency():
    if len(game_history) < 5:
        return None
    recent = list(game_history)[-10:]
    counts = Counter(recent)
    most_common = counts.most_common(1)[0]
    if most_common[1] / len(recent) > 0.4:
        return most_common[0]
    return None


def analyze_diff_correlation(diff_2d):
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
    streak = analyze_streak()
    if streak:
        return streak, "серия"
    alt = analyze_alternation()
    if alt:
        return alt, "чередование"
    corr = analyze_diff_correlation(diff_2d)
    if corr:
        return corr, "корреляция"
    freq = analyze_frequency()
    if freq:
        return freq, "частота"
    return "3/2", "фолбэк"


def get_active_target_dis():
    dis = set()
    for pred in active_preds:
        for di in pred["target_dis"]:
            dis.add(int(di))
    return dis


def update_diff_stats(diff_val, is_win):
    if diff_val not in diff_stats:
        diff_stats[diff_val] = {"total": 0, "win": 0, "loss": 0}
    diff_stats[diff_val]["total"] += 1
    if is_win:
        diff_stats[diff_val]["win"] += 1
    else:
        diff_stats[diff_val]["loss"] += 1


# ==================== АНОНСЫ (ПРИОРИТЕТ №1) ====================
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
    except Exception:
        return None


def send_to_channel(text):
    if not CHANNEL_ID:
        return
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
    except Exception as e:
        print(f"⚠️ send: {e}")


# ==================== ПРОГНОЗЫ (ФОН) ====================
def make_prediction(di_num, prev_2d, curr_2d, diff_2d):
    if not PREDICTION_CHANNEL_ID:
        return False
    if len(active_preds) >= MAX_ACTIVE_PREDS:
        return False

    target_dis = [di_num, di_num + 1, di_num + 2]
    active_dis = get_active_target_dis()
    if any(di in active_dis for di in target_dis):
        return False

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
        print(f"⚠️ Прогноз: {e}")
    return False


def check_active_predictions():
    """Фоновая проверка прогнозов — НЕ блокирует анонсы"""
    if not active_preds:
        return

    checks_done = 0
    preds_to_remove = []

    for pred in list(active_preds):
        if checks_done >= MAX_CHECKS_PER_CYCLE:
            break

        target_dis = [int(x) for x in pred["target_dis"]]
        prediction_val = pred.get("prediction", "3/2")
        first_di = target_dis[0] if target_dis else None

        for target_di in target_dis:
            if checks_done >= MAX_CHECKS_PER_CYCLE:
                break
            if target_di in pred["results"]:
                continue

            game_gid = di_to_gid.get(target_di)
            if not game_gid:
                continue

            outcome = fetch_game_cards_outcome(game_gid)
            checks_done += 1

            if outcome:
                pred["results"][target_di] = outcome
                is_first = (target_di == first_di)
                record_result(pred["diff_2d"], outcome, is_first_game=is_first)

        has_hit = any(res == prediction_val for res in pred["results"].values())
        all_finished = len(pred["results"]) >= len(target_dis)
        age = time.time() - pred.get("created_at", 0)
        is_timeout = age > PRED_TIMEOUT

        if is_timeout and not all_finished:
            all_finished = True
            has_hit = False

        if has_hit or all_finished:
            if is_timeout:
                status_badge = "⚪ <b>ОТМЕНЕН</b>"
            elif has_hit:
                status_badge = "🟩 <b>ПРОХОД</b>"
            else:
                status_badge = "🟥 <b>ПРОМАХ</b>"

            res_list = []
            for di in target_dis:
                if di in pred["results"]:
                    mark = "✅" if pred["results"][di] == prediction_val else "❌"
                    res_list.append(f"{pred['results'][di]}{mark}")
                else:
                    res_list.append("⏱")

            updated_text = (
                f"🎰 <b>Игра № {target_dis[0]}</b>\n"
                f"🃏 <b>Прогноз:</b> {prediction_val}\n"
                f"🧠 <b>Метод:</b> {pred.get('method', '?')}\n"
                f"🔄 <b>Догонов:</b> 2\n"
                f"📌 <b>Результат:</b> {' | '.join(res_list)}\n"
                f"Итог: {status_badge}"
            )

            try:
                bot.edit_message_text(
                    chat_id=pred["chat_id"],
                    message_id=pred["msg_id"],
                    text=updated_text,
                    parse_mode="HTML"
                )
            except Exception:
                pass

            if not is_timeout:
                update_diff_stats(pred["diff_2d"], is_win=has_hit)

            preds_to_remove.append(pred)

    for p in preds_to_remove:
        if p in active_preds:
            active_preds.remove(p)


# ==================== ФОНОВЫЙ ПОТОК: ПРОГНОЗЫ ====================
def prediction_worker():
    """Отдельный поток для проверки прогнозов. Не мешает анонсам."""
    print("🔮 Фоновый поток прогнозов запущен")
    while True:
        try:
            with lock:
                check_active_predictions()
        except Exception as e:
            print(f"⚠️ Prediction worker: {e}")
        time.sleep(15)  # Проверяем прогнозы раз в 15 секунд


# ==================== ГЛАВНЫЙ ЦИКЛ: АНОНСЫ ====================
def api_cycle():
    global last_processed_gid

    raw_games = fetch_data()
    if not raw_games:
        return

    games = [g for g in raw_games if g.get("SI") == BACCARAT_SPORT_ID]

    # Обновляем DI → GID
    for game in games:
        di = game.get("DI")
        gid = game.get("I")
        if di and gid:
            di_to_gid[int(di)] = int(gid)

    # ✅ АНОНСЫ — приоритет №1
    new_games = []
    for game in games:
        gid = game.get("I")
        if not gid or gid in sent_games:
            continue
        sent_games.add(gid)
        new_games.append(game)

        text = format_game_info(game)
        if text:
            send_to_channel(text)

    new_games.sort(key=lambda g: int(g.get("DI") or 0))

    # Генерация прогнозов (быстро, без HTTP-запросов к статистике)
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
                        print(f"🔥 Прогноз #N{di_num} | Δ2D={diff_2d}")

        last_processed_gid = gid_num

    # Мягкая очистка
    if len(sent_games) > 300:
        sent_games.clear()
    if len(di_to_gid) > 500:
        keys = list(di_to_gid.keys())[:200]
        for k in keys:
            del di_to_gid[k]


def main():
    print(f"🚀 ЗАПУСК | РЕЖИМ: {MODE}")
    print(f"📢 Анонсы: приоритет")
    print(f"🔮 Прогнозы: фоновый поток")

    try:
        bot.remove_webhook()
    except Exception:
        pass

    # ✅ Запускаем фоновый поток для прогнозов
    pred_thread = threading.Thread(target=prediction_worker, daemon=True)
    pred_thread.start()

    # Основной цикл — только анонсы и генерация прогнозов
    while True:
        try:
            api_cycle()
            time.sleep(8)
        except Exception as e:
            print(f"⚠️ Цикл: {e}")
            time.sleep(15)


if __name__ == "__main__":
    main()
