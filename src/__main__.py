"""Entry point for eNkrypt's Steam Redeemer (python -m src)."""

from __future__ import annotations

import argparse
import sys

from src.chooser import humble_chooser_mode
from src.export import export_mode
from src.ownership import DEFAULT_FILTER_THRESHOLD, DEFAULT_MATCH_THRESHOLD
from src.redeemer import redeem_steam_keys
from src.utils import (
    console,
    find_dict_keys,
    print_error,
    print_info,
    print_rule,
    print_success,
    print_warning,
    prompt_menu,
)

_MODES = ["Auto-Redeem", "Export keys", "Humble Choice chooser"]


def prompt_mode() -> str:
    """Prompt the user to select an operating mode."""
    print_rule("Select Mode")
    idx = prompt_menu(_MODES)
    return str(idx + 1)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="steam-redeemer",
        description="Bulk-redeem Humble Bundle Steam keys.",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Non-interactive mode for cron/scheduled runs. "
        "Requires valid saved sessions in .state/.",
    )
    parser.add_argument(
        "--reveal-all",
        action="store_true",
        help="With --auto: reveal and redeem unrevealed keys even without "
        "ownership data. Default is to skip unrevealed keys to preserve gift links.",
    )
    parser.add_argument(
        "--ownership-threshold",
        type=int,
        default=None,
        metavar="0-100",
        help="Override both fuzzy-match thresholds used for ownership "
        f"detection (defaults: filter={DEFAULT_FILTER_THRESHOLD}, "
        f"match={DEFAULT_MATCH_THRESHOLD}) with a single stricter value, e.g. "
        "85. The defaults can let loosely-related titles match (e.g. "
        "'Spirit' matching 'The Spirit Lift'); raise this if genuinely "
        "unowned games are being skipped as already-owned.",
    )
    return parser.parse_args(argv)


def _filter_previous_keys(steam_keys: list[dict]) -> list[dict]:
    """Drop keys already recorded in errored/already_owned/redeemed CSVs."""
    for filter_file in ["errored.csv", "already_owned.csv", "redeemed.csv"]:
        try:
            with open(filter_file, "r", encoding="utf-8-sig") as f:
                rows = [line.strip().split(",") for line in f if line.strip()]
            seen_steam_keys = {row[2] for row in rows if len(row) >= 3 and row[2]}
            steam_keys = [
                key for key in steam_keys
                if key.get("redeemed_key_val", "") not in seen_steam_keys
            ]
        except Exception:
            pass
    return steam_keys


def main(argv: list[str] | None = None) -> None:
    """Main orchestration: Humble login -> fetch orders -> mode selection -> dispatch."""
    from src.humble_browser import HumbleBrowser, HumbleLoginRequired

    args = _parse_args(argv)

    # Redirect stderr to error.log
    sys.stderr = open("error.log", "a")

    browser = HumbleBrowser()
    try:
        try:
            browser.ensure_logged_in(interactive=not args.auto)
        except HumbleLoginRequired:
            print_error(
                "Humble session expired \u2014 run without --auto to log in "
                "(needs your authenticator code)."
            )
            sys.exit(1)
        print_success("Successfully signed in on Humble.")

        with console.status("Fetching Humble orders\u2026", spinner="dots"):
            order_details, failed = browser.fetch_order_details()

        print_success(f"Fetched {len(order_details)} orders from Humble.")
        if failed:
            print_warning(
                f"Couldn't fetch {len(failed)} orders — continuing without "
                f"them. Re-run to pick them up."
            )

        if not args.auto:
            desired_mode = prompt_mode()
            if desired_mode == "2":
                export_mode(browser, order_details)
                sys.exit()
            if desired_mode == "3":
                humble_chooser_mode(browser, order_details)
                sys.exit()

        # Auto-Redeem mode
        steam_keys = list(find_dict_keys(order_details, "steam_app_id", True))
        original_length = len(steam_keys)
        steam_keys = _filter_previous_keys(steam_keys)
        if len(steam_keys) != original_length:
            print_info(
                f"Filtered {original_length - len(steam_keys)} keys from previous runs"
            )

        revealed = sum(1 for k in steam_keys if k.get("redeemed_key_val"))
        unrevealed = len(steam_keys) - revealed

        print_rule("Key Summary")
        console.print(f"[bold]{len(steam_keys)}[/bold] Steam keys total")
        console.print(
            f"[green]{revealed}[/green] revealed  \u00b7  "
            f"[yellow]{unrevealed}[/yellow] unrevealed"
        )
        console.print()

        redeem_steam_keys(
            browser,
            steam_keys,
            auto=args.auto,
            reveal_all=args.reveal_all,
            ownership_threshold=args.ownership_threshold,
        )
    finally:
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n  [dim]Interrupted by user.[/dim]")
        sys.exit(130)
