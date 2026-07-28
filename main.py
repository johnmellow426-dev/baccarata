import requests
import json
import time
from collections import defaultdict

# --- НАСТРОЙКИ ---
LIST_URL = "https://melbet-5427.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"
DETAIL_URL_TEMPLATE = "https://melbet-5427.pro/service-api/LiveFeed/GetGameZip?id={game_id}&isSubGames=true&GroupEvents=true&countevents=250&grMode=4&partner=8&topGroups=&country=192&marketType=1&isNewBuilder=true"

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

def fetch_game_details(game_id):
    try:
        url = DETAIL_URL_TEMPLATE.format(game_id=game_id)
        resp = requests.get(url, headers=HEADERS, timeout=10, proxies=NO_PROXY)
        if resp.status_code != 200:
            return None, None
            
        data = resp.json()
        value = data.get("Value", {})
        
        player_suits = []
        sc_s = value.get("SC", {}).get("S", [])
        for item in sc_s:
            if item.get("Key") == "P":
                try:
                    cards = json.loads(item.get("Value", "[]"))
                    player_suits = [c.get("S") for c in cards if c.get("S") in SUITS]
                except json.JSONDecodeError:
                    pass
        
        current_odds = {0: 1.90, 1: 1.90, 2: 1.90, 3: 1.90}
        ge = value.get("GE", [])
        for group in ge:
            if group.get("G") == 10185:
                events = group.get("E", [[]])[0]
                for event in events:
                    name = event.get("PL", {}).get("N", "")
                    cf = event.get("C")
                    if "Пики" in name: current_odds[0] = cf
                    elif "Трефы" in name: current_odds[1] = cf
                    elif "Бубны" in name: current_odds[2] = cf
                    elif "Червы" in name: current_odds[3] = cf
                break
                
        return player_suits, current_odds
        
    except Exception as e:
        print(f"⚠️ Ошибка деталей #{game_id}: {e}")
        return None, None

def fetch_finished_games_results():
    global history
    
    try:
        resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
        data = resp.json()
        games = data.get("Value", [])
        
        for game in games:
            game_id = game.get("I")
            scores = game.get("SC", {})
            
            is_finished = scores.get("CPS") == "Игра завершена"
            
            if is_finished and game_id and game_id not in processed_game_ids:
                # Получаем счет игры
                final_score = scores.get("FS", {})
                s1 = final_score.get("S1", 0)
                s2 = final_score.get("S2", 0)
                
                print(f"📥 Загружаем завершенную игру #{game_id} | Счет: {s1}-{s2}...")
                
                player_suits, _ = fetch_game_details(game_id)
                
                if player_suits:
                    history.extend(player_suits)
                    processed_game_ids.add(game_id)
                    print(f"✅ Добавлены масти Игрока: {[SUITS[s]['symbol'] for s in player_suits]}")
                else:
                    print(f"⚠️ Не удалось получить масти для игры #{game_id}")
                    
        if len(history) > WINDOW_SIZE * 3:
            history = history[-(WINDOW_SIZE * 3):]
            
    except Exception as e:
        print(f"⚠️ Ошибка списка игр: {e}")

def calculate_anomaly_scores(current_odds):
    scores = {}
    suit_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    suit_last_seen = {0: -1, 1: -1, 2: -1, 3: -1}
    total_cards = len(history)
    
    for idx, suit in enumerate(history):
        suit_counts[suit] += 1
        suit_last_seen[suit] = idx
    
    for suit in SUITS:
        last_seen = suit_last_seen[suit]
        streak = total_cards if last_seen == -1 else (total_cards - 1) - last_seen
        streak_score = min(streak / 5.0, 2.0)

        actual_freq = suit_counts[suit] / total_cards if total_cards > 0 else 0.25
        freq_score = (0.25 - actual_freq) * 8.0

        historical_odds[suit].append(current_odds[suit])
        if len(historical_odds[suit]) > WINDOW_SIZE:
            historical_odds[suit].pop(0)
            
        avg_odds = sum(historical_odds[suit]) / len(historical_odds[suit]) if historical_odds[suit] else current_odds[suit]
        odds_drop = avg_odds - current_odds[suit]
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
    print("🚀 Запуск анализатора БАККАРЫ (Счет + Масти)...")
    print("Накапливаю историю завершенных игр...\n")
    
    for _ in range(3):
        fetch_finished_games_results()
        time.sleep(2)
        
    if len(history) < 5:
        print("⚠️ Мало данных. Скрипт продолжит копить историю в реальном времени.\n")

    last_prediction_id = None

    while True:
        try:
            print("="*70)
            print(f"🔄 Сканирование... (В истории {len(history)} карт Игрока)")
            
            resp = requests.get(LIST_URL, headers=HEADERS, timeout=10, proxies=NO_PROXY)
            data = resp.json()
            games = data.get("Value", [])
            
            fetch_finished_games_results()
            
            next_game = None
            for g in games:
                if g.get("SC", {}).get("I") == "Ставки до начала игры":
                    next_game = g
                    break
            
            if not next_game:
                print("⏳ Нет предстоящих игр. Жду обновления списка...")
                time.sleep(5)
                continue
                
            next_game_id = next_game.get("I")
            
            if next_game_id != last_prediction_id:
                print(f"\n🎯 Анализ следующей игры Баккары (ID: {next_game_id})")
                
                _, current_odds = fetch_game_details(next_game_id)
                
                if current_odds:
                    scores = calculate_anomaly_scores(current_odds)
                    sorted_suits = sorted(scores.items(), key=lambda x: x[1]["score"], reverse=True)
                    
                    print("\n📊 РЕЙТИНГ АНОМАЛИЙ МАСТЕЙ (Игрок):")
                    print(f"{'Масть':<10} | {'Скор':<6} | {'Задержка':<8} | {'Частота':<8} | {'Кф (Δ)':<10}")
                    print("-" * 65)
                    
                    for suit, d in sorted_suits:
                        info = SUITS[suit]
                        print(f"{info['symbol']} {info['name']:<6} | {d['score']:<6.2f} | {d['streak']:<8} | {d['freq']:<8} | {d['current_cf']} ({d['odds_drop']})")
                        
                    best_suit = sorted_suits[0][0]
                    best_data = sorted_suits[0][1]
                    
                    print("\n" + "="*70)
                    print(f"🔥 ВЕРДИКТ АЛГОРИТМА:")
                    print(f"Прогноз масти Игрока: {SUITS[best_suit]['symbol']} {SUITS[best_suit]['name']}")
                    print(f"Причина: Задержка {best_data['streak']} карт, частота {best_data['freq']}, падение кф {best_data['odds_drop']}")
                    print("="*70 + "\n")
                    
                    last_prediction_id = next_game_id
                else:
                    print("⚠️ Не удалось получить коэффициенты для предстоящей игры.")
            
            time.sleep(10)
            
        except KeyboardInterrupt:
            print("\n🛑 Остановка.")
            break
        except Exception as e:
            print(f" Критическая ошибка: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
