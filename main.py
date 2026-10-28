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
# ПОДДЕРЖКА style В InlineKeyboardButton (оставлена для совместимости старого кода)
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

# Для Railway обязательно задайте BOT_TOKEN и DATABASE_URL в Variables.
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная BOT_TOKEN не задана в Railway Variables")
bot = telebot.TeleBot(TOKEN)

ADMIN_IDS = [6522383487, 7727345054]
user_states = {}
admin_broadcast_state = {}

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Переменная DATABASE_URL не задана в Railway Variables")
db_info = urlparse(DATABASE_URL)


def get_conn():
    """Создаёт SSL-подключение к PostgreSQL/Supabase."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.dbapi.connect(
        user=db_info.username,
        password=unquote(db_info.password) if db_info.password else None,
        host=db_info.hostname,
        port=db_info.port or 5432,
        database=db_info.path.lstrip("/") or "postgres",
        ssl_context=ctx,
    )


# ==============================================================================
# НАСТРОЙКИ КУРСА GMP
# ==============================================================================
DEFAULT_GMP_RATE_UNDER_500 = 0.37
DEFAULT_GMP_RATE_FROM_500 = 0.36


def get_gmp_rates():
    """Курс в рублях за 1 GMP: отдельно для сумм до 499 ₽ и от 500 ₽."""
    rates = {
        "under_500": DEFAULT_GMP_RATE_UNDER_500,
        "from_500": DEFAULT_GMP_RATE_FROM_500,
    }
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT setting_key, setting_value
            FROM bot_settings
            WHERE setting_key IN ('gmp_rate_under_500', 'gmp_rate_from_500')
        """)
        for key, value in cursor.fetchall():
            if key == "gmp_rate_under_500":
                rates["under_500"] = float(value)
            elif key == "gmp_rate_from_500":
                rates["from_500"] = float(value)
        return rates
    finally:
        cursor.close()
        conn.close()


def set_gmp_rates(under_500: float, from_500: float):
    """Сохраняет оба курса одной транзакцией."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.executemany("""
            INSERT INTO bot_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON CONFLICT (setting_key)
            DO UPDATE SET setting_value = EXCLUDED.setting_value
        """, [
            ("gmp_rate_under_500", float(under_500)),
            ("gmp_rate_from_500", float(from_500)),
        ])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


def init_db():
    """Создаёт таблицы/колонки, не сбрасывая существующие данные."""
    conn = get_conn()
    cursor = conn.cursor()
    try:
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
        for statement in (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_orders INT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_gmp_received INT DEFAULT 0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_rub_spent DOUBLE PRECISION DEFAULT 0.0",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referrer_id BIGINT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_bonus_until TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS welcome_bonus_until TIMESTAMPTZ",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS welcome_bonus_used BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
        ):
            cursor.execute(statement)

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
        for statement in (
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS file_id TEXT",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'pending'",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS bonus_desc TEXT",
        ):
            cursor.execute(statement)

        # Сохраняем прежнюю нумерацию заказов; не сбрасываем sequence, если заказы уже есть.
        cursor.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM orders WHERE order_id >= 113) THEN
                    PERFORM setval('orders_order_id_seq', 112, true);
                END IF;
            END $$;
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                review_id SERIAL PRIMARY KEY,
                order_id INT,
                user_id BIGINT,
                rub_amount DOUBLE PRECISION,
                gmp_amount INT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Настройки курса. Значения по умолчанию добавляются только при отсутствии ключа.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value DOUBLE PRECISION NOT NULL
            );
        """)
        cursor.execute("""
            INSERT INTO bot_settings (setting_key, setting_value)
            VALUES ('gmp_rate_under_500', %s), ('gmp_rate_from_500', %s)
            ON CONFLICT (setting_key) DO NOTHING
        """, (DEFAULT_GMP_RATE_UNDER_500, DEFAULT_GMP_RATE_FROM_500))

        conn.commit()
        logger.info("✅ Supabase: таблицы и настройки курса готовы.")
    except Exception:
        conn.rollback()
        logger.exception("❌ Ошибка инициализации базы данных")
        raise
    finally:
        cursor.close()
        conn.close()


def upsert_user(user_id: int, first_name: str, username: str, referrer_id: int = None):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, referrer_id, total_orders FROM users WHERE user_id = %s", (user_id,))
        exists = cursor.fetchone()
        is_new = False
        ref_extended = False

        if not exists:
            is_new = True
            valid_referrer = None
            if referrer_id and referrer_id != user_id:
                cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (referrer_id,))
                if cursor.fetchone():
                    valid_referrer = referrer_id
                    cursor.execute("""
                        UPDATE users
                        SET ref_bonus_until = CASE
                            WHEN ref_bonus_until > CURRENT_TIMESTAMP
                            THEN ref_bonus_until + INTERVAL '24 hours'
                            ELSE CURRENT_TIMESTAMP + INTERVAL '24 hours'
                        END
                        WHERE user_id = %s
                    """, (referrer_id,))
                    ref_extended = True
            cursor.execute("""
                INSERT INTO users (
                    user_id, first_name, username, referrer_id,
                    welcome_bonus_until, welcome_bonus_used, created_at
                ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP + INTERVAL '24 hours', FALSE, CURRENT_TIMESTAMP)
            """, (user_id, first_name, username, valid_referrer))
        else:
            current_ref, total_orders = exists[1], exists[2] or 0
            if referrer_id and referrer_id != user_id and current_ref is None and total_orders == 0:
                cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (referrer_id,))
                if cursor.fetchone():
                    cursor.execute("""
                        UPDATE users SET referrer_id = %s,
                            welcome_bonus_until = CURRENT_TIMESTAMP + INTERVAL '24 hours',
                            welcome_bonus_used = FALSE
                        WHERE user_id = %s
                    """, (referrer_id, user_id))
                    cursor.execute("""
                        UPDATE users SET ref_bonus_until = CASE
                            WHEN ref_bonus_until > CURRENT_TIMESTAMP
                            THEN ref_bonus_until + INTERVAL '24 hours'
                            ELSE CURRENT_TIMESTAMP + INTERVAL '24 hours'
                        END WHERE user_id = %s
                    """, (referrer_id,))
                    ref_extended = True
            cursor.execute("UPDATE users SET first_name = %s, username = %s WHERE user_id = %s",
                           (first_name, username, user_id))
        conn.commit()
        return is_new, ref_extended
    except Exception:
        conn.rollback()
        logger.exception("Ошибка upsert_user")
        return False, False
    finally:
        cursor.close()
        conn.close()


