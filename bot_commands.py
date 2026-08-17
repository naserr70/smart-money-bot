"""
Persian inline-button menu + command handling for incoming Telegram
updates (via webhook — see main.py's /telegram/webhook route).
access_control.py just tracks state; this is the only place that builds UI
or parses user input.

Menu (available to everyone once authorized):
  📊 اطلاعات من — shows role, join date, expiry, days remaining

Admin-only menu items (admin = ADMIN_CHAT_ID / defaults to CHAT_ID):
  👥 لیست کاربران   — list every active user + any unused invite passwords
  📈 وضعیت ربات     — bot health (last cycle times, errors, active users)
  ➕ اعطای دسترسی   — create a UNIQUE password for one person, with its own
                       access duration, without needing to know their
                       chat_id in advance. They redeem it themselves by
                       sending that password to the bot.
  ➖ حذف دسترسی     — revoke an already-authorized user's access (by chat_id)
  📢 ارسال پیام به همه — admin types any message, it's sent to every active
                       user right now (also doubles as a delivery test)
  🧪 تست سیگنال      — pushes a fake signal through the exact same
                       to_telegram()+broadcast_chunked path real signals use

Old-style text commands (/grant <chat_id> <days>, /revoke, /users) still
work too, for anyone who prefers typing and already knows a chat_id.

Messages use Telegram's HTML parse_mode — every non-literal string (a
password the admin typed, a Telegram username, a chat_id string, etc.) goes
through formatting.esc() before being interpolated, so a value containing
'<' or '&' can't break message parsing the way unescaped Markdown
characters used to ("can't parse entities").

Multi-step admin flows track a small amount of per-admin in-memory "what
are we waiting for" state in `_pending`. This is NOT persisted across
restarts — if the service restarts mid-flow, the admin just taps the
button again.

Every admin-gate check logs its outcome (chat_id + result) — if a button
looks unresponsive, the Render logs will show exactly whether the request
even arrived and what `is_admin` resolved to, which is almost always a
mismatched ADMIN_CHAT_ID rather than a code bug.
"""
import logging
from typing import Callable, Dict, Optional

from access_control import AccessControl
from config import Settings
from formatting import esc
from signals import MarketSignal, SignalDirection, TriggerType
from telegram_notifier import TelegramNotifier

log = logging.getLogger("smart_money_bot.bot_commands")

# admin_chat_id -> {"action": str, ...}
_pending: Dict[str, dict] = {}

CANCEL_WORDS = {"لغو", "cancel", "/cancel"}


# ==================== menu builders ====================

def _main_menu_markup(is_admin: bool) -> dict:
    rows = [[{"text": "📊 اطلاعات من", "callback_data": "info"}]]
    if is_admin:
        rows.append([{"text": "👥 لیست کاربران", "callback_data": "admin_users"},
                     {"text": "📈 وضعیت ربات", "callback_data": "admin_status"}])
        rows.append([{"text": "➕ اعطای دسترسی", "callback_data": "admin_grant"},
                     {"text": "➖ حذف دسترسی", "callback_data": "admin_revoke"}])
        rows.append([{"text": "📢 ارسال پیام به همه", "callback_data": "admin_testsend"},
                     {"text": "🧪 تست سیگنال", "callback_data": "admin_testsignal"}])
    return {"inline_keyboard": rows}


def _back_markup() -> dict:
    return {"inline_keyboard": [[{"text": "🔙 بازگشت به منو", "callback_data": "back"}]]}


def _cancel_markup() -> dict:
    return {"inline_keyboard": [[{"text": "❌ لغو", "callback_data": "cancel"}]]}


def _days_choice_markup(prefix: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "۷ روز", "callback_data": f"{prefix}:7"}, {"text": "۳۰ روز", "callback_data": f"{prefix}:30"}],
        [{"text": "۹۰ روز", "callback_data": f"{prefix}:90"}, {"text": "نامحدود", "callback_data": f"{prefix}:unlimited"}],
        [{"text": "❌ لغو", "callback_data": "cancel"}],
    ]}


