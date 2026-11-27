import os
import time
import datetime
import pandas as pd
import requests
from twelvedata import TDClient

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

td = TDClient(apikey=TWELVEDATA_API_KEY)

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"]
ALL_PAIRS = [
    "EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD", "USD/CAD", "USD/JPY", "USD/CHF",
    "EUR/GBP", "EUR/JPY", "EUR/AUD", "EUR/NZD", "EUR/CAD", "EUR/CHF",
    "GBP/JPY", "GBP/AUD", "GBP/NZD", "GBP/CAD", "GBP/CHF",
    "AUD/JPY", "AUD/NZD", "AUD/CAD", "AUD/CHF",
    "NZD/JPY", "NZD/CAD", "NZD/CHF",
    "CAD/JPY", "CAD/CHF", "CHF/JPY"
]


def get_pht_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def is_forex_weekend(pht_dt):
    # Monday=0 ... Friday=4, Saturday=5, Sunday=6
    return pht_dt.weekday() in (5, 6)


def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, data=payload, timeout=15)
        print(f"Telegram status: {res.status_code}")
        if res.status_code != 200:
            print(f"Markdown send failed ({res.status_code}): {res.text}")
            fallback_payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
            res_fb = requests.post(url, data=fallback_payload, timeout=15)
            print(f"Fallback status: {res_fb.status_code} | {res_fb.text}")
    except Exception as e:
        print(f"Error sending Telegram message: {e}")


def get_enhanced_candle_score(df, tf_key="1h"):
    if df is None or len(df) < 3:
        return 0

    # Compare the last two closed candles (iloc[1] vs iloc[2]) to avoid forming-candle noise
    curr = float(df["close"].iloc[1])
    prev = float(df["close"].iloc[2])
    high = float(df["high"].iloc[1])
    low = float(df["low"].iloc[1])

    if prev == 0 or (high - low) == 0:
        return 0

    pct_change = ((curr - prev) / prev) * 100

    # Balanced thresholds for 30m intraday momentum; standard thresholds for 1h+
    if tf_key == "30m":
        mild_thresh, strong_thresh = 0.06, 0.18
    else:
        mild_thresh, strong_thresh = 0.10, 0.35

    if pct_change >= strong_thresh:
        return 2
    elif pct_change >= mild_thresh:
        return 1
    elif pct_change <= -strong_thresh:
        return -2
    elif pct_change <= -mild_thresh:
        return -1
    return 0


