import os
import time
import json
import datetime
import requests
import telebot

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")

API_URL = "https://melbet-2814.pro/service-api/LiveFeed/Get1x2_VZip?sports=236&champs=2050671&count=40&gr=1521&mode=4&country=192&partner=8&getEmpty=true&virtualSports=true&noFilterBlockEvent=true"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
sent_games = set()

def fetch_data():
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"⚠️ Ошибка запроса: {e}")
    return []

def format_game_info(game):
    """Форматирует информацию об игре для Telegram"""
    try:
        game_id = game.get("I", "N/A")
        sport_name = game.get("SN", "N/A")
        display_id = game.get("DI", "N/A")
        event_counter = game.get("EC", "N/A")
        league_id = game.get("LI", "N/A")
        sport_id = game.get("SI", "N/A")
        
        text = (
            f"🎮 ИГРА #N{game_id}   Display ID: {display_id}\n"
            f"──────────────────────────────\n"
            f"📊 Информация:\n"
            f"  Спорт: {sport_name}\n"
            f"  Системные данные:\n"
            f"  Event Counter: {event_counter}\n"
            f"  League ID: {league_id}\n"
            f"  Sport ID: {sport_id}"
        )
        
        return text
    except Exception as e:
        print(f"⚠️ Ошибка форматирования: {e}")
        return None

def send_to_channel(text):
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        print("✅ Сообщение отправлено")
        return True
    except Exception as e:
        print(f" Ошибка отправки: {e}")
        return False

def main():
    print(" ЗАПУСК БОТА-МОНИТОРА API")
    print("=" * 60)
    
    send_to_channel("🟢 <b>Бот мониторинга запущен</b>")
    
    cycle = 0
    
    while True:
        try:
            cycle += 1
            print(f"\n🔄 Цикл #{cycle} - Запрос к API...")
            
            games = fetch_data()
            
            if not games:
                print("⚠️ Нет данных от API")
                time.sleep(10)
                continue
            
            print(f"📊 Получено {len(games)} игр")
            
            new_games_count = 0
            
            for game in games:
                game_id = game.get("I")
                if not game_id or game_id in sent_games:
                    continue
                
                sent_games.add(game_id)
                new_games_count += 1
                
                formatted_text = format_game_info(game)
                if formatted_text:
                    send_to_channel(formatted_text)
            
            if new_games_count > 0:
                print(f"✅ Отправлено {new_games_count} новых игр")
            
            # Очищаем старые ID (оставляем только последние 100)
            if len(sent_games) > 100:
                sent_games.clear()
                print("🗑️ Очищена история игр")
            
            time.sleep(15)
            
        except Exception as e:
            print(f"️ Ошибка в главном цикле: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(30)

if __name__ == "__main__":
    main()