def get_user_bonuses(user_id: int) -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT referrer_id, welcome_bonus_used, total_orders,
                COALESCE(EXTRACT(EPOCH FROM (welcome_bonus_until - CURRENT_TIMESTAMP))::INT, 0),
                COALESCE(EXTRACT(EPOCH FROM (ref_bonus_until - CURRENT_TIMESTAMP))::INT, 0),
                (SELECT COUNT(*) FROM users WHERE referrer_id = %s)
            FROM users WHERE user_id = %s
        """, (user_id, user_id))
        row = cursor.fetchone()
        if not row:
            return {"welcome_5": False, "welcome_seconds_left": 0, "welcome_time_str": "00:00:00",
                    "ref_2": False, "ref_seconds_left": 0, "ref_time_str": "00:00:00",
                    "refs_count": 0, "has_referrer": False}

        ref_id, welcome_used, total_orders, welcome_sec, ref_sec, refs_count = row

        def fmt_time(seconds):
            seconds = max(int(seconds or 0), 0)
            return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"

        welcome_sec = int(welcome_sec or 0)
        ref_sec = int(ref_sec or 0)
        return {
            "welcome_5": not welcome_used and (total_orders or 0) == 0 and welcome_sec > 0,
            "welcome_seconds_left": max(welcome_sec, 0),
            "welcome_time_str": fmt_time(welcome_sec),
            "ref_2": ref_sec > 0,
            "ref_seconds_left": max(ref_sec, 0),
            "ref_time_str": fmt_time(ref_sec),
            "refs_count": refs_count or 0,
            "has_referrer": bool(ref_id),
        }
    except Exception:
        logger.exception("Ошибка get_user_bonuses")
        return {"welcome_5": False, "welcome_seconds_left": 0, "welcome_time_str": "00:00:00",
                "ref_2": False, "ref_seconds_left": 0, "ref_time_str": "00:00:00",
                "refs_count": 0, "has_referrer": False}
    finally:
        cursor.close()
        conn.close()


def get_user_stats(user_id: int) -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT total_orders, total_gmp_received, total_rub_spent FROM users WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()
        if row:
            return {"total_orders": row[0] or 0, "total_gmp": row[1] or 0, "total_rub": row[2] or 0.0}
        return {"total_orders": 0, "total_gmp": 0, "total_rub": 0.0}
    except Exception:
        logger.exception("Ошибка get_user_stats")
        return {"total_orders": 0, "total_gmp": 0, "total_rub": 0.0}
    finally:
        cursor.close()
        conn.close()


def create_pending_order(user_id: int, rub: float, gmp: int, file_id: str = None, bonus_desc: str = None) -> int:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO orders (user_id, rub_amount, gmp_amount, file_id, bonus_desc, status)
            VALUES (%s, %s, %s, %s, %s, 'pending') RETURNING order_id
        """, (user_id, rub, gmp, file_id, bonus_desc))
        order_id = cursor.fetchone()[0]
        conn.commit()
        logger.info("Заявка #%s создана: user=%s rub=%s gmp=%s", order_id, user_id, rub, gmp)
        return order_id
    except Exception:
        conn.rollback()
        logger.exception("Ошибка create_pending_order")
        return None
    finally:
        cursor.close()
        conn.close()


def approve_order(order_id: int) -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, rub_amount, gmp_amount, status, bonus_desc FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "reason": "Заказ не найден"}
        user_id, rub, gmp, status, bonus_desc = row
        if status != "pending":
            return {"success": False, "reason": f"Заявка уже обработана (статус: {status})"}
        cursor.execute("UPDATE orders SET status = 'approved' WHERE order_id = %s", (order_id,))
        cursor.execute("""
            UPDATE users SET total_orders = COALESCE(total_orders, 0) + 1,
                total_gmp_received = COALESCE(total_gmp_received, 0) + %s,
                total_rub_spent = COALESCE(total_rub_spent, 0) + %s,
                welcome_bonus_used = TRUE WHERE user_id = %s
        """, (gmp, rub, user_id))
        conn.commit()
        logger.info("Заявка #%s подтверждена, user=%s", order_id, user_id)
        return {"success": True, "user_id": user_id, "rub": rub, "gmp": gmp, "bonus_desc": bonus_desc}
    except Exception as e:
        conn.rollback()
        logger.exception("Ошибка approve_order")
        return {"success": False, "reason": str(e)}
    finally:
        cursor.close()
        conn.close()


