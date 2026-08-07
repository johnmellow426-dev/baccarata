import os
import time
import json
import requests
import telebot

# --- НАСТРОЙКИ ОКРУЖЕНИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

BASE_DOMAIN = os.getenv("BASE_DOMAIN", "melbet-4866.pro")

# URL API LiveFeed для Баккары (sports=236)
VIRTUAL_URL = os.getenv(
    "VIRTUAL_URL",
    f"https://{BASE_DOMAIN}/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
)
STATISTIC_URL_TEMPLATE = os.getenv(
    "STATISTIC_URL_TEMPLATE",
    f"https://{BASE_DOMAIN}/cyber-api/mainfeedlive/web/cyber/v3/statistic?country=192&fcountry=192&gameId={{game_id}}&gr=1521&lng=ru&ref=8"
)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Referer": f"https://{BASE_DOMAIN}/",
}

# --- КОНСТАНТЫ И МАППИНГ ---
SUITS = {
    0: "♠️",
    1: "♣️",
    2: "♦️",
    3: "♥️"
}

CARD_VALUES = {
    1: "A", 14: "A",
    2: "2", 3: "3", 4: "4", 5: "5", 6: "6", 7: "7", 8: "8", 9: "9", 10: "10",
    11: "J", 12: "Q", 13: "K"
}

active_games = {}


# ============================================================
#   ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def extract_game_number(game_data):
    """Извлекает номер раунда (DI) из JSON."""
    tn_val = game_data.get("DI") or game_data.get("TN")

    if not tn_val or not str(tn_val).isdigit():
        sc = game_data.get("SC", {})
        tn_val = sc.get("DI") or sc.get("CP")

    if tn_val is not None:
        try:
            return int(tn_val)
        except (ValueError, TypeError):
            pass

    return 0


def get_card_symbol(card_value, suit_code):
    val_str = CARD_VALUES.get(card_value, "?")
    suit_str = SUITS.get(suit_code, "?")
    return f"{val_str}{suit_str}"


def parse_cards_detail(cards_str):
    """Разбирает список карт из JSON статистики."""
    try:
        if isinstance(cards_str, list):
            cards = cards_str
        else:
            cards = json.loads(cards_str)
            
        symbols = []
        for c in cards:
            cv, cs = c.get("CV", 0), c.get("CS", 0)
            symbols.append(get_card_symbol(cv, cs))
        return symbols
    except Exception:
        return []


def get_active_games_info(session):
    try:
        resp = session.get(VIRTUAL_URL, headers=HEADERS, timeout=10)
        data = resp.json()
        
        raw_games = data.get("Value", []) or data.get("games", [])
        if isinstance(raw_games, dict):
            raw_games = [raw_games]

        result = []
        for idx, g in enumerate(raw_games):
            game_id = g.get("I") or g.get("id")
            if not game_id:
                continue

            sc = g.get("SC", {}) or g.get("scores", {})
            is_finished = (sc.get("CPS") == "Игра завершена") or (sc.get("currentPeriodName") == "Игра завершена")

            result.append({
                "id": game_id,
                "index": idx,
                "is_finished": is_finished,
                "raw_data": g
            })
            
        return result
    except Exception as e:
        print(f"❌ Ошибка получения списка игр Баккары: {e}")
        return []


# ============================================================
#   ОСНОВНОЙ ЦИКЛ
# ============================================================

