import os
import time
import json
import threading
import requests
import telebot

# ==================== НАСТРОЙКИ (ENV) ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")                     # Канал для анонсов будущих игр
PREDICTION_CHANNEL_ID = os.getenv("PREDICTION_CHANNEL_ID") # Канал для сигналов/прогнозов

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

active_preds = []   # Активные прогнозы
diff_stats = {}     # Статистика: { diff_val: {"total": 0, "win": 0, "loss": 0} }
di_to_gid = {}      # Реестр соответствия Display ID (DI) -> Game ID (GID)


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
    """Запрашивает детальную статистику завершенной игры"""
    url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=7)
        if resp.status_code == 200 and resp.text.strip():
            data = resp.json()
            stat = data.get("statistic", {}).get("main", {})
            
            p1_raw = stat.get("P1")
            p2_raw = stat.get("P2")
            
            if p1_raw is None or p2_raw is None:
                return None
            
            # 1. Если пришли JSON-строки карт
            if isinstance(p1_raw, str) and p1_raw.startswith("["):
                p1_cards = json.loads(p1_raw)
                p2_cards = json.loads(p2_raw) if isinstance(p2_raw, str) else p2_raw
                return f"{len(p1_cards)}/{len(p2_cards)}"
            
            # 2. Если пришли списки Python
            elif isinstance(p1_raw, list) and isinstance(p2_raw, list):
                return f"{len(p1_raw)}/{len(p2_raw)}"
            
            # 3. Если API передает сразу число карт
            elif isinstance(p1_raw, (int, str)) and isinstance(p2_raw, (int, str)):
                return f"{p1_raw}/{p2_raw}"
                
    except Exception as e:
        print(f"⚠️ Ошибка парсинга карт (Game ID {game_id}): {e}")
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
        
        print(f"📊 [|Δ2D|={diff_val}] Проходов: {wins}/{total} ({winrate}%) | Промахов: {diff_stats[diff_val]['loss']}")


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
    """Периодически проверяет карты для всех нерасчитанных активных прогнозов"""
    with lock:
        preds_to_remove = []
        for pred in list(active_preds):
            target_dis = [int(x) for x in pred["target_dis"]]
            
            # Запрашиваем результат для тех целевых игр, где результат еще не сохранен
            for target_di in target_dis:
                if target_di not in pred["results"]:
                    game_gid = di_to_gid.get(target_di)
                    if game_gid:
                        outcome = fetch_game_cards_outcome(game_gid)
                        if outcome:
                            print(f"🔎 Получены карты для Игра № {target_di} (ID: {game_gid}): {outcome}")
                            pred["results"][target_di] = outcome
            
            # Попадание если хотя бы в одной из сыгранных игр выпало 3/2
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
                    print(f"✅ Прогноз на № {pred['target_dis'][0]} успешно закрыт! (Результаты: {res_str})")
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

    # Обновляем связку Display ID -> Game ID для всех игр в линии
    for game in games:
        di = game.get("DI")
        gid = game.get("I")
        if di and gid:
            di_to_gid[int(di)] = int(gid)

    # Проверяем карточные исходы для активных прогнозов
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
                # Абсолютная разница (модуль)
                diff_2d = abs(prev_2d - curr_2d)
                
                # УСЛОВИЕ: Разница |Δ2D| больше 12 и меньше 67
                if 12 < diff_2d < 67:
                    if make_prediction(di_num, prev_2d, curr_2d, diff_2d):
                        print(f"🔥 Прогноз отправлен на #N{di_num} | 2D: {prev_2d} ➔ {curr_2d} | |Δ2D| = {diff_2d}")

        last_processed_gid = gid_num

    # Очистка старых данных
    if len(sent_games) > 300: 
        sent_games.clear()
    if len(di_to_gid) > 500:
        di_to_gid.clear()


def main():
    print("🚀 ЗАПУСК БОТА С АКТИВНЫМ МОНИТОРИНГОМ ИСХОДОВ")
    
    try:
        bot.remove_webhook()
    except Exception as e:
        print(f"⚠️ Ошибка сброса webhook: {e}")

    while True:
        try:
            api_cycle()
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ Ошибка цикла: {e}")
            time.sleep(20)


if __name__ == "__main__":
    main()