def reject_order(order_id: int) -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id, rub_amount, gmp_amount, status FROM orders WHERE order_id = %s FOR UPDATE", (order_id,))
        row = cursor.fetchone()
        if not row:
            return {"success": False, "reason": "Заказ не найден"}
        user_id, rub, gmp, status = row
        if status != "pending":
            return {"success": False, "reason": f"Заявка уже обработана (статус: {status})"}
        cursor.execute("UPDATE orders SET status = 'rejected' WHERE order_id = %s", (order_id,))
        conn.commit()
        logger.info("Заявка #%s отклонена", order_id)
        return {"success": True, "user_id": user_id, "rub": rub, "gmp": gmp}
    except Exception as e:
        conn.rollback()
        logger.exception("Ошибка reject_order")
        return {"success": False, "reason": str(e)}
    finally:
        cursor.close()
        conn.close()


def get_user_orders_history(user_id: int, limit: int = 5) -> list:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT order_id, rub_amount, gmp_amount, status, TO_CHAR(created_at, 'DD.MM.YYYY HH24:MI')
            FROM orders WHERE user_id = %s ORDER BY order_id DESC LIMIT %s
        """, (user_id, limit))
        return cursor.fetchall() or []
    except Exception:
        logger.exception("Ошибка get_user_orders_history")
        return []
    finally:
        cursor.close()
        conn.close()


def get_last_approved_order(user_id: int):
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT order_id, rub_amount, gmp_amount FROM orders
            WHERE user_id = %s AND status = 'approved' ORDER BY order_id DESC LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        return {"order_id": row[0], "rub": row[1], "gmp": row[2]} if row else None
    except Exception:
        logger.exception("Ошибка get_last_approved_order")
        return None
    finally:
        cursor.close()
        conn.close()


def create_review(order_id: int, user_id: int, rub: float, gmp: int) -> int:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO reviews (order_id, user_id, rub_amount, gmp_amount)
            VALUES (%s, %s, %s, %s) RETURNING review_id
        """, (order_id, user_id, rub, gmp))
        review_id = cursor.fetchone()[0]
        conn.commit()
        return review_id
    except Exception:
        conn.rollback()
        logger.exception("Ошибка create_review")
        return order_id or 1
    finally:
        cursor.close()
        conn.close()


def get_admin_detailed_stats() -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE")
        today_users = cursor.fetchone()[0] or 0
        cursor.execute("""
            SELECT COUNT(*), COALESCE(SUM(gmp_amount), 0), COALESCE(SUM(rub_amount), 0.0)
            FROM orders WHERE status = 'approved'
        """)
        row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) FROM orders WHERE status = 'pending'")
        pending_orders = cursor.fetchone()[0] or 0
        cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        total_invited = cursor.fetchone()[0] or 0
        return {
            "total_users": total_users, "today_users": today_users,
            "approved_orders": (row[0] or 0) + 112,
            "total_gmp": row[1] or 0, "total_rub": row[2] or 0.0,
            "pending_orders": pending_orders, "total_invited": total_invited,
        }
    except Exception:
        logger.exception("Ошибка get_admin_detailed_stats")
        return {"total_users": 0, "today_users": 0, "approved_orders": 112,
                "total_gmp": 0, "total_rub": 0.0, "pending_orders": 0, "total_invited": 0}
    finally:
        cursor.close()
        conn.close()


def get_admin_referral_stats() -> dict:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM users WHERE referrer_id IS NOT NULL")
        total_invited = cursor.fetchone()[0] or 0
        cursor.execute("""
            SELECT u.user_id, u.first_name, u.username, COUNT(r.user_id) AS ref_count
            FROM users u JOIN users r ON r.referrer_id = u.user_id
            GROUP BY u.user_id, u.first_name, u.username
            ORDER BY ref_count DESC LIMIT 15
        """)
        return {"total_invited": total_invited, "top_referrers": cursor.fetchall() or []}
    except Exception:
        logger.exception("Ошибка get_admin_referral_stats")
        return {"total_invited": 0, "top_referrers": []}
    finally:
        cursor.close()
        conn.close()


def get_all_user_ids() -> list:
    conn = get_conn()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT user_id FROM users")
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        logger.exception("Ошибка get_all_user_ids")
        return []
    finally:
        cursor.close()
        conn.close()


def parse_broadcast_buttons(text: str):
    if not text or text.lower().strip() in {"пропустить", "нет", "-", "без кнопок"}:
        return None
    markup = types.InlineKeyboardMarkup()
    for line in text.strip().splitlines():
        parts = [part.strip() for part in line.split("|") if part.strip()]
        row = []
        for i in range(0, len(parts) - 1, 2):
            label, url = parts[i], parts[i + 1]
            if url.startswith(("http://", "https://", "tg://")):
                row.append(types.InlineKeyboardButton(label, url=url, style="primary"))
        if row:
            markup.row(*row)
    return markup if markup.keyboard else None


