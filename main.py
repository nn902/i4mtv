import html
import os
import telebot
from telebot import types
import re
import time
import ssl
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote
import pg8000.dbapi

# ==============================================================================
# 🎨 БЕЗОПАСНАЯ ПОДДЕРЖКА ПАРАМЕТРА STYLE В INLINEKEYBOARDBUTTON
# ==============================================================================
_orig_inline_btn_init = types.InlineKeyboardButton.__init__

def _safe_inline_btn_init(self, *args, **kwargs):
    style = kwargs.pop("style", None)
    try:
        _orig_inline_btn_init(self, *args, **kwargs)
    except TypeError:
        _orig_inline_btn_init(self, *args)
    if style is not None:
        self.style = style

types.InlineKeyboardButton.__init__ = _safe_inline_btn_init

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GMPBot")

# Токен бота
TOKEN = os.getenv("BOT_TOKEN", "8853332126:AAFWbcYv0yURZuXcRgcK0-GGvJP3WrhpBQI")
bot = telebot.TeleBot(TOKEN)

# Список числовых ID администраторов
ADMIN_IDS = [6522383487, 7727345054]

# Состояния пользователей и админ-рассылки
user_states = {}
admin_broadcast_state = {}

# ==============================================================================
# 🗄 ПОДКЛЮЧЕНИЕ К SUPABASE (POSTGRESQL ЧЕРЕЗ PG8000 И SSL)
# ==============================================================================
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://postgres:chikamalika2382@db.jvrpptcptdkfmbvsvjgp.supabase.co:5432/postgres"
)

def get_conn():
    """Создает защищенное SSL-подключение к PostgreSQL / Supabase"""
    db_info = urlparse(DATABASE_URL)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    return pg8000.dbapi.connect(
        user=db_info.username or "postgres",
        password=unquote(db_info.password) if db_info.password else None,
        host=db_info.hostname,
        port=db_info.port or 5432,
        database=db_info.path[1:] if db_info.path else "postgres",
        ssl_context=ctx
    )

def init_db():
    """Создаёт таблицы, последовательность заказов с 113 и недостающие колонки при старте"""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        
        # 1. Таблица пользователей
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                first_name VARCHAR(255),
                username VARCHAR(255),
                total_orders INT DEFAULT 0,
                total_gmp_received INT DEFAULT 0,
                total_rub_spent DOUBLE PRECISION DEFAULT 0.0,
                referrer_id BIGINT,
                ref_bonus_until TIMESTAMPTZ,
                welcome_bonus_until TIMESTAMPTZ,
                welcome_bonus_used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255);")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_orders INT DEFAULT 0;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_gmp_received INT DEFAULT 0;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_rub_spent DOUBLE PRECISION DEFAULT 0.0;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_bonus_until TIMESTAMPTZ;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS welcome_bonus_until TIMESTAMPTZ;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS welcome_bonus_used BOOLEAN DEFAULT FALSE;")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")

        # 2. Таблица истории заказов / заявок (настраиваем последовательность со 113)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                rub_amount DOUBLE PRECISION NOT NULL,
                gmp_amount INT NOT NULL,
                status VARCHAR(50) DEFAULT 'pending',
                file_id TEXT,
                bonus_desc TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS file_id TEXT;")
        cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'pending';")
        cursor.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS bonus_desc TEXT;")

        # Установка стартового значения sequence для order_id со 113, если таблица пустая
        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM orders) THEN
                    EXECUTE 'ALTER SEQUENCE orders_order_id_seq RESTART WITH 113';
                END IF;
            END $$;
        """)

        conn.commit()
        cursor.close()
        conn.close()
        logger.info("✅ База данных Supabase успешно инициализирована (заказы со 113).")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации базы данных: {e}", exc_info=True)

def upsert_user(user_id: int, first_name: str, username: str, referrer_id: int = None):
    """
    Регистрирует или обновляет пользователя.
    Если перешел по рефке (referrer_id):
    1. Привязываем referrer_id (даже если пользователь уже был в базе, но еще не делал заказов).
    2. Выдаем или обновляем welcome_bonus_until (+24 часа с текущего момента) с бонусом +5%.
    3. Пригласившему (referrer_id) даем реф-бонус +24 часа к ref_bonus_until.
    """
    try:
        conn = get_conn()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id, referrer_id, total_orders FROM users WHERE user_id = %s;", (user_id,))
        exists = cursor.fetchone()
        
        is_new = False
        ref_extended = False

        if not exists:
            is_new = True
            valid_referrer = None
            if referrer_id and referrer_id != user_id:
                cursor.execute("SELECT user_id FROM users WHERE user_id = %s;", (referrer_id,))
                if cursor.fetchone():
                    valid_referrer = referrer_id
                    cursor.execute("""
                        UPDATE users
                        SET ref_bonus_until = CASE 
                            WHEN ref_bonus_until > CURRENT_TIMESTAMP THEN ref_bonus_until + INTERVAL '24 hours'
                            ELSE CURRENT_TIMESTAMP + INTERVAL '24 hours'
                        END
                        WHERE user_id = %s;
                    """, (referrer_id,))
                    ref_extended = True

            cursor.execute("""
                INSERT INTO users (
                    user_id, first_name, username, referrer_id, 
                    welcome_bonus_until, welcome_bonus_used, created_at
                )
                VALUES (
                    %s, %s, %s, %s, 
                    CURRENT_TIMESTAMP + INTERVAL '24 hours', FALSE, CURRENT_TIMESTAMP
                );
            """, (user_id, first_name, username, valid_referrer))
        else:
            current_ref = exists[1]
            total_orders = exists[2] or 0
            
            # Привязываем реферера если пользователь еще не делал заказов
            if referrer_id and referrer_id != user_id and current_ref is None and total_orders == 0:
                cursor.execute("SELECT user_id FROM users WHERE user_id = %s;", (referrer_id,))
                if cursor.fetchone():
                    cursor.execute("""
                        UPDATE users
                        SET referrer_id = %s,
                            welcome_bonus_until = CURRENT_TIMESTAMP + INTERVAL '24 hours',
                            welcome_bonus_used = FALSE
                        WHERE user_id = %s;
                    """, (referrer_id, user_id))
                    
                    cursor.execute("""
                        UPDATE users
                        SET ref_bonus_until = CASE 
                            WHEN ref_bonus_until > CURRENT_TIMESTAMP THEN ref_bonus_until + INTERVAL '24 hours'
                            ELSE CURRENT_TIMESTAMP + INTERVAL '24 hours'
                        END
                        WHERE user_id = %s;
                    """, (referrer_id,))
                    ref_extended = True

            cursor.execute("""
                UPDATE users 
                SET first_name = %s, username = %s
                WHERE user_id = %s;
            """, (first_name, username, user_id))

        conn.commit()
        cursor.close()
        conn.close()
        return is_new, ref_extended
    except Exception as e:
        logger.error(f"Ошибка upsert_user: {e}")
        return False, False

def get_user_bonuses(user_id: int) -> dict:
    """
    Проверяет состояние бонусов пользователя через БД PostgreSQL (без ошибок таймзон).
    Возвращает флаги, секунды и строки таймеров чч:мм:сс.
    """
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                referrer_id,
                welcome_bonus_used, 
                total_orders, 
                COALESCE(EXTRACT(EPOCH FROM (welcome_bonus_until - CURRENT_TIMESTAMP))::INT, 0) AS welcome_sec_left,
                COALESCE(EXTRACT(EPOCH FROM (ref_bonus_until - CURRENT_TIMESTAMP))::INT, 0) AS ref_sec_left,
                (SELECT COUNT(*) FROM users WHERE referrer_id = %s) as refs_count
            FROM users WHERE user_id = %s;
        """, (user_id, user_id))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if not row:
            return {
                "welcome_5": False, "welcome_seconds_left": 0, "welcome_time_str": "00:00:00",
                "ref_2": False, "ref_seconds_left": 0, "ref_time_str": "00:00:00", 
                "refs_count": 0, "has_referrer": False
            }

        ref_id, welcome_used, total_orders, welcome_sec, ref_sec, refs_count = row

        # 1. Приветственный бонус / бонус по рефке (+5% на 24 часа)
        welcome_5 = False
        welcome_time_str = "00:00:00"
        if not welcome_used and (total_orders or 0) == 0 and welcome_sec > 0:
            welcome_5 = True
            h = welcome_sec // 3600
            m = (welcome_sec % 3600) // 60
            s = welcome_sec % 60
            welcome_time_str = f"{h:02d}:{m:02d}:{s:02d}"

        # 2. Реферальный бонус (+2%)
        ref_2 = False
        ref_time_str = "00:00:00"
        if ref_sec > 0:
            ref_2 = True
            h = ref_sec // 3600
            m = (ref_sec % 3600) // 60
            s = ref_sec % 60
            ref_time_str = f"{h:02d}:{m:02d}:{s:02d}"

        return {
            "welcome_5": welcome_5,
            "welcome_seconds_left": max(welcome_sec, 0),
            "welcome_time_str": welcome_time_str,
            "ref_2": ref_2,
            "ref_seconds_left": max(ref_sec, 0),
            "ref_time_str": ref_time_str,
            "refs_count": refs_count or 0,
            "has_referrer": bool(ref_id)
        }
    except Exception as e:
        logger.error(f"Ошибка get_user_bonuses: {e}")
        return {
            "welcome_5": False, "welcome_seconds_left": 0, "welcome_time_str": "00:00:00",
            "ref_2": False, "ref_seconds_left": 0, "ref_time_str": "00:00:00", 
            "refs_count": 0, "has_referrer": False
        }

