"""
Telegram Outreach Console (Streamlit + Telethon)maa
Anti-ban: slow sends, daily caps, dedup list, halt on official flood signals.
Optimized with humanlike typing dynamics, spintax variations, and unique hash randomization.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import random
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st
from telethon import TelegramClient
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)

BASE_DIR = Path(__file__).resolve().parent
SENT_LIST_PATH = BASE_DIR / "sent_list.txt"
DAILY_COUNTER_PATH = BASE_DIR / "daily_send_counter.json"

DAILY_SEND_LIMIT = 20
DELAY_MIN_SEC = 300
DELAY_MAX_SEC = 600
FAIL_DELAY_MIN_SEC = 90
FAIL_DELAY_MAX_SEC = 180
CONSECUTIVE_FAIL_LIMIT = 3
FLOOD_WAIT_BUFFER_SEC = 30

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SendHalt(Exception):
    """Official Telegram rate-limit signal — stop immediately to protect the account."""

    def __init__(self, message: str, cooldown_sec: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.cooldown_sec = cooldown_sec


def parse_spintax(text: str) -> str:
    """
    解析文本中的 Spintax 语法。
    例如将 "{Hi|Hello} {friend|bro}" 随机转换为 "Hi bro" 或 "Hello friend"
    """
    pattern = re.compile(r"\{([^{}]+)\}")
    while True:
        match = pattern.search(text)
        if not match:
            break
        options = match.group(1).split("|")
        text = text.replace(match.group(0), random.choice(options), 1)
    return text


def build_message(name: str, signature: str, promo_body: str) -> str:
    """Greeting uses Excel `name`; Text variations applied to avoid blueprint text banning."""
    display_name = (name or "Customer").strip()
    
    # 针对签名和正文启用 SpinTax 解析
    sig_parsed = parse_spintax(signature or "Support").strip()
    body_parsed = parse_spintax(promo_body or "").strip()
    
    # 1. 自动对开头问候语进行随机变形
    greeting_templates = [
        f"Hi Mr/Ms {display_name}!",
        f"Hello Mr/Ms {display_name},",
        f"Good day Mr/Ms {display_name}!",
        f"Hi {display_name},"
    ]
    greeting = random.choice(greeting_templates)
    
    # 2. 自动对身份介绍语进行随机变形
    intro_templates = [
        f"This is {sig_parsed}.",
        f"I'm {sig_parsed} here.",
        f"{sig_parsed} here.",
    ]
    intro = random.choice(intro_templates)
    
    lines = [greeting, intro]
    if body_parsed:
        lines.append("")
        lines.append(body_parsed)
        
    # 3. 🛡️ 【微观字节防封特征锁】
    # 即使大段正文恰好抽到相同的组合，末尾的唯一识别码也会让整条消息的字节数据完全不同，打碎特征码过滤。
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    random_hash = "".join(random.choices(abc, k=4))
    lines.append("")
    lines.append(f"`[ID: #{random_hash}]`") # 以小字代码块格式附带在最底部
    
    return "\n".join(lines)


def normalize_phone(raw: Any) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    s = str(raw).strip()
    if s.endswith(".0"):
        s = s[:-2]
    s = re.sub(r"[^\d+]", "", s)
    if not s:
        return ""
    if not s.startswith("+"):
        s = "+" + s.lstrip("0")
    return s


def read_customer_file(uploaded_file) -> pd.DataFrame:
    """Load customer list from CSV or Excel (.xlsx, .xls, .xlsm, etc.)."""
    uploaded_file.seek(0)
    name = (uploaded_file.name or "").lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file)
    if name.endswith(".xls"):
        return pd.read_excel(uploaded_file, engine="xlrd")
    return pd.read_excel(uploaded_file)


def normalize_excel_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Map spreadsheet columns to: username (label), name (greeting), phone (send target)."""
    alias_map = {
        "username": (
            "username",
            "user name",
            "telegram username",
            "tg username",
        ),
        "name": ("name", "customer name", "display name"),
        "phone": (
            "phone",
            "mobile",
            "contact number",
            "contact",
            "phone number",
            "handphone",
        ),
    }
    rename: dict[str, str] = {}
    used: set[str] = set()
    for col in df.columns:
        key = str(col).strip().lower()
        for canonical, aliases in alias_map.items():
            if canonical in used:
                continue
            if key == canonical or key in aliases:
                rename[col] = canonical
                used.add(canonical)
                break
    return df.rename(columns=rename)


