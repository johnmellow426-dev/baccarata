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

# Отслеживание уже отправленных игр
sent_games = set()

def fetch_data():
    """Получает данные из API"""
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("Value", [])
    except Exception as e:
        print(f"️ Ошибка запроса: {e}")
    return []

def format_game_info(game):
    """Форматирует информацию об игре для Telegram"""
    try:
        # Основные данные
        game_id = game.get("I", "N/A")
        sport_name = game.get("SN", "N/A")
        league_name = game.get("L", "N/A")
        
        # Время
        start_time = game.get("S", 0)
        if start_time:
            dt = datetime.datetime.fromtimestamp(start_time)
            time_str = dt.strftime("%H:%M:%S (%d.%m.%Y)")
        else:
            time_str = "N/A"
        
        # Участники
        player_name = game.get("O1", "Игрок")
        banker_name = game.get("O2", "Банкир")
        
        # Коэффициенты (делим на 100)
        player_coef = game.get("O1C", 0) / 100 if game.get("O1C") else 0
        banker_coef = game.get("O2C", 0) / 100 if game.get("O2C") else 0
        
        # IDs
        player_id = game.get("O1I", "N/A")
        banker_id = game.get("O2I", "N/A")
        
        # Статус
        market_status = game.get("MS", [0])
        status = "🟢 Активен" if market_status and market_status[0] == 0 else "🔴 Закрыт"
        
        # Дополнительные поля
        display_id = game.get("DI", "N/A")
        event_counter = game.get("EC", "N/A")
        country = game.get("CN", "World")
        
        text = (
            f"🎮 <b>ИГРА #{game_id}</b>\n"
            f"{'─' * 30}\n"
            f"📊 <b>Информация:</b>\n"
            f"  Спорт: {sport_name}\n"
            f"  Лига: {league_name}\n"
            f"  Страна: {country}\n"
            f"  Старт: {time_str}\n"
            f"  Статус: {status}\n"
            f"\n"
            f"👥 <b>Участники:</b>\n"
            f"  🔵 {player_name}\n"
            f"     ├─ ID: {player_id}\n"
            f"     └─ Коэф: {player_coef:.2f}\n"
            f"  🔴 {banker_name}\n"
            f"     ├─ ID: {banker_id}\n"
            f"     └─ Коэф: {banker_coef:.2f}\n"
            f"\n"
            f" <b>Системные данные:</b>\n"
            f"  Display ID: {display_id}\n"
            f"  Event Counter: {event_counter}\n"
            f"  League ID: {game.get('LI', 'N/A')}\n"
            f"  Sport ID: {game.get('SI', 'N/A')}\n"
        )
        
        return text
    except Exception as e:
        print(f"⚠️ Ошибка форматирования: {e}")
        return None

def send_to_channel(text):
    """Отправляет сообщение в канал"""
    try:
        bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
        print("✅ Сообщение отправлено")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

def send_raw_json(games):
    """Отправляет сырой JSON (первые 5 игр для примера)"""
    try:
        # Отправляем только первые 3 игры чтобы не спамить
        preview = games[:3] if len(games) > 3 else games
        
        json_str = json.dumps(preview, indent=2, ensure_ascii=False)
        
        # Если JSON слишком большой, отправляем как файл
        if len(json_str) > 4000:
            with open("games_data.json", "w", encoding="utf-8") as f:
                json.dump(games, f, indent=2, ensure_ascii=False)
            
            with open("games_data.json", "rb") as f:
                bot.send_document(CHANNEL_ID, f, caption="📄 Полные данные JSON (файл)")
            print("✅ JSON отправлен как файл")
        else:
            text = f"<pre>{json_str}</pre>"
            bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
            print("✅ JSON отправлен")
    except Exception as e:
        print(f"❌ Ошибка отправки JSON: {e}")

def main():
    print("🚀 ЗАПУСК БОТА-МОНИТОРА API")
    print("=" * 60)
    
    # Отправляем приветствие
    send_to_channel("🟢 <b>Бот мониторинга API запущен</b>\n\nОтслеживаю виртуальную Баккару...")
    
    last_send_time = 0
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
            
            # Отправляем сырой JSON при первом запуске и каждые 10 циклов
            if cycle == 1 or cycle % 10 == 0:
                send_raw_json(games)
            
            # Отправляем информацию о новых играх
            current_time = time.time()
            new_games_count = 0
            
            for game in games:
                game_id = game.get("I")
                if not game_id or game_id in sent_games:
                    continue
                
                # Новая игра!
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
            
            # Ждем перед следующим запросом
            time.sleep(15)
            
        except Exception as e:
            print(f"⚠️ Ошибка в главном цикле: {e}")
            import traceback
            traceback.print_exc()
            time.sleep(30)

if __name__ == "__main__":
    main()
