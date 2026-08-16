"""
Handles incoming Telegram updates (via webhook — see main.py's
/telegram/webhook route). This is the only place that parses user-facing
commands; access_control.py just tracks state.

Commands:
  Everyone:
    /start            -> welcome message (shows developer credit) + asks
                          for the password if not yet authorized
    <any other text>   -> if not authorized yet, treated as a password
                          attempt; the message is deleted after handling
                          either way so the password doesn't sit in the
                          chat history
  Admin only (ADMIN_CHAT_ID / defaults to CHAT_ID):
    /grant <chat_id> <days|unlimited>  -> authorize a user for N days
    /revoke <chat_id>                  -> remove a user's access
    /users                             -> list everyone with access + expiry
"""
import logging

from access_control import AccessControl
from config import Settings
from telegram_notifier import TelegramNotifier

log = logging.getLogger("smart_money_bot.bot_commands")


def _welcome_text(settings: Settings, already_authorized: bool, expiry_text: str = "") -> str:
    header = (
        f"🤖 *ربات رصد اسمارت مانی نوبیتکس*\n"
        f"👨‍💻 *توسعه‌دهنده:* {settings.developer_name}\n\n"
    )
    if already_authorized:
        return header + f"✅ شما در حال حاضر دسترسی دارید.\n⏳ *انقضا:* `{expiry_text}`"
    return header + "🔒 برای استفاده از ربات، لطفاً رمز عبور را ارسال کنید:"


def handle_update(update: dict, settings: Settings, access: AccessControl, notifier: TelegramNotifier) -> None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return  # ignore non-message updates (reactions, etc.)

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    message_id = message.get("message_id")
    if chat_id is None or not text:
        return
    chat_id = str(chat_id)

    is_admin = access.is_admin(chat_id)

    # ---------- admin commands ----------
    if is_admin and text.startswith("/grant"):
        _handle_grant(text, chat_id, settings, access, notifier)
        return
    if is_admin and text.startswith("/revoke"):
        _handle_revoke(text, chat_id, settings, access, notifier)
        return
    if is_admin and text.startswith("/users"):
        _handle_list_users(chat_id, access, notifier)
        return

    # ---------- /start ----------
    if text.startswith("/start"):
        already = access.is_authorized(chat_id)
        notifier.send(
            _welcome_text(settings, already, access.expiry_text(chat_id)),
            chat_id=chat_id,
        )
        return

    # ---------- password attempt ----------
    if not access.is_authorized(chat_id):
        if settings.bot_access_password and text == settings.bot_access_password:
            access.grant(chat_id, days=settings.default_access_duration_days or None,
                         label=chat.get("username", ""))
            notifier.send(
                f"✅ *دسترسی تایید شد.*\n⏳ *انقضا:* `{access.expiry_text(chat_id)}`\n\n"
                f"از این پس هشدارهای ورود/خروج پول هوشمند و رصد کیف‌پول برای شما ارسال می‌شود.",
                chat_id=chat_id,
            )
            if settings.admin_chat_id_resolved and settings.admin_chat_id_resolved != chat_id:
                notifier.send(f"👤 کاربر جدید تایید شد: `{chat_id}`", chat_id=settings.admin_chat_id_resolved)
        else:
            notifier.send("❌ رمز عبور اشتباه است.", chat_id=chat_id)
        # Delete the password attempt either way — it shouldn't sit visibly
        # in the chat history once it's served its purpose.
        if message_id:
            notifier.delete(message_id, chat_id=chat_id)
        return

    # Already-authorized user sent some other text — nothing to do (this
    # bot is alert-only; it doesn't have a general chat feature).


def _handle_grant(text: str, admin_chat_id: str, settings: Settings, access: AccessControl,
                   notifier: TelegramNotifier) -> None:
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
                   chat_id=admin_chat_id)
    notifier.send(
        f"🎉 دسترسی شما به ربات فعال شد.\n⏳ *انقضا:* `{access.expiry_text(target_chat_id)}`",
        chat_id=target_chat_id,
    )


def _handle_revoke(text: str, admin_chat_id: str, settings: Settings, access: AccessControl,
                    notifier: TelegramNotifier) -> None:
    parts = text.split()
    if len(parts) < 2:
        notifier.send("فرمت درست: `/revoke <chat_id>`", chat_id=admin_chat_id)
        return
    target_chat_id = parts[1]
    existed = access.revoke(target_chat_id)
    notifier.send("✅ دسترسی حذف شد." if existed else "این کاربر از قبل دسترسی نداشت.", chat_id=admin_chat_id)


def _handle_list_users(admin_chat_id: str, access: AccessControl, notifier: TelegramNotifier) -> None:
    users = access.list_users()
    if not users:
        notifier.send("هیچ کاربری (به‌جز ادمین) دسترسی ندارد.", chat_id=admin_chat_id)
        return
    lines = ["👥 *کاربران مجاز:*\n"]
    for chat_id, entry in users.items():
        expires_at = entry.get("expires_at") or "نامحدود"
        label = entry.get("label") or ""
        lines.append(f"`{chat_id}` {('(' + label + ')') if label else ''} — تا `{expires_at}`")
    notifier.send("\n".join(lines), chat_id=admin_chat_id)