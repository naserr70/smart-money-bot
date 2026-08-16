"""
Persian inline-button menu + command handling for incoming Telegram
updates (via webhook — see main.py's /telegram/webhook route).
access_control.py just tracks state; this is the only place that builds UI
or parses user input.

Menu (available to everyone once authorized):
  📊 اطلاعات من — shows role, join date, expiry, days remaining

Admin-only menu items (admin = ADMIN_CHAT_ID / defaults to CHAT_ID):
  👥 لیست کاربران   — list every user with access + expiry
  📈 وضعیت ربات     — bot health (last cycle times, errors, active users)
  ➕ اعطای دسترسی   — grant a specific chat_id a specific number of days
  ➖ حذف دسترسی     — revoke a user's access

Old-style text commands (/grant, /revoke, /users) still work too, for
anyone who prefers typing over tapping.

Multi-step admin flows (grant/revoke) track a small amount of per-admin
in-memory "what are we waiting for" state in `_pending`. This is NOT
persisted across restarts — if the service restarts mid-flow, the admin
just taps the button again. That's an acceptable simplification here.
"""
import logging
from typing import Callable, Dict, Optional

from access_control import AccessControl
from config import Settings
from telegram_notifier import TelegramNotifier

log = logging.getLogger("smart_money_bot.bot_commands")

# admin_chat_id -> {"action": str, "target": str|None}
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
    return {"inline_keyboard": rows}


def _back_markup() -> dict:
    return {"inline_keyboard": [[{"text": "🔙 بازگشت به منو", "callback_data": "back"}]]}


def _cancel_markup() -> dict:
    return {"inline_keyboard": [[{"text": "❌ لغو", "callback_data": "cancel"}]]}


def _days_choice_markup() -> dict:
    return {"inline_keyboard": [
        [{"text": "۷ روز", "callback_data": "grantdays:7"}, {"text": "۳۰ روز", "callback_data": "grantdays:30"}],
        [{"text": "۹۰ روز", "callback_data": "grantdays:90"}, {"text": "نامحدود", "callback_data": "grantdays:unlimited"}],
        [{"text": "❌ لغو", "callback_data": "cancel"}],
    ]}


def _welcome_text(settings: Settings, already_authorized: bool, expiry_text: str = "") -> str:
    header = f"🤖 *ربات رصد اسمارت مانی نوبیتکس*\n👨‍💻 *توسعه‌دهنده:* {settings.developer_name}\n\n"
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
        f"📊 *اطلاعات شما*\n\n"
        f"🆔 *شناسه چت:* `{chat_id}`\n"
        f"🎖 *نقش:* {role}\n"
        f"📅 *تاریخ عضویت:* `{joined}`\n"
        f"⏳ *تاریخ انقضا:* `{expiry}`\n"
        f"🗓 *روزهای باقی‌مانده:* `{days_left_text}`"
    )


def _users_list_text(access: AccessControl) -> str:
    users = access.list_users()
    if not users:
        return "👥 *کاربران مجاز:*\n\nهیچ کاربری (به‌جز ادمین) دسترسی ندارد."
    lines = ["👥 *کاربران مجاز:*\n"]
    for chat_id, entry in users.items():
        expires_at = entry.get("expires_at") or "نامحدود"
        label = entry.get("label") or ""
        lines.append(f"`{chat_id}` {('(' + label + ')') if label else ''} — تا `{expires_at}`")
    return "\n".join(lines)


# ==================== top-level dispatch ====================

def handle_update(update: dict, settings: Settings, access: AccessControl, notifier: TelegramNotifier,
                   admin_status_provider: Optional[Callable[[], str]] = None) -> None:
    if "callback_query" in update:
        _handle_callback_query(update["callback_query"], settings, access, notifier, admin_status_provider)
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return  # ignore other update types (reactions, etc.)

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
    pending = _pending.get(chat_id)

    if text in CANCEL_WORDS and pending:
        _pending.pop(chat_id, None)
        notifier.send("لغو شد.", chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))
        return

    if is_admin and pending:
        if pending["action"] == "awaiting_grant_target":
            pending["action"], pending["target"] = "awaiting_grant_days", text
            notifier.send(f"⏳ برای `{text}` چند روز دسترسی می‌دهید؟", chat_id=chat_id,
                          reply_markup=_days_choice_markup())
            return
        if pending["action"] == "awaiting_revoke_target":
            existed = access.revoke(text)
            _pending.pop(chat_id, None)
            notifier.send("✅ دسترسی حذف شد." if existed else "این کاربر از قبل دسترسی نداشت.",
                          chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))
            return

    if text in ("/start", "/menu"):
        already = access.is_authorized(chat_id)
        notifier.send(_welcome_text(settings, already, access.expiry_text(chat_id)), chat_id=chat_id,
                      reply_markup=_main_menu_markup(is_admin) if already else None)
        return

    # Backward-compatible text commands, still admin-only.
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
        if settings.bot_access_password and text == settings.bot_access_password:
            access.grant(chat_id, days=settings.default_access_duration_days or None,
                         label=chat.get("username", ""))
            notifier.send(
                f"✅ *دسترسی تایید شد.*\n⏳ *انقضا:* `{access.expiry_text(chat_id)}`",
                chat_id=chat_id, reply_markup=_main_menu_markup(access.is_admin(chat_id)),
            )
            admin_id = settings.admin_chat_id_resolved
            if admin_id and admin_id != chat_id:
                notifier.send(f"👤 کاربر جدید تایید شد: `{chat_id}`", chat_id=admin_id)
        else:
            notifier.send("❌ رمز عبور اشتباه است.", chat_id=chat_id)
        if message_id:
            notifier.delete(message_id, chat_id=chat_id)  # don't leave the password sitting in the chat
        return

    # Authorized user sent free text with nothing pending — just show the menu.
    notifier.send("از منوی زیر استفاده کنید 👇", chat_id=chat_id, reply_markup=_main_menu_markup(is_admin))


