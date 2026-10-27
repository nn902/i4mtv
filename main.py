import telebot
from telebot import types
import re

# Токен бота
TOKEN = "8853332126:AAGG3DJB0UFV9bOX8W4mXYEZ7rm28s9eHPQ"
bot = telebot.TeleBot(TOKEN)

# Список числовых ID администраторов
ADMIN_IDS = [6522383487, 7727345054]

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

# Состояния пользователей
user_states = {}

# Текст главного меню
START_TEXT = (
    f"{EMOJI_STAR} Здравствуй, {{name}}! {EMOJI_STAR}\n\n"
    f"{EMOJI_INVADER} Ты попал в бота-продавца GMP.\n"
    f"{EMOJI_LIGHT} Здесь ты можешь быстро приобрести GMP по выгодному курсу!\n\n"
    f"{EMOJI_DIAMOND} Наши преимущества:\n"
    f"• {EMOJI_ROCKET} Быстрая выдача\n"
    f"• {EMOJI_CARD} Удобные способы оплаты\n\n"
    f"{EMOJI_SPARKLES} Выбери что тебя интересует:"
)

# === КЛАВИАТУРЫ ===

def main_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    
    btn_buy = types.InlineKeyboardButton("💸 Купить GMP", callback_data="buy_gmp", style="success")
    btn_channel = types.InlineKeyboardButton("📢 Канал", callback_data="open_channel", style="primary")
    btn_support = types.InlineKeyboardButton("🆘 Поддержка", callback_data="open_support", style="primary")
    btn_reviews = types.InlineKeyboardButton("⭐ Отзывы", callback_data="open_reviews", style="primary")
    btn_profile = types.InlineKeyboardButton("👤 Мой профиль", callback_data="open_profile", style="primary")
    btn_info = types.InlineKeyboardButton("🌻 Информация о выводе", callback_data="info_withdraw", style="primary")
    
    btn_privacy = types.InlineKeyboardButton(
        "🔐 Политика конфиденциальности", 
        url="https://telegra.ph/Politika-konfidencialnosti-06-21-31"
    )
    btn_terms = types.InlineKeyboardButton(
        "📜 Пользовательское соглашение", 
        url="https://telegra.ph/Polzovatelskoe-soglashenie-04-01-19"
    )
    
    markup.add(btn_buy)
    markup.add(btn_channel, btn_support)
    markup.add(btn_reviews, btn_profile)
    markup.add(btn_info)
    markup.add(btn_privacy)
    markup.add(btn_terms)
    
    return markup

def channel_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("📢 Перейти в канал", url="https://t.me/Kitaez_com", style="success")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main", style="danger")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def reviews_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("⭐ Посмотреть отзывы", url="https://t.me/OTZIVI_KRABIKA_TB", style="success")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main", style="danger")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def support_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_link = types.InlineKeyboardButton("👤 Написать @KRABIK_TB", url="https://t.me/KRABIK_TB", style="success")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main", style="danger")
    markup.add(btn_link)
    markup.add(btn_back)
    return markup

def profile_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_orders = types.InlineKeyboardButton("📋 Все мои заказы", callback_data="my_orders_stub", style="primary")
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main", style="danger")
    markup.add(btn_orders)
    markup.add(btn_back)
    return markup

def back_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("🔙 Назад", callback_data="back_to_main", style="danger")
    markup.add(btn_back)
    return markup


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

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


# === ОБРАБОТКА НАЖАТИЙ НА КНОПКИ ===

