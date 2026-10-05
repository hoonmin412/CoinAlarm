"""모니터링 지표 현황 표를 PNG 이미지로 렌더링한다.

Cloud Functions(Linux)에는 한글 폰트가 없으므로 assets/에 번들한 NanumGothic을 직접 등록해서 쓴다.
"""

from __future__ import annotations

import io
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
for _font_file in ("NanumGothic-Regular.ttf", "NanumGothic-Bold.ttf"):
    fm.fontManager.addfont(str(ASSETS_DIR / _font_file))
plt.rcParams["font.family"] = "NanumGothic"
plt.rcParams["axes.unicode_minus"] = False

SURFACE = "#fcfcfb"
INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
HEADER_BG = "#f2f1ee"
INDEX_BG = "#f7f6f2"
UP_RED = "#e34948"      # 현재가 > 이동평균선
DOWN_BLUE = "#2a78d6"   # 현재가 < 이동평균선
RSI_HOT = "#e34948"     # RSI 70 이상
RSI_COLD = "#2a78d6"    # RSI 40 이하


def _fmt_price(v: float | None, unit: str) -> str:
    if v is None:
        return "-"
    if unit == "pt":
        return f"{v:,.2f}pt"
    return f"{v:,.0f}원" if v >= 100 else f"{v:,.2f}원"


def render_summary_image(rows: list[dict], windows: list[int], title: str, subtitle: str) -> bytes:
    """rows: dict(name, unit, price, ma={window: value}, rsi, is_index) 리스트 → PNG bytes."""
    columns = ["코인명", "현재가격"] + [f"{w}일선" for w in windows] + ["RSI(14)"]
    n_cols = len(columns)
    first_w = 0.26
    rest_w = (1 - first_w) / (n_cols - 1)
    col_widths = [first_w] + [rest_w] * (n_cols - 1)
    x_edges = [0.0]
    for w in col_widths:
        x_edges.append(x_edges[-1] + w)

    row_h_in, header_h_in, title_h_in, footer_h_in = 0.78, 0.6, 0.95, 0.5
    fig_w = 11.0
    fig_h = title_h_in + header_h_in + row_h_in * len(rows) + footer_h_in
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=150, facecolor=SURFACE)

    ax_t = fig.add_axes([0, 1 - title_h_in / fig_h, 1, title_h_in / fig_h])
    ax_t.axis("off")
    ax_t.text(0.03, 0.62, title, fontsize=17, fontweight="bold", color=INK_PRIMARY, va="center")
    ax_t.text(0.03, 0.2, subtitle, fontsize=10, color=INK_MUTED, va="center")

    body_h = header_h_in + row_h_in * len(rows)
    ax = fig.add_axes([0.02, footer_h_in / fig_h, 0.96, body_h / fig_h])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, body_h)
    ax.invert_yaxis()
    ax.axis("off")

    ax.add_patch(Rectangle((0, 0), 1, header_h_in, facecolor=HEADER_BG, edgecolor="none"))
    for i, name in enumerate(columns):
        cx = (x_edges[i] + x_edges[i + 1]) / 2 if i else x_edges[0] + 0.015
        ax.text(cx, header_h_in / 2, name, ha="center" if i else "left", va="center",
                fontsize=11, fontweight="bold", color=INK_SECONDARY)

    for r_i, row in enumerate(rows):
        top = header_h_in + r_i * row_h_in
        mid = top + row_h_in / 2
        if row.get("is_index"):
            ax.add_patch(Rectangle((0, top), 1, row_h_in, facecolor=INDEX_BG, edgecolor="none"))
        ax.plot([0, 1], [top + row_h_in] * 2, color=GRIDLINE, linewidth=0.8)

        ax.text(x_edges[0] + 0.015, mid, row["name"], ha="left", va="center", fontsize=11.5,
                fontweight="bold", color=INK_PRIMARY)
        price = row["price"]
        ax.text((x_edges[1] + x_edges[2]) / 2, mid, _fmt_price(price, row["unit"]),
                ha="center", va="center", fontsize=11.5, fontweight="bold", color=INK_PRIMARY)

        for j, w in enumerate(windows):
            ma = row["ma"].get(w)
            color = INK_MUTED if ma is None else (UP_RED if price > ma else DOWN_BLUE)
            cx = (x_edges[2 + j] + x_edges[3 + j]) / 2
            ax.text(cx, mid - 0.13, _fmt_price(ma, row["unit"]),
                    ha="center", va="center", fontsize=11.5, fontweight="bold", color=color)
            if ma:
                ax.text(cx, mid + 0.17, f"({(price / ma - 1) * 100:+.2f}%)",
                        ha="center", va="center", fontsize=9.5, fontweight="bold", color=color)

        rsi = row["rsi"]
        rsi_color = INK_MUTED if rsi is None else (RSI_HOT if rsi >= 70 else RSI_COLD if rsi <= 40 else INK_PRIMARY)
        ax.text((x_edges[-2] + x_edges[-1]) / 2, mid, "-" if rsi is None else f"{rsi:.1f}",
                ha="center", va="center", fontsize=11.5, fontweight="bold", color=rsi_color)

    ax_f = fig.add_axes([0, 0, 1, footer_h_in / fig_h])
    ax_f.axis("off")
    ax_f.text(0.03, 0.5,
              "이동평균선: 현재가가 위면 빨강, 아래면 파랑 / 괄호 = 이평선 대비 현재가 등락률  ·  RSI: 70 이상 빨강 / 40 이하 파랑  ·  업비트 일봉 종가 기준",
              fontsize=8.5, color=INK_MUTED, va="center")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    plt.close(fig)
    return buf.getvalue()