def chunk_list(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def fetch_all_scores():
    timeframes = {"30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}
    pair_scores = {pair: {"30m": 0, "1h": 0, "4h": 0, "1d": 0} for pair in ALL_PAIRS}
    batches = list(chunk_list(ALL_PAIRS, 7))  # 7 symbols = 7 credits per call (under 8/min free limit)

    for tf_key, tf_val in timeframes.items():
        for idx, batch in enumerate(batches):
            try:
                ts = td.time_series(symbol=batch, interval=tf_val, outputsize=4)
                df_all = ts.as_pandas()

                if isinstance(df_all.index, pd.MultiIndex):
                    top_symbols = set(df_all.index.get_level_values(0))
                    for pair in batch:
                        if pair in top_symbols:
                            sub_df = df_all.xs(pair, level=0)
                            pair_scores[pair][tf_key] = get_enhanced_candle_score(sub_df, tf_key)
                elif len(batch) == 1 and df_all is not None and not df_all.empty:
                    pair_scores[batch[0]][tf_key] = get_enhanced_candle_score(df_all, tf_key)
            except Exception as e:
                print(f"Error fetching batch {batch} [{tf_key}]: {e}")

            is_last_call = (tf_key == "1d" and idx == len(batches) - 1)
            if not is_last_call:
                time.sleep(61.0)

    return pair_scores


def get_market_session(utc_hour):
    if 7 <= utc_hour < 16:
        return "London/NY Overlap (High Liquidity 🟢)"
    elif 0 <= utc_hour < 8:
        return "Asian Session (Moderate Liquidity 🟡)"
    else:
        return "Late NY / Inter-session Chop (Low Liquidity ⚠️)"


def run_precision_scan():
    pht_now = get_pht_now()
    if is_forex_weekend(pht_now):
        print(f"Skipping scan on {pht_now.strftime('%A')} ({pht_now}) — Forex market closed.")
        return

    if not TWELVEDATA_API_KEY:
        print("Missing TWELVEDATA_API_KEY environment variable.")
        return

    pair_scores = fetch_all_scores()
    mom_scores = {c: {"30m": 0, "1h": 0, "4h": 0, "1d": 0} for c in CURRENCIES}
    sweep_scores = {c: {"30m": 0, "1h": 0, "4h": 0, "1d": 0} for c in CURRENCIES}

    for pair in ALL_PAIRS:
        base, quote = pair.split("/")
        p_scores = pair_scores[pair]
        for tf in ["30m", "1h", "4h", "1d"]:
            m_val = p_scores[tf]
            s_val = 1 if m_val > 0 else (-1 if m_val < 0 else 0)

            if base in mom_scores:
                mom_scores[base][tf] += m_val
                sweep_scores[base][tf] += s_val
            if quote in mom_scores:
                mom_scores[quote][tf] -= m_val
                sweep_scores[quote][tf] -= s_val

    currency_table = []
    for c in CURRENCIES:
        s_30m = sweep_scores[c]["30m"]
        s_1h = sweep_scores[c]["1h"]
        s_4h = sweep_scores[c]["4h"]
        s_1d = sweep_scores[c]["1d"]
        swp_net = s_30m + s_1h + s_4h + s_1d
        mom_net = sum(mom_scores[c].values())

        currency_table.append({
            "Currency": c,
            "30m": s_30m,
            "1h": s_1h,
            "4h": s_4h,
            "1d": s_1d,
            "SwpNet": swp_net,
            "MomNet": mom_net
        })

    currency_table = sorted(currency_table, key=lambda x: (x["SwpNet"], x["MomNet"]), reverse=True)

    # Individual timeframes show Sweep (-7..+7); SwpNet & MomNet show totals side by side
    currency_report = (
        "📊 *8-CURRENCY SCOREBOARD (SWEEP -7..+7)*\n"
        "`Curr | 30m | 1h  | 4h  | 1d  | Swp | Mom`\n"
        "`---------------------------------------`\n"
    )
    for item in currency_table:
        currency_report += (
            f"`{item['Currency']:<4} | {item['30m']:<3} | {item['1h']:<3} | "
            f"{item['4h']:<3} | {item['1d']:<3} | {item['SwpNet']:<3} | {item['MomNet']:<3}`\n"
        )

    intraday_setups, macro_setups, confluence_setups = [], [], []
    for pair in ALL_PAIRS:
        base, quote = pair.split("/")
        b_intra = mom_scores[base]["30m"] + mom_scores[base]["1h"]
        q_intra = mom_scores[quote]["30m"] + mom_scores[quote]["1h"]
        b_macro = mom_scores[base]["4h"] + mom_scores[base]["1d"]
        q_macro = mom_scores[quote]["4h"] + mom_scores[quote]["1d"]

        intra_gap = b_intra - q_intra
        macro_gap = b_macro - q_macro

        intra_bias = "🟢 BUY" if intra_gap >= 3 else ("🔴 SELL" if intra_gap <= -3 else "⚪ MIXED")
        macro_bias = "🟢 BUY" if macro_gap >= 3 else ("🔴 SELL" if macro_gap <= -3 else "⚪ MIXED")

        if intra_bias != "⚪ MIXED":
            intraday_setups.append({"pair": pair, "bias": intra_bias, "gap": intra_gap, "abs_gap": abs(intra_gap)})
        if macro_bias != "⚪ MIXED":
            macro_setups.append({"pair": pair, "bias": macro_bias, "gap": macro_gap, "abs_gap": abs(macro_gap)})
        if intra_bias == macro_bias and intra_bias != "⚪ MIXED":
            confluence_setups.append({
                "pair": pair,
                "bias": intra_bias,
                "intra_gap": intra_gap,
                "macro_gap": macro_gap,
                "score": abs(intra_gap) + abs(macro_gap)
            })

    intraday_setups = sorted(intraday_setups, key=lambda x: x["abs_gap"], reverse=True)
    macro_setups = sorted(macro_setups, key=lambda x: x["abs_gap"], reverse=True)
    confluence_setups = sorted(confluence_setups, key=lambda x: x["score"], reverse=True)

    conf_rep = "⭐ *FULL CONFLUENCE*\n" + (
        "\n".join([
            f"• `{i['pair']:<7}` ➔ *{i['bias']}* (30m+1H: `{i['intra_gap']:+d}` | 4H+1D: `{i['macro_gap']:+d}`)"
            for i in confluence_setups
        ]) if confluence_setups else "_No dual-timeframe confluence setups._"
    )
    intra_rep = "\n⚡ *30m + 1H Intraday Trades*\n" + (
        "\n".join([
            f"• `{i['pair']:<7}` ➔ *{i['bias']}* (Gap: `{i['gap']:+d}`)"
            for i in intraday_setups
        ]) if intraday_setups else "_No active intraday momentum trades._"
    )
    macro_rep = "\n🏛️ *4H + 1D Macro Structure Trades*\n" + (
        "\n".join([
            f"• `{i['pair']:<7}` ➔ *{i['bias']}* (Gap: `{i['gap']:+d}`)"
            for i in macro_setups
        ]) if macro_setups else "_No active macro structure trends._"
    )

    utc_now = datetime.datetime.now(datetime.timezone.utc)

    full_message = (
        f"🤖 *28-PAIR PRECISION TRADING ENGINE*\n"
        f"🕒 _Scan Time: {pht_now.strftime('%Y-%m-%d %H:%M:%S')} PHT_\n"
        f"🌍 _Market State: {get_market_session(utc_now.hour)}_\n\n"
        f"{conf_rep}\n\n{intra_rep}\n\n{macro_rep}\n\n"
        f"{currency_report}\n"
        f"⚠️ _Use 1H/4H/1D for bias; wait for S/R pullbacks before entry._"
    )
    send_telegram_message(full_message)


if __name__ == "__main__":
    run_precision_scan()
