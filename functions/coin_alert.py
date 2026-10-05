"""업비트 코인 가격 → 60/120일선 돌파 텔레그램 알림.

감시 대상: config.yaml의 코인들 + 나만의 지표(알트 지수).
알트 지수는 코인별 가격을 윈도 첫날 = 100으로 정규화한 뒤 동일 비중으로 평균낸 값이다.
(원화 가격을 그대로 평균내면 ETH 같은 고가 코인이 지수를 지배해 버리기 때문)

환경변수: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
테스트: DRY_RUN=1 이면 텔레그램/Firestore 없이 결과만 로그로 출력한다.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yaml

UPBIT = "https://api.upbit.com/v1"
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
KST = ZoneInfo("Asia/Seoul")
CANDLE_COUNT = 200  # 업비트 1회 최대치. 120일선 + 여유분

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("coin-alert")

DRY_RUN = os.environ.get("DRY_RUN") == "1"


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────
# 상태 (Firestore)
# ──────────────────────────────────────────────────────────────
_fs = None


def _firestore():
    global _fs
    if _fs is None:
        from google.cloud import firestore

        _fs = firestore.Client()
    return _fs


def load_state() -> dict:
    if DRY_RUN:
        return {}
    doc = _firestore().collection("coin_alert").document("state").get()
    return doc.to_dict() if doc.exists else {}


def save_state(state: dict) -> None:
    if not DRY_RUN:
        _firestore().collection("coin_alert").document("state").set(state)


# ──────────────────────────────────────────────────────────────
# 업비트 API
# ──────────────────────────────────────────────────────────────
def upbit_get(path: str, params: dict, cfg: dict):
    for attempt in range(cfg["max_retries"]):
        resp = requests.get(f"{UPBIT}{path}", params=params, timeout=15)
        if resp.status_code == 429 and attempt < cfg["max_retries"] - 1:
            time.sleep(1.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("unreachable")


def fetch_live_prices(cfg: dict) -> dict[str, dict]:
    markets = ",".join(c["market"] for c in cfg["coins"].values())
    rows = upbit_get("/ticker", {"markets": markets}, cfg)
    by_market = {r["market"]: r for r in rows}
    return {sym: by_market[c["market"]] for sym, c in cfg["coins"].items()}


def fetch_daily_closes(market: str, cfg: dict) -> pd.Series:
    """일봉 종가 (KST 날짜 인덱스, 오래된 순). 마지막 값은 진행 중인 오늘 봉."""
    rows = upbit_get("/candles/days", {"market": market, "count": CANDLE_COUNT}, cfg)
    return pd.Series(
        {r["candle_date_time_kst"][:10]: float(r["trade_price"]) for r in rows}
    ).sort_index()


# ──────────────────────────────────────────────────────────────
# 신호 판정
# ──────────────────────────────────────────────────────────────
@dataclass
class Target:
    key: str           # BTC, ETH ... , INDEX
    display: str
    closes: pd.Series  # 일봉 종가 (마지막 = 오늘)
    price: float       # 현재가 (지수는 지수값)
    unit: str          # "원" 또는 "pt"

    def fmt(self, v: float) -> str:
        if self.unit == "pt":
            return f"{v:,.2f}pt"
        return f"{v:,.0f}원" if v >= 100 else f"{v:,.2f}원"


@dataclass
class Signal:
    key: str
    state: str
    is_alert: bool
    text: str


def build_targets(cfg: dict) -> list[Target]:
    live = fetch_live_prices(cfg)
    closes: dict[str, pd.Series] = {}
    for sym, c in cfg["coins"].items():
        s = fetch_daily_closes(c["market"], cfg)
        time.sleep(cfg["request_delay_sec"])
        s.iloc[-1] = live[sym]["trade_price"]  # 오늘 봉 종가를 현재가로 갱신
        closes[sym] = s

    targets = [
        Target(sym, f"{c['name']} ({sym})", closes[sym], float(live[sym]["trade_price"]), "원")
        for sym, c in cfg["coins"].items()
    ]

    idx = cfg.get("custom_index")
    if idx:
        df = pd.DataFrame({m: closes[m] for m in idx["members"]}).dropna()  # 공통 날짜만
        index_series = (df / df.iloc[0] * 100).mean(axis=1)
        targets.append(Target("INDEX", f"{idx['name']} ({'+'.join(idx['members'])})",
                              index_series, float(index_series.iloc[-1]), "pt"))
    return targets


def evaluate(t: Target, cfg: dict) -> list[Signal]:
    """현재가 vs 어제 종가를 각 이동평균선과 비교해 돌파/이탈 판정."""
    signals: list[Signal] = []
    closes = t.closes
    for window in cfg["ma_windows"]:
        if len(closes) < window + 1:
            log.info("%s: 데이터 부족으로 %d일선 건너뜀 (%d개)", t.key, window, len(closes))
            continue
        ma_now = closes.iloc[-window:].mean()
        ma_before = closes.iloc[-window - 1:-1].mean()
        was_below = closes.iloc[-2] <= ma_before
        is_above = t.price > ma_now

        state, text = "none", ""
        if was_below and is_above:
            state = "break_up"
            text = f"🟢 {window}일선 상향 돌파 → 현재 {t.fmt(t.price)} > {window}일선 {t.fmt(ma_now)}"
        elif (not was_below) and (not is_above):
            state = "break_down"
            text = f"🔴 {window}일선 하향 이탈 → 현재 {t.fmt(t.price)} < {window}일선 {t.fmt(ma_now)}"
        signals.append(Signal(f"ma{window}", state, state != "none", text))
    return signals


def compute_rsi(close: pd.Series, period: int) -> float | None:
    """Wilder RSI의 마지막 값."""
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    return float(100 - 100 / (1 + last_gain / last_loss))


def build_summary_rows(targets: list[Target], cfg: dict) -> list[dict]:
    rows = []
    for t in targets:
        ma = {w: float(t.closes.iloc[-w:].mean()) for w in cfg["summary_ma_windows"] if len(t.closes) >= w}
        rows.append(dict(
            name=t.display if t.key != "INDEX" else cfg["custom_index"]["name"],
            unit=t.unit, price=t.price, ma=ma,
            rsi=compute_rsi(t.closes, cfg["rsi_period"]), is_index=t.key == "INDEX",
        ))
    return rows


def render_summary(targets: list[Target], cfg: dict) -> bytes:
    import chart

    now = datetime.now(KST)
    idx = cfg.get("custom_index")
    sub = f"{now:%Y-%m-%d %H:%M} KST · 업비트"
    if idx:
        sub += f" · {idx['name']} = {'+'.join(idx['members'])} 동일비중 평균 (윈도 첫날=100)"
    return chart.render_summary_image(
        build_summary_rows(targets, cfg), cfg["summary_ma_windows"], "코인 모니터링 현황", sub)


def is_summary_time(cfg: dict) -> bool:
    return os.environ.get("FORCE_SUMMARY") == "1" or datetime.now(KST).hour in cfg["summary_hours_kst"]


def should_notify(state: dict, key: str, signal: Signal, cooldown_hours: int) -> bool:
    """동일 신호 최초 발생 후 cooldown_hours 억제. 방향이 바뀌면 즉시 알림."""
    now = datetime.now(timezone.utc)
    entry = dict(state.get(key) or {})
    entry["state"] = signal.state
    entry["checked_at"] = now.isoformat()
    state[key] = entry

    if not signal.is_alert:
        return False

    last_at, last_state = entry.get("alerted_at"), entry.get("alerted_state")
    if last_at and last_state == signal.state:
        try:
            elapsed = now - datetime.fromisoformat(last_at)
        except ValueError:
            elapsed = timedelta(hours=cooldown_hours + 1)
        if elapsed < timedelta(hours=cooldown_hours):
            log.info("%s: 쿨다운 중", key)
            return False

    entry["alerted_at"] = now.isoformat()
    entry["alerted_state"] = signal.state
    return True


# ──────────────────────────────────────────────────────────────
# 텔레그램
# ──────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    if DRY_RUN:
        log.info("[DRY_RUN] 텔레그램 메시지:\n%s", message)
        return
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    if not resp.ok:
        log.error("텔레그램 전송 실패 (%s): %s", resp.status_code, resp.text)
    else:
        log.info("텔레그램 전송 완료")


def send_telegram_photo(photo: bytes, caption: str = "") -> None:
    preview = os.environ.get("PREVIEW_PATH")
    if preview:
        Path(preview).write_bytes(photo)
        log.info("미리보기 이미지 저장: %s", preview)
    if DRY_RUN:
        return
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": caption},
        files={"photo": ("summary.png", photo, "image/png")},
        timeout=30,
    )
    if not resp.ok:
        log.error("텔레그램 사진 전송 실패 (%s): %s", resp.status_code, resp.text)
    else:
        log.info("텔레그램 사진 전송 완료")


def notify_fatal_error(exc: Exception) -> None:
    try:
        send_telegram(f"⚠️ 코인 알림 실행 오류\n<code>{type(exc).__name__}: {exc}</code>")
    except Exception:  # noqa: BLE001
        log.exception("오류 알림 전송 실패")


# ──────────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────────
def main() -> int:
    cfg = load_config()
    targets = build_targets(cfg)
    state = load_state()

    lines: list[str] = []
    for t in targets:
        for sig in evaluate(t, cfg):
            log.info("%s %s: %s", t.key, sig.key, sig.state)
            if should_notify(state, f"{t.key}:{sig.key}", sig, cfg["cooldown_hours"]):
                lines.append(f"· <b>{t.display}</b>\n\t{sig.text}")

    save_state(state)

    if lines:
        now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        send_telegram(f"🪙 코인 이동평균선 알림 ({now} KST)\n\n" + "\n".join(lines))
    else:
        log.info("보낼 알림 없음")

    if is_summary_time(cfg):
        send_telegram_photo(render_summary(targets, cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