# === ПРЕМИУМ ЭМОДЗИ ===
EMOJI_CARD = '<tg-emoji emoji-id="5470060791883374114">🪪</tg-emoji>'
EMOJI_WRITE = '<tg-emoji emoji-id="5375296873982604963">✍️</tg-emoji>'
EMOJI_MONEY = '<tg-emoji emoji-id="5373001317042101552">💰</tg-emoji>'
EMOJI_CHART_UP = '<tg-emoji emoji-id="5431577498364158238">📈</tg-emoji>'
EMOJI_CHART = '<tg-emoji emoji-id="5458799228719472718">📊</tg-emoji>'
EMOJI_STAR_BIG = '<tg-emoji emoji-id="5370869711888194012">🌟</tg-emoji>'
EMOJI_INVADER = '<tg-emoji emoji-id="5805528485371843693">👾</tg-emoji>'
EMOJI_LIGHT = '<tg-emoji emoji-id="5427168083074628963">⚡️</tg-emoji>'
EMOJI_DIAMOND = '<tg-emoji emoji-id="6041761071155386269">💎</tg-emoji>'
EMOJI_ROCKET = '<tg-emoji emoji-id="5805630830147540745">🚀</tg-emoji>'
EMOJI_SPARKLES = '<tg-emoji emoji-id="5190498849440931467">✨</tg-emoji>'
EMOJI_DEV = '<tg-emoji emoji-id="5373012449597335010">👨‍💻</tg-emoji>'
EMOJI_USER = '<tg-emoji emoji-id="5841276284155467413">👤</tg-emoji>'
EMOJI_NAME_TAG = '<tg-emoji emoji-id="5334882760735598374">🔤</tg-emoji>'
EMOJI_MEMO = '<tg-emoji emoji-id="5431499171045581032">📝</tg-emoji>'
EMOJI_GIFT = '<tg-emoji emoji-id="5203996991054432397">🎁</tg-emoji>'
EMOJI_CHANNEL = '<tg-emoji emoji-id="5452002597592382164">📣</tg-emoji>'
EMOJI_STAR = '<tg-emoji emoji-id="5438496463044752972">⭐️</tg-emoji>'
EMOJI_SUNFLOWER = '<tg-emoji emoji-id="5211226911367257670">🌻</tg-emoji>'
EMOJI_MUSHROOM = '<tg-emoji emoji-id="5197546963788126956">🍄</tg-emoji>'
EMOJI_KISS = '<tg-emoji emoji-id="5253922906378881072">💋</tg-emoji>'
EMOJI_CLIP = '<tg-emoji emoji-id="5197269100878907942">📋</tg-emoji>'
EMOJI_WARN = '<tg-emoji emoji-id="5274099962655816924">❗️</tg-emoji>'
EMOJI_PHONE = '<tg-emoji emoji-id="5407025283456835913">📱</tg-emoji>'
EMOJI_CAMERA = '<tg-emoji emoji-id="5235837920081887219">📸</tg-emoji>'
EMOJI_CHECK = '<tg-emoji emoji-id="5206607081334906820">✔️</tg-emoji>'
EMOJI_CHECC = '<tg-emoji emoji-id="5465198403573012261">👉</tg-emoji>'
EMOJI_CSECK = '<tg-emoji emoji-id="5454093885888281840">⚠️</tg-emoji>'
EMOJI_CHAT = '<tg-emoji emoji-id="5443038326535759644">💬</tg-emoji>'
EMOJI_CHATTT = '<tg-emoji emoji-id="5461151367559141950">🎉</tg-emoji>'
EMOJI_DIA = '<tg-emoji emoji-id="5231449120635370684">💸</tg-emoji>'
EMOJI_DAA = '<tg-emoji emoji-id="5427168083074628963">💎</tg-emoji>'