def get_user_stats(user_id: int) -> dict:
    """Возвращает статистику пользователя из БД"""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT total_orders, total_gmp_received, total_rub_spent
            FROM users WHERE user_id = %s
        """, (user_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return {
                "total_orders": row[0] or 0,
                "total_gmp": row[1] or 0,
                "total_rub": row[2] or 0.0
            }
        return {"total_orders": 0, "total_gmp": 0, "total_rub": 0.0}
    except Exception as e:
        logger.error(f"Ошибка get_user_stats: {e}")
        return {"total_orders": 0, "total_gmp": 0, "total_rub": 0.0}

def create_pending_order(user_id: int, rub: float, gmp: int, file_id: str = None, bonus_desc: str = None) -> int:
    """Записывает заказ со статусом 'pending' и возвращает order_id"""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO orders (user_id, rub_amount, gmp_amount, file_id, bonus_desc, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            RETURNING order_id;
        """, (user_id, rub, gmp, file_id, bonus_desc))

        order_id = cursor.fetchone()[0]

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Заявка #{order_id} создана (pending): user={user_id}, rub={rub}, gmp={gmp}, bonus={bonus_desc}")
        return order_id
    except Exception as e:
        logger.error(f"Ошибка create_pending_order: {e}")
        return None

def approve_order(order_id: int) -> dict:
    """Подтверждает заказ: меняет статус на 'approved', обновляет статистику и возвращает детали для отзыва"""
    try:
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT user_id, rub_amount, gmp_amount, status, bonus_desc FROM orders WHERE order_id = %s", (order_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return {"success": False, "reason": "Заказ не найден"}

        user_id, rub, gmp, status, bonus_desc = row[0], row[1], row[2], row[3], row[4]

        if status != "pending":
            cursor.close()
            conn.close()
            return {"success": False, "reason": f"Заявка уже обработана (статус: {status})"}

        cursor.execute("UPDATE orders SET status = 'approved' WHERE order_id = %s", (order_id,))

        cursor.execute("""
            UPDATE users
            SET total_orders = COALESCE(total_orders, 0) + 1,
                total_gmp_received = COALESCE(total_gmp_received, 0) + %s,
                total_rub_spent = COALESCE(total_rub_spent, 0) + %s,
                welcome_bonus_used = TRUE
            WHERE user_id = %s
        """, (gmp, rub, user_id))

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Заявка #{order_id} подтверждена админом. Статистика у user={user_id} обновлена.")
        return {"success": True, "user_id": user_id, "rub": rub, "gmp": gmp, "bonus_desc": bonus_desc}
    except Exception as e:
        logger.error(f"Ошибка approve_order: {e}")
        return {"success": False, "reason": str(e)}

def reject_order(order_id: int) -> dict:
    """Отклоняет заказ: меняет статус на 'rejected' без изменения статистики"""
    try:
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT user_id, rub_amount, gmp_amount, status FROM orders WHERE order_id = %s", (order_id,))
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return {"success": False, "reason": "Заказ не найден"}

        user_id, rub, gmp, status = row[0], row[1], row[2], row[3]

        if status != "pending":
            cursor.close()
            conn.close()
            return {"success": False, "reason": f"Заявка уже обработана (статус: {status})"}

        cursor.execute("UPDATE orders SET status = 'rejected' WHERE order_id = %s", (order_id,))

        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Заявка #{order_id} отклонена админом.")
        return {"success": True, "user_id": user_id, "rub": rub, "gmp": gmp}
    except Exception as e:
        logger.error(f"Ошибка reject_order: {e}")
        return {"success": False, "reason": str(e)}

def get_user_orders_history(user_id: int, limit: int = 5) -> list:
    """Возвращает последние заказы пользователя"""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT order_id, rub_amount, gmp_amount, status, TO_CHAR(created_at, 'DD.MM.YYYY HH24:MI')
            FROM orders
            WHERE user_id = %s
            ORDER BY order_id DESC
            LIMIT %s
        """, (user_id, limit))
        rows = cursor.fetchall() or []
        cursor.close()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Ошибка get_user_orders_history: {e}")
        return []

# ==============================================================================
# 📊 СТАТИСТИКА И РАССЫЛКА ДЛЯ АДМИНКИ
# ==============================================================================

def get_admin_detailed_stats() -> dict:
    """Собирает аналитику для админки"""
    try:
        conn = get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users;")
        total_users = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE;")
        today_users = cursor.fetchone()[0] or 0

        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(gmp_amount), 0), COALESCE(SUM(rub_amount), 0.0)
            FROM orders WHERE status = 'approved';
        """)
        row = cursor.fetchone()
        approved_orders = row[0] or 0
        total_gmp = row[1] or 0
        total_rub = row[2] or 0.0

        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending';")
        pending_orders = cursor.fetchone()[0] or 0

        cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL;")
        total_invited = cursor.fetchone()[0] or 0

        cursor.close()
        conn.close()
        return {
            "total_users": total_users,
            "today_users": today_users,
            "approved_orders": approved_orders,
            "total_gmp": total_gmp,
            "total_rub": total_rub,
            "pending_orders": pending_orders,
            "total_invited": total_invited
        }
    except Exception as e:
        logger.error(f"Ошибка get_admin_detailed_stats: {e}")
        return {"total_users": 0, "today_users": 0, "approved_orders": 0, "total_gmp": 0, "total_rub": 0.0, "pending_orders": 0, "total_invited": 0}

def get_admin_referral_stats() -> dict:
    """Собирает общую статистику рефералов для админки"""
    try:
        conn = get_conn()
        cursor = conn.cursor()

        # Общее количество приглашенных пользователей
        cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL;")
        total_invited = cursor.fetchone()[0] or 0

        # Топ рефереров с подсчетом приглашенных
        cursor.execute("""
            SELECT u.user_id, u.first_name, u.username, COUNT(r.user_id) AS ref_count
            FROM users u
            JOIN users r ON r.referrer_id = u.user_id
            GROUP BY u.user_id, u.first_name, u.username
            ORDER BY ref_count DESC
            LIMIT 15;
        """)
        top_referrers = cursor.fetchall() or []

        cursor.close()
        conn.close()
        return {
            "total_invited": total_invited,
            "top_referrers": top_referrers
        }
    except Exception as e:
        logger.error(f"Ошибка get_admin_referral_stats: {e}")
        return {"total_invited": 0, "top_referrers": []}