def main():
    global active_games
    print("🚀 Запуск бота трансляции Баккары...")
    session = requests.Session()

    while True:
        try:
            games_info = get_active_games_info(session)
            if not games_info:
                time.sleep(3)
                continue

            current_game_ids = set(g["id"] for g in games_info if g["id"])

            for g_info in games_info:
                game_id = g_info["id"]
                if not game_id:
                    continue

                # 1. АНОНС НОВОЙ ИГРЫ
                if game_id not in active_games:
                    game_num = extract_game_number(g_info["raw_data"])
                    announcement_text = f"🎴 <b>Баккара #N{game_num}</b>\n⏳ Ожидание начала... (ID: {game_id})"
                    
                    msg_id = None
                    if CHANNEL_ID:
                        try:
                            sent = bot.send_message(CHANNEL_ID, announcement_text, parse_mode="HTML")
                            msg_id = sent.message_id
                            print(f"📡 Анонс игры Баккара #N{game_num} (ID: {game_id})")
                        except Exception as e:
                            print(f"⚠️ Ошибка отправки анонса #N{game_num}: {e}")

                    active_games[game_id] = {
                        "message_id": msg_id,
                        "game_num": game_num,
                        "last_state": "",
                        "is_finished": False
                    }

                slot = active_games[game_id]
                game_num = slot["game_num"]

                # 2. ПОЛУЧЕНИЕ СТАТИСТИКИ ХОДА ИГРЫ
                stat_url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
                resp = session.get(stat_url, headers=HEADERS, timeout=5)
                if resp.status_code == 200 and resp.text.strip():
                    data = resp.json()
                    score_detail = data.get("fullScoreDetail", {})
                    
                    # Очки Игрока (P1) и Банкира (P2) в Баккаре
                    p1_score = score_detail.get("scoreOpp1", 0)
                    p2_score = score_detail.get("scoreOpp2", 0)
                    status = data.get("currentPeriodName", "")

                    stat = data.get("statistic", {}).get("main", {})
                    p1_cards = parse_cards_detail(stat.get("P1", "[]"))
                    p2_cards = parse_cards_detail(stat.get("P2", "[]"))

                    is_finished = (status == "Игра завершена")

                    # 3. ФОРМИРОВАНИЕ СООБЩЕНИЯ
                    current_state = f"{p1_score}_{p2_score}_{'_'.join(p1_cards)}_{'_'.join(p2_cards)}_{is_finished}"

                    if current_state != slot["last_state"] and (p1_cards or p2_cards):
                        str_p1_cards = " ".join(p1_cards) if p1_cards else "—"
                        str_p2_cards = " ".join(p2_cards) if p2_cards else "—"

                        if not is_finished:
                            msg = (
                                f"🎴 <b>Баккара #N{game_num}</b>\n"
                                f"──────────────────────────────\n"
                                f"🔵 <b>Игрок:</b> {p1_score} очк. [{str_p1_cards}]\n"
                                f"🔴 <b>Банкир:</b> {p2_score} очк. [{str_p2_cards}]\n"
                                f"──────────────────────────────\n"
                                f"⏳ <i>Раунд в процессе...</i>\n"
                                f"(ID: {game_id})"
                            )
                        else:
                            # Определение победителя в Баккаре
                            if p1_score > p2_score:
                                winner_str = "🏆 <b>Победа: ИГРОК 🔵</b>"
                            elif p2_score > p1_score:
                                winner_str = "🏆 <b>Победа: БАНКИР 🔴</b>"
                            else:
                                winner_str = "🤝 <b>НИЧЬЯ 🟡</b>"

                            msg = (
                                f"🎴 <b>Баккара #N{game_num} | ИТОГ</b>\n"
                                f"──────────────────────────────\n"
                                f"🔵 <b>Игрок:</b> {p1_score} очк. [{str_p1_cards}]\n"
                                f"🔴 <b>Банкир:</b> {p2_score} очк. [{str_p2_cards}]\n"
                                f"──────────────────────────────\n"
                                f"{winner_str}\n"
                                f"(ID: {game_id})"
                            )

                        try:
                            if slot["message_id"] and CHANNEL_ID:
                                bot.edit_message_text(
                                    chat_id=CHANNEL_ID,
                                    message_id=slot["message_id"],
                                    text=msg,
                                    parse_mode="HTML"
                                )
                            elif CHANNEL_ID:
                                sent = bot.send_message(CHANNEL_ID, msg, parse_mode="HTML")
                                slot["message_id"] = sent.message_id
                        except Exception as e:
                            print(f"⚠️ Ошибка обновления сообщения в Telegram: {e}")

                        slot["last_state"] = current_state
                        if is_finished:
                            slot["is_finished"] = True
                            print(f"✅ Игра Баккара #N{game_num} завершена: Игрок {p1_score} - {p2_score} Банкир")

            # Очистка завершенных игр из памяти
            finished_to_remove = [
                gid for gid, data in active_games.items()
                if data["is_finished"] and gid not in current_game_ids
            ]
            for gid in finished_to_remove:
                del active_games[gid]

            time.sleep(3)

        except Exception as e:
            print(f"❌ Ошибка главного цикла: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