def row_username(row: pd.Series) -> str:
    v = row.get("username", "")
    if v is None or pd.isna(v):
        return ""
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return ""
    return s


def row_name(row: pd.Series) -> str:
    v = row.get("name", "")
    if v is None or pd.isna(v):
        return ""
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return ""
    return s


def row_customer_phone(row: pd.Series) -> str:
    return normalize_phone(row.get("phone"))


def is_effectively_empty_row(row: pd.Series) -> bool:
    return not (row_customer_phone(row) or row_name(row) or row_username(row))


def format_customer_label(row: pd.Series) -> str:
    parts: list[str] = []
    uname = row_username(row)
    if uname:
        parts.append(f"@{uname.lstrip('@')} (ref)")
    name = row_name(row)
    if name:
        parts.append(name)
    phone = row_customer_phone(row)
    if phone:
        parts.append(phone)
    return " · ".join(parts) if parts else "Unknown customer"


def session_path_for_phone(phone: str) -> Path:
    safe = re.sub(r"[^\d]", "", phone)
    return BASE_DIR / f"session_{safe}.session"


def load_sent_set() -> set[str]:
    if not SENT_LIST_PATH.exists():
        return set()
    with open(SENT_LIST_PATH, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def append_sent_target(target: str) -> None:
    key = target.strip().lower()
    if not key:
        return
    current_set = load_sent_set()
    if key in current_set:
        return
    with open(SENT_LIST_PATH, "a", encoding="utf-8") as f:
        f.write(key + "\n")


def target_key_from_row(row: pd.Series) -> str:
    phone = row_customer_phone(row)
    if phone:
        return phone.lower()
    uname = row_username(row)
    if uname:
        u = uname if uname.startswith("@") else f"@{uname}"
        return u.lower()
    return ""


def load_daily_count(phone: str) -> tuple[str, int]:
    today = date.today().isoformat()
    phone_key = re.sub(r"[^\d]", "", phone)
    if DAILY_COUNTER_PATH.exists():
        try:
            with open(DAILY_COUNTER_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
        except (json.JSONDecodeError, OSError):
            store = {}
    else:
        store = {}
    entry = store.get(phone_key, {})
    if entry.get("date") != today:
        return today, 0
    return today, int(entry.get("count", 0))


def save_daily_count(phone: str, count: int) -> None:
    today = date.today().isoformat()
    phone_key = re.sub(r"[^\d]", "", phone)
    store: dict[str, Any] = {}
    if DAILY_COUNTER_PATH.exists():
        try:
            with open(DAILY_COUNTER_PATH, "r", encoding="utf-8") as f:
                store = json.load(f)
        except (json.JSONDecodeError, OSError):
            store = {}
    store[phone_key] = {"date": today, "count": count}
    with open(DAILY_COUNTER_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)


def is_critical_rpc_error(exc: RPCError) -> bool:
    return type(exc).__name__ in {
        "PeerFloodError",
        "UserDeactivatedBanError",
        "UserDeactivatedError",
        "PhoneNumberBannedError",
    }


async def resolve_and_send(
    client: TelegramClient,
    row: pd.Series,
    message: str,
    banner_bytes: Optional[bytes],
    banner_name: Optional[str],
) -> None:
    contact = row_customer_phone(row)
    if not contact:
        raise ValueError("Missing customer phone number in this row")

    entity = await client.get_entity(contact)

    # 模拟手滑、停顿、看对话框的预备动作 (2.5 到 4.5秒)
    await asyncio.sleep(random.uniform(2.5, 4.5))

    try:
        # 触发打字中状态
        async with client.action(entity, "typing"):
            # 根据动态变化后的最终文本长度，实时计算打字机耗时
            typing_delay = len(message) * random.uniform(0.15, 0.25)
            typing_delay = min(typing_delay, 12.0)
            await asyncio.sleep(typing_delay)

        if banner_bytes:
            buf = io.BytesIO(banner_bytes)
            buf.name = banner_name or "banner.jpg"
            await client.send_file(
                entity,
                buf,
                caption=message,
                force_document=False,
                parse_mode="md",
            )
        else:
            await client.send_message(entity, message, parse_mode="md")

    except PeerFloodError as exc:
        raise SendHalt(
            "PeerFloodError: Telegram flagged this account as too active. "
            "Stop sending for at least 24 hours.",
        ) from exc
    except FloodWaitError as exc:
        raise SendHalt(
            f"FloodWaitError: Telegram requires a {exc.seconds}s wait. Task halted.",
            cooldown_sec=exc.seconds,
        ) from exc
    except ChatWriteForbiddenError as exc:
        raise SendHalt("Cannot message this user (no permission or blocked).") from exc
    except UserPrivacyRestrictedError as exc:
        raise ValueError("User privacy settings block messages from strangers") from exc
    except RPCError as exc:
        if is_critical_rpc_error(exc):
            raise SendHalt(f"{type(exc).__name__}: {exc}") from exc
        raise


async def ensure_client_authorized(
    client: TelegramClient,
    phone: str,
    tg_code: str,
    tg_password: str,
    phone_code_hash: Optional[str],
    log_callback,
) -> tuple[bool, Optional[str]]:
    if await client.is_user_authorized():
        log_callback("Using saved session — already signed in")
        return True, None

    if not tg_code:
        sent = await client.send_code_request(phone)
        log_callback("Verification code sent. Enter it below, then click Confirm & Send.")
        return False, sent.phone_code_hash

    try:
        await client.sign_in(
            phone,
            tg_code.strip(),
            phone_code_hash=phone_code_hash,
        )
    except SessionPasswordNeededError:
        if not tg_password:
            log_callback("2FA required. Enter password, then click Confirm & Send.")
            return False, phone_code_hash
        await client.sign_in(password=tg_password.strip())
    except PhoneCodeInvalidError:
        log_callback("Failed: invalid verification code")
        return False, phone_code_hash

    log_callback("Telegram sign-in successful")
    return True, None


async def run_send_pipeline(
    api_id: int,
    api_hash: str,
    phone: str,
    signature: str,
    promo_body: str,
    df: pd.DataFrame,
    banner_bytes: Optional[bytes],
    banner_name: Optional[str],
    tg_code: str,
    tg_password: str,
    phone_code_hash: Optional[str],
    log_callback,
) -> tuple[bool, Optional[str]]:
    session_file = str(session_path_for_phone(phone))
    client = TelegramClient(session_file, api_id, api_hash)
    fail_streak = 0

    try:
        await client.connect()
        authorized, pending_hash = await ensure_client_authorized(
            client, phone, tg_code, tg_password, phone_code_hash, log_callback
        )
        if not authorized:
            return False, pending_hash

        log_callback("Sending to customers (anti-ban mode enabled)…")

        sent_set = load_sent_set()
        _, daily_count = load_daily_count(phone)

        for idx, row in df.iterrows():
            if daily_count >= DAILY_SEND_LIMIT:
                log_callback(
                    f"Daily safety limit reached ({DAILY_SEND_LIMIT}). Stopping."
                )
                break

            if not row_customer_phone(row):
                if not is_effectively_empty_row(row):
                    log_callback(
                        f"Row {idx + 1}: skipped (no phone) — {format_customer_label(row)}"
                    )
                continue

            target_key = target_key_from_row(row)
            if not target_key:
                log_callback(f"Row {idx + 1}: skipped (no dedup key)")
                continue
            if target_key in sent_set:
                log_callback(f"Skipped {target_key} (already in sent_list.txt)")
                continue

            name = row_name(row)
            
            # 🎯 每次实时循环都重新构建具有高度随机性的文本内容
            message = build_message(name, signature, promo_body)
            label = format_customer_label(row)

            success = False
            try:
                sent_set.add(target_key)
                await resolve_and_send(client, row, message, banner_bytes, banner_name)
                success = True
                fail_streak = 0
                log_callback(f"Sent to {label}")
                
            except SendHalt as halt:
                log_callback(halt.message)
                if halt.cooldown_sec > 0:
                    wait = halt.cooldown_sec + FLOOD_WAIT_BUFFER_SEC
                    log_callback(f"Honoring cooldown — waiting {wait}s…")
                    await asyncio.sleep(wait)
                log_callback("Emergency stop to protect your account. Retry tomorrow.")
                break
                
            except ValueError as exc:
                log_callback(f"Skipped {label}: {exc}")
                append_sent_target(target_key)
                dead_delay = random.randint(15, 35)
                log_callback(f"Antispam Jitter: Random pause {dead_delay}s to obfuscate search frequency…")
                await asyncio.sleep(dead_delay)
                
            except Exception as exc:
                fail_streak += 1
                log_callback(f"Failed: {type(exc).__name__} — {exc}")
                append_sent_target(target_key)
                
                if fail_streak >= CONSECUTIVE_FAIL_LIMIT:
                    log_callback(
                        f"{fail_streak} consecutive failures — stopping to avoid spam flags."
                    )
                    break
                delay = random.randint(FAIL_DELAY_MIN_SEC, FAIL_DELAY_MAX_SEC)
                log_callback(f"Cooling down {delay}s after failure…")
                await asyncio.sleep(delay)
                
            finally:
                append_sent_target(target_key)
                sent_set.add(target_key)

            if success:
                daily_count += 1
                save_daily_count(phone, daily_count)
                delay = random.randint(DELAY_MIN_SEC, DELAY_MAX_SEC)
                log_callback(
                    f"Sent today {daily_count}/{DAILY_SEND_LIMIT} — "
                    f"waiting {delay}s (~{delay // 60} min)…"
                )
                await asyncio.sleep(delay)

    except PhoneNumberInvalidError:
        log_callback("Failed: invalid phone number format")
    except Exception as exc:
        log_callback(f"Fatal: {type(exc).__name__} — {exc}")
        logger.exception("send pipeline")
    finally:
        await client.disconnect()

    return True, None


def main() -> None:
    st.set_page_config(
        page_title="Telegram Outreach",
        page_icon="📨",
        layout="wide",
    )
    st.title("📨 Telegram Outreach Console")
    st.caption(
        "Slow sends · daily cap · dedup list · dynamic content mixing · stops on PeerFlood / FloodWait. "
    )

    with st.sidebar:
        st.subheader("Anti-ban rules")
        st.markdown(
            f"""
- Max **{DAILY_SEND_LIMIT}** messages per account per day
- **{DELAY_MIN_SEC // 60}–{DELAY_MAX_SEC // 60}** min random delay after each success
- Each customer tried **once** (`sent_list.txt`)
- SpinTax text mixing `{'{A|B}'}` support enabled
- Microscopic dynamic hash locker appended
- **Immediate stop** on `PeerFlood` / `FloodWait`
- Stop after **{CONSECUTIVE_FAIL_LIMIT}** consecutive failures
            """
        )
        st.markdown(
            "[GitHub repo](https://github.com/liam1217zy-cpu/telethon)"
        )
        if st.session_state.get("last_phone"):
            _, used = load_daily_count(st.session_state.last_phone)
            st.metric("Sent today (current account)", f"{used} / {DAILY_SEND_LIMIT}")

    banner_file = st.file_uploader(
        "Banner image (optional)",
        type=["png", "jpg", "jpeg", "webp"],
    )
    excel_file = st.file_uploader(
        "Customer list (Excel / CSV)",
        type=["csv", "xlsx", "xls", "xlsm"],
        help="phone = send target · name = greeting (Hi Mr/Ms …) · username = your reference only",
    )

    preview: Optional[pd.DataFrame] = None
    if excel_file is not None:
        try:
            preview = normalize_excel_columns(read_customer_file(excel_file))
            st.caption(
                f"Loaded **{len(preview)}** rows · **phone** sends · **name** greets · "
                f"**username** logs only"
            )
            show_cols = [c for c in ("username", "name", "phone") if c in preview.columns]
            st.dataframe(preview[show_cols].head(8), use_container_width=True)
            if "name" in preview.columns and len(preview) > 0:
                sample_name = row_name(preview.iloc[0]) or "Customer"
                st.markdown("**Message Content Sandbox (Example Generated Variation):**")
                st.code(
                    build_message(
                        sample_name, 
                        "{Max|Alex| Max}", 
                        "We prepared {a surprise bonus|exclusive free rewards|an invitation gift} for you this June! Login {today|now} to check."
                    ),
                    language=None,
                )
            if "phone" not in preview.columns:
                st.error(
                    "No phone column found. Use: phone, mobile, contact number, phone number, etc."
                )
        except Exception as exc:
            st.warning(f"File preview failed: {exc}")

    with st.form("send_form", clear_on_submit=False):
        col1, col2 = st.columns(2)
        with col1:
            api_id = st.number_input(
                "api_id",
                min_value=1,
                step=1,
                value=1,
                help="From https://my.telegram.org",
            )
            api_hash = st.text_input("api_hash", type="password")
            phone = st.text_input(
                "Your Telegram login phone (sender account)",
                placeholder="+60123456789",
                help="Your personal Telegram number — not customer numbers from Excel.",
            )
        with col2:
            signature = st.text_input(
                "Your name (Supports SpinTax, e.g. {Max|Alex})",
                placeholder="Max",
                help='Appears randomly dynamically mixed into greetings.',
            )
            promo_body = st.text_area(
                "Message content (Supports SpinTax, e.g. {bonus|gift})",
                height=160,
                placeholder="Promotion or notice text — supports nested spin combinations.",
            )

        submitted = st.form_submit_button("Start sending", type="primary")

    st.subheader("Telegram sign-in")
    st.text_input(
        "Verification code (SMS / Telegram app)",
        key="tg_code",
        help="First time: click Start sending to receive a code, then Confirm & Send.",
    )
    st.text_input("2FA password (if enabled)", type="password", key="tg_pwd")

    log_panel = st.empty()
    if "send_logs" not in st.session_state:
        st.session_state.send_logs = []

    def append_log(line: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        st.session_state.send_logs.append(f"[{ts}] {line}")
        log_panel.markdown(
            "### Live log\n```\n"
            + "\n".join(st.session_state.send_logs[-80:])
            + "\n```"
        )

    confirm_login = st.button("Confirm & Send", type="secondary")
    run_clicked = submitted or confirm_login

    if not run_clicked:
        st.info("Fill the form and click Start sending. Only message opted-in customers.")
        return

    if not api_id or not api_hash or not phone:
        st.warning("Please enter api_id, api_hash, and your login phone.")
        return
    if excel_file is None:
        st.warning("Please upload the customer list file (Excel or CSV).")
        return

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        st.warning("Invalid login phone format.")
        return

    st.session_state.last_phone = phone_norm

    try:
        df = normalize_excel_columns(read_customer_file(excel_file))
    except Exception as exc:
        st.error(f"File parse error: {exc}")
        return

    if "phone" not in df.columns:
        st.error(
            "File must include a customer phone column "
            "(phone, mobile, contact number, phone number, etc.)."
        )
        return
    if "name" not in df.columns:
        st.warning("No name column found — messages will use greeting 'Customer'.")

    banner_bytes: Optional[bytes] = None
    banner_name: Optional[str] = None
    if banner_file is not None:
        banner_bytes = banner_file.read()
        banner_name = getattr(banner_file, "name", None)

    if submitted:
        st.session_state.send_logs = []
        st.session_state.pop("phone_code_hash", None)
        st.session_state.job_params = {
            "api_id": int(api_id),
            "api_hash": api_hash.strip(),
            "phone": phone_norm,
            "signature": signature,
            "promo_body": promo_body,
            "df": df,
            "banner_bytes": banner_bytes,
            "banner_name": banner_name,
        }

    job = st.session_state.get("job_params")
    if not job:
        st.warning("Click Start sending first to save your settings.")
        return

    phone_norm = job["phone"]
    append_log("Starting — connecting to Telegram…")
    append_log(f"Session file: {session_path_for_phone(phone_norm).name}")
    append_log(f"Dedup file: {SENT_LIST_PATH.name} ({len(load_sent_set())} entries)")

    _, daily_start = load_daily_count(phone_norm)
    append_log(f"Sent today: {daily_start}/{DAILY_SEND_LIMIT}")

    tg_code = st.session_state.get("tg_code", "") or ""
    tg_pwd = st.session_state.get("tg_pwd", "") or ""
    phone_code_hash = st.session_state.get("phone_code_hash") or None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    finished = False
    pending_hash: Optional[str] = None
    try:
        finished, pending_hash = loop.run_until_complete(
            run_send_pipeline(
                api_id=job["api_id"],
                api_hash=job["api_hash"],
                phone=job["phone"],
                signature=job["signature"],
                promo_body=job["promo_body"],
                df=job["df"],
                banner_bytes=job["banner_bytes"],
                banner_name=job.get("banner_name"),
                tg_code=tg_code,
                tg_password=tg_pwd,
                phone_code_hash=phone_code_hash,
                log_callback=append_log,
            )
        )
    finally:
        loop.close()

    if pending_hash:
        st.session_state.phone_code_hash = pending_hash
    elif finished:
        st.session_state.pop("phone_code_hash", None)

    if finished:
        append_log("Done.")
        st.success("Finished. See the live log above.")
    else:
        st.warning("Waiting for verification code or 2FA — click Confirm & Send.")


if __name__ == "__main__":
    main()