def get_all_user_ids() -> list:
    """Получает ID всех пользователей для рассылки"""
    try:
        conn = get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users;")
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [r[0] for r in rows]
    except Exception as e:
        logger.error(f"Ошибка get_all_user_ids: {e}")
        return []

def parse_broadcast_buttons(text: str):
    """Парсинг инлайн-кнопок из текста: Кнопка | https://ссылка"""
    if not text or text.lower().strip() in ["пропустить", "нет", "-", "без кнопок"]:
        return None
    markup = types.InlineKeyboardMarkup()
    lines = text.strip().split("\n")
    for line in lines:
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 2:
            row_buttons = []
            for i in range(0, len(parts), 2):
                if i + 1 < len(parts):
                    btn_text = parts[i]
                    btn_url = parts[i + 1]
                    if btn_url.startswith(("http://", "https://", "tg://")):
                        row_buttons.append(types.InlineKeyboardButton(text=btn_text, url=btn_url, style="primary"))
            if row_buttons:
                markup.row(*row_buttons)
    return markup if len(markup.keyboard) > 0 else None

# === ПРЕМИУМ ЭМОДЗИ ===
EMOJI_CARD     = '<tg-emoji emoji-id="5470060791883374114">🪪</tg-emoji>'
EMOJI_WRITE    = '<tg-emoji emoji-id="5375296873982604963">✍️</tg-emoji>'
EMOJI_MONEY    = '<tg-emoji emoji-id="5373001317042101552">💰</tg-emoji>'
EMOJI_CHART_UP = '<tg-emoji emoji-id="5431577498364158238">📈</tg-emoji>'
EMOJI_CHART    = '<tg-emoji emoji-id="5458799228719472718">📊</tg-emoji>'
EMOJI_STAR_BIG = '<tg-emoji emoji-id="5370869711888194012">🌟</tg-emoji>'
EMOJI_INVADER  = '<tg-emoji emoji-id="5805528485371843693">👾</tg-emoji>'
EMOJI_LIGHT    = '<tg-emoji emoji-id="5427168083074628963">⚡️</tg-emoji>'
EMOJI_DIAMOND  = '<tg-emoji emoji-id="6041761071155386269">💎</tg-emoji>'
EMOJI_ROCKET   = '<tg-emoji emoji-id="5805630830147540745">🚀</tg-emoji>'
EMOJI_SPARKLES = '<tg-emoji emoji-id="5190498849440931467">✨</tg-emoji>'
EMOJI_DEV      = '<tg-emoji emoji-id="5373012449597335010">👨‍💻</tg-emoji>'
EMOJI_USER     = '<tg-emoji emoji-id="5841276284155467413">👤</tg-emoji>'
EMOJI_NAME_TAG = '<tg-emoji emoji-id="5334882760735598374">🔤</tg-emoji>'
EMOJI_MEMO     = '<tg-emoji emoji-id="5431499171045581032">📝</tg-emoji>'
EMOJI_GIFT     = '<tg-emoji emoji-id="5203996991054432397">🎁</tg-emoji>'
EMOJI_CHANNEL  = '<tg-emoji emoji-id="5452002597592382164">📣</tg-emoji>'
EMOJI_STAR     = '<tg-emoji emoji-id="5438496463044752972">⭐️</tg-emoji>'
EMOJI_SUNFLOWER= '<tg-emoji emoji-id="5211226911367257670">🌻</tg-emoji>'
EMOJI_MUSHROOM = '<tg-emoji emoji-id="5197546963788126956">🍄</tg-emoji>'
EMOJI_KISS     = '<tg-emoji emoji-id="5253922906378881072">💋</tg-emoji>'
EMOJI_CLIP     = '<tg-emoji emoji-id="5197269100878907942">📋</tg-emoji>'
EMOJI_WARN     = '<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji>'
EMOJI_PHONE    = '<tg-emoji emoji-id="5407025283456835913">📱</tg-emoji>'
EMOJI_CAMERA   = '<tg-emoji emoji-id="5235837920081887219">📸</tg-emoji>'
EMOJI_CHECK    = '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>'
EMOJI_CHECC    = '<tg-emoji emoji-id="5465198403573012261">👉</tg-emoji>'
EMOJI_CSECK    = '<tg-emoji emoji-id="5454093885888281840">⚠️</tg-emoji>'

# Текст главного меню с блоком таймера сгорания сверху
START_TEXT = (
    f"{{bonus_block}}"
    f"{EMOJI_STAR} Здравствуй, {{name}}! {EMOJI_STAR}\n\n"
    f"{EMOJI_INVADER} Ты попал в бота-продавца GMP.\n"
    f"{EMOJI_LIGHT} Здесь ты можешь быстро приобрести GMP по выгодному курсу!\n\n"
    f"{EMOJI_DIAMOND} Наши преимущества:\n"
    f"• {EMOJI_ROCKET} Быстрая выдача\n"
    f"• {EMOJI_CARD} Удобные способы оплаты\n\n"
    f"{EMOJI_SPARKLES} Выбери что тебя интересует:"
)

# === КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ ===

def main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_buy = types.InlineKeyboardButton("💸 Купить GMP", callback_data="buy_gmp", style="success")
    btn_channel = types.InlineKeyboardButton("📢 Канал", callback_data="open_channel", style="primary")
    btn_support = types.InlineKeyboardButton("🆘 Поддержка", callback_data="open_support", style="primary")
    btn_reviews = types.InlineKeyboardButton("⭐ Отзывы", callback_data="open_reviews", style="primary")
    btn_ref = types.InlineKeyboardButton("👥 Реферальная система (+2%)", callback_data="open_referral", style="primary")
    btn_profile = types.InlineKeyboardButton("👤 Мой профиль", callback_data="open_profile", style="primary")
    btn_info = types.InlineKeyboardButton("🌻 Информация о выводе", callback_data="info_withdraw", style="danger")
    btn_privacy = types.InlineKeyboardButton("🔐 Политика конфиденциальности", url="https://telegra.ph/Politika-konfidencialnosti-06-21-31")
    btn_terms = types.InlineKeyboardButton("📜 Пользовательское соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19")
    markup.add(btn_buy)
    markup.add(btn_ref)
    markup.add(btn_channel, btn_support)
    markup.add(btn_reviews, btn_profile)
    markup.add(btn_info)
    markup.add(btn_privacy)
    markup.add(btn_terms)
    return markup