START_TEXT = (
    "{bonus_block}"
    f"{EMOJI_STAR} Здравствуй, {{name}}! {EMOJI_STAR}\n\n"
    f"{EMOJI_INVADER} Ты попал в бота-продавца GMP.\n"
    f"{EMOJI_LIGHT} Здесь ты можешь быстро приобрести GMP по выгодному курсу!\n\n"
    f"{EMOJI_DIAMOND} Наши преимущества:\n"
    f"• {EMOJI_ROCKET} Быстрая выдача\n"
    f"• {EMOJI_CARD} Удобные способы оплаты\n\n"
    f"{EMOJI_SPARKLES} Выбери что тебя интересует:"
)


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
    markup.add(types.InlineKeyboardButton("📢 Перейти в канал", url="https://t.me/Kitaez_com", style="primary"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


def reviews_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⭐ Посмотреть отзывы", url="https://t.me/OTZIVI_KRABIKA_TB", style="primary"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


def order_success_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(types.InlineKeyboardButton("⭐ Посмотреть отзывы", url="https://t.me/OTZIVI_KRABIKA_TB", style="success"))
    markup.add(types.InlineKeyboardButton("🔙 Вернуться в меню", callback_data="back_to_main", style="danger"))
    return markup


def support_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👤 Написать @KRABIK_TB", url="https://t.me/KRABIK_TB", style="primary"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


def profile_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Все мои заказы", callback_data="my_orders_stub", style="success"),
               types.InlineKeyboardButton("👥 Рефералы", callback_data="open_referral", style="primary"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


def referral_keyboard(bot_username: str, user_id: int):
    markup = types.InlineKeyboardMarkup()
    ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    share_url = f"https://t.me/share/url?url={ref_link}&text=Купи%20GMP%20по%20самому%20выгодному%20курсу!%20Получи%20бонус%205%%20на%20первый%20заказ!"
    markup.add(types.InlineKeyboardButton("🔗 Поделиться ссылкой", url=share_url, style="success"))
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main"))
    return markup


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
    markup.add(types.InlineKeyboardButton("🚀 Запустить рассылку", callback_data="admin_bc_confirm", style="success"),
               types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel", style="danger"))
    return markup


def skip_buttons_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⏩ Пропустить (без кнопок)", callback_data="admin_bc_skip_buttons", style="primary"))
    markup.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel", style="danger"))
    return markup


def is_probable_amount(text):
    if not text or text.startswith('/'):
        return False
    txt = text.lower().replace(" ", "")
    cleaned = re.sub(r'[₽ррууббlluubbr]', '', txt).replace(',', '.')
    try:
        val = float(cleaned)
        return 50 <= val <= 50000
    except ValueError:
        return False


def build_bonus_countdown_block(bonuses: dict) -> str:
    parts = []
    if bonuses["welcome_5"]:
        ref_note = "по реферальной ссылке" if bonuses["has_referrer"] else "на 1-й заказ"
        parts.append(
            f"🔥 <b>ВНИМАНИЕ! Бонус +5% GMP ({ref_note}) сгорит через: <code>{bonuses['welcome_time_str']}</code></b>\n"
            "⚡️ <i>Успейте оформить заказ прямо сейчас! Нажмите «💸 Купить GMP» ниже.</i>"
        )
    if bonuses["ref_2"]:
        parts.append(
            f"⏰ <b>Реферальный бонус +2% GMP сгорит через: <code>{bonuses['ref_time_str']}</code></b>\n"
            "🚀 <i>Не упустите повышенный процент! Оплачивайте заказ быстрее.</i>"
        )
    return "\n\n".join(parts) + ("\n\n━━━━━━━━━━━━━━━━━━━━\n\n" if parts else "")


@bot.message_handler(commands=['admin', 'adm'])
def handle_admin_command(message):
    if message.from_user.id not in ADMIN_IDS:
        return
    bot.send_message(message.chat.id,
        "👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА GMP</b>\n━━━━━━━━━━━━━━━━━━━━\nВыберите действие:",
        reply_markup=admin_main_keyboard(), parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    user_id = call.from_user.id
    name = call.from_user.first_name or "Пользователь"
    username = f"@{call.from_user.username}" if call.from_user.username else "Не установлен"
    upsert_user(user_id, name, username)

    if call.data.startswith("admin_"):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, text="❌ Доступ запрещён!", show_alert=True)
            return

        if call.data == "admin_stats":
            bot.answer_callback_query(call.id)
            s = get_admin_detailed_stats()
            text = (
                "📊 <b>СТАТИСТИКА БОТА И ПРОДАЖ</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👥 Пользователей: <b>{s['total_users']:,}</b>\n"
                f"🔗 По реферальным ссылкам: <b>{s['total_invited']:,}</b>\n"
                f"🆕 Сегодня: <b>{s['today_users']:,}</b>\n\n"
                f"💰 Успешных заказов: <b>{s['approved_orders']:,}</b>\n"
                f"💎 Выдано GMP: <b>{s['total_gmp']:,}</b>\n"
                f"💵 Оборот: <b>{s['total_rub']:,.2f} ₽</b>\n"
                f"⏳ Заявок ожидает: <b>{s['pending_orders']}</b>\n\n"
                f"🕒 Обновлено: <i>{datetime.now().strftime('%H:%M:%S')}</i>"
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats"),
                       types.InlineKeyboardButton("🔙 Назад", callback_data="admin_menu"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")

        elif call.data == "admin_referrals":
            bot.answer_callback_query(call.id)
            ref_data = get_admin_referral_stats()
            text = "👥 <b>РЕФЕРАЛЬНАЯ СТАТИСТИКА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
            text += f"Всего приглашено: <b>{ref_data['total_invited']}</b>\n\n🏆 <b>Топ рефереров:</b>\n"
            if not ref_data["top_referrers"]:
                text += "<i>Пока приглашений нет.</i>"
            else:
                for idx, (uid, fname, uname, count) in enumerate(ref_data["top_referrers"], 1):
                    display = f"@{uname.lstrip('@')}" if uname and uname != "Не установлен" else (fname or f"ID {uid}")
                    text += f"<b>{idx}.</b> {display} (<code>{uid}</code>) — {count}\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="admin_referrals"),
                       types.InlineKeyboardButton("🔙 Назад", callback_data="admin_menu"))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=markup, parse_mode="HTML")

        elif call.data == "admin_menu":
            bot.answer_callback_query(call.id)
            bot.edit_message_text("👑 <b>ПАНЕЛЬ АДМИНИСТРАТОРА GMP</b>\n━━━━━━━━━━━━━━━━━━━━\nВыберите действие:",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=admin_main_keyboard(), parse_mode="HTML")

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
            cancel_kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="admin_bc_cancel"))
            bot.send_message(call.message.chat.id,
                "📢 <b>СОЗДАНИЕ РАССЫЛКИ</b>\n\nОтправьте текст, фото или видео. Форматирование сохранится.",
                reply_markup=cancel_kb, parse_mode="HTML")

        elif call.data == "admin_bc_skip_buttons":
            bot.answer_callback_query(call.id)
            state = admin_broadcast_state.get(user_id)
            if not state or state.get("step") != "waiting_buttons":
                return
            state["reply_markup"] = None
            state["step"] = "confirm"
            bot.send_message(call.message.chat.id, "👀 <b>Предпросмотр рассылки:</b>", parse_mode="HTML")
            bot.copy_message(call.message.chat.id, state["chat_id"], state["message_id"])
            total = len(get_all_user_ids())
            bot.send_message(call.message.chat.id, f"Получателей: <b>{total}</b>. Запустить?",
                             reply_markup=broadcast_confirm_keyboard(), parse_mode="HTML")

        elif call.data == "admin_bc_cancel":
            bot.answer_callback_query(call.id, "Рассылка отменена.")
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
            user_ids = get_all_user_ids()
            bot.send_message(call.message.chat.id, f"🚀 Рассылка запущена: {len(user_ids)} получателей.")
            ok = 0
            failed = 0
            for uid in user_ids:
                try:
                    bot.copy_message(uid, state["chat_id"], state["message_id"],
                                     reply_markup=state.get("reply_markup"))
                    ok += 1
                except Exception:
                    failed += 1
                time.sleep(0.04)
            bot.send_message(call.message.chat.id,
                f"✅ Рассылка завершена. Доставлено: {ok}, ошибок: {failed}, всего: {len(user_ids)}.",
                reply_markup=admin_main_keyboard())
        return

    # --- Модерация заявок ---
    if call.data.startswith(("adm_approve_", "adm_reject_")):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "❌ Недостаточно прав!", show_alert=True)
            return
        order_id = int(call.data.split("_")[-1])
        if call.data.startswith("adm_approve_"):
            res = approve_order(order_id)
        else:
            res = reject_order(order_id)
        if not res.get("success"):
            bot.answer_callback_query(call.id, f"⚠️ {res.get('reason', 'Ошибка')}", show_alert=True)
            return
        bot.answer_callback_query(call.id, "✅ Заявка обработана", show_alert=True)
        target = res["user_id"]
        if call.data.startswith("adm_approve_"):
            msg = f"🎉 <b>Заказ #{order_id} подтверждён!</b>\nНачислено: <b>{res['gmp']:,} GMP</b>"
            try:
                sent = bot.send_message(target, msg, reply_markup=order_success_keyboard(), parse_mode="HTML")
                user_states[target] = {"state": "waiting_review", "order_id": order_id,
                                       "rub": res["rub"], "gmp": res["gmp"], "prompt_msg_id": sent.message_id}
            except Exception:
                logger.exception("Не удалось уведомить пользователя %s", target)
        else:
            try:
                bot.send_message(target, "❌ Ваша заявка отклонена. Если это ошибка, обратитесь в поддержку @KRABIK_TB.")
            except Exception:
                logger.exception("Не удалось уведомить пользователя %s", target)
        return

    # --- Кнопки пользователя ---
    if call.data == "back_to_main":
        bot.answer_callback_query(call.id)
        user_states[user_id] = None
        bonuses = get_user_bonuses(user_id)
        bot.edit_message_text(START_TEXT.format(name=name, bonus_block=build_bonus_countdown_block(bonuses)),
                              call.message.chat.id, call.message.message_id,
                              reply_markup=main_keyboard(), parse_mode="HTML")
    elif call.data == "open_channel":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(f"{EMOJI_CHANNEL} Наш официальный канал:", call.message.chat.id,
                              call.message.message_id, reply_markup=channel_keyboard(), parse_mode="HTML")
    elif call.data == "open_reviews":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(f"{EMOJI_STAR} Отзывы наших клиентов:", call.message.chat.id,
                              call.message.message_id, reply_markup=reviews_keyboard(), parse_mode="HTML")
    elif call.data == "open_support":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(f"{EMOJI_DEV} Служба поддержки: @KRABIK_TB", call.message.chat.id,
                              call.message.message_id, reply_markup=support_keyboard(), parse_mode="HTML")
    elif call.data == "open_referral":
        bot.answer_callback_query(call.id)
        bonuses = get_user_bonuses(user_id)
        bot_user = bot.get_me().username or "GMPBot"
        ref_link = f"https://t.me/{bot_user}?start=ref_{user_id}"
        status = (f"🟢 <b>Активен (+2%)</b>, осталось <b>{bonuses['ref_time_str']}</b>" if bonuses["ref_2"]
                  else "⚪️ <i>Не активен</i>")
        ref_text = (f"👥 <b>РЕФЕРАЛЬНАЯ СИСТЕМА</b>\n━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Приглашай друзей и получай <b>+2% к каждому заказу</b>.\n"
                    f"Приглашено: <b>{bonuses['refs_count']}</b>\nСтатус: {status}\n\n"
                    f"Ссылка: <code>{ref_link}</code>")
        bot.edit_message_text(ref_text, call.message.chat.id, call.message.message_id,
                              reply_markup=referral_keyboard(bot_user, user_id), parse_mode="HTML")
    elif call.data in ("profile", "open_profile"):
        bot.answer_callback_query(call.id)
        stats = get_user_stats(user_id)
        bonuses = get_user_bonuses(user_id)
        text = (f"👤 <b>МОЙ ПРОФИЛЬ</b>\nID: <code>{user_id}</code>\nИмя: {name}\n"
                f"Юзернейм: {username}\n\n📊 <b>СТАТИСТИКА</b>\n"
                f"Заказов: <b>{stats['total_orders']}</b>\nGMP: <b>{stats['total_gmp']:,}</b>\n"
                f"Потрачено: <b>{stats['total_rub']:,.0f} ₽</b>\nРефералов: <b>{bonuses['refs_count']}</b>")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=profile_keyboard(), parse_mode="HTML")
    elif call.data == "info_withdraw":
        bot.answer_callback_query(call.id)
        text = (f"{EMOJI_SUNFLOWER} <b>Время вывода GMP</b>\n\nОбычно 5–10 минут.\n\n"
                f"{EMOJI_MUSHROOM} <b>Время активности администратора (МСК):</b>\n"
                "Будни: 01:15–17:30\nСуббота: 01:15–18:30\nВоскресенье: 04:30–18:00")
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=back_keyboard(), parse_mode="HTML")
    elif call.data == "buy_gmp":
        bot.answer_callback_query(call.id)
        user_states[user_id] = "awaiting_rubles"
        bonuses = get_user_bonuses(user_id)
        rates = get_gmp_rates()
        hints = []
        if bonuses["welcome_5"]:
            hints.append(f"🎁 Бонус +5% ({bonuses['welcome_time_str']})")
        if bonuses["ref_2"]:
            hints.append(f"👥 Реферальный бонус +2% ({bonuses['ref_time_str']})")
        hint_text = "\n" + "\n".join(hints) if hints else ""
        text = (f"{EMOJI_CARD} <b>Отправьте сумму в рублях</b>\n"
                "Минимум: 50 ₽, максимум: 50 000 ₽\n\n"
                f"{EMOJI_CHART_UP} <b>Актуальный курс:</b>\n"
                f"До 499 ₽: {rates['under_500']:.4f} ₽ = 1 GMP\n"
                f"От 500 ₽: {rates['from_500']:.4f} ₽ = 1 GMP{hint_text}")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.send_message(call.message.chat.id, text, reply_markup=back_keyboard(), parse_mode="HTML")
    elif call.data == "pay_confirm":
        state_data = user_states.get(user_id)
        if not isinstance(state_data, dict) or not state_data.get("file_id"):
            bot.answer_callback_query(call.id, "Сначала отправьте чек.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        rub, gmp = state_data["rub"], state_data["gmp"]
        order_id = create_pending_order(user_id, rub, gmp, state_data["file_id"], state_data.get("bonus_desc"))
        user_states[user_id] = None
        admin_text = (f"🔔 <b>Заявка #{order_id}</b>\nПользователь: {name}\nID: <code>{user_id}</code>\n"
                      f"Юзернейм: {username}\nСумма: <b>{rub} ₽</b>\nGMP: <b>{gmp:,}</b>")
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_approve_{order_id}"),
               types.InlineKeyboardButton("❌ Отклонить", callback_data=f"adm_reject_{order_id}"))
        for admin_id in ADMIN_IDS:
            try:
                if state_data.get("is_photo"):
                    bot.send_photo(admin_id, state_data["file_id"], caption=admin_text, reply_markup=kb, parse_mode="HTML")
                else:
                    bot.send_document(admin_id, state_data["file_id"], caption=admin_text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                logger.exception("Ошибка отправки заявки админу %s", admin_id)
        bot.send_message(call.message.chat.id, f"✅ Заявка #{order_id} отправлена на проверку.", reply_markup=back_keyboard())
    elif call.data == "my_orders_stub":
        bot.answer_callback_query(call.id)
        rows = get_user_orders_history(user_id)
        if not rows:
            bot.send_message(call.message.chat.id, "📋 Заказов пока нет.")
        else:
            text = "📋 <b>Последние заказы:</b>\n"
            for oid, rub, gmp, status, date in rows:
                text += f"#{oid} · {date} · {status} · {rub:.0f} ₽ → {gmp:,} GMP\n"
            bot.send_message(call.message.chat.id, text, reply_markup=back_keyboard(), parse_mode="HTML")


# ==============================================================================
# ОТЗЫВЫ
# ==============================================================================
ALL_MEDIA_TYPES = ['text', 'photo', 'video', 'document', 'audio', 'voice', 'video_note', 'sticker', 'animation']


def is_review_message(message):
    if message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        if "Понравился сервис?" in reply_text or "успешно выполнен" in reply_text:
            return True
    state = user_states.get(message.from_user.id)
    if isinstance(state, dict) and state.get("state") == "waiting_review":
        if message.text and (message.text.startswith("/") or is_probable_amount(message.text)):
            return False
        return True
    return False


@bot.message_handler(func=is_review_message, content_types=ALL_MEDIA_TYPES)
def handle_review_reply(message):
    user_id = message.from_user.id
    if not message.from_user.username:
        logger.info("Отзыв user_id=%s пропущен: нет username", user_id)
        return
    buyer_username = f"@{message.from_user.username}"
    text_content = message.text or message.caption or ""
    if "@KRABIK_TB" not in text_content.upper():
        logger.info("Отзыв от %s пропущен: нет тега @KRABIK_TB", buyer_username)
        return

    state = user_states.pop(user_id, None)
    order_id = state.get("order_id") if isinstance(state, dict) else None
    rub = state.get("rub") if isinstance(state, dict) else None
    gmp = state.get("gmp") if isinstance(state, dict) else None
    if message.reply_to_message:
        reply_text = message.reply_to_message.text or message.reply_to_message.caption or ""
        match = re.search(r"Заказ #(\d+)", reply_text)
        if match:
            order_id = int(match.group(1))
    if not order_id or rub is None or gmp is None:
        last_order = get_last_approved_order(user_id)
        if last_order:
            order_id, rub, gmp = last_order["order_id"], last_order["rub"], last_order["gmp"]
        else:
            order_id, rub, gmp = order_id or 112, rub or 0, gmp or 0

    create_review(order_id, user_id, rub, gmp)
    safe_text = (text_content or "[медиа-отзыв без текста]")
    admin_review_msg = (
        f"Отзыв #{order_id}\nПродано — {int(gmp)} GMP\nЗа {rub:g} ₽\n"
        f"{buyer_username}:\n\n{safe_text}"
    )
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, admin_review_msg)
        except Exception:
            logger.exception("Ошибка отправки отзыва админу %s", admin_id)
    try:
        bot.reply_to(message, "🎉 Спасибо за отзыв! Он передан администраторам.")
    except Exception:
        logger.exception("Не удалось подтвердить получение отзыва")


