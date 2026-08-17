"""
Shared helper for building Telegram messages safely.

Switching every message from Markdown to HTML parse_mode (see
telegram_notifier.py) fixes the class of bug where a password, Telegram
username, or any other user-typed text containing '_', '*', or '`' breaks
Telegram's legacy Markdown parser with "can't parse entities". HTML mode is
far more forgiving — but ANY dynamic/user-controlled text still needs to be
escaped before being placed between HTML tags, or a value containing '<' or
'&' would break HTML parsing instead. This is that one escaping function,
used everywhere a non-literal string gets interpolated into a message.
"""
import html


def esc(value) -> str:
    """Escape a value for safe interpolation into an HTML-parse-mode
    Telegram message. Always use this around anything that isn't a literal
    string you wrote yourself — passwords, usernames, labels, symbols/token
    names from external APIs, chat_ids typed by an admin, etc."""
    return html.escape(str(value), quote=False)