def channel_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("📢 Перейти в канал", url="https://t.me/Kitaez_com", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def reviews_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("⭐ Посмотреть отзывы", url="https://t.me/OTZIVI_KRABIKA_TB", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def order_success_keyboard():
    """Клавиатура после успешного выполнения заказа админом"""
    markup = types.InlineKeyboardMarkup(row_width=1)
    btn_reviews = types.InlineKeyboardButton("⭐ Посмотреть отзывы", url="https://t.me/OTZIVI_KRABIKA_TB", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main")
    markup.add(btn_reviews, btn_back)
    return markup

def support_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("👤 Написать @KRABIK_TB", url="https://t.me/KRABIK_TB", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def profile_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_orders = types.InlineKeyboardButton("📋 Все мои заказы", callback_data="my_orders_stub", style="success")
    btn_ref = types.InlineKeyboardButton("👥 Рефералы", callback_data="open_referral", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_orders, btn_ref)
    markup.add(btn_back)
    return markup

def referral_keyboard(bot_username: str, user_id: int):
    markup = types.InlineKeyboardMarkup()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={ref_link}&text=Купи%20GMP%20по%20самому%20выгодному%20курсу!%20Получи%20бонус%205%%20на%20первый%20заказ!"
    btn_share = types.InlineKeyboardButton("🔗 Поделиться ссылкой", url=share_url, style="success")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_share)
    markup.add(btn_back)
    return markup

def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    markup.add(btn_back)
    return markup

# === КЛАВИАТУРЫ АДМИНКИ ===

def admin_main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_stats = types.InlineKeyboardButton("📊 Статистика", callback_data="admin_stats", style="primary")
    btn_broadcast = types.InlineKeyboardButton("📢 Рассылка", callback_data="admin_broadcast_start", style="primary")
    btn_referrals = types.InlineKeyboardButton("👥 Рефералы", callback_data="admin_referrals", style="success")
    btn_close = types.InlineKeyboardButton("❌ Закрыть", callback_data="admin_close", style="danger")
    markup.add(btn_stats, btn_broadcast)
    markup.add(btn_referrals)
    markup.add(btn_close)
    return markup

def broadcast_confirm_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_send = types.InlineKeyboardButton("🚀 Запустить рассылку", callback_data="admin_bc_confirm", style="success")
    btn_cancel = types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel", style="danger")
    markup.add(btn_send, btn_cancel)
    return markup

def skip_buttons_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_skip = types.InlineKeyboardButton("⏩ Пропустить (без кнопок)", callback_data="admin_bc_skip_buttons", style="primary")
    btn_cancel = types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel", style="danger")
    markup.add(btn_skip)
    markup.add(btn_cancel)
    return markup

def is_probable_amount(text):
    if not text or text.startswith('/'):
        return False
    txt = text.lower().replace(" ", "")
    cleaned = re.sub(r'[₽ррууббlluubbr]', '', txt)
    cleaned = cleaned.replace(',', '.')
    try:
        val = float(cleaned)
        return 50 <= val <= 50000
    except ValueError:
        return False

def build_bonus_countdown_block(bonuses: dict) -> str:
    """Формирует блок с таймером сгорания бонусов и призывом к покупке для сообщения /start"""
    parts = []
    
    # 1. Приветственный бонус / бонус по рефке (+5%)
    if bonuses["welcome_5"]:
        time_str = bonuses["welcome_time_str"]
        ref_note = "по реферальной ссылке" if bonuses["has_referrer"] else "на 1-й заказ"
        parts.append(
            f"🔥 <b>ВНИМАНИЕ! Бонус +5% GMP ({ref_note}) сгорит через: <code>{time_str}</code></b>\n"
            f"⚡️ <i>Успейте оформить заказ прямо сейчас по самой выгодной цене! Нажмите «💸 Купить GMP» ниже.</i>"
        )
        
    # 2. Реферальный бонус (+2%)
    if bonuses["ref_2"]:
        time_str = bonuses["ref_time_str"]
        parts.append(
            f"⏰ <b>Реферальный бонус +2% GMP сгорит через: <code>{time_str}</code></b>\n"
            f"🚀 <i>Не упустите повышенный процент! Оплачивайте заказ быстрее.</i>"
        )
        
    if parts:
        return "\n\n".join(parts) + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    return ""


# ==============================================================================
# 👑 КОМАНДЫ АДМИНИСТРАТОРА
# ==============================================================================

@bot.message_handler(commands=['admin', 'adm'])
def handle_admin_command(message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        return

    text = (
        "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА GMP</b>\n"
        "• • • • • • • • • • • • •\n"
        "Выберите нужное действие ниже:"
    )
    bot.send_message(message.chat.id, text, reply_markup=admin_main_keyboard(), parse_mode="HTML")


# ==============================================================================
# ОБРАБОТКА CALLBACK КНОПОК
# ==============================================================================

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    user_id = call.from_user.id
    name = call.from_user.first_name if call.from_user.first_name else "Пользователь"
    username = f"@{call.from_user.username}" if call.from_user.username else "Не установлен"
    
    upsert_user(user_id, name, username)

    # --- ДЕЙСТВИЯ АДМИН-ПАНЕЛИ ---
    if call.data.startswith("admin_"):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, text="❌ Доступ запрещен!", show_alert=True)
            return

        if call.data == "admin_stats":
            bot.answer_callback_query(call.id)
            s = get_admin_detailed_stats()
            stats_text = (
                "📊 <b>СТАТИСТИКА БОТА И ПРОДАЖ</b>\n"
                "• • • • • • • • • • • •\n\n"
                f"👥 <b>Пользователи:</b>\n"
                f"• Всего пользователей: <b>{s['total_users']:,}</b> чел.\n"
                f"• Пришло по реф. ссылкам: <b>{s['total_invited']:,}</b> чел.\n"
                f"• Пришло за сегодня: <b>{s['today_users']:,}</b> чел.\n\n"
                f"💰 <b>Заказы и GMP:</b>\n"
                f"• Успешных заказов: <b>{s['approved_orders']:,}</b>\n"
                f"• Выдано GMP: <b>{s['total_gmp']:,} GMP</b>\n"
                f"• Всего заработано: <b>{s['total_rub']:,.2f} ₽</b>\n"
                f"• Заявок в очереди: <b>{s['pending_orders']}</b> шт.\n\n"
                f"🕒 <i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats", style="primary"),
                types.InlineKeyboardButton("🔙 Назад", callback_data="admin_menu", style="danger")
            )
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=stats_text, reply_markup=markup, parse_mode="HTML")

        elif call.data == "admin_referrals":
            bot.answer_callback_query(call.id)
            ref_data = get_admin_referral_stats()
            total_inv = ref_data["total_invited"]
            top_list = ref_data["top_referrers"]

            ref_text = (
                f"👥 <b>СТАТИСТИКА РЕФЕРАЛЬНОЙ СИСТЕМЫ</b>\n"
                f"• • • • • • • • • • • • •\n\n"
                f"🔗 Всего пользователей, пришедших по приглашениям: <b>{total_inv}</b> чел.\n\n"
                f"🏆 <b>ТОП РЕФЕРЕРОВ (кто больше всех пригласил):</b>\n"
            )

            if not top_list:
                ref_text += "<i>Пока никто не приглашал пользователей по реф. ссылкам</i>\n"
            else:
                for idx, (uid, fname, uname, count) in enumerate(top_list, 1):
                    user_disp = f"@{uname.replace('@', '')}" if uname and uname != "Не установлен" else (fname or f"ID: {uid}")
                    ref_text += f"<b>{idx}.</b> {user_disp} (<code>{uid}</code>) — <b>{count}</b> чел.\n"

            ref_text += f"\n🕒 <i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>"

            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_referrals", style="primary"),
                types.InlineKeyboardButton("🔙 Назад", callback_data="admin_menu", style="danger")
            )
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=ref_text, reply_markup=markup, parse_mode="HTML")

        elif call.data == "admin_menu":
            bot.answer_callback_query(call.id)
            text = "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА GMP</b>\n━━━━━━━━━━━━━━━━━━━━\nВыберите нужное действие ниже:"
            bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=text, reply_markup=admin_main_keyboard(), parse_mode="HTML")

        elif call.data == "admin_close":
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

        elif call.data == "admin_broadcast_start":
            bot.answer_callback_query(call.id)
            admin_broadcast_state[user_id] = {"step": "waiting_content"}
            cancel_kb = types.InlineKeyboardMarkup()
            cancel_kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel", style="danger"))
            bot.send_message(
                call.message.chat.id,
                "📢 <b>СОЗДАНИЕ РАССЫЛКИ</b>\n\n"
                "Отправьте сообщение (текст, фото или видео).\n"
                "<i>Все стили шрифтов, форматирование и премиум-эмодзи сохранятся полностью!</i>",
                reply_markup=cancel_kb,
                parse_mode="HTML"
            )

        elif call.data == "admin_bc_skip_buttons":
            bot.answer_callback_query(call.id)
            state = admin_broadcast_state.get(user_id)
            if not state or state.get("step") != "waiting_buttons":
                return
            state["reply_markup"] = None
            state["step"] = "confirm"
            bot.send_message(call.message.chat.id, "👀 <b>Предпросмотр сообщения для рассылки:</b>", parse_mode="HTML")
            bot.copy_message(chat_id=call.message.chat.id, from_chat_id=state["chat_id"], message_id=state["message_id"])
            total_users = len(get_all_user_ids())
            bot.send_message(call.message.chat.id, f"Готово к отправке!\n👥 Получателей: <b>{total_users}</b>\n\nНачать рассылку?", reply_markup=broadcast_confirm_keyboard(), parse_mode="HTML")

        elif call.data == "admin_bc_cancel":
            bot.answer_callback_query(call.id, text="Рассылка отменена.")
            admin_broadcast_state.pop(user_id, None)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass

        elif call.data == "admin_bc_confirm":
            bot.answer_callback_query(call.id)
            state = admin_broadcast_state.pop(user_id, None)
            if not state:
                bot.send_message(call.message.chat.id, "❌ Данные рассылки устарели.")
                return

            src_chat_id = state["chat_id"]
            src_msg_id = state["message_id"]
            kb = state.get("reply_markup")
            user_ids = get_all_user_ids()
            total = len(user_ids)

            bot.send_message(call.message.chat.id, f"🚀 <b>Рассылка запущена...</b>\nПользователей: <b>{total}</b>", parse_mode="HTML")

            success_count = 0
            fail_count = 0

            for uid in user_ids:
                try:
                    bot.copy_message(chat_id=uid, from_chat_id=src_chat_id, message_id=src_msg_id, reply_markup=kb)
                    success_count += 1
                except Exception:
                    fail_count += 1
                time.sleep(0.04)

            bot.send_message(
                call.message.chat.id,
                f"✅ <b>Рассылка завершена!</b>\n\n"
                f"• Доставлено: <b>{success_count}</b>\n"
                f"• Ошибок / Заблокировано: <b>{fail_count}</b>\n"
                f"• Всего пользователей: <b>{total}</b>",
                reply_markup=admin_main_keyboard(),
                parse_mode="HTML"
            )
        return

    # --- ПРОВЕРКА ЗАЯВОК АДМИНИСТРАТОРОМ ---
    if call.data.startswith("adm_approve_") or call.data.startswith("adm_reject_"):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, text="❌ Недостаточно прав!", show_alert=True)
            return

        is_approve = call.data.startswith("adm_approve_")
        order_id = int(call.data.split("_")[-1])

        if is_approve:
            res = approve_order(order_id)
            if not res["success"]:
                bot.answer_callback_query(call.id, text=f"⚠️ {res['reason']}", show_alert=True)
                return

            bot.answer_callback_query(call.id, text=f"✅ Заявка #{order_id} подтверждена!", show_alert=True)
            admin_name = call.from_user.first_name or "Админ"
            new_text = f"✅ <b>Заявка #{order_id} ПОДТВЕРЖДЕНА</b>\nПринял админ: {admin_name}\n\n"
            if call.message.caption:
                new_text += call.message.caption
            elif call.message.text:
                new_text += call.message.text

            try:
                if call.message.photo or call.message.document:
                    bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=new_text, parse_mode="HTML")
                else:
                    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=new_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка редактирования админ-сообщения: {e}")

            target_user = res["user_id"]
            bonus_info = f"\n<i>Учтен бонус: {res.get('bonus_desc')}</i>" if res.get('bonus_desc') else ""

            user_msg = (
                f"🎉 Заказ #{order_id} успешно выполнен!\n\n"
                f"💎 Вам начислено: {res['gmp']:,} GMP{bonus_info}\n\n"
                f"Спасибо за доверие и приятной игры! 🚀\n\n"
                f"<blockquote>🌟 Понравился сервис? \n\n"
                f"Нажмите «Ответить» (Reply) на это сообщение со своими впечатлениями и тегом @KRABIK_TB — ваш отзыв сразу попадет на Канал отзывов! 💬</blockquote>"
            )
            try:
                bot.send_message(target_user, user_msg, reply_markup=order_success_keyboard(), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление пользователю {target_user}: {e}")

        else:
            res = reject_order(order_id)
            if not res["success"]:
                bot.answer_callback_query(call.id, text=f"⚠️ {res['reason']}", show_alert=True)
                return

            bot.answer_callback_query(call.id, text=f"❌ Заявка #{order_id} отклонена!", show_alert=True)
            admin_name = call.from_user.first_name or "Админ"
            new_text = f"❌ <b>Заявка #{order_id} ОТКЛОНЕНА</b>\nОтклонил админ: {admin_name}\n\n"
            if call.message.caption:
                new_text += call.message.caption
            elif call.message.text:
                new_text += call.message.text

            try:
                if call.message.photo or call.message.document:
                    bot.edit_message_caption(chat_id=call.message.chat.id, message_id=call.message.message_id, caption=new_text, parse_mode="HTML")
                else:
                    bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=new_text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Ошибка редактирования админ-сообщения: {e}")

            target_user = res["user_id"]
            user_msg = (
                f"{EMOJI_CSECK} <b>ВАША ЗАЯВКА ОТКЛОНЕНА</b>\n\n"
                f"Администратор не подтвердил ваш платеж #{order_id}.\n"
                f"Возможные причины:\n"
                f"• Чек не читаем или не отправлен перевод\n"
                f"• Сумма перевода не совпадает\n"
                f"• Оплата не поступила на счет\n"
                f"• Перевод без обязательного описания\n\n"
                f"Если произошла ошибка, напишите в тех.поддержку {EMOJI_DEV} @KRABIK_TB"
            )
            try:
                bot.send_message(target_user, user_msg, reply_markup=support_keyboard(), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить отклонение пользователю {target_user}: {e}")
        return

    # --- ОБЫЧНЫЕ КНОПКИ ПОЛЬЗОВАТЕЛЯ ---
    if call.data == "back_to_main":
        bot.answer_callback_query(call.id)
        user_states[user_id] = None
        bonuses = get_user_bonuses(user_id)
        bonus_block = build_bonus_countdown_block(bonuses)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=START_TEXT.format(name=name, bonus_block=bonus_block),
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        
    elif call.data == "open_channel":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{EMOJI_CHANNEL} Наш официальный канал:",
            reply_markup=channel_keyboard(),
            parse_mode="HTML"
        )
        
    elif call.data == "open_reviews":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{EMOJI_STAR} Отзывы наших довольных клиентов:",
            reply_markup=reviews_keyboard(),
            parse_mode="HTML"
        )
        
    elif call.data == "open_support":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=f"{EMOJI_DEV} Служба поддержки:\n{EMOJI_USER} @KRABIK_TB",
            reply_markup=support_keyboard(),
            parse_mode="HTML"
        )

    elif call.data == "open_referral":
        bot.answer_callback_query(call.id)
        bonuses = get_user_bonuses(user_id)
        bot_info = bot.get_me()
        bot_user = bot_info.username or "GMPBot"
        ref_link = f"https://t.me/{bot_user}?start=ref_{user_id}"

        status_text = (
            f"🟢 <b>АКТИВЕН (+2% GMP)</b> — осталось <b>{bonuses['ref_time_str']}</b>"
            if bonuses["ref_2"] else
            "⚪️ <i>Не активен (пригласите друга для активации)</i>"
        )

        ref_text = (
            f"👥 <b>РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n"
            f"• • • • • • • • • • • • •\n\n"
            f"Приглашай друзей и получай <b>+2% к каждому заказу GMP</b>!\n\n"
            f"⏳ <b>Как работает бонус времени:</b>\n"
            f"• 1 друг = бонус на <b>24 часа</b>\n"
            f"• 2 друга = бонус на <b>48 часов</b>\n"
            f"• Каждый следующий друг продлевает бонус ещё на <b>+24 часа</b>!\n\n"
            f"🎁 <i>Твой друг при переходе по ссылке получает бонус <b>+5% GMP на первый заказ</b>!</i>\n\n"
            f"📊 <b>Твоя статистика:</b>\n"
            f"• Приглашено друзей: <b>{bonuses['refs_count']}</b> чел.\n"
            f"• Статус бонуса: {status_text}\n\n"
            f"🔗 <b>Твоя реферальная ссылка:</b>\n"
            f"<code>{ref_link}</code>"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=ref_text,
            reply_markup=referral_keyboard(bot_user, user_id),
            parse_mode="HTML"
        )
        
    elif call.data in ["profile", "open_profile"]:
        bot.answer_callback_query(call.id)
        stats = get_user_stats(user_id)
        bonuses = get_user_bonuses(user_id)
        
        bonus_status = []
        if bonuses["welcome_5"]:
            bonus_status.append(f"🎁 Бонус +5% (осталось {bonuses['welcome_time_str']})")
        if bonuses["ref_2"]:
            bonus_status.append(f"👥 Реферальный +2% (осталось {bonuses['ref_time_str']})")
        bonus_str = ", ".join(bonus_status) if bonus_status else "нет активных"

        profile_text = (
            f"{EMOJI_USER} <b>МОЙ ПРОФИЛЬ</b>\n"
            f"{EMOJI_CARD} ID: <code>{user_id}</code>\n"
            f"{EMOJI_NAME_TAG} Имя: {name}\n"
            f"{EMOJI_USER} Юзернейм: {username}\n\n"
            f"{EMOJI_CHART} <b>СТАТИСТИКА ПОКУПОК</b>\n"
            f"🛒 Успешных заказов: <b>{stats['total_orders']}</b>\n"
            f"{EMOJI_GIFT} Получено GMP: <b>{stats['total_gmp']:,} GMP</b>\n"
            f"{EMOJI_MONEY} Потрачено рублей: <b>{stats['total_rub']:,.0f}₽</b>\n"
            f"👥 Приглашено друзей: <b>{bonuses['refs_count']}</b> чел.\n"
            f"✨ Бонусы: <b>{bonus_str}</b>"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=profile_text,
            reply_markup=profile_keyboard(),
            parse_mode="HTML"
        )
        
    elif call.data == "info_withdraw":
        bot.answer_callback_query(call.id)
        withdraw_text = (
            f"{EMOJI_SUNFLOWER} <b>Время вывода GMP</b>\n\n"
            "В среднем не превышает 5-10 минут, но если админ занят, время может увеличиться.\n"
            f"{EMOJI_GIFT} За слишком долгое ожидание вы гарантированно получите бонус!\n\n"
            f"{EMOJI_MUSHROOM} <b>Время активности админа (МСК):</b>\n"
            "• Будние дни: 01:15 - 17.30\n"
            "• Суббота: 01:15 - 18:30\n"
            "• Воскресенье: 4:30 - 18:00\n\n"
            f"Пожалуйста, будьте понимающими и спокойно ожидайте ваши GMP {EMOJI_KISS}"
        )
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=withdraw_text,
            reply_markup=back_keyboard(),
            parse_mode="HTML"
        )
        
    elif call.data == "buy_gmp":
        bot.answer_callback_query(call.id)
        user_states[user_id] = "awaiting_rubles"
        bonuses = get_user_bonuses(user_id)
        
        bonus_hints = []
        if bonuses["welcome_5"]:
            ref_label = "по реф. ссылке" if bonuses["has_referrer"] else "на 1-й заказ"
            bonus_hints.append(f"🎁 <i>Вам доступен бонус +5% GMP ({ref_label})! До сгорания: <code>{bonuses['welcome_time_str']}</code></i>")
        if bonuses["ref_2"]:
            bonus_hints.append(f"👥 <i>Активен реферальный бонус +2% GMP! До сгорания: <code>{bonuses['ref_time_str']}</code></i>")
        bonus_hint_text = ("\n" + "\n".join(bonus_hints) + "\n") if bonus_hints else ""

        calc_text = (
            f"{EMOJI_CARD} <b>Для подсчёта GMP, отправьте сумму в рублях</b>\n"
            f"{EMOJI_WRITE} <i>Напишите просто числом (например: 500)</i>\n\n"
            f"{EMOJI_MONEY} Минимальная сумма: 50₽\n"
            f"{EMOJI_MONEY} Максимальная сумма: 50000₽\n\n"
            f"{EMOJI_CHART_UP} <b>Актуальный курс:</b>\n"
            f"{EMOJI_CHART} до 499₽: 0.37₽ = 1 GMP\n"
            f"{EMOJI_CHART} от 500₽: 0.36₽ = 1 GMP\n"
            f"{bonus_hint_text}"
        )
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
            
        bot.send_message(
            call.message.chat.id,
            calc_text,
            reply_markup=back_keyboard(),
            parse_mode="HTML"
        )

    elif call.data == "pay_confirm":
        state_data = user_states.get(user_id)
        
        if isinstance(state_data, dict) and state_data.get("file_id") is None:
            bot.answer_callback_query(
                call.id, 
                text="Пожалуйста, сначала отправьте скриншот оплаты фото-сообщением в чат!", 
                show_alert=True
            )
            
        elif isinstance(state_data, dict) and state_data.get("file_id") is not None:
            bot.answer_callback_query(call.id)
            rub = state_data["rub"]
            gmp = state_data["gmp"]
            base_gmp = state_data.get("base_gmp", gmp)
            file_id = state_data["file_id"]
            is_photo = state_data["is_photo"]
            admin_bonus_text = state_data.get("admin_bonus_text", "")
            bonus_desc = state_data.get("bonus_desc", "")
            
            order_id = create_pending_order(user_id, rub, gmp, file_id, bonus_desc)
            user_states[user_id] = None
            
            # Подробный отчёт для администратора
            admin_msg = (
                f"🔔 <b>НОВАЯ ЗАЯВКА НА ПРОВЕРКУ #{order_id}</b>\n\n"
                f"{EMOJI_USER} Пользователь: {name}\n"
                f"{EMOJI_CARD} ID: <code>{user_id}</code>\n"
                f"{EMOJI_NAME_TAG} Юзернейм: {username}\n\n"
                f"{EMOJI_MONEY} Сумма перевода: <b>{rub}₽</b>\n"
                f"{EMOJI_DIAMOND} GMP к выдаче: <b>{gmp:,} GMP</b>\n"
                f"{admin_bonus_text}\n"
                f"{EMOJI_WARN} <i>Проверьте комментарий к переводу (за кофе / на подарок / сбор)!</i>"
            )
            
            admin_markup = types.InlineKeyboardMarkup(row_width=2)
            btn_approve = types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_approve_{order_id}")
            btn_reject = types.InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_reject_{order_id}")
            admin_markup.add(btn_approve, btn_reject)

            for admin in ADMIN_IDS:
                try:
                    if is_photo:
                        bot.send_photo(admin, file_id, caption=admin_msg, reply_markup=admin_markup, parse_mode="HTML")
                    else:
                        bot.send_document(admin, file_id, caption=admin_msg, reply_markup=admin_markup, parse_mode="HTML")
                except Exception as e:
                    logger.error(f"[!] Ошибка отправки админу {admin}: {e}")

            bot.send_message(
                call.message.chat.id,
                f"{EMOJI_CHECK} <b>Заявка #{order_id} и чек успешно отправлены на проверку!</b>\n"
                "Администратор проверяет ваш платеж и описание перевода. Ожидайте зачисления в течение 5-10 минут.",
                reply_markup=back_keyboard(),
                parse_mode="HTML"
            )
        else:
            bot.answer_callback_query(call.id, text="Сессия оплаты истекла. Нажмите «Купить GMP» заново.", show_alert=True)

    elif call.data == "my_orders_stub":
        bot.answer_callback_query(call.id)
        orders = get_user_orders_history(user_id)
        if not orders:
            bot.send_message(call.message.chat.id, f"{EMOJI_CLIP} У вас пока нет совершенных заказов.", parse_mode="HTML")
        else:
            txt = f"{EMOJI_CLIP} <b>Ваши последние заказы:</b>\n• • • • • • • • • • • • •\n"
            for o in orders:
                oid, orub, ogmp, ostatus, odate = o
                status_str = "✅ Выдан" if ostatus == "approved" else ("❌ Отклонен" if ostatus == "rejected" else "⏳ На проверке")
                txt += f"• <b>Заказ #{oid}</b> ({odate}) — {status_str}\n  Сумма: <b>{orub:,.0f}₽</b> → <b>{ogmp:,} GMP</b>\n\n"
            bot.send_message(call.message.chat.id, txt, reply_markup=back_keyboard(), parse_mode="HTML")