# ==============================================================================
# МЕДИА / ЧЕКИ / РАССЫЛКА
# ==============================================================================
@bot.message_handler(content_types=['photo', 'document', 'video', 'animation'])
def handle_media(message):
    user_id = message.from_user.id
    if user_id in ADMIN_IDS and user_id in admin_broadcast_state:
        state = admin_broadcast_state[user_id]
        if state.get("step") == "waiting_content":
            state.update({"message_id": message.message_id, "chat_id": message.chat.id, "step": "waiting_buttons"})
            bot.send_message(message.chat.id,
                "🔘 Отправьте кнопки в формате <code>Текст | https://ссылка</code> или нажмите «Пропустить».",
                reply_markup=skip_buttons_keyboard(), parse_mode="HTML")
            return
    state_data = user_states.get(user_id)
    if isinstance(state_data, dict) and state_data.get("state") == "awaiting_screenshot":
        media = getattr(message, message.content_type, None)
        if message.content_type == "photo":
            file_id, is_photo = message.photo[-1].file_id, True
        elif media and getattr(media, "file_id", None):
            file_id, is_photo = media.file_id, False
        else:
            return
        state_data.update({"file_id": file_id, "is_photo": is_photo})
        bot.reply_to(message, "📸 Чек получен. Нажмите «Подтвердить оплату».")