def _handle_callback_query(callback: dict, settings: Settings, access: AccessControl,
                            notifier: TelegramNotifier, admin_status_provider: Optional[Callable[[], str]]) -> None:
    callback_id = callback.get("id")
    data = callback.get("data", "")
    message = callback.get("message", {}) or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")

    notifier.answer_callback_query(callback_id)  # clears the button's loading spinner regardless

    if not chat_id or not message_id:
        return

    if not access.is_authorized(chat_id):
        notifier.edit_message(chat_id, message_id, "🔒 دسترسی شما فعال نیست. لطفاً ابتدا رمز عبور را ارسال کنید.")
        return

    is_admin = access.is_admin(chat_id)

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
    if data == "admin_grant":
        _pending[chat_id] = {"action": "awaiting_grant_target"}
        notifier.edit_message(chat_id, message_id, "🆔 شناسه چت (chat_id) کاربر مورد نظر را در پیام بعدی ارسال کنید:",
                              reply_markup=_cancel_markup())
        return
    if data == "admin_revoke":
        _pending[chat_id] = {"action": "awaiting_revoke_target"}
        notifier.edit_message(chat_id, message_id, "🆔 شناسه چت کاربری که می‌خواهید حذفش کنید را ارسال کنید:",
                              reply_markup=_cancel_markup())
        return
    if data.startswith("grantdays:"):
        pending = _pending.get(chat_id)
        if not pending or pending.get("action") != "awaiting_grant_days":
            notifier.edit_message(chat_id, message_id, "این عملیات منقضی شده؛ دوباره از منو تلاش کنید.",
                                  reply_markup=_main_menu_markup(is_admin))
            return
        target = pending["target"]
        days_str = data.split(":", 1)[1]
        days = None if days_str == "unlimited" else float(days_str)
        access.grant(target, days=days)
        _pending.pop(chat_id, None)
        notifier.edit_message(
            chat_id, message_id,
            f"✅ دسترسی برای `{target}` تنظیم شد.\n⏳ انقضا: `{access.expiry_text(target)}`",
            reply_markup=_main_menu_markup(is_admin),
        )
        notifier.send(
            f"🎉 دسترسی شما به ربات فعال شد.\n⏳ *انقضا:* `{access.expiry_text(target)}`\n\n"
            f"برای مشاهده‌ی وضعیت خودتان دستور /start را بزنید.",
            chat_id=target,
        )
        return


# ==================== legacy text-command helpers ====================

def _legacy_grant(text: str, admin_chat_id: str, access: AccessControl, notifier: TelegramNotifier) -> None:
    parts = text.split()
    if len(parts) < 3:
        notifier.send("فرمت درست: `/grant <chat_id> <روز یا unlimited>`", chat_id=admin_chat_id)
        return
    target_chat_id, duration_str = parts[1], parts[2]
    days = None
    if duration_str.lower() != "unlimited":
        try:
            days = float(duration_str)
        except ValueError:
            notifier.send("مقدار روز باید عدد باشد یا `unlimited`.", chat_id=admin_chat_id)
            return
    access.grant(target_chat_id, days=days)
    notifier.send(f"✅ دسترسی برای `{target_chat_id}` تنظیم شد. انقضا: `{access.expiry_text(target_chat_id)}`",
                  chat_id=admin_chat_id, reply_markup=_main_menu_markup(True))
    notifier.send(f"🎉 دسترسی شما فعال شد.\n⏳ *انقضا:* `{access.expiry_text(target_chat_id)}`", chat_id=target_chat_id)


def _legacy_revoke(text: str, admin_chat_id: str, access: AccessControl, notifier: TelegramNotifier) -> None:
    parts = text.split()
    if len(parts) < 2:
        notifier.send("فرمت درست: `/revoke <chat_id>`", chat_id=admin_chat_id)
        return
    existed = access.revoke(parts[1])
    notifier.send("✅ دسترسی حذف شد." if existed else "این کاربر از قبل دسترسی نداشت.",
                  chat_id=admin_chat_id, reply_markup=_main_menu_markup(True))