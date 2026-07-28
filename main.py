import requests
import json
import time
from collections import defaultdict

# --- НАСТРОЙКИ ДЛЯ БАККАРЫ (используем надежный формат gamesByChamp) ---
VIRTUAL_URL = "https://melbet-5427.pro/cyber-api/mainfeedlive/web/cyber/v3/gamesByChamp?cfView=3&champId=2050671&country=192&fcountry=192&gr=1521&lng=ru&ref=8"
STATISTIC_URL_TEMPLATE = "https://melbet-5427.pro/cyber-api/mainfeedlive/web/cyber/v3/statistic?country=192&fcountry=192&gameId={game_id}&gr=1521&lng=ru&ref=8"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://melbet-5427.pro/",
}

NO_PROXY = {"http": None, "https": None}

SUITS = {
    0: {"name": "Пики", "symbol": "♠️"},
    1: {"name": "Трефы", "symbol": "♣️"},
    2: {"name": "Бубны", "symbol": "♦️"},
    3: {"name": "Червы", "symbol": "♥️"}
}

history = []
historical_odds = {0: [], 1: [], 2: [], 3: []}
processed_game_ids = set()
WINDOW_SIZE = 30

def get_suit_from_name(name):
    if "Пики" in name: return 0
    if "Трефы" in name: return 1
    if "Бубны" in name: return 2
    if "Червы" in name: return 3
    return -1

