"""Telegram notification construction and dispatch helpers."""

import argparse
import html
from typing import Optional

from modules.logging import Log
from modules.models import VariantUpdate
from modules.runtime import RunContext
from modules.telegram import TgNotify


def create_notifier(ctx: RunContext, args: argparse.Namespace) -> Optional[TgNotify]:
    if args.skip_telegram or args.register_update:
        return None

    token = ctx.env.get("bot_token")
    chat = ctx.env.get("chat_id")
    telegraph_token = ctx.env.get("telegraph_token")
    if not token or not chat or not telegraph_token:
        with ctx.notice_lock:
            if not ctx.telegram_notice_printed:
                if ctx.dry_run:
                    Log.i(
                        "Dry-run mode: Telegram env vars not set; notifications skipped."
                    )
                else:
                    Log.w("Telegram env vars not set, skipping notifications")
                ctx.telegram_notice_printed = True
        return None

    try:
        return TgNotify(token, chat, telegraph_token, session=ctx.session())
    except ValueError as exc:
        Log.e(f"Telegram setup failed: {exc}")
        return None


def is_sweep_mode(args: argparse.Namespace) -> bool:
    """Sweep mode = processing multiple configs in a single run (--config-dir).

    In sweep mode, Telegram notifications are buffered and drained at the end
    with SWEEP_TELEGRAM_DELAY-second gaps to avoid bursting the Telegram API.
    """
    return getattr(args, "config_dir", None) is not None


def telegram_available(ctx: RunContext, args: argparse.Namespace) -> bool:
    """True if a TgNotify would be created (env vars set, not skipped/disabled)."""
    if getattr(args, "skip_telegram", False) or getattr(args, "register_update", False):
        return False
    env = ctx.env
    return bool(
        env.get("bot_token") and env.get("chat_id") and env.get("telegraph_token")
    )


def build_notification_message(update: VariantUpdate) -> str:
    region_line = f" ({update.region_name})" if update.region_name else ""
    os_line = f"<b>OS:</b> {update.sdk_message}\n" if update.sdk_message else ""
    inc = update.data.get("post_build_incremental")
    spl = update.data.get("post_security_patch_level")
    build_date = update.data.get("build_date")
    return (
        f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
        f"<b>Device:</b> {update.cfg.model}{region_line}\n"
        f"\n"
        f"<b>Title:</b> {update.title}\n"
        f"{os_line}\n"
        f"{update.desc}\n\n"
        f"<b>Size:</b> {update.size}\n"
        + (f"<b>Incremental:</b> <code>{inc}</code>\n" if inc else "")
        + (f"<b>Security patch:</b> {spl}\n" if spl else "")
        + f"<b>Fingerprint:</b> <code>{update.target_fp}</code>"
        + (f"\n<b>Build date:</b> {build_date} (CST)" if build_date else "")
        + (
            f"\n<b>Google OTA link:</b> <code>{html.escape(update.url, quote=False)}</code>"
            if update.url
            else ""
        )
    )