def _welcome_text(settings: Settings, already_authorized: bool, expiry_text: str = "") -> str:
    header = f"🤖 <b>ربات رصد اسمارت مانی نوبیتکس</b>\n👨‍💻 <b>توسعه‌دهنده:</b> {esc(settings.developer_name)}\n\n"
    if already_authorized:
        return header + "از منوی زیر استفاده کنید 👇"
    return header + "🔒 برای استفاده از ربات، لطفاً رمز عبور را ارسال کنید:"


def _info_text(chat_id: str, access: AccessControl) -> str:
    is_admin = access.is_admin(chat_id)
    if is_admin:
        role, expiry, joined, days_left_text = "ادمین 👑", "نامحدود", "-", "نامحدود"
    else:
        entry = access.get_entry(chat_id) or {}
        role = "کاربر عادی"
        expiry = access.expiry_text(chat_id)
        joined = entry.get("granted_at", "-")
        days_left = access.days_remaining(chat_id)
        days_left_text = "نامحدود" if days_left is None else f"{days_left:.1f} روز"
    return (
        f"📊 <b>اطلاعات شما</b>\n\n"
        f"🆔 <b>شناسه چت:</b> <code>{esc(chat_id)}</code>\n"
        f"🎖 <b>نقش:</b> {role}\n"
        f"📅 <b>تاریخ عضویت:</b> <code>{esc(joined)}</code>\n"
        f"⏳ <b>تاریخ انقضا:</b> <code>{esc(expiry)}</code>\n"
        f"🗓 <b>روزهای باقی‌مانده:</b> <code>{esc(days_left_text)}</code>"
    )


def _users_list_text(access: AccessControl) -> str:
    users = access.list_users()
    lines = ["👥 <b>کاربران مجاز:</b>\n"]
    if not users:
        lines.append("هیچ کاربری (به‌جز ادمین) دسترسی ندارد.")
    else:
        for chat_id, entry in users.items():
            expires_at = entry.get("expires_at") or "نامحدود"
            label = entry.get("label") or ""
            label_part = f" ({esc(label)})" if label else ""
            lines.append(f"<code>{esc(chat_id)}</code>{label_part} — تا <code>{esc(expires_at)}</code>")

    invites = access.list_unused_invites()
    if invites:
        lines.append("\n🔑 <b>رمزهای ساخته‌شده و هنوز استفاده‌نشده:</b>\n")
        for password, entry in invites.items():
            days = entry.get("days")
            days_text = "نامحدود" if days is None else f"{days} روز"
            lines.append(f"<code>{esc(password)}</code> — {esc(days_text)}")
    return "\n".join(lines)


def _broadcast_targets(settings: Settings, access: AccessControl):
    admin_id = settings.admin_chat_id_resolved
    targets = ([admin_id] if admin_id else []) + access.active_chat_ids()
    return list(dict.fromkeys(t for t in targets if t))


def _broadcast_custom_message(text: str, settings: Settings, access: AccessControl, notifier: TelegramNotifier) -> str:
    """Sends an admin-authored message to every currently-active target
    RIGHT NOW and reports exactly which chat_ids succeeded and which
    failed — this doubles as a delivery diagnostic (like the old fixed
    test message did) while also being an actual announcement tool."""
    targets = _broadcast_targets(settings, access)
    if not targets:
        return "📤 هیچ گیرنده‌ی فعالی (حتی ادمین) پیدا نشد — چیزی برای ارسال نیست."

    message_text = f"📢 <b>پیام از ادمین:</b>\n\n{esc(text)}"
    ok, failed = [], []
    for target in targets:
        msg_id = notifier.send(message_text, chat_id=target)
        (ok if msg_id else failed).append(target)

    lines = [f"📤 <b>نتیجه‌ی ارسال</b> ({len(ok)}/{len(targets)} موفق):\n"]
    if failed:
        lines.append("❌ <b>ناموفق برای:</b> " + ", ".join(f"<code>{esc(t)}</code>" for t in failed))
        lines.append("\nدلیل دقیق هرکدوم رو توی لاگ Render، دنبال همین chat_id بگرد (خط «تلگرام خطای HTTP ...»). "
                     "علت رایج: آن کاربر هرگز مستقیماً با ربات /start نزده، یا ربات را بلاک کرده.")
    else:
        lines.append("✅ به همه با موفقیت ارسال شد.")
    return "\n".join(lines)