def fetch_finished_games_results():
    try:
        resp = requests.get(VIRTUAL_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
        data = resp.json()
        # Поддержка и gamesByChamp, и VZip
        games = data.get("games", data.get("Value", []))
        
        for game in games:
            game_id = game.get("id", game.get("I"))
            scores = game.get("scores", game.get("SC", {}))
            
            # Универсальная проверка на завершение
            is_finished = (
                scores.get("currentPeriodName") == "Игра завершена" or 
                game.get("S") == 3
            )
            
            if is_finished and game_id and game_id not in processed_game_ids:
                stat_url = STATISTIC_URL_TEMPLATE.format(game_id=game_id)
                stat_resp = requests.get(stat_url, headers=HEADERS, timeout=5, proxies=NO_PROXY)
                
                if stat_resp.status_code == 200:
                    stat_data = stat_resp.json()
                    p1_cards_str = stat_data.get("statistic", {}).get("main", {}).get("P1", "[]")
                    
                    try:
                        p1_cards = json.loads(p1_cards_str)
                        game_suits = [c.get("CS") for c in p1_cards if c.get("CS") in SUITS]
                        
                        if game_suits:
                            history.extend(game_suits)
                            processed_game_ids.add(game_id)
                            print(f"✅ Баккара #{game_id}: масти Игрока {[SUITS[s]['symbol'] for s in game_suits]}")
                    except json.JSONDecodeError:
                        pass
                        
        if len(history) > WINDOW_SIZE * 3:
            history = history[-(WINDOW_SIZE * 3):]
            
    except Exception as e:
        print(f"⚠️ Ошибка сбора истории: {e}")

def get_current_odds(game_data):
    current_odds = {0: 1.75, 1: 1.75, 2: 1.75, 3: 1.75}
    # Поддержка и eventGroups, и O (для VZip)
    odds_groups = game_data.get("eventGroups", game_data.get("O", []))
    
    for group in odds_groups:
        group_id = group.get("groupId", group.get("G"))
        # 8443 - это масти Игрока в Баккаре/21 очко
        if group_id == 8443:
            events = group.get("events", group.get("C", [[]]))
            if isinstance(events, list) and len(events) > 0 and isinstance(events[0], list):
                events = events[0]
                
            for event in events:
                player_info = event.get("player", event.get("P", {}))
                name = player_info.get("name", "")
                suit_idx = get_suit_from_name(name)
                if suit_idx != -1:
                    current_odds[suit_idx] = event.get("cf", event.get("Cf", 1.75))
    return current_odds

def calculate_anomaly_scores(current_odds):
    scores = {}
    suit_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    suit_last_seen = {0: -1, 1: -1, 2: -1, 3: -1}
    total_cards_in_window = len(history)
    
    for idx, suit in enumerate(history):
        suit_counts[suit] += 1
        suit_last_seen[suit] = idx
    
    for suit in SUITS:
        last_seen_idx = suit_last_seen[suit]
        streak = total_cards_in_window if last_seen_idx == -1 else (total_cards_in_window - 1) - last_seen_idx
        streak_score = min(streak / 5.0, 2.0)

        actual_freq = suit_counts[suit] / total_cards_in_window if total_cards_in_window > 0 else 0.25
        freq_deviation = 0.25 - actual_freq
        freq_score = freq_deviation * 8.0

        historical_odds[suit].append(current_odds[suit])
        if len(historical_odds[suit]) > WINDOW_SIZE:
            historical_odds[suit].pop(0)
            
        avg_historical_odds = sum(historical_odds[suit]) / len(historical_odds[suit]) if historical_odds[suit] else current_odds[suit]
        odds_drop = avg_historical_odds - current_odds[suit]
        odds_score = max(odds_drop * 5.0, 0.0)

        total_score = streak_score + freq_score + odds_score
        scores[suit] = {
            "score": total_score,
            "streak": streak,
            "freq": f"{actual_freq*100:.1f}%",
            "odds_drop": f"{odds_drop:+.3f}",
            "current_cf": current_odds[suit]
        }
    return scores

def main():
    print("🚀 Запуск анализатора аномалий МАСТЕЙ ИГРОКА (БАККАРА)...")
    print("Собираю начальные данные, подождите...\n")
    
    for _ in range(3):
        fetch_finished_games_results()
        time.sleep(2)
        
    if len(history) < 10:
        print("⚠️ ВНИМАНИЕ: Недостаточно данных. Скрипт копит историю в реальном времени.\n")

    last_prediction_game_id = None

    while True:
        try:
            print("="*70)
            print(f"🔄 Сканирование рынка Баккары... (В истории {len(history)} карт Игрока)")
            
            resp = requests.get(VIRTUAL_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
            data = resp.json()
            games = data.get("games", data.get("Value", []))
            
            # УНИВЕРСАЛЬНЫЙ ПОИСК СЛЕДУЮЩЕЙ ИГРЫ
            next_game = None
            for g in games:
                scores = g.get("scores", g.get("SC", {}))
                full_score = scores.get("fullScore", scores.get("FS", "0-0"))
                current_period = scores.get("currentPeriodName", "")
                info = scores.get("info", "")
                non_started = g.get("nonStarted", False) or g.get("S") == 1
                
                # Если игра не началась, ИЛИ счет 0-0, ИЛИ статус "Ставки до начала игры"
                if non_started or full_score == "0-0" or current_period == "Ставки до начала игры" or info == "Ставки до начала игры":
                    next_game = g
                    break
            
            if not next_game:
                print("⏳ Все игры идут или завершены. Жду обновления списка (Баккара запускается волнами)...")
                time.sleep(5)
                continue
                
            fetch_finished_games_results()
            
            next_game_id = next_game.get("id", next_game.get("I"))
            
            if next_game_id != last_prediction_game_id:
                print(f"\n🎯 Прогноз для следующей игры Баккары (ID: {next_game_id})")
                
                current_odds = get_current_odds(next_game)
                scores = calculate_anomaly_scores(current_odds)
                
                sorted_suits = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
                
                print("\n📊 РЕЙТИНГ АНОМАЛИЙ МАСТЕЙ (Игрок в Баккаре):")
                print(f"{'Масть':<10} | {'Скор':<6} | {'Задержка':<8} | {'Частота':<8} | {'Кф (Δ)':<10}")
                print("-" * 65)
                
                for suit, data in sorted_suits:
                    info = SUITS[suit]
                    print(f"{info['symbol']} {info['name']:<6} | {data['score']:<6.2f} | {data['streak']:<8} | {data['freq']:<8} | {data['current_cf']} ({data['odds_drop']})")
                    
                best_suit = sorted_suits[0][0]
                best_data = sorted_suits[0][1]
                
                print("\n" + "="*70)
                print(f"🔥 ВЕРДИКТ АЛГОРИТМА ДЛЯ БАККАРЫ:")
                print(f"Наибольшая аномалия у масти Игрока: {SUITS[best_suit]['symbol']} {SUITS[best_suit]['name']}")
                print(f"Причина: Не выпадала {best_data['streak']} раз(а), частота {best_data['freq']}, кф {best_data['current_cf']}")
                print("="*70 + "\n")
                
                last_prediction_game_id = next_game_id
            
            time.sleep(10)
            
        except KeyboardInterrupt:
            print("\n🛑 Остановка скрипта.")
            break
        except Exception as e:
            print(f"❌ Критическая ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