# === ОБРАБОТЧИК МЕДИА И СКРИНШОТОВ (ФОТО, ВИДЕО, ФАЙЛЫ) ===

@bot.message_handler(content_types=['photo', 'document', 'video', 'animation'])
def handle_media(message):
    user_id = message.from_user.id
    
    # 1. Если админ отправляет контент для рассылки
    if user_id in ADMIN_IDS and user_id in admin_broadcast_state:
        state = admin_broadcast_state[user_id]
        if state.get("step") == "waiting_content":
            state["message_id"] = message.message_id
            state["chat_id"] = message.chat.id
            state["step"] = "waiting_buttons"
            
            help_text = (
                "🔘 <b>Добавление инлайн-кнопок к сообщению:</b>\n\n"
                "Отправьте список кнопок в формате:\n"
                "<code>Текст кнопки | https://ссылка.com</code>\n\n"
                "Если кнопки не нужны, нажмите кнопку ниже:"
            )
            bot.send_message(message.chat.id, help_text, reply_markup=skip_buttons_keyboard(), parse_mode="HTML")
            return

    # 2. Пользователь присылает чек
    state_data = user_states.get(user_id)
    if isinstance(state_data, dict) and state_data.get("state") == "awaiting_screenshot":
        if message.content_type == 'photo':
            file_id = message.photo[-1].file_id
            is_photo = True
        else:
            file_id = getattr(message, message.content_type).file_id
            is_photo = False
            
        state_data["file_id"] = file_id
        state_data["is_photo"] = is_photo
        user_states[user_id] = state_data
        
        bot.reply_to(
            message,
            f"{EMOJI_CAMERA} <b>Скриншот успешно получен!</b>\n\n"
            f"Теперь нажмите кнопку <b>«✅ Подтвердить оплату»</b> под сообщением с реквизитами выше, чтобы отправить чек администратору на проверку.",
            parse_mode="HTML"
        )