def _send_test_signal(settings: Settings, access: AccessControl, notifier: TelegramNotifier) -> str:
    """Builds a fake-but-realistic MarketSignal and pushes it through
    to_telegram() + broadcast_chunked — the EXACT same two functions a real
    market signal goes through in main.py's market_loop. This is what
    proves (or disproves) that the full signal pipeline reaches everyone,
    as opposed to just plain text messages (which _test_broadcast_text
    already confirmed). If this arrives for everyone but real signals
    still don't, the remaining explanation is simply that no real
    volume/price/whale event has crossed the configured thresholds yet —
    not a delivery problem."""
    fake_signal = MarketSignal(
        symbol="TEST", price=1.2345, change_5m=5.0, change_24h=12.3,
        inflow_usd=123456.0, spike_multiplier=4.2,
        direction=SignalDirection.INFLOW, trigger=TriggerType.STATIC, zscore=None,
    )
    targets = _broadcast_targets(settings, access)
    if not targets:
        return "هیچ گیرنده‌ی فعالی پیدا نشد."
    notifier.broadcast_chunked([fake_signal.to_telegram()], targets)
    recipients = "\n".join(f"• <code>{esc(t)}</code>" for t in targets)
    return (
        f"🧪 یک سیگنال آزمایشی، دقیقاً از همون کدی که سیگنال‌های واقعی رد می‌شن، "
        f"به {len(targets)} گیرنده فرستاده شد:\n\n{recipients}\n\n"
        f"اگه این پیام تستی («TEST») رو همه‌ی این افراد توی تلگرام دیدن ولی سیگنال واقعی نمی‌بینن، "
        f"یعنی مشکل ارسال نیست — فقط هنوز هیچ سیگنال واقعی‌ای (پامپ/نهنگ) از وقتی این کاربر اضافه شده رخ نداده."
    )


# ==================== top-level dispatch ====================

def handle_update(update: dict, settings: Settings, access: AccessControl, notifier: TelegramNotifier,
                   admin_status_provider: Optional[Callable[[], str]] = None) -> None:
    if "callback_query" in update:
        _handle_callback_query(update["callback_query"], settings, access, notifier, admin_status_provider)
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        log.info(f"نوع آپدیت پشتیبانی‌نشده نادیده گرفته شد: {list(update.keys())}")
        return

    chat = message.get("chat", {})
    raw_chat_id = chat.get("id")
    if raw_chat_id is None:
        return
    chat_id = str(raw_chat_id)
    text = (message.get("text") or "").strip()
    message_id = message.get("message_id")
    if not text:
        return

    is_admin = access.is_admin(chat_id)
    log.info(f"پیام از chat_id={chat_id} is_admin={is_admin} admin_chat_id_resolved={settings.admin_chat_id_resolved!r} text={text[:40]!r}")
    pending = _pending.get(chat_id)

    if text in CANCEL_WORDS and pending:
        _pending.pop(chat_id, None)
        notifier.send("لغو شد.", chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))
        return

    if is_admin and pending:
        if pending["action"] == "awaiting_invite_password":
            pending["action"], pending["password"] = "awaiting_invite_days", text
            notifier.send(f"⏳ برای رمز <code>{esc(text)}</code> چند روز دسترسی می‌دهید؟", chat_id=chat_id,
                          reply_markup=_days_choice_markup("invitedays"))
            return
        if pending["action"] == "awaiting_revoke_target":
            existed = access.revoke(text)
            _pending.pop(chat_id, None)
            notifier.send("✅ دسترسی حذف شد." if existed else "این کاربر از قبل دسترسی نداشت.",
                          chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))
            return
        if pending["action"] == "awaiting_broadcast_message":
            _pending.pop(chat_id, None)
            result_text = _broadcast_custom_message(text, settings, access, notifier)
            notifier.send(result_text, chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))
            return

    if text in ("/start", "/menu"):
        already = access.is_authorized(chat_id)
        notifier.send(_welcome_text(settings, already, access.expiry_text(chat_id)), chat_id=chat_id,
                      reply_markup=_main_menu_markup(is_admin) if already else None)
        return

    # Backward-compatible text commands (direct chat_id-based grant), still admin-only.
    if is_admin and text.startswith("/grant"):
        _legacy_grant(text, chat_id, access, notifier)
        return
    if is_admin and text.startswith("/revoke"):
        _legacy_revoke(text, chat_id, access, notifier)
        return
    if is_admin and text.startswith("/users"):
        notifier.send(_users_list_text(access), chat_id=chat_id, reply_markup=_back_markup())
        return

    # ---------- password gate ----------
    if not access.is_authorized(chat_id):
        found, days = access.consume_invite(text, chat_id)
        if found:
            access.grant(chat_id, days=days, label=chat.get("username", ""))
            _notify_new_authorization(settings, access, notifier, chat_id)
        elif settings.bot_access_password and text == settings.bot_access_password:
            access.grant(chat_id, days=settings.default_access_duration_days or None,
                         label=chat.get("username", ""))
            _notify_new_authorization(settings, access, notifier, chat_id)
        else:
            notifier.send("❌ رمز عبور اشتباه است.", chat_id=chat_id)
        if message_id:
            notifier.delete(message_id, chat_id=chat_id)  # don't leave the password sitting in the chat
        return

    # Authorized user sent free text with nothing pending — just show the menu.
    notifier.send("از منوی زیر استفاده کنید 👇", chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))


