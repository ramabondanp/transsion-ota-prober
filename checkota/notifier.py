"""Telegram notification construction and dispatch helpers."""

import argparse
import html
from typing import Optional

from checkota.logging import Log
from checkota.models import VariantUpdate
from checkota.runtime import RunContext
from checkota.telegram import TgNotify


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


def build_notification_message(update: VariantUpdate) -> str:
    E = html.escape  # local alias; quote=False keeps URLs unquoted
    region_line_raw = f" ({update.region_name})" if update.region_name else ""
    # os_line is built HTML (literal <b>OS:</b>); escape only the sdk content.
    sdk = E(str(update.sdk_message), quote=False) if update.sdk_message else None
    os_line = f"<b>OS:</b> {sdk}\n" if sdk else ""
    inc = update.data.get("post_build_incremental")
    spl = update.data.get("post_security_patch_level")
    build_date = update.data.get("build_date")
    return (
        f"<blockquote><b>OTA Update Available</b></blockquote>\n\n"
        f"<b>Device:</b> {E(str(update.cfg.model), quote=False)}{E(region_line_raw, quote=False)}\n"
        f"\n"
        f"<b>Title:</b> {E(str(update.title), quote=False)}\n"
        f"{os_line}\n"
        # OTA descriptions are HTML-ish (<small>/<font>/<br>) and must stay
        # raw here so TgNotify._sanitize_html can normalize them before send.
        f"{str(update.desc)}\n\n"
        f"<b>Size:</b> {E(str(update.size), quote=False)}\n"
        + (
            f"<b>Incremental:</b> <code>{E(str(inc), quote=False)}</code>\n"
            if inc
            else ""
        )
        + (f"<b>Security patch:</b> {E(str(spl), quote=False)}\n" if spl else "")
        + f"<b>Fingerprint:</b> <code>{E(str(update.target_fp), quote=False)}</code>"
        + (
            f"\n<b>Build date:</b> {E(str(build_date), quote=False)} (CST)"
            if build_date
            else ""
        )
        + (
            f"\n<b>Google OTA link:</b> <code>{E(update.url, quote=False)}</code>"
            if update.url
            else ""
        )
    )