def create_review(order_id, user_id, rub_amount, gmp_amount):
    conn = get_conn()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            INSERT INTO reviews (order_id, user_id, rub_amount, gmp_amount)
            VALUES (%s, %s, %s, %s)
            RETURNING review_id
        """, (order_id, user_id, rub_amount, gmp_amount))

        review_id = cursor.fetchone()[0]
        conn.commit()
        return review_id

    except Exception as e:
        conn.rollback()
        logger.error(f"Ошибка создания отзыва: {e}")
        return None

    finally:
        cursor.close()
        conn.close()


# === ЕДИНЫЙ ПЕРЕХВАТЧИК СООБЩЕНИЙ ===

@bot.message_handler(content_types=['text'])
def handle_all_text(message):
    user_id = message.from_user.id
    name = message.from_user.first_name if message.from_user.first_name else "Пользователь"
    username = f"@{message.from_user.username}" if message.from_user.username else "Не установлен"

# Проверка: если пользователь отвечает (Reply) на сообщение бота с отзывом и у него есть юзернейм
if message.reply_to_message and message.from_user.username:
    replied_text = message.reply_to_message.text or message.reply_to_message.caption or ""
    # Проверяем, что это сообщение об успешном заказе
    if "успешно выполнен!" in replied_text:
        # Извлекаем номер заказа из текста
        order_num_match = re.search(r"Заказ #(\d+)", replied_text)
        ord_num = order_num_match.group(1) if order_num_match else "0"

        # Ищем сумму GMP и рублей из БД по этому order_id
        try:
            conn = get_conn()
            cursor = conn.cursor()
            cursor.execute("SELECT gmp_amount, rub_amount FROM orders WHERE order_id = %s;", (int(ord_num),))
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            gmp_val = row[0] if row else 0
            rub_val = int(row[1]) if row else 0
        except Exception:
            gmp_val = 0
            rub_val = 0

        buyer_username = f"@{message.from_user.username}"
        review_text = message.text
        
        # ИСПРАВЛЕННЫЕ ОТСТУПЫ ЗДЕСЬ:
        review_id = create_review(
            int(ord_num),
            user_id,
            rub_val,
            gmp_val
        )

        if review_id is None:
            review_id = 171

        # Формируем отчет для админов
        # Заменил несуществующие EMOJI_..._PREMIUM на обычные из твоего кода.
        # Если нужен html.escape, добавь в самый верх файла: import html
        admin_review_msg = (
            f"<b>Отзыв #{review_id}</b>\n"
            f"Продано — {gmp_val} {EMOJI_DIAMOND}\n"
            f"За {rub_val} {EMOJI_MONEY}\n\n"
            f"<b>Комментарий от покупателя — {buyer_username}:</b>\n\n"
            f"{review_text}"
        )

        for admin in ADMIN_IDS:
            try:
                bot.send_message(admin, admin_review_msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось переслать отзыв админу {admin}: {e}")

    
    # Проверка на реферальный переход: /start ref_12345678
    referrer_id = None
    if message.text and message.text.startswith('/start'):
        parts = message.text.split()
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1].replace("ref_", ""))
            except ValueError:
                referrer_id = None

    is_new_user, ref_extended = upsert_user(user_id, name, username, referrer_id)

    # Если рефереру был продлен бонус — отправляем ему уведомление
    if ref_extended and referrer_id:
        try:
            bot.send_message(
                referrer_id,
                f"🎉 <b>По вашей реферальной ссылке зарегистрировался новый пользователь ({name})!</b>\n\n"
                f"✨ Ваш бонус <b>+2% GMP</b> продлен ещё на <b>+24 часа</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    # Шаги рассылки для админа
    if user_id in ADMIN_IDS and user_id in admin_broadcast_state:
        state = admin_broadcast_state[user_id]
        step = state.get("step")
        if step == "waiting_content":
            state["message_id"] = message.message_id
            state["chat_id"] = message.chat.id
            state["step"] = "waiting_buttons"
            help_text = (
                "🔘 <b>Добавление инлайн-кнопок к сообщению:</b>\n\n"
                "Отправьте список кнопок в формате:\n"
                "<code>Текст кнопки | https://ссылка.com</code>\n\n"
                "Если кнопки не нужны, нажмите кнопку ниже:"
            )
            bot.send_message(message.chat.id, help_text, reply_markup=skip_buttons_keyboard(), parse_mode="HTML")
            return
        elif step == "waiting_buttons":
            custom_markup = parse_broadcast_buttons(message.text)
            state["reply_markup"] = custom_markup
            state["step"] = "confirm"
            bot.send_message(message.chat.id, "👀 <b>Предпросмотр сообщения для рассылки с кнопками:</b>", parse_mode="HTML")
            bot.copy_message(chat_id=message.chat.id, from_chat_id=state["chat_id"], message_id=state["message_id"], reply_markup=custom_markup)
            total_users = len(get_all_user_ids())
            bot.send_message(message.chat.id, f"Готово к отправке!\n👥 Получателей: <b>{total_users}</b>\n\nНачать рассылку?", reply_markup=broadcast_confirm_keyboard(), parse_mode="HTML")
            return

    if message.text and message.text.startswith('/start'):
        user_states[user_id] = None
        bonuses = get_user_bonuses(user_id)
        
        bonus_block = build_bonus_countdown_block(bonuses)
        full_text = START_TEXT.format(name=name, bonus_block=bonus_block)

        bot.send_message(
            message.chat.id, 
            full_text, 
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        return

    state = user_states.get(user_id)
    
    if state == "awaiting_rubles" or is_probable_amount(message.text):
        txt = message.text.lower().replace(" ", "")
        clean_text = re.sub(r'[₽ррууббlluubbr]', '', txt)
        clean_text = clean_text.replace(',', '.')

        try:
            rub_value = float(clean_text)
        except Exception:
            bot.send_message(
                message.chat.id, 
                "❌ Не удалось распознать сумму.\nПожалуйста, напишите просто число (например: 540).\nНажмите кнопку «💸 Купить GMP» заново.",
                parse_mode="HTML"
            )
            return

        if rub_value < 50 or rub_value > 50000:
            bot.send_message(
                message.chat.id, 
                f"{EMOJI_WARN} Сумма должна быть в пределах от <b>50₽</b> до <b>50000₽</b>.\nНажмите кнопку «💸 Купить GMP» заново.",
                parse_mode="HTML"
            )
            return

        rate = 0.37 if rub_value <= 499 else 0.36
        base_gmp = int(rub_value / rate)

        # Проверяем бонусы пользователя
        bonuses = get_user_bonuses(user_id)
        bonus_percent = 0
        bonus_user_notes = []
        admin_lines = []

        # 1. Приветственный бонус / бонус по рефке +5%
        if bonuses["welcome_5"]:
            bonus_percent += 5
            ref_tag = "перешел по реф. ссылке" if bonuses["has_referrer"] else "приветственный бонус"
            bonus_user_notes.append(f"+5% ({ref_tag})")
            admin_lines.append(f"🎁 <b>Скидка/бонус:</b> {ref_tag} (+5% к выдаче GMP)")
            admin_lines.append(f"⏳ <b>Успел оформить за 24ч:</b> да (оставалось <code>{bonuses['welcome_time_str']}</code>)")

        # 2. Реферальный бонус +2% (если пригласил кого-то)
        if bonuses["ref_2"]:
            bonus_percent += 2
            bonus_user_notes.append("+2% (за приглашение друзей)")
            admin_lines.append(f"👥 <b>Бонус реферера:</b> пригласил друзей (+2% к выдаче GMP, осталось <code>{bonuses['ref_time_str']}</code>)")

        bonus_gmp_val = int(base_gmp * (bonus_percent / 100))
        final_gmp = base_gmp + bonus_gmp_val

        if admin_lines:
            admin_lines.append(f"ℹ️ <i>Базово: {base_gmp:,} GMP ➔ С бонусом: {final_gmp:,} GMP (+{bonus_gmp_val} GMP)</i>")
            admin_bonus_text = "\n" + "\n".join(admin_lines) + "\n"
        else:
            admin_bonus_text = ""

        bonus_desc = ", ".join(bonus_user_notes) if bonus_user_notes else None

        user_states[user_id] = {
            "state": "awaiting_screenshot",
            "rub": int(rub_value),
            "gmp": final_gmp,
            "base_gmp": base_gmp,
            "admin_bonus_text": admin_bonus_text,
            "bonus_desc": bonus_desc,
            "file_id": None,
            "is_photo": True
        }

        # Блок бонуса для пользователя при оформлении
        bonus_calc_line = ""
        if bonus_percent > 0:
            time_left_str = bonuses['welcome_time_str'] if bonuses['welcome_5'] else bonuses['ref_time_str']
            bonus_calc_line = (
                f"\n🎁 <i>Применен бонус +{bonus_percent}%: +{bonus_gmp_val} GMP в подарок!</i>\n"
                f"⏳ <i>До сгорания бонуса осталось: <code>{time_left_str}</code></i>\n"
            )

        line1 = f"{EMOJI_CARD} <b>{int(rub_value)}₽ → {final_gmp:,} GMP</b>{bonus_calc_line}"
        line2 = f"{EMOJI_CLIP} <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ (СБП / ПЕРЕВОД):</b>"
        line3 = f"{EMOJI_WARN} Демид Андреевич П."
        line4 = f"{EMOJI_WARN} Банк: <b>ВТБ</b>"
        line5 = f"{EMOJI_WARN} <i>Будьте внимательны: при переводе на другой банк средства не возвращаю, GMP не выдаю!</i>"
        line6 = f"{EMOJI_PHONE} <b>Номер телефона (СБП):</b>\n<code>+79142515945</code>"
        
        line7 = (
            f"{EMOJI_CSECK} <b>ОБЯЗАТЕЛЬНОЕ ПОЛЕ «ОПИСАНИЕ / СООБЩЕНИЕ ПОЛУЧАТЕЛЮ»:</b>\n"
            f"При переводе в приложении банка в поле описания напишите ОДНУ из фраз:\n"
            f"{EMOJI_CHECC} <code>на подарок</code>\n"
            f"{EMOJI_CHECC} спасибо за помощь\n"
            f"{EMOJI_CHECC} <code>сбор</code>\n"
            f"{EMOJI_CHECC} <code>на общие нужды</code>\n\n"
            f"{EMOJI_WARN * 2} <b>ВНИМАНИЕ:</b> Если вы переведёте без указания одного из этих описаний — <b>будет выдано только 50% GMP</b> (штраф 50% без возврата!)"
        )

        line8 = (
            f"{EMOJI_CAMERA} <b>ИНСТРУКЦИЯ К ОПЛАТЕ:</b>\n"
            f"1. Переведите <b>{int(rub_value)}₽</b> по номеру телефона в банк <b>ВТБ</b>.\n"
            f"2. Обязательно укажите в комментарии: <code>за кофе</code> или <code>на подарок</code>.\n"
            f"3. Сохраните чек или сделайте скриншот платежа.\n"
            f"4. <u><b>ОТПРАВЬТЕ СКРИНШОТ ФОТО-СООБЩЕНИЕМ В ЭТОТ ЧАТ!</b></u>\n"
            f"5. Нажмите кнопку «✅ Подтвердить оплату» ниже."
        )

        requisites_text = f"{line1}\n\n{line2}\n{line3}\n{line4}\n{line5}\n\n{line6}\n\n{line7}\n\n{line8}"

        markup = types.InlineKeyboardMarkup()
        btn_confirm = types.InlineKeyboardButton("✅ Подтвердить оплату", callback_data="pay_confirm", style="success")
        btn_cancel = types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_main", style="danger")
        markup.add(btn_confirm)
        markup.add(btn_cancel)

        bot.send_message(message.chat.id, requisites_text, reply_markup=markup, parse_mode="HTML")


# === ЗАПУСК БОТА ===
if __name__ == "__main__":
    init_db()
    try:
        bot.remove_webhook()
    except Exception:
        pass
        
    logger.info("🚀 Бот успешно запущен!")
    bot.polling(none_stop=True, interval=0, timeout=20)