def _notify_new_authorization(settings: Settings, access: AccessControl, notifier: TelegramNotifier, chat_id: str) -> None:
    notifier.send(
        f"✅ <b>دسترسی تایید شد.</b>\n⏳ <b>انقضا:</b> <code>{esc(access.expiry_text(chat_id))}</code>",
        chat_id=chat_id, reply_markup=_main_menu_markup(access.is_admin(chat_id)),
    )
    admin_id = settings.admin_chat_id_resolved
    if admin_id and admin_id != chat_id:
        notifier.send(f"👤 کاربر جدید تایید شد: <code>{esc(chat_id)}</code>", chat_id=admin_id)


def _handle_callback_query(callback: dict, settings: Settings, access: AccessControl,
                            notifier: TelegramNotifier, admin_status_provider: Optional[Callable[[], str]]) -> None:
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {}) or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")

    notifier.answer_callback_query(callback_id)  # clears the button's loading spinner regardless

    if not chat_id or not message_id:
        log.warning(f"callback_query بدون chat_id/message_id قابل استفاده نادیده گرفته شد: data={data!r}")
        return

    if not access.is_authorized(chat_id):
        log.info(f"callback از chat_id غیرمجاز رد شد: {chat_id}")
        notifier.edit_message(chat_id, message_id, "🔒 دسترسی شما فعال نیست. لطفاً ابتدا رمز عبور را ارسال کنید.")
        return

    is_admin = access.is_admin(chat_id)
    log.info(f"callback data={data!r} از chat_id={chat_id} is_admin={is_admin}")

    if data == "info":
        notifier.edit_message(chat_id, message_id, _info_text(chat_id, access), reply_markup=_back_markup())
        return
    if data == "back":
        _pending.pop(chat_id, None)
        notifier.edit_message(chat_id, message_id, _welcome_text(settings, True, access.expiry_text(chat_id)),
                              reply_markup=_main_menu_markup(is_admin))
        return
    if data == "cancel":
        _pending.pop(chat_id, None)
        notifier.edit_message(chat_id, message_id, "لغو شد.", reply_markup=_main_menu_markup(is_admin))
        return

    if not is_admin:
        log.info(f"دسترسی ادمین رد شد برای chat_id={chat_id} (admin_chat_id_resolved={settings.admin_chat_id_resolved!r})")
        notifier.edit_message(chat_id, message_id, "⛔ این بخش فقط برای ادمین است.", reply_markup=_back_markup())
        return

    # ---------- admin-only callbacks ----------
    if data == "admin_users":
        notifier.edit_message(chat_id, message_id, _users_list_text(access), reply_markup=_back_markup())
        return
    if data == "admin_status":
        text = admin_status_provider() if admin_status_provider else "در دسترس نیست."
        notifier.edit_message(chat_id, message_id, text, reply_markup=_back_markup())
        return
    if data == "admin_testsend":
        _pending[chat_id] = {"action": "awaiting_broadcast_message"}
        notifier.edit_message(
            chat_id, message_id,
            "📝 پیامی که می‌خواهید برای همه‌ی کاربران فعال ارسال شود را بنویسید:",
            reply_markup=_cancel_markup(),
        )
        return
    if data == "admin_testsignal":
        notifier.edit_message(chat_id, message_id, "⏳ در حال ارسال سیگنال تستی...")
        result_text = _send_test_signal(settings, access, notifier)
        notifier.edit_message(chat_id, message_id, result_text, reply_markup=_back_markup())
        return

    if data == "admin_grant":
        _pending[chat_id] = {"action": "awaiting_invite_password"}
        notifier.edit_message(
            chat_id, message_id,
            "🔑 یک رمز عبور دلخواه <b>منحصربه‌فرد</b> برای این کاربر بنویسید (مثلاً یک اسم+عدد):\n\n"
            "این رمز را بعداً خودتان به همان شخص می‌دهید؛ او با ارسال همین رمز به ربات فعال می‌شود — "
            "لازم نیست از قبل chat_id او را بدانید.",
            reply_markup=_cancel_markup(),
        )
        return
    if data == "admin_revoke":
        _pending[chat_id] = {"action": "awaiting_revoke_target"}
        notifier.edit_message(chat_id, message_id, "🆔 شناسه چت کاربری که می‌خواهید دسترسی‌اش را حذف کنید ارسال کنید:",
                              reply_markup=_cancel_markup())
        return

    if data.startswith("invitedays:"):
        pending = _pending.get(chat_id)
        if not pending or pending.get("action") != "awaiting_invite_days":
            notifier.edit_message(chat_id, message_id, "این عملیات منقضی شده؛ دوباره از منو تلاش کنید.",
                                  reply_markup=_main_menu_markup(is_admin))
            return
        password = pending["password"]
        days_str = data.split(":", 1)[1]
        days = None if days_str == "unlimited" else float(days_str)
        access.create_invite(password, days=days)
        _pending.pop(chat_id, None)
        days_text = "نامحدود" if days is None else f"{days:.0f} روز"
        notifier.edit_message(
            chat_id, message_id,
            f"✅ رمز اختصاصی ساخته شد:\n\n🔑 رمز: <code>{esc(password)}</code>\n⏳ مدت: {esc(days_text)}\n\n"
            f"این رمز را به کاربر مورد نظر بدهید تا با ارسال /start و سپس این رمز، دسترسی‌اش فعال شود.",
            reply_markup=_main_menu_markup(is_admin),
        )
        return