@bot.callback_query_handler(func=lambda call: True)
def callback_inline(call):
    user_id = call.from_user.id
    name = call.from_user.first_name if call.from_user.first_name else "Пользователь"
    username = f"@{call.from_user.username}" if call.from_user.username else "Не установлен"
    
    if call.data == "back_to_main":
        bot.answer_callback_query(call.id)
        user_states[user_id] = None
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=START_TEXT.format(name=name),
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
        
    elif call.data in ["profile", "open_profile"]:
        bot.answer_callback_query(call.id)
        profile_text = (
            f"{EMOJI_USER} <b>МОЙ ПРОФИЛЬ</b>\n"
            f"{EMOJI_CARD} ID: <code>{user_id}</code>\n"
            f"{EMOJI_NAME_TAG} Имя: {name}\n"
            f"{EMOJI_USER} Юзернейм: {username}\n\n"
            f"{EMOJI_CHART} <b>СТАТИСТИКА ПОКУПОК</b>\n"
            f"🛒 Всего заказов: 0\n"
            f"{EMOJI_GIFT} Получено GMP: 0"
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
            "• Будние дни: 00:00 - 21:00\n"
            "• Выходные: 00:00 - 21:00\n\n"
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
        calc_text = (
            f"{EMOJI_CARD} <b>Для подсчёта GMP, отправьте сумму в рублях</b>\n"
            f"{EMOJI_WRITE} <i>Напишите просто числом (например: 500)</i>\n\n"
            f"{EMOJI_MONEY} Минимальная сумма: 50₽\n"
            f"{EMOJI_MONEY} Максимальная сумма: 50000₽\n\n"
            f"{EMOJI_CHART_UP} <b>Актуальный курс:</b>\n"
            f"{EMOJI_CHART} до 499₽: 0.38₽ = 1 GMP\n"
            f"{EMOJI_CHART} от 500₽: 0.37₽ = 1 GMP"
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

    # Кнопка подтверждения оплаты
    elif call.data == "pay_confirm":
        state_data = user_states.get(user_id)
        
        # Если скриншот еще не отправлен
        if isinstance(state_data, dict) and state_data.get("file_id") is None:
            bot.answer_callback_query(
                call.id, 
                text="Пожалуйста, сначала отправьте скриншот оплаты фото-сообщением в чат!", 
                show_alert=True
            )
            
        # Если скриншот уже успешно отправлен
        elif isinstance(state_data, dict) and state_data.get("file_id") is not None:
            bot.answer_callback_query(call.id)
            rub = state_data["rub"]
            gmp = state_data["gmp"]
            file_id = state_data["file_id"]
            is_photo = state_data["is_photo"]
            
            # Сбрасываем состояние
            user_states[user_id] = None
            
            admin_msg = (
                f"🔔 <b>Новая заявка на оплату!</b>\n\n"
                f"{EMOJI_USER} Пользователь: {name}\n"
                f"{EMOJI_CARD} ID: <code>{user_id}</code>\n"
                f"{EMOJI_NAME_TAG} Юзернейм: {username}\n\n"
                f"{EMOJI_MONEY} Сумма перевода: <b>{rub}₽</b>\n"
                f"{EMOJI_DIAMOND} GMP к выдаче: <b>{gmp} GMP</b>"
            )
            
            # Отправка обоим администраторам
            for admin in ADMIN_IDS:
                try:
                    if is_photo:
                        bot.send_photo(admin, file_id, caption=admin_msg, parse_mode="HTML")
                    else:
                        bot.send_document(admin, file_id, caption=admin_msg, parse_mode="HTML")
                except Exception as e:
                    print(f"[!] Ошибка отправки админу {admin}: {e}")

            bot.send_message(
                call.message.chat.id,
                f"{EMOJI_CHECK} <b>Ваша заявка и чек успешно отправлены администратору!</b>\n"
                "Пожалуйста, ожидайте зачисления в течение 5-10 минут.",
                reply_markup=back_keyboard(),
                parse_mode="HTML"
            )
        else:
            bot.answer_callback_query(call.id, text="Сессия оплаты истекла. Нажмите «Купить GMP» заново.", show_alert=True)

    elif call.data == "my_orders_stub":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, f"{EMOJI_CLIP} У вас пока нет совершенных заказов.", parse_mode="HTML")


# === ОБРАБОТЧИК СКРИНШОТОВ (ФОТО И ФАЙЛОВ) ===

@bot.message_handler(content_types=['photo', 'document'])
def handle_screenshot(message):
    user_id = message.from_user.id
    state_data = user_states.get(user_id)
    
    # Проверяем, ждет ли бот скриншот
    if isinstance(state_data, dict) and state_data.get("state") == "awaiting_screenshot":
        if message.content_type == 'photo':
            file_id = message.photo[-1].file_id
            is_photo = True
        else:
            file_id = message.document.file_id
            is_photo = False
            
        # Сохраняем скриншот в состояние пользователя
        state_data["file_id"] = file_id
        state_data["is_photo"] = is_photo
        user_states[user_id] = state_data
        
        bot.reply_to(
            message,
            f"{EMOJI_CAMERA} <b>Скриншот успешно получен!</b>\n\n"
            f"Теперь нажмите кнопку <b>«✅ Подтвердить оплату»</b> под сообщением с реквизитами выше, чтобы завершить оформление.",
            parse_mode="HTML"
        )


# === ЕДИНЫЙ ПЕРЕХВАТЧИК СООБЩЕНИЙ ===

@bot.message_handler(content_types=['text'])
def handle_all_text(message):
    user_id = message.from_user.id
    
    if message.text.startswith('/start'):
        user_states[user_id] = None
        name = message.from_user.first_name if message.from_user.first_name else "Пользователь"
        bot.send_message(
            message.chat.id, 
            START_TEXT.format(name=name), 
            reply_markup=main_keyboard(),
            parse_mode="HTML"
        )
        return

    state = user_states.get(user_id)
    
    # Срабатывает, если бот ждёт ввода суммы ИЛИ если текст похож на сумму
    if state == "awaiting_rubles" or is_probable_amount(message.text):
        
        # Очистка текста от лишних символов
        txt = message.text.lower().replace(" ", "")
        clean_text = re.sub(r'[₽ррууббlluubbr]', '', txt)
        clean_text = clean_text.replace(',', '.')

        try:
            rub_value = float(clean_text)
        except Exception:
            bot.send_message(
                message.chat.id, 
                "❌ Не удалось распознать сумму.\n"
                "Пожалуйста, напишите просто число (например: 540).\n"
                "Нажмите кнопку «💸 Купить GMP» заново.",
                parse_mode="HTML"
            )
            return

        if rub_value < 50 or rub_value > 50000:
            bot.send_message(
                message.chat.id, 
                f"{EMOJI_WARN} Сумма должна быть в пределах от <b>50₽</b> до <b>50000₽</b>.\n"
                "Нажмите кнопку «💸 Купить GMP» заново.",
                parse_mode="HTML"
            )
            return

        # Расчет GMP по курсу
        rate = 0.38 if rub_value <= 499 else 0.37
        gmp_value = int(rub_value / rate)

        # Переводим в состояние ожидания скриншота
        user_states[user_id] = {
            "state": "awaiting_screenshot",
            "rub": int(rub_value),
            "gmp": gmp_value,
            "file_id": None,
            "is_photo": True
        }

        requisites_text = (
            f"{EMOJI_CARD} <b>{int(rub_value)}₽ → {gmp_value} GMP</b>\n\n"
            f"{EMOJI_CLIP} <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:</b>\n"
            f"{EMOJI_WARN} Демид Андреевич П.\n"
            f"{EMOJI_WARN} Банк: ВТБ\n"
            f"{EMOJI_WARN} <i>Будьте внимательны, при переводе на другой банк средства не возвращаю, GMP не выдаю!</i>\n\n"
            f"{EMOJI_PHONE} <b>Номер телефона:</b>\n"
            f"<code>+79142515945</code>\n\n"
            f"{EMOJI_CARD} <b>Номер карты:</b>\n"
            f"<code>2200249202954571</code>\n\n"
            f"{EMOJI_CAMERA} <b>ИНСТРУКЦИЯ К ОПЛАТЕ:</b>\n"
            f"1. Переведите <b>{int(rub_value)}₽</b> по указанным реквизитам.\n"
            f"2. Сохраните чек или сделайте скриншот платежа.\n"
            f"3. <u><b>ОТПРАВЬТЕ СКРИНШОТ ФОТО-СООБЩЕНИЕМ В ЭТОТ ЧАТ!</b></u>\n"
            f"4. Нажмите кнопку «✅ Подтвердить оплату» ниже."
        )

        markup = types.InlineKeyboardMarkup()
        btn_confirm = types.InlineKeyboardButton("✅ Подтвердить оплату", callback_data="pay_confirm", style="success")
        btn_cancel = types.InlineKeyboardButton("❌ Отмена", callback_data="back_to_main", style="danger")
        markup.add(btn_confirm)
        markup.add(btn_cancel)

        bot.send_message(message.chat.id, requisites_text, reply_markup=markup, parse_mode="HTML")


# === ЗАПУСК БОТА ===
if __name__ == "__main__":
    try:
        bot.remove_webhook()
    except Exception:
        pass
        
    print("Бот успешно запущен!")
    bot.polling(none_stop=True, interval=0, timeout=20)