@bot.message_handler(content_types=['text'])
def handle_all_text(message):
    user_id = message.from_user.id
    name = message.from_user.first_name or "Пользователь"
    username = f"@{message.from_user.username}" if message.from_user.username else "Не установлен"

    referrer_id = None
    if message.text.startswith("/start"):
        parts = message.text.split()
        if len(parts) > 1 and parts[1].startswith("ref_"):
            try:
                referrer_id = int(parts[1][4:])
            except ValueError:
                pass
    is_new, ref_extended = upsert_user(user_id, name, username, referrer_id)
    if ref_extended and referrer_id:
        try:
            bot.send_message(referrer_id, f"🎉 По вашей ссылке зарегистрировался новый пользователь ({name})! Бонус +2% продлён на 24 часа.")
        except Exception:
            logger.exception("Ошибка уведомления реферера")

    if user_id in ADMIN_IDS and user_id in admin_broadcast_state:
        state = admin_broadcast_state[user_id]
        if state.get("step") == "waiting_content":
            state.update({"message_id": message.message_id, "chat_id": message.chat.id, "step": "waiting_buttons"})
            bot.send_message(message.chat.id,
                "🔘 Отправьте кнопки в формате <code>Текст | https://ссылка</code> или нажмите «Пропустить».",
                reply_markup=skip_buttons_keyboard(), parse_mode="HTML")
            return
        if state.get("step") == "waiting_buttons":
            state["reply_markup"] = parse_broadcast_buttons(message.text)
            state["step"] = "confirm"
            bot.send_message(message.chat.id, "👀 Предпросмотр рассылки:")
            bot.copy_message(message.chat.id, state["chat_id"], state["message_id"], reply_markup=state["reply_markup"])
            bot.send_message(message.chat.id, f"Получателей: {len(get_all_user_ids())}. Запустить?",
                             reply_markup=broadcast_confirm_keyboard())
            return

    if message.text.startswith("/start"):
        user_states[user_id] = None
        bonuses = get_user_bonuses(user_id)
        bot.send_message(message.chat.id,
            START_TEXT.format(name=name, bonus_block=build_bonus_countdown_block(bonuses)),
            reply_markup=main_keyboard(), parse_mode="HTML")
        return

    if user_states.get(user_id) == "awaiting_rubles" or is_probable_amount(message.text):
        clean = re.sub(r'[₽ррууббlluubbr]', '', message.text.lower().replace(" ", "")).replace(",", ".")
        try:
            rub = float(clean)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Не удалось распознать сумму. Введите, например, 500.")
            return
        if not 50 <= rub <= 50000:
            bot.send_message(message.chat.id, "❗️ Сумма должна быть от 50 до 50000 ₽.")
            return

        rates = get_gmp_rates()
        rate = rates["under_500"] if rub <= 499 else rates["from_500"]
        base_gmp = int(rub / rate)
        bonuses = get_user_bonuses(user_id)
        bonus_percent = (5 if bonuses["welcome_5"] else 0) + (2 if bonuses["ref_2"] else 0)
        bonus_gmp = int(base_gmp * bonus_percent / 100)
        final_gmp = base_gmp + bonus_gmp
        notes = []
        if bonuses["welcome_5"]:
            notes.append("+5% приветственный бонус")
        if bonuses["ref_2"]:
            notes.append("+2% реферальный бонус")
        bonus_desc = ", ".join(notes) or None
        user_states[user_id] = {
            "state": "awaiting_screenshot", "rub": int(rub), "gmp": final_gmp,
            "base_gmp": base_gmp, "bonus_desc": bonus_desc, "file_id": None, "is_photo": True,
        }
        rates_line = f"Курс: {rate:.4f} ₽ за 1 GMP."
        bonus_line = f"\n🎁 Бонус +{bonus_percent}%: +{bonus_gmp} GMP." if bonus_percent else ""
        text = (
            f"🪪 <b>{int(rub)} ₽ → {final_gmp:,} GMP</b>\n{rates_line}{bonus_line}\n\n"
            "📋 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ</b>\n"
            "Получатель: Демид Андреевич П.\nБанк: <b>ВТБ</b>\n"
            "СБП: <code>+79142515945</code>\n\n"
            "Укажите комментарий к переводу и отправьте чек в этот чат."
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✅ Подтвердить оплату", callback_data="pay_confirm"))
        kb.add(types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_main"))
        bot.send_message(message.chat.id, text, reply_markup=kb, parse_mode="HTML")


if __name__ == "__main__":
    init_db()
    try:
        bot.remove_webhook()
    except Exception:
        pass
    logger.info("🚀 Бот успешно запущен")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=10)
        except Exception:
            logger.exception("Сбой polling, переподключение через 5 секунд")
            time.sleep(5)