# ==================== legacy text-command helpers (direct chat_id grant) ====================

def _legacy_grant(text: str, admin_chat_id: str, access: AccessControl, notifier: TelegramNotifier) -> None:
    parts = text.split()
    if len(parts) < 3:
        notifier.send("فرمت درست: /grant &lt;chat_id&gt; &lt;روز یا unlimited&gt;", chat_id=admin_chat_id)
        return
    target_chat_id, duration_str = parts[1], parts[2]
    days = None
    if duration_str.lower() != "unlimited":
        try:
            days = float(duration_str)
        except ValueError:
            notifier.send("مقدار روز باید عدد باشد یا unlimited.", chat_id=admin_chat_id)
            return
    access.grant(target_chat_id, days=days)
    notifier.send(f"✅ دسترسی برای <code>{esc(target_chat_id)}</code> تنظیم شد. انقضا: <code>{esc(access.expiry_text(target_chat_id))}</code>",
                  chat_id=admin_chat_id, reply_markup=_main_menu_markup(True))
    notifier.send(f"🎉 دسترسی شما فعال شد.\n⏳ <b>انقضا:</b> <code>{esc(access.expiry_text(target_chat_id))}</code>", chat_id=target_chat_id)


def _legacy_revoke(text: str, admin_chat_id: str, access: AccessControl, notifier: TelegramNotifier) -> None:
    parts = text.split()
    if len(parts) < 2:
        notifier.send("فرمت درست: /revoke &lt;chat_id&gt;", chat_id=admin_chat_id)
        return
    existed = access.revoke(parts[1])
    notifier.send("✅ دسترسی حذف شد." if existed else "این کاربر از قبل دسترسی نداشت.",
                  chat_id=admin_chat_id, reply_markup=_main_menu_markup(True))
