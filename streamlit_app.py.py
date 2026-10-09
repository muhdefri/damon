import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from PIL import Image
from io import BytesIO
import zipfile
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import re
import io
import math
import unicodedata

# ============================================================
# PAGE CONFIG
# ============================================================
st.set_page_config(
    page_title="RAN KPI Dashboard",
    page_icon="📡",
    layout="wide",
)

# Dashboard selector is separate from the existing RAN KPI logic.
_dashboard_mode = st.sidebar.selectbox(
    "Dashboard",
    ["RAN KPI Dashboard", "TWAMP Packet Loss"],
    key="dashboard_mode_selector",
)
if _dashboard_mode == "RAN KPI Dashboard":
    st.title("📡 RAN KPI Dashboard")
    st.caption("Test version — CSV / CSV.GZ")

# ============================================================
# OPTIONAL TWAMP PACKET LOSS DASHBOARD
# ============================================================
def render_twamp_dashboard():
    st.title("📡 TWAMP Packet Loss Dashboard")
    st.caption("Packet Loss target: 0.7% | Meet: ≤ 0.7% | Not Meet: > 0.7%")

    twamp_file = st.sidebar.file_uploader(
        "Upload TWAMP CSV / CSV.GZ",
        type=["csv", "gz"],
        key="twamp_csv_uploader",
        help="Upload the TWAMP export as .csv or .csv.gz.",
    )
    if twamp_file is None:
        st.info("Upload file TWAMP CSV / CSV.GZ dari sidebar. Dashboard KPI RAN tetap tersedia melalui pilihan Dashboard.")
        return

    try:
        filename = twamp_file.name.lower().strip()
        compression = "gzip" if filename.endswith(".csv.gz") else None
        raw = pd.read_csv(twamp_file, compression=compression, low_memory=False)
    except Exception as exc:
        st.error(f"Gagal membaca file TWAMP: {exc}")
        return

    raw.columns = [str(c).strip().lstrip("\\ufeff") for c in raw.columns]
    packet_loss_col = next(
        (c for c in raw.columns
         if re.sub(r"\\s+", "", str(c)).lower()
         in {"vs.bstwamp.roundtrip.dropmeans(%)", "vs.bstwamp.roundtrip.dropmeans"}),
        None,
    )

    # Robust fallback for CSV exports with delimiter/encoding/header quirks.
    # The standard comma-separated read is attempted first; only retry if the
    # expected KPI header was not found.
    if packet_loss_col is None:
        filename = twamp_file.name.lower().strip()
        compression = "gzip" if filename.endswith(".csv.gz") else None
        for encoding in ("utf-8-sig", "latin1"):
            for sep in (None, ";", "\\t", ","):
                try:
                    twamp_file.seek(0)
                    candidate_df = pd.read_csv(
                        twamp_file, compression=compression, low_memory=False,
                        encoding=encoding, sep=sep, engine="python"
                    )
                    candidate_df.columns = [
                        str(c).strip().lstrip("\\ufeff") for c in candidate_df.columns
                    ]
                    candidate_col = next(
                        (c for c in candidate_df.columns
                         if re.sub(r"\\s+", "", str(c)).lower()
                         in {"vs.bstwamp.roundtrip.dropmeans(%)", "vs.bstwamp.roundtrip.dropmeans"}),
                        None,
                    )
                    if candidate_col is not None:
                        raw, packet_loss_col = candidate_df, candidate_col
                        break
                except Exception:
                    continue
            if packet_loss_col is not None:
                break

    if packet_loss_col is None:
        st.error(
            "Kolom Packet Loss tidak ditemukan. Pastikan file yang di-upload adalah "
            "CSV TWAMP dengan header `VS.BSTWAMP.RoundTrip.DropMeans(%)` di kolom Z."
        )
        st.write("Jumlah kolom terbaca:", len(raw.columns))
        st.write("Contoh header terbaca:", [str(c) for c in raw.columns[:12]])
        return

    # Support daily exports (Date), combined timestamps (Datetime/Timestamp),
    # and hourly exports where Date and Time are separate columns.
    date_col = next(
        (c for c in raw.columns if c.strip().lower() in {"datetime", "timestamp"}),
        None,
    )
    if date_col is None:
        date_col = next((c for c in raw.columns if c.strip().lower() == "date"), None)
    time_col = next(
        (c for c in raw.columns if c.strip().lower() in {"time", "hour", "hourly"}),
        None,
    )
    if date_col is None and time_col is not None:
        date_col = time_col

    # Prefer TowerID such as SUM-SB-SPE-0424. If it is absent or the value
    # does not contain the SUM site code, fall back to the full eNodeB name.
    def _norm_col(value):
        return re.sub(r"[^a-z0-9]", "", str(value).lower())

    tower_col = next((c for c in raw.columns if _norm_col(c) in {
        "towerid", "tower", "siteid", "sitecode"
    }), None)
    enodeb_col = next((c for c in raw.columns if _norm_col(c) in {
        "fullenodebname", "enodebname", "fullenodeb", "enodeb"
    }), None)
    # Robust fallback for exports where the eNodeB header contains extra text
    # or has been altered by spreadsheet/CSV tools.
    if enodeb_col is None:
        enodeb_col = next(
            (c for c in raw.columns
             if "enodeb" in _norm_col(c) and "name" in _norm_col(c)),
            None,
        )
    # In the user's TWAMP export, column B is explicitly "eNodeB Name".
    if enodeb_col is None and len(raw.columns) > 1:
        second_col = raw.columns[1]
        if raw[second_col].astype(str).str.contains(r"#|LTE|NR", case=False, regex=True).mean() > 0.5:
            enodeb_col = second_col
    site_col = tower_col or enodeb_col or next(
        (c for c in raw.columns if _norm_col(c) in {"sitename", "site"}), None
    )
    if date_col is None:
        st.error("Kolom tanggal tidak ditemukan. Dashboard membutuhkan kolom Date/Datetime.")
        return

    df = raw.copy()
    if tower_col and enodeb_col:
        tower_values = df[tower_col].fillna("").astype(str).str.strip()
        use_tower = tower_values.str.upper().str.contains("SUM", regex=False) & tower_values.ne("")
        df["_DisplaySite"] = df[enodeb_col].fillna("").astype(str).str.strip()
        df.loc[use_tower, "_DisplaySite"] = tower_values.loc[use_tower]
        df["_DisplaySite"] = df["_DisplaySite"].replace("", "Site tidak diketahui")
        site_col = "_DisplaySite"
    elif tower_col:
        df["_DisplaySite"] = df[tower_col].fillna("").astype(str).str.strip()
        if enodeb_col:
            fallback = ~df["_DisplaySite"].str.upper().str.contains("SUM", regex=False)
            df.loc[fallback, "_DisplaySite"] = df.loc[fallback, enodeb_col].fillna("").astype(str).str.strip()
        site_col = "_DisplaySite"
    elif enodeb_col:
        df["_DisplaySite"] = df[enodeb_col].fillna("").astype(str).str.strip()
        site_col = "_DisplaySite"

    if time_col is not None and date_col is not None and time_col != date_col and date_col.strip().lower() == "date":
        # Combine separate Date + Time fields into a real timestamp for hourly charts.
        df["_Date"] = pd.to_datetime(
            df[date_col].astype(str).str.strip()
            + " "
            + df[time_col].astype(str).str.strip(),
            errors="coerce",
        )
    else:
        df["_Date"] = pd.to_datetime(df[date_col], errors="coerce")
    df["_PacketLoss"] = pd.to_numeric(df[packet_loss_col].astype(str).str.replace("%", "", regex=False).str.strip(), errors="coerce")
    df = df.dropna(subset=["_Date", "_PacketLoss"]).sort_values("_Date")
    if df.empty:
        st.warning("Tidak ada data tanggal dan Packet Loss numerik yang valid.")
        return

    target = 0.7
    df["_Status"] = df["_PacketLoss"].le(target).map({True: "Meet", False: "Not Meet"})

    # Filters are intentionally scoped to the TWAMP module only.
    # Choose explicit Start Date / End Date; all summaries and records below
    # use the same selected interval.
    with st.sidebar:
        st.subheader("TWAMP Filters")
        chart_resolution = st.selectbox(
            "Data Resolution",
            ["Auto Detect", "Daily", "Hourly"],
            index=0,
            key="twamp_data_resolution",
            help="Auto Detect mengikuti timestamp pada CSV. Hourly mempertahankan waktu pengukuran; Daily merangkum nilai maksimum per site per hari.",
        )
        if site_col:
            site_values = sorted(df[site_col].dropna().astype(str).unique().tolist())
            selected_sites = st.multiselect(
                "Site / eNodeB", site_values, default=site_values,
                key="twamp_selected_sites",
            )
            if selected_sites:
                df = df[df[site_col].astype(str).isin(selected_sites)]

        available_dates = sorted(pd.Timestamp(d).date() for d in df["_Date"].dt.date.unique())
        min_date = available_dates[0]
        max_date = available_dates[-1]
        start_col, end_col = st.columns(2)
        with start_col:
            start_date = st.selectbox(
                "Start Date",
                options=available_dates,
                index=0,
                format_func=lambda d: d.strftime("%d %b %Y"),
                key="twamp_start_date",
            )
        valid_end_dates = [d for d in available_dates if d >= start_date]
        with end_col:
            default_end_index = len(valid_end_dates) - 1
            end_date = st.selectbox(
                "End Date",
                options=valid_end_dates,
                index=default_end_index,
                format_func=lambda d: d.strftime("%d %b %Y"),
                key="twamp_end_date",
            )
        if start_date > end_date:
            st.warning("Start Date tidak boleh melewati End Date.")
            return
        st.caption(f"Periode aktif: {start_date:%d %b %Y} – {end_date:%d %b %Y}")
        df = df[df["_Date"].dt.date.between(start_date, end_date)]

    if df.empty:
        st.warning("Tidak ada data setelah filter dipilih.")
        return

    has_intraday_timestamps = bool((df["_Date"] != df["_Date"].dt.normalize()).any())
    if chart_resolution == "Auto Detect":
        active_resolution = "Hourly" if has_intraday_timestamps else "Daily"
    else:
        active_resolution = chart_resolution
    st.caption(f"Resolusi chart TWAMP: **{active_resolution}**")

    total = len(df)
    not_meet = int((df["_Status"] == "Not Meet").sum())
    meet = total - not_meet
    avg_pl = float(df["_PacketLoss"].mean())
    max_pl = float(df["_PacketLoss"].max())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Records", f"{total:,}")
    c2.metric("Meet (≤ 0.7%)", f"{meet:,}", f"{meet / total * 100:.1f}%")
    c3.metric("Not Meet (> 0.7%)", f"{not_meet:,}", f"{not_meet / total * 100:.1f}%", delta_color="inverse")
    c4.metric("Average Packet Loss", f"{avg_pl:.4f}%")

    # Optional RTT chart, when the TWAMP export includes this KPI.
    rtt_col = "VS.BSTWAMP.Rtt.Means(ms)"
    if rtt_col in df.columns:
        st.subheader("Average RTT")
        rtt = pd.to_numeric(df[rtt_col], errors="coerce")
        rtt_fig = go.Figure()
        rtt_fig.add_trace(go.Scatter(x=df["_Date"], y=rtt, mode="lines+markers", name="Average RTT (ms)"))
        rtt_fig.update_layout(template="plotly_white", height=360, xaxis_title="Date", yaxis_title="RTT (ms)")
        st.plotly_chart(rtt_fig, use_container_width=True)

    # ------------------------------------------------------------
    # Site-level condition summary over the FULL selected interval.
    # This intentionally replaces the old per-status aggregation:
    # one site gets one row, classified by daily PL behavior.
    # KPI dashboard code below this TWAMP module is left untouched.
    # ------------------------------------------------------------
    st.subheader("Packet Loss Summary — Site Condition")
    st.caption(
        f"Evaluasi periode {start_date:%d %b %Y} – {end_date:%d %b %Y}. "
        "PL harian memakai nilai maksimum jika ada beberapa record per hari. "
        "Meet harian: PL ≤ 0,7%; Not Meet harian: PL > 0,7%."
    )

    summary_source = df.copy()
    if tower_col:
        summary_source["TowerID"] = summary_source[tower_col].fillna("").astype(str).str.strip()
    else:
        summary_source["TowerID"] = ""

    if enodeb_col:
        summary_source["Full eNodeB Name"] = summary_source[enodeb_col].fillna("").astype(str).str.strip()
    elif site_col:
        summary_source["Full eNodeB Name"] = summary_source[site_col].fillna("").astype(str).str.strip()
    else:
        summary_source["Full eNodeB Name"] = ""

    enodeb_text = summary_source["Full eNodeB Name"]
    extracted_tower = enodeb_text.str.extract(r"(SUM-[^#]*)", flags=re.IGNORECASE, expand=False)
    missing_tower = summary_source["TowerID"].eq("")
    summary_source.loc[missing_tower, "TowerID"] = extracted_tower[missing_tower].fillna("")
    has_sum_tower = summary_source["TowerID"].str.upper().str.startswith("SUM-") & summary_source["TowerID"].ne("")
    summary_source["Site Display"] = summary_source["Full eNodeB Name"]
    summary_source.loc[has_sum_tower, "Site Display"] = summary_source.loc[has_sum_tower, "TowerID"]
    summary_source["Site Display"] = summary_source["Site Display"].replace("", "Site tidak diketahui")
    summary_source["_Day"] = summary_source["_Date"].dt.normalize()

    # Daily maximum ensures brief PL spikes are not hidden by averaging.
    daily = (
        summary_source.groupby(["Site Display", "_Day"], dropna=False)["_PacketLoss"]
        .max()
        .reset_index(name="Daily_Max_PL_Percent")
    )
    daily["_Daily_Not_Meet"] = daily["Daily_Max_PL_Percent"] > target

    period_start = pd.Timestamp(start_date)
    period_end = pd.Timestamp(end_date)
    total_calendar_days = (period_end - period_start).days + 1
    condition_rows = []

    for site_name, grp in daily.groupby("Site Display", dropna=False):
        grp = grp.sort_values("_Day")
        site_rows = summary_source[summary_source["Site Display"].astype(str) == str(site_name)]
        bad_days = set(grp.loc[grp["_Daily_Not_Meet"], "_Day"].tolist())
        observed_days = set(grp["_Day"].tolist())
        not_meet_days = len(bad_days)

        # Consecutive Not Meet days ending at the selected End Date.
        consecutive = 0
        cursor = period_end
        while cursor in observed_days and cursor in bad_days:
            consecutive += 1
            cursor -= pd.Timedelta(days=1)

        if not_meet_days == 0:
            condition = "Normal"
        elif not_meet_days == 1:
            condition = "Spike"
        elif not_meet_days <= 3:
            condition = "Recurring High"
        else:
            condition = "Persistent High"

        daily_sorted = grp.sort_values("_Day")
        latest_day_row = daily_sorted.iloc[-1]
        latest_date = latest_day_row["_Day"]
        latest_pl = float(latest_day_row["Daily_Max_PL_Percent"])
        previous_rows = daily_sorted[daily_sorted["_Day"] < latest_date]
        if previous_rows.empty:
            trend = "Insufficient history"
            change = None
        else:
            previous_pl = float(previous_rows.iloc[-1]["Daily_Max_PL_Percent"])
            change = latest_pl - previous_pl
            if change < -1e-12:
                trend = "Improving"
            elif change > 1e-12:
                trend = "Worsening"
            else:
                trend = "Unchanged"

        tower_vals = site_rows["TowerID"].dropna().astype(str).str.strip()
        enodeb_vals = site_rows["Full eNodeB Name"].dropna().astype(str).str.strip()
        tower_value = next((v for v in tower_vals if v), "")
        enodeb_value = next((v for v in enodeb_vals if v), "")

        condition_rows.append({
            "TowerID": tower_value,
            "Full eNodeB Name": enodeb_value or str(site_name),
            "Site Display": str(site_name),
            "Period Start": period_start.strftime("%Y-%m-%d"),
            "Period End": period_end.strftime("%Y-%m-%d"),
            "Days With Data": int(grp["_Day"].nunique()),
            "Missing Days": int(max(total_calendar_days - grp["_Day"].nunique(), 0)),
            "Not Meet Days": int(not_meet_days),
            "Not Meet Days (%)": round(not_meet_days / total_calendar_days * 100, 2),
            "Consecutive Not Meet Days at End": int(consecutive),
            "Latest Data Date": latest_date.strftime("%Y-%m-%d"),
            "Latest Daily Max PL (%)": latest_pl,
            "Max PL in Period (%)": float(grp["Daily_Max_PL_Percent"].max()),
            "PL Trend vs Previous Available Day": trend,
            "PL Change (percentage points)": change,
            "Site Condition": condition,
        })

    condition_summary = pd.DataFrame(condition_rows)
    # Chart uses the exact Site Display column already constructed for the summary.
    # Do not rebuild site names independently: TowerID may be extracted from
    # Full eNodeB Name in summary_source when TowerID is blank in the CSV.
    chart_df = summary_source.copy()
    chart_df["_ChartSiteDisplay"] = (
        chart_df["Site Display"].fillna("").astype(str).str.strip()
        .replace("", "Site tidak diketahui")
    )
    if active_resolution == "Daily":
        chart_df["_ChartDay"] = chart_df["_Date"].dt.normalize()
        chart_df = (
            chart_df.groupby(["_ChartSiteDisplay", "_ChartDay"], as_index=False)["_PacketLoss"]
            .max()
            .rename(columns={"_ChartDay": "_Date"})
        )

    # Use the same Site Condition classifications as the summary table.
    st.subheader("TWAMP Packet Loss")
    condition_options = ["All", "Normal", "Spike", "Recurring High", "Persistent High"]
    selected_condition = st.selectbox(
        "Filter chart by Site Condition",
        condition_options,
        index=0,
        key="twamp_chart_condition_filter",
    )

    if selected_condition != "All":
        condition_sites = condition_summary.loc[
            condition_summary["Site Condition"].eq(selected_condition),
            "Site Display",
        ].astype(str).unique().tolist() if not condition_summary.empty else []
        chart_df = chart_df[chart_df["_ChartSiteDisplay"].astype(str).isin(condition_sites)]

    fig = go.Figure()
    if not chart_df.empty:
        for site_name, grp in chart_df.groupby("_ChartSiteDisplay", dropna=False):
            grp = grp.sort_values("_Date")
            fig.add_trace(go.Scatter(
                x=grp["_Date"], y=grp["_PacketLoss"], mode="lines+markers",
                name=str(site_name), showlegend=True, line=dict(width=2),
                hovertemplate="%{x}<br>Packet Loss: %{y:.4f}%<extra>%{fullData.name}</extra>",
            ))

    fig.add_hline(
        y=target, line_dash="dash", line_color="red",
        annotation_text="Target PL 0.7%", annotation_position="top left",
    )
    fig.update_layout(
        template="plotly_white",
        showlegend=True,
        height=520,
        autosize=True,
        xaxis_title="Date / Time" if active_resolution == "Hourly" else "Date",
        yaxis_title="Packet Loss (%)",
        hovermode="x unified",
        legend_title_text="Site / eNodeB",
        margin=dict(l=8, r=8, t=24, b=72),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.18,
            xanchor="center",
            x=0.5,
            title=dict(text="Site / eNodeB"),
        ),
    )
    if chart_df.empty:
        st.info(f"Tidak ada site dengan kondisi '{selected_condition}' pada periode terpilih.")
    else:
        st.plotly_chart(fig, use_container_width=True, config={"responsive": True})

    if not condition_summary.empty:
        condition_order = {
            "Persistent High": 0, "Recurring High": 1, "Spike": 2, "Normal": 3
        }
        condition_summary["_order"] = condition_summary["Site Condition"].map(condition_order)
        condition_summary = condition_summary.sort_values(
            ["_order", "Not Meet Days", "Max PL in Period (%)"],
            ascending=[True, False, False],
        ).drop(columns="_order").reset_index(drop=True)
        condition_summary.insert(0, "No", range(1, len(condition_summary) + 1))

        # Excel-like freeze panes for the TWAMP Site Condition summary:
        # keep the first six identity/period columns fixed while scrolling horizontally.
        # Keep the existing Streamlit dataframe available as a fallback.
        try:
            import streamlit.components.v1 as components

            freeze_widths = [52, 110, 350, 110, 100, 100]
            left_offsets = []
            running_width = 0
            for width in freeze_widths:
                left_offsets.append(running_width)
                running_width += width

            table_html = condition_summary.to_html(
                index=False,
                escape=True,
                border=0,
                classes="twamp-freeze-table",
            )
            sticky_css = """
            <style>
              html, body { margin: 0; padding: 0; font-family: sans-serif; }
              .twamp-scroll { width: 100%; height: 560px; overflow: auto; border: 1px solid #e5e7eb; }
              table.twamp-freeze-table { border-collapse: separate; border-spacing: 0; width: max-content; min-width: 100%; font-size: 12px; color: #374151; }
              .twamp-freeze-table th, .twamp-freeze-table td {
                box-sizing: border-box; padding: 7px 8px; white-space: nowrap;
                border-right: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb;
                background: white; text-align: left;
              }
              .twamp-freeze-table thead th { position: sticky; top: 0; z-index: 30; background: #f3f4f6; font-weight: 600; }
              .twamp-freeze-table th:nth-child(1), .twamp-freeze-table td:nth-child(1) { position: sticky; left: 0px; min-width: 52px; width: 52px; z-index: 20; }
              .twamp-freeze-table th:nth-child(2), .twamp-freeze-table td:nth-child(2) { position: sticky; left: 52px; min-width: 110px; width: 110px; z-index: 20; }
              .twamp-freeze-table th:nth-child(3), .twamp-freeze-table td:nth-child(3) { position: sticky; left: 162px; min-width: 350px; width: 350px; max-width: 350px; overflow: hidden; text-overflow: ellipsis; z-index: 20; }
              .twamp-freeze-table th:nth-child(4), .twamp-freeze-table td:nth-child(4) { position: sticky; left: 512px; min-width: 110px; width: 110px; z-index: 20; }
              .twamp-freeze-table th:nth-child(5), .twamp-freeze-table td:nth-child(5) { position: sticky; left: 622px; min-width: 100px; width: 100px; z-index: 20; }
              .twamp-freeze-table th:nth-child(6), .twamp-freeze-table td:nth-child(6) { position: sticky; left: 722px; min-width: 100px; width: 100px; z-index: 20; }
              .twamp-freeze-table thead th:nth-child(-n+6) { z-index: 40; background: #eef2f7; }
              .twamp-freeze-table tbody tr:hover td { background: #f8fafc; }
              .twamp-freeze-table tbody tr:hover td:nth-child(-n+6) { background: #f8fafc; }
            </style>
            """
            components.html(
                sticky_css + '<div class="twamp-scroll">' + table_html + '</div>',
                height=580,
                scrolling=False,
            )
        except Exception:
            st.dataframe(condition_summary, use_container_width=True, hide_index=True)

        st.download_button(
            "Download Site Condition Summary CSV",
            data=condition_summary.to_csv(index=False).encode("utf-8-sig"),
            file_name="twamp_site_condition_selected_period.csv",
            mime="text/csv",
            key="twamp_selected_period_condition_download",
        )

        # Convenient status-specific downloads retain the same whole-period
        # classification rather than splitting individual measurements by status.
        for condition_name, file_stub in [
            ("Persistent High", "persistent_high"),
            ("Recurring High", "recurring_high"),
            ("Spike", "spike"),
            ("Normal", "normal"),
        ]:
            subset = condition_summary[condition_summary["Site Condition"] == condition_name]
            if not subset.empty:
                st.download_button(
                    f"Download {condition_name} Sites CSV",
                    data=subset.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"twamp_{file_stub}_sites.csv",
                    mime="text/csv",
                    key=f"twamp_condition_{file_stub}_download",
                )
    else:
        st.info("Belum ada data untuk summary kondisi site pada periode terpilih.")

    st.subheader("Not Meet Records")
    bad = df[df["_Status"] == "Not Meet"].copy()
    display_cols = [date_col]
    if site_col:
        display_cols.append(site_col)
    display_cols += [packet_loss_col, "_Status"]
    if bad.empty:
        st.success("Tidak ada record Not Meet pada filter saat ini.")
    else:
        st.dataframe(
            bad[display_cols].sort_values(by=packet_loss_col, ascending=False),
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            "Download Not Meet Records CSV",
            data=bad[display_cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="twamp_packet_loss_not_meet_records.csv",
            mime="text/csv",
        )

    with st.expander("View all TWAMP records"):
        show_cols = [date_col]
        if site_col:
            show_cols.append(site_col)
        show_cols += [packet_loss_col, "_Status"]
        if rtt_col in df.columns:
            show_cols.append(rtt_col)
        st.dataframe(df[show_cols], use_container_width=True, hide_index=True)
        st.download_button(
            "Download filtered TWAMP CSV",
            data=df[show_cols].to_csv(index=False).encode("utf-8-sig"),
            file_name="twamp_packet_loss_filtered.csv",
            mime="text/csv",
        )


if _dashboard_mode == "TWAMP Packet Loss":
    render_twamp_dashboard()
    st.stop()



# ============================================================
# CHART RENDER COLLECTOR
# ============================================================
# Keep the existing dashboard rendering exactly the same while
# collecting the displayed Plotly figures for grouped download.
_download_figures = []

def show_chart(fig, compact_summary=False, **kwargs):
    """
    Render charts with a large plotting area and a responsive Cell Name legend.

    - Keeps the original Cell Name values.
    - Legend is always below the chart.
    - Legend wraps to multiple rows automatically.
    - Chart height grows with the number of traces.
    - Lines/markers are explicitly kept visible.
    """
    _download_figures.append(fig)

    # Count actual named traces.
    trace_count = sum(
        1 for trace in fig.data
        if getattr(trace, "name", None)
    )

    # More traces -> more legend rows -> more chart height.
    # Keep the plot area itself large; only the overall figure grows.
    legend_rows = max(1, math.ceil(max(trace_count, 1) / 3))
    chart_height = max(
        540,
        430 + (legend_rows * 42),
    )

    # Apply line-only properties ONLY to line/scatter traces.
    # Some KPI charts (e.g. TA Distribution) use Bar traces, and
    # Bar does not support Scatter-only properties such as connectgaps.
    # Applying those properties globally causes Plotly ValueError.
    for trace in fig.data:
        trace_type = getattr(trace, "type", "")

        if trace_type in ("scatter", "scattergl", "scatter3d"):
            # Do NOT overwrite a line width explicitly configured by the
            # chart itself (important for Site Level Summary TTI/Availability).
            current_width = None
            current_marker_size = None
            try:
                current_width = trace.line.width
            except Exception:
                pass
            try:
                current_marker_size = trace.marker.size
            except Exception:
                pass

            update_kwargs = {
                "connectgaps": True,
                "opacity": 1.0,
            }
            if current_width is None:
                update_kwargs["line"] = dict(width=2.2)
            if current_marker_size is None:
                update_kwargs["marker"] = dict(size=4)

            trace.update(**update_kwargs)

        elif trace_type == "bar":
            # Preserve the bar opacity selected by the chart.
            # This keeps the Payload background translucent so the KPI
            # lines remain visually dominant.
            pass
        else:
            # Safe fallback for other trace types.
            try:
                trace.update(opacity=1.0)
            except Exception:
                pass

    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(
            family="Arial",
            size=10,
            color="#333333",
        ),
        title=dict(
            font=dict(
                family="Arial",
                size=15,
                color="#111111",
            ),
            x=0.0,
            xanchor="left",
        ),
        xaxis=dict(
            showgrid=True,
            gridcolor="#e5e5e5",
            gridwidth=1,
            zeroline=False,
            showline=True,
            linecolor="#777777",
            linewidth=1,
            automargin=True,
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor="#e5e5e5",
            gridwidth=1,
            zeroline=False,
            showline=True,
            linecolor="#777777",
            linewidth=1,
            automargin=True,
        ),
        legend=dict(
            title=dict(
                text="Cell Name",
                font=dict(
                    family="Arial Black",
                    size=11,
                    color="#222222",
                ),
            ),
            font=dict(
                family="Arial Black",
                size=10,
                color="#222222",
            ),
            orientation="h",
            yanchor="top",
            y=-0.18,
            xanchor="center",
            x=0.5,
            bgcolor="rgba(255,255,255,0)",
            traceorder="normal",
            itemsizing="constant",
            # Fixed entry width makes long Cell Names wrap instead of
            # squeezing the plotting area horizontally.
            entrywidth=190,
            entrywidthmode="pixels",
        ),
        height=chart_height,
        margin=dict(
            l=42 if not compact_summary else 18,
            r=2 if not compact_summary else 0,
            t=65,
            # Reserve enough space for all legend rows.
            b=max(125, 90 + (legend_rows * 34)),
        ),
    )

    st.plotly_chart(fig, **kwargs)


# ============================================================
# KPI CONFIG
# Based on the actual KPI CSV headers supplied for this test.
# ============================================================
KPI_CONFIG = {
    # ========================================================
    # Accessibility
    # ========================================================
    "4G Cell Availability": {
        "column": "4G Cell Availability(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "SSSR": {
        "column": "SSSR (LTE)(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "RRC Setup SR": {
        "column": "RRC Setup Success Rate(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "E-RAB Setup SR": {
        "column": "HTI_E-RAB Setup Success Rate",
        "category": "Accessibility",
        "unit": "%",
    },
    "E-RAB Setup SR (ALL)_NPM": {
        "column": "ERAB Setup Success Rate (ALL)_NPM",
        "category": "Accessibility",
        "unit": "%",
    },
    "E-RAB Setup SR Test": {
        "column": "ERAB Setup Success Rate_Test",
        "category": "Accessibility",
        "unit": "%",
    },
    "RACH Success Rate": {
        "column": "RACH Success Rate(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "CBRA Success Rate": {
        "column": "ISL_BZG_CBRA Success Rate(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "CBRA Counter": {
        "column": "CBRA",
        "category": "Accessibility",
        "unit": "",
    },
    "CBRA Num": {
        "column": "CBRA Num",
        "category": "Accessibility",
        "unit": "",
    },
    "CBRA_Num": {
        "column": "CBRA_Num",
        "category": "Accessibility",
        "unit": "",
    },
    "VoLTE CSSR (%)": {
        "column": "Voice Call Setup Success Rate (VoLTE)(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "E-RAB Setup SR VoIP": {
        "column": "E-RAB Setup Success Rate (VoIP)(%)",
        "category": "Accessibility",
        "unit": "%",
    },
    "S1 Setup SR": {
        "column": "HX4_S1 Setup Success Rate",
        "category": "Accessibility",
        "unit": "%",
    },
    "VoLTE CSSR": {
        "column": "Voice Call Setup Success Rate (VoLTE)",
        "category": "Accessibility",
        "unit": "%",
    },
    "HX4 VoLTE CSSR": {
        "column": "HX4_VOLTE CSSR",
        "category": "Accessibility",
        "unit": "%",
    },

    # ========================================================
    # Retainability
    # ========================================================
    "E-RAB Drop": {
        "column": "ERAB Drop (LTE)",
        "category": "Retainability",
        "unit": "%",
    },
    "Radio Drop Rate": {
        "column": "Radio Drop Rate",
        "category": "Retainability",
        "unit": "%",
    },
    "E2E Drop Rate": {
        "column": "E2E Drop Rate",
        "category": "Retainability",
        "unit": "%",
    },
    "E-RAB Abnormal Release": {
        "column": "L.E-RAB.AbnormRel",
        "category": "Retainability",
        "unit": "",
    },
    "Radio Abnormal Release": {
        "column": "L.E-RAB.AbnormRel.Radio",
        "category": "Retainability",
        "unit": "",
    },
    "TNL Abnormal Release": {
        "column": "L.E-RAB.AbnormRel.TNL",
        "category": "Retainability",
        "unit": "",
    },

    # ========================================================
    # Mobility
    # ========================================================
    "HOSR Inter": {
        "column": "HOSR Inter(%)",
        "category": "Mobility",
        "unit": "%",
    },
    "HOSR Intra": {
        "column": "HOSR Intra(%)",
        "category": "Mobility",
        "unit": "%",
    },
    "HOSR IRAT": {
        "column": "HOSR IRAT(%)",
        "category": "Mobility",
        "unit": "%",
    },
    "HOSR Inter+Intra": {
        "column": "HOSR INTER-INTRA FREQ",
        "category": "Mobility",
        "unit": "%",
    },
    "HOSR Inter-Intra Freq": {
        "column": "HOSR INTER-INTRA FREQ",
        "category": "Mobility",
        "unit": "%",
    },
    "Pop 4G HOSR Inter+Intra": {
        "column": "Pop 4G HOSR Inter+Intra",
        "category": "Mobility",
        "unit": "%",
    },

    # ========================================================
    # Utilization
    # ========================================================
    "PRB": {
        "column": "4G DL PRB Rate",
        "category": "Utilization",
        "unit": "%",
    },
    "DL PRB Utilization": {
        "column": "MM.DL PRB Utilization(%)",
        "category": "Utilization",
        "unit": "%",
    },
    "HX4 DL PRB Utilization": {
        "column": "HX4_DL PRB Utilization",
        "category": "Utilization",
        "unit": "%",
    },
    "HX4 UL PRB Utilization": {
        "column": "HX4_UL PRB Utilization",
        "category": "Utilization",
        "unit": "%",
    },
    "RRC User": {
        "column": "SWAP2016_4G_Avg RRC User_New(number)",
        "category": "Utilization",
        "unit": "Users",
    },
    "RRC User Max": {
        "column": "SWAP2016_4G_Max RRC User_New(number)",
        "category": "Utilization",
        "unit": "Users",
    },
    "CAP RRC Connected User": {
        "column": "CAP RRC Connected User",
        "category": "Utilization",
        "unit": "Users",
    },
    "Number of RRC Connected User": {
        "column": "Number of RRC Connected User",
        "category": "Utilization",
        "unit": "Users",
    },
    "Last TTI Ratio": {
        "column": "Last TTI Ratio %",
        "category": "Utilization",
        "unit": "%",
    },

    # ========================================================
    # Traffic
    # ========================================================
    "Payload": {
        "column": "4GTotalPayloadGB",
        "category": "Traffic",
        "unit": "GB",
        "chart": "stacked",
    },
    "DL Payload": {
        "column": "Payload_DL_GB(%)",
        "category": "Traffic",
        "unit": "",
    },
    "UL Payload": {
        "column": "UL Payload (GB)",
        "category": "Traffic",
        "unit": "GB",
    },
    "VoLTE Traffic": {
        "column": "VoLTE Traffic (Erl)(Erl)",
        "category": "Traffic",
        "unit": "Erl",
    },
    "DL Cell Throughput": {
        "column": "DL Cell Throughput(Mbit/s)",
        "category": "Traffic",
        "unit": "Mbps",
    },
    "UL Cell Throughput": {
        "column": "UL Cell Throughput(Mbit/s)",
        "category": "Traffic",
        "unit": "Mbps",
    },
    "DL User Throughput": {
        "column": "DL User Throughput (Mbps)(MB/s)",
        "category": "Traffic",
        "unit": "Mbps",
    },
    "UL User Throughput": {
        "column": "UL User Throughput (Mbps)(MB/s)",
        "category": "Traffic",
        "unit": "Mbps",
    },
    "HX4 DL User Throughput": {
        "column": "HX4_DL User Throughput (Mbps)",
        "category": "Traffic",
        "unit": "Mbps",
    },
    "HX4 UL User Throughput": {
        "column": "HX4_UL User Throughput (Mbps)",
        "category": "Traffic",
        "unit": "Mbps",
    },

    # ========================================================
    # Radio / Coverage / Quality
    # ========================================================
    "Average TA": {
        "column": "Average TA (m)",
        "category": "Radio/Coverage",
        "unit": "m",
    },
    "TA Distribution": {
        "column": "L.RA.TA.UE.Index0",
        "category": "Radio/Coverage",
        "unit": "UE",
        "chart": "ta_distribution",
    },
    "Average TA New": {
        "column": "Average TA (meters) new",
        "category": "Radio/Coverage",
        "unit": "m",
    },
    "CQI": {
        "column": "CQI Avg",
        "category": "Radio/Coverage",
        "unit": "",
    },
    "UL Interference": {
        "column": "L.UL.Interference.Avg(dBm)",
        "category": "Radio/Coverage",
        "unit": "dBm",
    },
    "UL RSSI": {
        "column": "UL_RSSI_LTE_New",
        "category": "Radio/Coverage",
        "unit": "dBm",
    },
    "UL RSSI PUCCH": {
        "column": "UL RSSI PUCCH (dBm)_Num",
        "category": "Radio/Coverage",
        "unit": "dBm",
    },
    "Cell Edge User Ratio": {
        "column": "Cell Edge User Ratio %",
        "category": "Radio/Coverage",
        "unit": "%",
    },
    "Edge User Ratio": {
        "column": "Edge User Ratio(PL9~14/PL0~14)",
        "category": "Radio/Coverage",
        "unit": "%",
    },
    "RANK2 Rate": {
        "column": "RANK2 Rate",
        "category": "Radio/Coverage",
        "unit": "%",
    },
    "SQM SSSR": {
        "column": "SQM_SSSR",
        "category": "Radio/Coverage",
        "unit": "%",
    },

    # ========================================================
    # Spectrum Efficiency
    # ========================================================
    "DL New Spectrum Efficiency": {
        "column": "DL New Spectrum Efficiency",
        "category": "Spectrum",
        "unit": "",
    },
    "DL Spectrum Efficiency": {
        "column": "Spectrum Efficiency (DL)",
        "category": "Spectrum",
        "unit": "",
    },

    # ========================================================
    # VoLTE / Packet Quality
    # ========================================================
    "VoLTE User Average": {
        "column": "VOLTE User average",
        "category": "VoLTE",
        "unit": "Users",
    },
    "VoLTE User Max": {
        "column": "VoLTE User Max",
        "category": "VoLTE",
        "unit": "Users",
    },
    "VoLTE DL Packet Loss": {
        "column": "VoLTE DL packet loss Ratio(%)",
        "category": "VoLTE",
        "unit": "%",
    },
    "VoLTE UL Packet Loss": {
        "column": "VoLTE UL packet loss Ratio",
        "category": "VoLTE",
        "unit": "%",
    },
    "QCI1 DL Packet Loss": {
        "column": "HX4_DL Packet Loss Rate of QCI1",
        "category": "VoLTE",
        "unit": "%",
    },
    "QCI1 UL Packet Loss": {
        "column": "HX4_UL Packet Loss Rate of QCI1",
        "category": "VoLTE",
        "unit": "%",
    },
    "Latency": {
        "column": "HX4_Latency",
        "category": "VoLTE",
        "unit": "ms",
    },

    # ========================================================
    # Failure / Root Cause Counters
    # ========================================================
    "RRC Setup Fail - No Reply": {
        "column": "L.RRC.SetupFail.NoReply",
        "category": "Failure Counters",
        "unit": "",
    },
    "RRC Setup Fail - Reject": {
        "column": "L.RRC.SetupFail.Rej",
        "category": "Failure Counters",
        "unit": "",
    },
    "RRC Setup Fail - Flow Control": {
        "column": "L.RRC.SetupFail.Rej.FlowCtrl",
        "category": "Failure Counters",
        "unit": "",
    },
    "RRC Setup Fail - MME Overload": {
        "column": "L.RRC.SetupFail.Rej.MMEOverload",
        "category": "Failure Counters",
        "unit": "",
    },
    "E-RAB FailEst - No Reply": {
        "column": "L.E-RAB.FailEst.NoReply",
        "category": "Failure Counters",
        "unit": "",
    },
    "E-RAB FailEst - MME": {
        "column": "L.E-RAB.FailEst.MME",
        "category": "Failure Counters",
        "unit": "",
    },
    "E-RAB FailEst - TNL": {
        "column": "L.E-RAB.FailEst.TNL",
        "category": "Failure Counters",
        "unit": "",
    },
    "E-RAB FailEst - RNL": {
        "column": "L.E-RAB.FailEst.RNL",
        "category": "Failure Counters",
        "unit": "",
    },
    "E-RAB FailEst - No Radio Resource": {
        "column": "L.E-RAB.FailEst.NoRadioRes",
        "category": "Failure Counters",
        "unit": "",
    },
}

# ============================================================
# KPI HEADER ALIASES
# ============================================================
#
# The dashboard has a canonical KPI name, but uploaded CSVs may
# use slightly different export/header names.
#
# Matching order:
#   exact -> case-insensitive -> normalized -> aliases
#
# If nothing matches, the KPI is NOT silently guessed. It will
# appear in the dashboard warning as "not found".
# ============================================================

KPI_HEADER_ALIASES = {

    "4G Cell Availability": [
        "4G Cell Availability(%)",
        "4G Cell Availability",
        "Cell Availability(%)",
        "Cell Availability",
        "4G Availability",
    ],

    "DL Payload": [
        "Payload_DL_GB(%)",
        "Payload DL GB",
        "DL Payload",
        "DL Payload (GB)",
    ],

    "SSSR": [
        "SSSR (LTE)(%)",
        "SSSR (%)",
        "SSSR(%)",
        "SSSR",
    ],

    "RRC Setup SR": [
        "RRC Setup Success Rate(%)",
        "RRC Setup Success Rate (%)",
        "RRC Setup SR",
        "RRC Setup Success Rate",
    ],

    "E-RAB Setup SR": [
        "HTI_E-RAB Setup Success Rate",
        "E-RAB Setup Success Rate",
        "E-RAB Setup SR",
        "ERAB Setup Success Rate",
    ],

    "UL Payload": [
        "UL Payload (GB)",
        "UL Payload",
        "UL Payload GB",
    ],

    "HX4 DL PRB Utilization": [
        "HX4_DL PRB Utilization",
        "HX4 DL PRB Utilization",
        "DL PRB Utilization",
    ],

    "HX4 UL PRB Utilization": [
        "HX4_UL PRB Utilization",
        "HX4 UL PRB Utilization",
        "UL PRB Utilization",
    ],

    "HX4 DL User Throughput": [
        "HX4_DL User Throughput (Mbps)",
        "HX4 DL User Throughput (Mbps)",
        "HX4 DL User Throughput",
    ],

    "DL User Throughput": [
        "DL User Throughput (Mbps)(MB/s)",
        "DL User Throughput (Mbps)",
        "DL User Throughput",
    ],

    "UL User Throughput": [
        "UL User Throughput (Mbps)(MB/s)",
        "UL User Throughput (Mbps)",
        "UL User Throughput",
    ],

    "Average TA": [
        "Average TA (m)",
        "Average TA (meters)",
        "Average TA (meters) new",
        "Average TA",
    ],

    "Latency": [
        "HX4_Latency",
        "HX4 Latency",
        "Latency",
        "Latency (ms)",
    ],

    "Last TTI Ratio": [
        "Last TTI Ratio %",
        "Last TTI Ratio(%)",
        "Last TTI Ratio",
    ],

    "Number of RRC Connected User": [
        "Number of RRC Connected User",
        "RRC Connected User",
        "Number of RRC Users",
    ],

    "RANK2 Rate": [
        "RANK2 Rate",
        "RANK2 Rate (%)",
        "RANK2",
    ],

    "DL Spectrum Efficiency": [
        "Spectrum Efficiency (DL)",
        "DL Spectrum Efficiency",
        "DL Spectrum Efficiency (%)",
    ],

    "CQI": [
        "CQI Avg",
        "CQI Average",
        "Average CQI",
        "CQI",
    ],

    "UL Interference": [
        "L.UL.Interference.Avg(dBm)",
        "UL Interference Avg(dBm)",
        "UL Interference",
        "UL Interference (dBm)",
    ],

    "HOSR Intra": [
        "HOSR Intra(%)",
        "HOSR Intra (%)",
        "HOSR Intra",
    ],

    "HOSR Inter": [
        "HOSR Inter(%)",
        "HOSR Inter (%)",
        "HOSR Inter",
    ],

    "E-RAB Setup SR VoIP": [
        "E-RAB Setup Success Rate (VoIP)(%)",
        "E-RAB Setup Success Rate (VoIP)",
        "E-RAB Setup SR VoIP",
        "ERAB Setup Success Rate VoIP",
    ],

    # Common identifiers
    "_Date": [
        "Date",
        "DATE",
        "date",
        "Timestamp",
        "Time",
    ],

    "_eNodeB": [
        "eNodeB Name",
        "eNodeBName",
        "ENodeB Name",
        "ENODEB Name",
        "eNodeB",
        "Site Name",
    ],

    "_Cell": [
        "Cell Name",
        "CellName",
        "CELL NAME",
        "Cell",
        "Cell_Name",
    ],

    "_LocalCell": [
        "LocalCell Id",
        "LocalCell ID",
        "LocalCellId",
        "Local Cell ID",
        "Local Cell Id",
    ],
}


def find_kpi_column(df, kpi_name):
    """
    Resolve the actual CSV column for a KPI.

    The canonical KPI_CONFIG column is checked first, followed by
    explicitly approved aliases.
    """
    canonical = KPI_CONFIG[kpi_name]["column"]

    candidates = [canonical]
    candidates.extend(
        KPI_HEADER_ALIASES.get(kpi_name, [])
    )

    return find_column(
        df,
        list(dict.fromkeys(candidates)),
    )


# ============================================================
# INTERNAL MASTER MAPPING
#
# The user should NOT upload this mapping.
# It is embedded in the dashboard.
#
# Source basis: the supplied CI / Sector / FreqBand master.
# Sector 0 is displayed as "Indoor".
# ============================================================
MASTER_MAPPING = {
    # L900
    1:  (1, "900"),
    2:  (2, "900"),
    3:  (3, "900"),

    # L1800 - standard
    4:  (1, "1800"),
    5:  (2, "1800"),
    6:  (3, "1800"),

    # L2100 - standard
    7:  (1, "2100"),
    8:  (2, "2100"),
    9:  (3, "2100"),

    # L700
    21: (1, "700"),
    22: (2, "700"),
    23: (3, "700"),

    # L850
    131: (1, "850"),
    132: (2, "850"),
    133: (3, "850"),
    134: (4, "850"),

    # L1800 additional sector groups
    14: (1, "1800"),
    15: (2, "1800"),
    16: (3, "1800"),
    24: (4, "1800"),
    34: (1, "1800"),
    35: (2, "1800"),
    36: (3, "1800"),
    44: (1, "1800"),
    45: (2, "1800"),
    46: (3, "1800"),

    # L2100 additional sector groups
    17: (1, "2100"),
    18: (2, "2100"),
    19: (3, "2100"),
    27: (4, "2100"),
    37: (1, "2100"),
    38: (2, "2100"),
    39: (3, "2100"),
    47: (1, "2100"),
    48: (2, "2100"),
    49: (3, "2100"),

    # 2300 F1
    111: (1, "2300F1"),
    112: (2, "2300F1"),
    113: (3, "2300F1"),

    # 2300 F2
    121: (1, "2300F2"),
    122: (2, "2300F2"),
    123: (3, "2300F2"),

    # Indoor / Sector 0
    51:  (0, "1800"),
    52:  (0, "1800"),
    94:  (0, "1800"),
    95:  (0, "1800"),
    96:  (0, "1800"),

    91:  (0, "2100"),
    92:  (0, "2100"),
    97:  (0, "2100"),
    98:  (0, "2100"),
    99:  (0, "2100"),

    141: (0, "2300F1"),
    142: (0, "2300F1"),
    143: (0, "2300F1"),

    151: (0, "2300F2"),
    152: (0, "2300F2"),
    153: (0, "2300F2"),
}

CI_TO_SECTOR = {
    ci_value: sector
    for ci_value, (sector, band) in MASTER_MAPPING.items()
}

CI_TO_BAND = {
    ci_value: band
    for ci_value, (sector, band) in MASTER_MAPPING.items()
}


def parse_kpi_numeric(series):
    """Convert KPI values from CSV to numeric safely.

    Handles common export formats such as:
    - 98.7
    - "98.7%"
    - "1,234.56"
    - blank / "-" / "N/A"

    Invalid/non-numeric values become NaN instead of breaking the dashboard.
    """
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    cleaned = (
        series.astype("string")
        .str.strip()
        .replace({"": pd.NA, "-": pd.NA, "N/A": pd.NA, "NA": pd.NA})
        .str.replace("%", "", regex=False)
        .str.replace(",", "", regex=False)
    )

    return pd.to_numeric(cleaned, errors="coerce")


def normalize_cell_name(value):
    """
    Normalize Cell Name values so visually identical cells cannot
    become separate Plotly series because of hidden Unicode characters,
    non-breaking spaces, inconsistent whitespace, or letter case.

    The returned display value is kept in a stable uppercase form so
    grouping/coloring always treats the same Cell Name as one series.
    """
    if pd.isna(value):
        return ""

    text = unicodedata.normalize("NFKC", str(value))

    # Remove common invisible characters from exported CSVs.
    text = (
        text
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\xa0", " ")
    )

    # Collapse repeated whitespace and trim.
    text = re.sub(r"\\s+", " ", text).strip()

    # Cell names in this dashboard are identifiers; case should not
    # create a second visual series.
    return text.upper()


def normalize_header(value):
    """
    Normalize a CSV header for tolerant matching.

    Example:
        "SSSR (LTE)(%)" -> "sssrltte"
        "SSSR_LTE"      -> "sssr_lte"
        "Cell Name"     -> "cellname"

    The function is intentionally conservative: it does not use
    fuzzy matching, so an unrelated KPI will not be silently mapped.
    """
    text = str(value).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def find_column(df, candidates):
    """
    Find a CSV column using:
      1. exact match
      2. case-insensitive match
      3. normalized-header match

    No fuzzy matching is used.
    """
    exact = {
        str(c).strip(): c
        for c in df.columns
    }

    for candidate in candidates:
        if candidate in exact:
            return exact[candidate]

    lower = {
        str(c).strip().lower(): c
        for c in df.columns
    }

    for candidate in candidates:
        key = candidate.strip().lower()
        if key in lower:
            return lower[key]

    normalized = {
        normalize_header(c): c
        for c in df.columns
    }

    for candidate in candidates:
        key = normalize_header(candidate)
        if key in normalized:
            return normalized[key]

    return None


def extract_sum_site(enodeb_name):
    """
    Example:
    4264504E_LTE_MUARA_BULIAN#SUM-JA-MBN-0130#MC
    -> SUM-JA-MBN-0130

    If there is no SUM- section, return blank.
    """
    if pd.isna(enodeb_name):
        return None

    text = str(enodeb_name).strip()
    match = re.search(r"(SUM-[^#]+)", text)

    return match.group(1).strip() if match else None


def normalize_sector(value):
    if pd.isna(value):
        return ""

    value = str(value).strip()

    if value == "0":
        return "Indoor"

    return f"S{value}" if value.isdigit() else value


def apply_internal_mapping(df, localcell_col):
    """
    Apply the embedded CI -> Sector/FreqBand master.

    We use a dictionary instead of a merge because the master
    contains repeated CI rows across many site records.
    This avoids multiplying KPI rows during the join.
    """
    ci = pd.to_numeric(
        df[localcell_col],
        errors="coerce",
    )

    df["_CI"] = ci.astype("Int64")

    df["_Sector_Number"] = df["_CI"].map(
        CI_TO_SECTOR
    )

    df["_FreqBand"] = df["_CI"].map(
        CI_TO_BAND
    )

    df["_Sector_Display"] = df["_Sector_Number"].apply(
        normalize_sector
    )

    return df


# ============================================================
# UPLOAD — RAW KPI CSV / CSV.GZ
# ============================================================
st.sidebar.header("Upload")

uploaded_csv = st.sidebar.file_uploader(
    "Upload KPI CSV / CSV.GZ",
    type=["csv", "gz"],
    help="Supported formats: .csv and .csv.gz",
)

if uploaded_csv is None:
    st.info("Upload your KPI CSV / CSV.GZ from the sidebar.")
    st.stop()

# ------------------------------------------------------------
# Detect the uploaded file format.
#
# Supported:
#   *.csv
#   *.csv.gz
#
# CSV.GZ is read directly by pandas; no manual unzip/extract
# step is required.
# ------------------------------------------------------------
uploaded_name = uploaded_csv.name.lower().strip()

if uploaded_name.endswith(".csv.gz"):
    input_compression = "gzip"
elif uploaded_name.endswith(".csv"):
    input_compression = None
else:
    st.error(
        "Unsupported file format. "
        "Please upload a .csv or .csv.gz file."
    )
    st.stop()


@st.cache_data(show_spinner=False)
def get_csv_headers(file_bytes, compression=None):
    """Read only the CSV header once and cache it."""
    header_df = pd.read_csv(
        io.BytesIO(file_bytes),
        compression=compression,
        nrows=0,
    )
    return tuple(
        str(column).strip()
        for column in header_df.columns
    )


@st.cache_data(show_spinner=False)
def load_and_prepare_csv(
    file_bytes,
    usecols,
    enodeb_column,
    cell_column,
    localcell_column,
    date_column,
    time_column=None,
    compression=None,
):
    """
    Read only columns used by the dashboard, then perform the
    expensive one-time preparation work.

    Streamlit caches this result, so sidebar changes do not force
    the CSV parsing and mapping work to run again.

    compression:
        None   -> normal .csv
        gzip   -> .csv.gz
    """
    frame = pd.read_csv(
        io.BytesIO(file_bytes),
        compression=compression,
        usecols=list(usecols),
        low_memory=False,
    )

    frame.columns = [
        str(column).strip()
        for column in frame.columns
    ]

    frame["_Date_Day"] = pd.to_datetime(
        frame[date_column],
        errors="coerce",
    ).dt.normalize()

    # Automatically support both daily and hourly exports.
    # If a Time column exists, preserve Date + Time for charting.
    if time_column is not None and time_column in frame.columns:
        frame["_Date"] = pd.to_datetime(
            frame[date_column].astype(str).str.strip()
            + " "
            + frame[time_column].astype(str).str.strip(),
            errors="coerce",
        )
        frame["_Is_Hourly"] = True
    else:
        frame["_Date"] = pd.to_datetime(
            frame[date_column],
            errors="coerce",
        )
        frame["_Is_Hourly"] = False

    frame["_Site_ID"] = frame[enodeb_column].apply(
        extract_sum_site
    )

    frame["_Site_ID_Search"] = (
        frame["_Site_ID"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.upper()
    )

    frame["_eNodeB_Search"] = (
        frame[enodeb_column]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    frame = apply_internal_mapping(
        frame,
        localcell_column,
    )

    frame["_Sector_Display"] = (
        frame["_Sector_Number"]
        .apply(normalize_sector)
    )

    frame["_Sector_Search"] = (
        frame["_Sector_Display"]
        .fillna("")
        .astype(str)
        .str.strip()
    )

    # Normalize Cell Name once at ingestion time.
    # This prevents Plotly from creating two traces for the same cell
    # when the raw CSV contains hidden Unicode characters, NBSPs,
    # repeated spaces, or case differences.
    frame["_Cell_Display"] = frame[cell_column].apply(
        normalize_cell_name
    )

    return frame


# UploadedFile itself is intentionally not passed into cache_data.
# Bytes give Streamlit a stable, cacheable input for the same file.
file_bytes = uploaded_csv.getvalue()

st.sidebar.caption(
    f"Loaded format: {'CSV.GZ (gzip)' if input_compression == 'gzip' else 'CSV'}"
)

with st.spinner("Preparing KPI CSV..."):

    csv_headers = get_csv_headers(
        file_bytes,
        compression=input_compression,
    )

    header_frame = pd.DataFrame(
        columns=csv_headers
    )

    # --------------------------------------------------------
    # Required identifiers
    # --------------------------------------------------------
    enodeb_col = find_column(
        header_frame,
        [
            "eNodeB Name",
            "eNodeBName",
            "ENodeB Name",
            "ENODEB Name",
        ],
    )

    cell_col = find_column(
        header_frame,
        [
            "Cell Name",
            "CellName",
            "CELL NAME",
        ],
    )

    localcell_col = find_column(
        header_frame,
        [
            "LocalCell Id",
            "LocalCell ID",
            "LocalCellId",
        ],
    )

    date_col = find_column(
        header_frame,
        [
            "Date",
            "DATE",
            "date",
        ],
    )

    time_col = find_column(
        header_frame,
        [
            "Time",
            "TIME",
            "time",
        ],
    )

    required = {
        "eNodeB Name": enodeb_col,
        "Cell Name": cell_col,
        "LocalCell Id": localcell_col,
        "Date": date_col,
    }

    missing = [
        name
        for name, actual in required.items()
        if actual is None
    ]

    if missing:
        st.error(
            "Required column(s) not found: "
            + ", ".join(missing)
        )
        st.write(
            "Available columns:",
            list(csv_headers),
        )
        st.stop()

    # --------------------------------------------------------
    # Resolve KPI columns from the header only.
    # --------------------------------------------------------
    kpi_actual_columns = {}

    for kpi_name in KPI_CONFIG:

        if kpi_name == "TA Distribution":
            ta0 = "L.RA.TA.UE.Index0"
            if ta0 in csv_headers:
                kpi_actual_columns[kpi_name] = ta0
            continue

        actual_col = find_kpi_column(
            header_frame,
            kpi_name,
        )

        if actual_col is not None:
            kpi_actual_columns[kpi_name] = actual_col

    # --------------------------------------------------------
    # Read only columns required by the dashboard.
    # This avoids loading unused raw KPI columns.
    # --------------------------------------------------------
    # Extra source columns used only by the optional Num/Denum KPI mode.
    NUM_DEN_SOURCE_COLUMNS = {
        "RACH Success Rate": ("RACH Success Rate_num_rs", "RACH Success Rate_denum_rs", "ratio_pct"),
        "VoLTE Call Success Rate": ("Voice Call Success Rate (VoLTE)_Num", "Voice Call Success Rate (VoLTE)_DEN", "ratio_pct"),
        "DL Spectrum Efficiency": ("DL Spectrum Efficiency Num", "DL Spectrum Efficiency DeNum", "ratio"),
        "UL RSSI PUCCH": ("UL RSSI PUCCH (dBm)_Num", "UL RSSI PUCCH (dBm)_Denum", "weighted_avg"),
        "Average TA": ("Average TA (m)_Num", "Average TA (m)_Den", "weighted_avg"),
        "UL User Throughput": ("SWAP2016_4G_UL User Thpt_Num", "SWAP2016_4G_UL User Thpt_Denum", "weighted_avg"),
    }
    num_den_source_columns = {
        column
        for numerator, denominator, _formula in NUM_DEN_SOURCE_COLUMNS.values()
        for column in (numerator, denominator)
        if column in csv_headers
    }

    columns_to_read = {
        enodeb_col,
        cell_col,
        localcell_col,
        date_col,
        *kpi_actual_columns.values(),
        *num_den_source_columns,
    }

    if time_col is not None:
        columns_to_read.add(time_col)

    ta_distribution_cols = []
    ta_distribution_col_map = {}
    for ta_index in range(12):
        expected_prefix = f"L.RA.TA.UE.Index{ta_index}"
        actual_ta_col = next(
            (
                header for header in csv_headers
                if str(header).strip() == expected_prefix
                or str(header).strip().startswith(expected_prefix + " ")
                or str(header).strip().startswith(expected_prefix + "(")
            ),
            None,
        )
        if actual_ta_col is not None:
            ta_distribution_cols.append(actual_ta_col)
            ta_distribution_col_map[ta_index] = actual_ta_col
    columns_to_read.update(ta_distribution_cols)

    columns_to_read = tuple(
        column
        for column in csv_headers
        if column in columns_to_read
    )

    df = load_and_prepare_csv(
        file_bytes,
        columns_to_read,
        enodeb_col,
        cell_col,
        localcell_col,
        date_col,
        time_col,
        compression=input_compression,
    )

# ============================================================
# KPI VALUE MODE — preserve normal mode; optionally derive KPI
# values from explicitly paired numerator/denominator counters.
# ============================================================
kpi_value_mode = st.sidebar.radio(
    "KPI Value Mode",
    ["KPI Biasa", "KPI Num/Denum"],
    index=0,
    help="KPI Num/Denum hanya mengaktifkan KPI yang memiliki pasangan counter yang dikenal.",
    key="kpi_value_mode_v69",
)

NUM_DEN_SOURCE_COLUMNS = {
    "RACH Success Rate": ("RACH Success Rate_num_rs", "RACH Success Rate_denum_rs", "ratio_pct"),
    "VoLTE Call Success Rate": ("Voice Call Success Rate (VoLTE)_Num", "Voice Call Success Rate (VoLTE)_DEN", "ratio_pct"),
    "DL Spectrum Efficiency": ("DL Spectrum Efficiency Num", "DL Spectrum Efficiency DeNum", "ratio"),
    "UL RSSI PUCCH": ("UL RSSI PUCCH (dBm)_Num", "UL RSSI PUCCH (dBm)_Denum", "weighted_avg"),
    "Average TA": ("Average TA (m)_Num", "Average TA (m)_Den", "weighted_avg"),
    "UL User Throughput": ("SWAP2016_4G_UL User Thpt_Num", "SWAP2016_4G_UL User Thpt_Denum", "weighted_avg"),
}

num_den_available = []
if kpi_value_mode == "KPI Num/Denum":
    for derived_name, (numerator_col, denominator_col, formula) in NUM_DEN_SOURCE_COLUMNS.items():
        if numerator_col not in df.columns or denominator_col not in df.columns:
            continue
        numerator = pd.to_numeric(df[numerator_col], errors="coerce")
        denominator = pd.to_numeric(df[denominator_col], errors="coerce")
        valid = denominator.notna() & denominator.ne(0) & numerator.notna()
        derived_values = pd.Series(float("nan"), index=df.index, dtype="float64")
        if formula == "ratio_pct":
            derived_values.loc[valid] = numerator.loc[valid] / denominator.loc[valid] * 100.0
        elif formula == "ratio":
            derived_values.loc[valid] = numerator.loc[valid] / denominator.loc[valid]
        else:
            # Numerator/denominator pairs for weighted averages are assumed to
            # be accumulated sum/count counters; validate this with KPI owners.
            derived_values.loc[valid] = numerator.loc[valid] / denominator.loc[valid]
        derived_column = f"__DERIVED_NUM_DEN__{derived_name}"
        df[derived_column] = derived_values
        num_den_available.append((derived_name, derived_column))

    st.sidebar.caption(
        f"Num/Denum KPI tersedia: {len(num_den_available)} pasangan counter terdeteksi."
    )
    if not num_den_available:
        st.sidebar.warning(
            "Tidak ditemukan pasangan Num/Denum yang lengkap pada CSV ini."
        )

# ============================================================
# KPI AVAILABILITY
# ============================================================
available_kpis = [
    kpi_name
    for kpi_name in KPI_CONFIG
    if kpi_name in kpi_actual_columns
]

missing_kpis = [
    kpi_name
    for kpi_name in KPI_CONFIG
    if kpi_name not in kpi_actual_columns
]

if kpi_value_mode == "KPI Num/Denum":
    for derived_name, derived_column in num_den_available:
        display_name = f"{derived_name} [Num/Denum]"
        if display_name not in KPI_CONFIG:
            base_category = (
                "Accessibility" if "Success Rate" in derived_name
                else "Spectrum" if "Spectrum Efficiency" in derived_name
                else "Radio/Coverage" if derived_name in ("UL RSSI PUCCH", "Average TA")
                else "Traffic"
            )
            KPI_CONFIG[display_name] = {
                "column": derived_column,
                "category": base_category,
                "unit": "%" if derived_name.endswith("Success Rate") else "",
            }
        kpi_actual_columns[display_name] = derived_column
        available_kpis.append(display_name)

if not available_kpis:

    st.error(
        "No configured KPI columns were found "
        "in the uploaded CSV."
    )

    st.write(
        "Available CSV columns:",
        list(csv_headers),
    )

    st.stop()

# ============================================================
# TOP FILTERS — SITE SEARCH + DATE RANGE
# ============================================================
# Keep the fast-access filters together with Chart Layout.
top_site_col, top_mode_col, top_date_col = st.columns([1.7, 1.7, 2.2])

with top_site_col:
    st.markdown("**Site Search**")
    site_input = st.text_input(
        "Site ID / eNodeB Name",
        placeholder="SUM-SU-BNJ-8236 or full eNodeB Name",
        label_visibility="collapsed",
    )

with top_mode_col:
    search_mode = st.radio(
        "Search mode",
        ["Site ID", "Full eNodeB Name"],
        horizontal=True,
    )

with top_date_col:
    st.caption("Search can use either Site ID (e.g. SUM-JA-MBN-0728) or the full eNodeB Name. This is independent from the Problem Cell selector below.")

    st.markdown("**Date Range**")

    # Date limits are calculated from the uploaded dataset first.
    all_valid_dates = df["_Date"].dropna()

    if not all_valid_dates.empty:
        global_min_date = all_valid_dates.min().date()
        global_max_date = all_valid_dates.max().date()

        date_range = st.date_input(
            "Date range",
            value=(global_min_date, global_max_date),
            min_value=global_min_date,
            max_value=global_max_date,
            label_visibility="collapsed",
        )
    else:
        date_range = ()

if search_mode == "Site ID":
    if site_input:
        # Robust Site ID matching:
        # 1) exact match against extracted SUM site ID
        # 2) fallback to searching the original eNodeB Name
        #    in case the CSV uses a slightly different naming pattern.
        raw_site_input = site_input.strip()

        # Users sometimes paste the full eNodeB Name while
        # "Site ID" mode is selected, e.g.
        # 426D531E_LTE_BLOCKCRIMBOILIR#SUM-JA-MRT-0195#NR
        # In that case extract the SUM site ID automatically.
        site_match = re.search(r"(SUM-[^#\\s]+)", raw_site_input, flags=re.IGNORECASE)
        site_key = (
            site_match.group(1).strip().upper()
            if site_match
            else raw_site_input.upper()
        )

        site_df = df[
            df["_Site_ID_Search"].eq(site_key)
        ].copy()

        if site_df.empty:
            site_df = df[
                df["_eNodeB_Search"]
                .str.upper()
                .str.contains(
                    re.escape(site_key),
                    na=False,
                    regex=True,
                )
            ].copy()

        # Final fallback: search the extracted SUM token directly in the
        # raw eNodeB column. This handles CSVs whose internal Site ID
        # extraction differs from the dashboard parser.
        if site_df.empty and site_match:
            site_df = df[
                df[enodeb_col]
                .fillna("")
                .astype(str)
                .str.upper()
                .str.contains(
                    re.escape(site_key),
                    na=False,
                    regex=True,
                )
            ].copy()
    else:
        site_df = df.iloc[0:0].copy()
else:
    if site_input:
        # Full eNodeB Name: allow exact match first, then
        # case-insensitive contains for easier searching.
        enodeb_key = site_input.strip()

        site_df = df[
            df["_eNodeB_Search"].eq(enodeb_key)
        ].copy()

        if site_df.empty:
            site_df = df[
                df["_eNodeB_Search"]
                .str.contains(
                    re.escape(enodeb_key),
                    case=False,
                    na=False,
                    regex=True,
                )
            ].copy()
    else:
        site_df = df.iloc[0:0].copy()

if not site_input:
    st.info(
        "Enter a Site ID or Full eNodeB Name "
        "in the top Site Search control."
    )
    st.stop()

if site_df.empty:
    st.warning(
        "No matching site/eNodeB Name found."
    )
    st.stop()

# ============================================================
# DATE FILTER
# ============================================================
if (
    isinstance(date_range, tuple)
    and len(date_range) == 2
):
    start_date, end_date = date_range

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    site_df = site_df[
        (
            site_df["_Date_Day"] >= start_ts
        )
        & (
            site_df["_Date_Day"] <= end_ts
        )
    ].copy()

# ============================================================
# DATA GRANULARITY
# ============================================================
# Daily CSV keeps the original dashboard behavior.
# Hourly CSV preserves Date + Time in the charts automatically.
is_hourly = bool(
    not site_df.empty
    and site_df["_Is_Hourly"].any()
)

if is_hourly:
    st.sidebar.caption("Data mode: Hourly")

sector_values = (
    site_df["_Sector_Display"]
    .dropna()
    .astype(str)
    .str.strip()
)

sector_values = [
    value
    for value in sector_values.unique()
    if value
]

preferred_order = [
    "S1",
    "S2",
    "S3",
    "S4",
    "Indoor",
]

ordered_sectors = [
    value
    for value in preferred_order
    if value in sector_values
]

ordered_sectors += sorted(
    value
    for value in sector_values
    if value not in ordered_sectors
)

if ordered_sectors:

    # Selection is rendered in the top Chart Layout control area below.
    selected_sectors = ordered_sectors.copy()

else:

    selected_sectors = []

    st.sidebar.warning(
        "No Sector mapping found for this site."
    )

# ============================================================
# SIDEBAR — FREQBAND FILTER
# ============================================================
band_values = (
    site_df["_FreqBand"]
    .dropna()
    .astype(str)
    .str.strip()
)
band_values = [
    value
    for value in band_values.unique()
    if value
]

# Selection is rendered in the top Chart Layout control area below.
selected_bands = sorted(band_values)

# ============================================================
# SIDEBAR — KPI
# ============================================================
st.sidebar.header("3. KPI")

if missing_kpis:
    st.sidebar.warning(
        f"{len(missing_kpis)} KPI column(s) not found in this CSV."
    )

    with st.sidebar.expander(
        "View missing KPI columns"
    ):
        for kpi_name in missing_kpis:
            st.write(
                f"- {kpi_name}: "
                f"`{KPI_CONFIG[kpi_name]['column']}`"
            )

# KPI selection is grouped only for easier menu navigation.
# Charts remain independent: one KPI + one Sector = one chart.
category_order = [
    "Accessibility",
    "Retainability",
    "Mobility",
    "Utilization",
    "Traffic",
    "Radio/Coverage",
    "Spectrum",
    "VoLTE",
    "Failure Counters",
]

selected_kpis = []

for category in category_order:
    category_kpis = [
        kpi
        for kpi in available_kpis
        if KPI_CONFIG[kpi]["category"] == category
    ]

    if not category_kpis:
        continue

    selected = st.sidebar.multiselect(
        category,
        category_kpis,
        default=(
            ["SSSR"]
            if category == "Accessibility"
            and "SSSR" in category_kpis
            else []
        ),
        key=f"kpi_{category}",
    )

    selected_kpis.extend(selected)

# ============================================================
# SITE SUMMARY
# ============================================================
st.subheader(
    f"Site: {site_key if search_mode == 'Site ID' and site_input else site_input}"
)

col1, col2, col3, col4 = st.columns(4)

col1.metric(
    "Rows",
    f"{len(site_df):,}",
)

col2.metric(
    "Cells",
    f"{site_df[cell_col].nunique():,}",
)

col3.metric(
    "FreqBands",
    f"{site_df['_FreqBand'].nunique():,}",
)

col4.metric(
    "Sectors",
    f"{site_df['_Sector_Display'].nunique():,}",
)

# ============================================================
# ENODEB INFORMATION
# ============================================================
with st.expander(
    "Matched eNodeB Name"
):

    for name in (
        site_df[enodeb_col]
        .dropna()
        .astype(str)
        .unique()
    ):
        st.code(name)

# ============================================================
# CELL MAPPING PREVIEW
# ============================================================
with st.expander(
    "Cell / Sector / FreqBand Mapping"
):

    mapping_preview = (
        site_df[
            [
                cell_col,
                localcell_col,
                "_Sector_Display",
                "_FreqBand",
            ]
        ]
        .drop_duplicates()
        .sort_values(
            [
                "_Sector_Display",
                "_FreqBand",
                cell_col,
            ]
        )
    )

    st.dataframe(
        mapping_preview,
        use_container_width=True,
    )

# ============================================================
# 4. QUICK KPI
# ============================================================
#
# Shortcut menu for the most frequently checked RAN KPIs.
# Selecting a KPI here uses the same chart engine as Section 3.
# ============================================================

QUICK_KPI = [
    "Payload",
    "4G Cell Availability",
    "HX4 DL PRB Utilization",
    "HX4 UL PRB Utilization",
    "DL User Throughput",
    "UL User Throughput",
    "Average TA",
    "TA Distribution",
    "Latency",
    "Last TTI Ratio",
    "Total Payload Sector",
    "Number of RRC Connected User",
    "RANK2 Rate",
    "CQI",
    "UL Interference",
    "HOSR Intra",
    "HOSR Inter",

    # Remaining KPI 1 items
    "SSSR",
    "RRC Setup SR",
    "E-RAB Setup SR",
    "DL Payload",
    "DL Spectrum Efficiency",
    "E-RAB Setup SR VoIP",
]

quick_kpis = st.sidebar.multiselect(
    "Quick KPI",
    [
        kpi
        for kpi in QUICK_KPI
        if kpi in KPI_CONFIG
        and kpi in kpi_actual_columns
    ],
    default=[],
    key="quick_kpi_selection",
)

# Add Quick KPI selections to the normal KPI selection.
# dict.fromkeys() removes duplicates while preserving order.
selected_kpis = list(
    dict.fromkeys(
        selected_kpis + quick_kpis
    )
)

if not selected_kpis and chart_layout not in {
    "KPI Analysis",
    "KPI Status Transition",
}:
    st.info("Select at least one KPI.")
    st.stop()

# ============================================================
# 5. CHART LAYOUT + CHART FILTERS
# ============================================================
# One independent layout selector:
#   Horizontal | Vertical | 2 Charts | KPI Analysis
#
# KPI Analysis is a FOURTH layout option. It is NOT inside 2 Charts
# and does not depend on the 2 Charts rendering logic.
st.markdown("### Chart Controls")

layout_col, sector_col, band_col = st.columns(
    [2.5, 1.5, 1.5],
    gap="small",
)

with layout_col:
    chart_layout = st.radio(
        "Chart Layout",
        [
            "Horizontal",
            "Vertical",
            "2 Charts",
            "KPI Analysis",
            "KPI Status Transition",
        ],
        index=0,
        horizontal=True,
        help=(
            "Choose one independent dashboard layout. "
            "The existing four layouts remain unchanged; "
            "KPI Status Transition is an additional comparison layout."
        ),
        key="chart_layout_selector",
    )

with sector_col:
    selected_sectors = st.multiselect(
        "Select Sector",
        ordered_sectors,
        default=ordered_sectors,
    )

with band_col:
    selected_bands = st.multiselect(
        "Select FreqBand",
        sorted(band_values),
        default=sorted(band_values),
        help="Choose which frequency bands are included in the charts.",
    )

# ============================================================
# GLOBAL BULK CELL + SITE + FREQBAND TARGET FILTER
# ============================================================
# If the user has already entered the exact Bulk Cell + Site + FreqBand
# list in KPI Analysis, make that list the source of truth for ALL chart
# layouts — Horizontal, Vertical, 2 Charts, and KPI Analysis.
#
# This fixes the previous behavior where the regular charts could still
# show every cell belonging to the selected site even though the user
# had supplied an exact Cell + Site + FreqBand target list.
#
# The input order can be any of:
#   FreqBand | Cell | Site
#   Site | FreqBand | Cell
#   Cell | FreqBand | Site
#
# The session-state value is used because the Bulk text area itself is
# rendered later inside KPI Analysis.
# ============================================================
global_bulk_text = st.session_state.get(
    "custom_kpi_analysis_bulk_cell_site_list",
    "",
)

global_bulk_pairs = []
global_bulk_cell_order = []

if isinstance(global_bulk_text, str) and global_bulk_text.strip():

    global_available_cells = set(
        site_df["_Cell_Display"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    global_available_sites = set(
        site_df["_Site_ID_Search"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    global_available_enodebs = set(
        site_df["_eNodeB_Search"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    global_available_bands = set(
        site_df["_FreqBand"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .map(
            lambda value: (
                value[1:]
                if value.startswith("L")
                else value
            )
        )
        .unique()
    )

    def _global_normalize_band(value):
        value = str(value).strip().upper()
        return value[1:] if value.startswith("L") else value

    for raw_line in global_bulk_text.splitlines():

        line = raw_line.strip()

        if not line:
            continue

        fields = [
            field.strip()
            for field in re.split(
                r"\t|\||;",
                line,
            )
            if field.strip()
        ]

        if len(fields) < 3:
            continue

        cell_value = None
        site_value = None
        band_value = None

        # Identify Cell Name.
        for field in fields:
            field_upper = field.upper()

            if field_upper in global_available_cells:
                cell_value = field_upper
                break

        # Identify Site ID / eNodeB.
        for field in fields:
            field_upper = field.upper()

            if field_upper in global_available_sites:
                site_value = field_upper
                break

            if field_upper in global_available_enodebs:
                site_value = field_upper
                break

        # Identify FreqBand.
        for field in fields:
            normalized_band = _global_normalize_band(field)

            if normalized_band in global_available_bands:
                band_value = normalized_band
                break

        if cell_value and site_value and band_value:
            target = (
                cell_value,
                site_value,
                band_value,
            )

            if target not in global_bulk_pairs:
                global_bulk_pairs.append(target)

            if cell_value not in global_bulk_cell_order:
                global_bulk_cell_order.append(cell_value)

if global_bulk_pairs:

    global_pair_mask = pd.Series(
        False,
        index=site_df.index,
    )

    for (
        target_cell,
        target_site,
        target_band,
    ) in global_bulk_pairs:

        cell_mask = (
            site_df["_Cell_Display"]
            .astype(str)
            .str.upper()
            .eq(target_cell)
        )

        if target_site.startswith("SUM-"):
            site_mask = (
                site_df["_Site_ID_Search"]
                .astype(str)
                .str.upper()
                .eq(target_site)
            )
        else:
            site_mask = (
                site_df["_eNodeB_Search"]
                .astype(str)
                .str.upper()
                .eq(target_site)
            )

        band_mask = (
            site_df["_FreqBand"]
            .astype(str)
            .str.upper()
            .map(_global_normalize_band)
            .eq(target_band)
        )

        global_pair_mask = (
            global_pair_mask
            | (
                cell_mask
                & site_mask
                & band_mask
            )
        )

    site_df = site_df[
        global_pair_mask
    ].copy()

    st.caption(
        "🎯 Exact Bulk Target Mode active: "
        f"{len(global_bulk_pairs):,} Cell + Site + FreqBand target(s). "
        "Charts show only the cells entered in the Bulk list."
    )

# Keep the date/site-filtered data for the Site Level Summary used by
# non-KPI-Analysis layouts. This is captured BEFORE Sector/FreqBand
# chart filters so the summary represents the complete selected site.
site_level_df = site_df.copy()

# Apply both chart filters before KPI processing.
if selected_sectors:
    site_df = site_df[
        site_df["_Sector_Search"].isin(selected_sectors)
    ].copy()
else:
    site_df = site_df.iloc[0:0].copy()

if selected_bands:
    site_df = site_df[
        site_df["_FreqBand"]
        .astype(str)
        .str.strip()
        .isin(selected_bands)
    ].copy()
else:
    site_df = site_df.iloc[0:0].copy()

# ============================================================
# SITE-LEVEL PAYLOAD
# ============================================================
#
# One additional chart for total site payload.
# All cells / sectors / frequency bands are summed by Date.
#
# Example:
#   S1 L1800 + S1 L2100 + S2 L1800 + S3 L900 ...
#   -> one total Site Payload trend
#
# This is independent from the sector-level Payload charts below.
# ============================================================

if chart_layout != "KPI Analysis" and ("Payload" in selected_kpis or "Total Payload Sector" in selected_kpis):

    payload_col = kpi_actual_columns["Payload"]

    site_payload_df = site_df[
        [
            "_Date",
            "_Date_Day",
            payload_col,
        ]
    ].copy()

    site_payload_df["_Payload_Value"] = parse_kpi_numeric(
        site_payload_df[payload_col]
    )

    site_payload_df["_Chart_Date"] = (
        site_payload_df["_Date"]
        if is_hourly
        else site_payload_df["_Date_Day"]
    )

    site_payload_df = site_payload_df.dropna(
        subset=[
            "_Chart_Date",
            "_Payload_Value",
        ]
    )

    if not site_payload_df.empty:

        site_payload_df = (
            site_payload_df
            .groupby(
                "_Chart_Date",
                as_index=False,
            )["_Payload_Value"]
            .sum()
            .sort_values("_Chart_Date")
        )

        site_payload_fig = px.area(
            site_payload_df,
            x="_Chart_Date",
            y="_Payload_Value",
            markers=False,
        )

        site_payload_fig.update_layout(
            title="Payload — Site Level",
            yaxis=dict(
                title="",
            ),
            xaxis=dict(
                title="Date",
                tickformat=(
                    "%b %d<br>%H:%M"
                    if is_hourly
                    else "%b %d"
                ),
                hoverformat=(
                    "%b %d, %Y %H:%M"
                    if is_hourly
                    else "%b %d, %Y"
                ),
            ),
            hovermode="x unified",
            height=430,
            margin=dict(
                l=40,
                r=30,
                t=65,
                b=55,
            ),
        )

        site_payload_fig.update_traces(
            hovertemplate=(
                "<b>Site Total Payload</b><br>"
                + (
                    "%{x|%b %d, %Y %H:%M}<br>"
                    if is_hourly
                    else "%{x|%b %d, %Y}<br>"
                )
                + "Payload: %{y:.2f} GB"
                "<extra></extra>"
            )
        )

        show_chart(
            site_payload_fig,
            use_container_width=True,
        )

        # ========================================================
        # BAND-LEVEL PAYLOAD
        # ========================================================
        #
        # One chart containing the total Payload of each band:
        # L900 | L1800 | L2100 | L850 | L2300 | L700
        #
        # All sectors/cells belonging to the same band are summed
        # by Date.
        # ========================================================

        band_order = [
            "900",
            "1800",
            "2100",
            "850",
            "2300",
            "700",
        ]

        band_labels = {
            "900": "L900",
            "1800": "L1800",
            "2100": "L2100",
            "850": "L850",
            "2300": "L2300",
            "700": "L700",
        }

        # Use the complete site-level dataset for Band Level so the
        # summary can show every band present at the selected site,
        # even when the Sector/FreqBand chart filters are narrowed.
        band_payload_source = site_level_df[
            [
                "_Date",
                "_Date_Day",
                "_FreqBand",
                payload_col,
            ]
        ].copy()

        band_payload_source["_Payload_Value"] = parse_kpi_numeric(
            band_payload_source[payload_col]
        )

        band_payload_source["_Chart_Date"] = (
            band_payload_source["_Date"]
            if is_hourly
            else band_payload_source["_Date_Day"]
        )

        # FreqBand is already produced by the internal master
        # mapping. Normalize it so values such as 1800 / L1800
        # can be handled consistently.
        band_payload_source["_Band_Key"] = (
            band_payload_source["_FreqBand"]
            .astype(str)
            .str.strip()
            .str.upper()
            .str.replace("L", "", regex=False)
        )

        # 2300 is represented in the master mapping as 2300F1 / 2300F2.
        # For Band Level Payload they are intentionally combined into one
        # logical band: L2300.
        band_payload_source["_Band_Key"] = (
            band_payload_source["_Band_Key"]
            .replace({
                "2300F1": "2300",
                "2300F2": "2300",
            })
        )

        band_payload_source = band_payload_source.dropna(
            subset=[
                "_Chart_Date",
                "_Payload_Value",
            ]
        )

        band_payload_source = band_payload_source[
            band_payload_source["_Band_Key"].isin(band_order)
        ]

        if not band_payload_source.empty:

            band_payload_df = (
                band_payload_source
                .groupby(
                    [
                        "_Chart_Date",
                        "_Band_Key",
                    ],
                    as_index=False,
                )["_Payload_Value"]
                .sum()
            )

            band_payload_df["_Band"] = (
                band_payload_df["_Band_Key"]
                .map(band_labels)
            )

            band_payload_df = (
                band_payload_df
                .sort_values(
                    [
                        "_Chart_Date",
                        "_Band_Key",
                    ]
                )
            )

            band_payload_fig = px.line(
                band_payload_df,
                x="_Chart_Date",
                y="_Payload_Value",
                color="_Band",
                category_orders={
                    "_Band": [
                        band_labels[b]
                        for b in band_order
                    ]
                },
                markers=False,
            )

            band_payload_fig.update_layout(
                title="Payload — Band Level",
                yaxis=dict(
                    title="",
                ),
                xaxis=dict(
                    title="Date",
                    tickformat=("%b %d<br>%H:%M" if is_hourly else "%b %d"),
                    hoverformat=("%b %d, %Y %H:%M" if is_hourly else "%b %d, %Y"),
                ),
                legend=dict(
                    title=dict(
                        text="Band",
                        font=dict(
                            size=11,
                            family="Arial Bold",
                        ),
                    ),
                    font=dict(
                        size=10,
                        family="Arial Bold",
                    ),
                    orientation="h",
                    yanchor="top",
                    y=-0.22,
                    xanchor="center",
                    x=0.5,
                ),
                hovermode="x unified",
                height=430,
                margin=dict(
                    l=40,
                    r=30,
                    t=65,
                    b=90,
                ),
            )

            band_payload_fig.update_traces(
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "%{x|%b %d, %Y}<br>"
                    "Payload: %{y:.2f} GB"
                    "<extra></extra>"
                )
            )

            show_chart(
                band_payload_fig,
                use_container_width=True,
            )

# ============================================================
# TIGHT CHART ROW SPACING
# ============================================================
# Reduce only the horizontal gap between rows that contain Plotly charts.
# This keeps the controls/sidebar layout unchanged.
st.markdown(
    """
    <style>
    div[data-testid="stHorizontalBlock"]:has(.js-plotly-plot) {
        gap: 0rem !important;
    }
    div[data-testid="stHorizontalBlock"]:has(.js-plotly-plot) > div[data-testid="stColumn"] {
        padding-left: 0rem !important;
        padding-right: 0rem !important;
        min-width: 0 !important;
    }
    div[data-testid="stPlotlyChart"] {
        width: 100% !important;
        max-width: none !important;
        margin-left: 0 !important;
        margin-right: 0 !important;
        padding-left: 0 !important;
        padding-right: 0 !important;
    }
    div[data-testid="stPlotlyChart"] > div {
        width: 100% !important;
        max-width: none !important;
        margin-left: 0 !important;
        margin-right: 0 !important;
        padding-left: 0 !important;
        padding-right: 0 !important;
    }
    div[data-testid="stPlotlyChart"] iframe {
        width: 100% !important;
        max-width: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================
# CHARTS
# ============================================================
#
# 1 KPI + 1 Sector = 1 independent chart.
#
# Horizontal:
#   S1 | S2 | S3
#   S4 | Indoor ...
#
# Vertical:
#   S1
#   S2
#   S3
#   S4
#
# All FreqBands inside the same Sector remain in that chart.
# Legend = FULL Cell Name.
# Payload = stacked area chart.
# Other KPIs = line chart.
# ============================================================

two_chart_index = 0
two_chart_cols = None

# KPI Analysis is its own layout, so the normal KPI chart renderer is
# completely bypassed when that layout is selected.
main_chart_kpis = (
    selected_kpis
    if chart_layout != "KPI Analysis"
    else []
)

for kpi_name in main_chart_kpis:

    if chart_layout == "2 Charts":
        if two_chart_index % 2 == 0:
            two_chart_cols = st.columns(2, gap=None)
        current_two_chart_col = two_chart_cols[two_chart_index % 2]
        two_chart_index += 1
    else:
        current_two_chart_col = None

    if kpi_name == "Total Payload Sector":
        continue

    config = KPI_CONFIG[kpi_name]
    # ========================================================
    # TA DISTRIBUTION
    # ========================================================
    if config.get("chart") == "ta_distribution":

        # In Vertical layout, TA Distribution is rendered once at the very bottom
        # with date/cell selectors, a UE-attempt bar chart, CDF on a secondary axis,
        # and a matching table. Other layouts retain the original TA chart.
        if chart_layout == "Vertical":
            continue

        ta_cols = [
            ta_distribution_col_map[i]
            for i in range(12)
            if i in ta_distribution_col_map
            and ta_distribution_col_map[i] in site_df.columns
        ]

        if not ta_cols:
            st.warning("TA Distribution columns were not found in the uploaded CSV.")
            continue

        ta_ranges = [
            "0-156 m",
            "156-234 m",
            "234-546 m",
            "546-1014 m",
            "1014-1950 m",
            "1950-3510 m",
            "3510-6630 m",
            "6630-14430 m",
            "14430-30030 m",
            "30030-53430 m",
            "53430-76830 m",
            "76830 m+",
        ]

        for sector in selected_sectors:
            sector_df = site_df[
                site_df["_Sector_Search"] == sector
            ].copy()

            if sector_df.empty:
                continue

            rows = []
            for idx, col in enumerate(ta_cols):
                values = parse_kpi_numeric(
                    sector_df[col]
                ).fillna(0)

                grouped = (
                    pd.DataFrame({
                        "_Cell_Display": sector_df["_Cell_Display"].apply(
                            normalize_cell_name
                        ),
                        "_Value": values,
                    })
                    .groupby(
                        "_Cell_Display",
                        as_index=False,
                        dropna=False,
                    )["_Value"]
                    .sum()
                )
                grouped["_TA_Range"] = ta_ranges[idx]
                rows.append(grouped)

            ta_df = pd.concat(rows, ignore_index=True)
            ta_df["_TA_Range"] = pd.Categorical(
                ta_df["_TA_Range"],
                categories=ta_ranges,
                ordered=True,
            )

            fig = px.bar(
                ta_df,
                x="_TA_Range",
                y="_Value",
                color="_Cell_Display",
                barmode="group",
                category_orders={"_TA_Range": ta_ranges},
            )

            fig.update_layout(
                title=f"TA Distribution — {sector}",
                xaxis=dict(title="TA Distance", tickangle=-35),
                yaxis=dict(title="UE Count"),
                height=450,
                margin=dict(l=25, r=25, t=60, b=115),
                legend=dict(
                    title=dict(
                        text="Cell Name",
                        font=dict(size=12, family="Arial Black"),
                    ),
                    font=dict(size=11, family="Arial Black"),
                    orientation="h",
                    yanchor="top",
                    y=-0.25,
                    xanchor="center",
                    x=0.5,
                ),
            )

            fig.update_traces(
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>"
                    "TA: %{x}<br>"
                    "UE: %{y:,.0f}<extra></extra>"
                )
            )

            show_chart(fig, use_container_width=True)

        continue

    kpi_col = kpi_actual_columns[kpi_name]

    work = site_df[
        [
            "_Date",
            "_Cell_Display",
            "_Sector_Search",
            "_Sector_Display",
            kpi_col,
        ]
    ].copy()

    work["_KPI_Value"] = parse_kpi_numeric(
        work[kpi_col]
    )

    work = work.dropna(subset=["_KPI_Value"])

    if work.empty:
        st.warning(
            f"No numeric data available for {kpi_name}."
        )
        continue

    chart_sectors = [
        sector
        for sector in selected_sectors
        if sector in work["_Sector_Search"].unique()

    ]

    if not chart_sectors:
        continue

    def render_combined_chart(plot_df):
        """Render one KPI chart containing all selected sectors/bands."""
        plot_df = plot_df.copy()

        if is_hourly:
            plot_df["_Chart_Date"] = plot_df["_Date"]
        else:
            plot_df["_Chart_Date"] = plot_df["_Date"].dt.normalize()

        plot_df = (
            plot_df
            .groupby(
                ["_Chart_Date", "_Cell_Display"],
                as_index=False,
                dropna=False,
            )["_KPI_Value"]
            .mean()
            .sort_values(
                ["_Chart_Date", "_Cell_Display"]
            )
        )

        # Final safety guard: one normalized Cell Name = one series.
        plot_df["_Cell_Display"] = (
            plot_df["_Cell_Display"]
            .map(normalize_cell_name)
        )

        plot_df = (
            plot_df
            .groupby(
                ["_Chart_Date", "_Cell_Display"],
                as_index=False,
                dropna=False,
            )["_KPI_Value"]
            .mean()
            .sort_values(
                ["_Chart_Date", "_Cell_Display"]
            )
        )

        # Preserve the existing hourly outage behavior.
        if is_hourly and not plot_df.empty:
            hourly_parts = []

            for cell_name, cell_df in plot_df.groupby(
                "_Cell_Display",
                sort=False,
            ):
                cell_df = (
                    cell_df
                    .set_index("_Chart_Date")
                    .sort_index()
                )

                full_hours = pd.date_range(
                    start=cell_df.index.min(),
                    end=cell_df.index.max(),
                    freq="1h",
                )

                cell_df = cell_df.reindex(full_hours)
                cell_df["_Cell_Display"] = cell_name
                cell_df["_KPI_Value"] = (
                    cell_df["_KPI_Value"]
                    .fillna(0)
                )
                cell_df.index.name = "_Chart_Date"
                hourly_parts.append(cell_df.reset_index())

            if hourly_parts:
                plot_df = pd.concat(
                    hourly_parts,
                    ignore_index=True,
                ).sort_values(
                    ["_Chart_Date", "_Cell_Display"]
                )

        if (
            kpi_name == "Payload"
            or config.get("chart") == "stacked"
        ):
            fig = px.area(
                plot_df,
                x="_Chart_Date",
                y="_KPI_Value",
                color="_Cell_Display",
                category_orders=(
                    {"_Cell_Display": global_bulk_cell_order}
                    if global_bulk_cell_order
                    else None
                ),
                markers=False,
            )
        else:
            fig = px.line(
                plot_df,
                x="_Chart_Date",
                y="_KPI_Value",
                color="_Cell_Display",
                category_orders=(
                    {"_Cell_Display": global_bulk_cell_order}
                    if global_bulk_cell_order
                    else None
                ),
                markers=True,
            )

        # Excel-like time axis is preserved.
        if is_hourly and not plot_df.empty:
            tick_start = plot_df["_Chart_Date"].min().floor("4h")
            tick_end = plot_df["_Chart_Date"].max().ceil("4h")
            hourly_ticks = pd.date_range(
                start=tick_start,
                end=tick_end,
                freq="4h",
            )
            hourly_tick_text = [
                (
                    f"{tick:%H:%M}<br>{tick:%Y-%m-%d}"
                    if tick.hour == 0
                    else f"{tick:%H:%M}<br>&nbsp;"
                )
                for tick in hourly_ticks
            ]
        else:
            hourly_ticks = None
            hourly_tick_text = None

        fig.update_layout(
            title=f"{kpi_name}",
            yaxis=dict(title=""),
            xaxis=dict(
                title=("Date / Time" if is_hourly else "Date"),
                tickmode=("array" if is_hourly else "auto"),
                tickvals=hourly_ticks if is_hourly else None,
                ticktext=hourly_tick_text if is_hourly else None,
                tickangle=0,
                tickfont=dict(size=9),
                hoverformat=(
                    "%Y-%m-%d %H:%M"
                    if is_hourly
                    else "%b %d, %Y"
                ),
            ),
            hovermode="x unified",
            height=470,
            margin=dict(l=35, r=20, t=90, b=75),
            legend=dict(
                title=dict(
                    text="Cell Name",
                    font=dict(
                        size=12,
                        family="Arial Black",
                    ),
                ),
                font=dict(
                    size=10,
                    family="Arial Black",
                ),
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            ),
        )

        fig.update_traces(
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                + (
                    "%{x|%b %d, %Y %H:%M}<br>"
                    if is_hourly
                    else "%{x|%b %d, %Y}<br>"
                )
                + f"{kpi_name}: "
                "%{y:.2f}"
                f" {config['unit']}"
                "<extra></extra>"
            )
        )

        show_chart(
            fig,
            use_container_width=True,
        )

    def render_sector_chart(plot_df, sector):
        plot_df = plot_df.copy()

        # Daily CSV keeps the original daily trend.
        # Hourly CSV preserves the Date + Time timestamp.
        if is_hourly:
            plot_df["_Chart_Date"] = plot_df["_Date"]
        else:
            plot_df["_Chart_Date"] = plot_df["_Date"].dt.normalize()

        plot_df = (
            plot_df
            .groupby(
                ["_Chart_Date", "_Cell_Display"],
                as_index=False,
                dropna=False,
            )["_KPI_Value"]
            .mean()
            .sort_values(
                ["_Chart_Date", "_Cell_Display"]
            )
        )

        # Final safety guard: one normalized Cell Name = one series.
        plot_df["_Cell_Display"] = (
            plot_df["_Cell_Display"]
            .map(normalize_cell_name)
        )

        plot_df = (
            plot_df
            .groupby(
                ["_Chart_Date", "_Cell_Display"],
                as_index=False,
                dropna=False,
            )["_KPI_Value"]
            .mean()
            .sort_values(
                ["_Chart_Date", "_Cell_Display"]
            )
        )

        # --------------------------------------------------------
        # Hourly outage handling:
        # If an hourly record is missing between two available
        # timestamps for the same Cell Name, treat the missing
        # hour as 0. This makes a site/cell outage visible as a
        # drop to zero instead of drawing a misleading diagonal
        # line from the last available hour to the next one.
        #
        # Daily CSV behavior is unchanged.
        # --------------------------------------------------------
        if is_hourly and not plot_df.empty:
            hourly_parts = []

            for cell_name, cell_df in plot_df.groupby(
                "_Cell_Display",
                sort=False,
            ):
                cell_df = (
                    cell_df
                    .set_index("_Chart_Date")
                    .sort_index()
                )

                # Keep the cell's own active time range so we do
                # not create artificial zeros before/after it exists.
                full_hours = pd.date_range(
                    start=cell_df.index.min(),
                    end=cell_df.index.max(),
                    freq="1h",
                )

                cell_df = cell_df.reindex(full_hours)
                cell_df["_Cell_Display"] = cell_name

                # Missing hourly observations = outage / no data.
                cell_df["_KPI_Value"] = (
                    cell_df["_KPI_Value"]
                    .fillna(0)
                )

                cell_df.index.name = "_Chart_Date"
                hourly_parts.append(
                    cell_df.reset_index()
                )

            plot_df = pd.concat(
                hourly_parts,
                ignore_index=True,
            ).sort_values(
                ["_Chart_Date", "_Cell_Display"]
            )

        if (
            kpi_name == "Payload"
            or config.get("chart") == "stacked"
        ):
            fig = px.area(
                plot_df,
                x="_Chart_Date",
                y="_KPI_Value",
                color="_Cell_Display",
                category_orders=(
                    {"_Cell_Display": global_bulk_cell_order}
                    if global_bulk_cell_order
                    else None
                ),
                markers=False,
            )
        else:
            fig = px.line(
                plot_df,
                x="_Chart_Date",
                y="_KPI_Value",
                color="_Cell_Display",
                category_orders=(
                    {"_Cell_Display": global_bulk_cell_order}
                    if global_bulk_cell_order
                    else None
                ),
                markers=True,
            )

        if chart_layout == "Vertical":
            chart_height = 520
            margins = dict(l=30, r=30, t=70, b=30)
            font_size = 10
            legend_settings = dict(
                title=dict(
                    text="Cell Name",
                    font=dict(
                        size=12,
                        family="Arial Black",
                    ),
                ),
                font=dict(
                    size=11,
                    family="Arial Black",
                ),
            )
        else:
            chart_height = 450
            margins = dict(l=8, r=15, t=90, b=75)
            font_size = 9
            legend_settings = dict(
                title=dict(
                    text="Cell Name",
                    font=dict(
                        size=12,
                        family="Arial Black",
                    ),
                ),
                font=dict(
                    size=11,
                    family="Arial Black",
                ),
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            )

        # Excel-style hourly axis:
        # hour on the first line, date underneath.
        # Date is shown at 00:00 only to avoid the dense/overlapping
        # date labels seen when every hourly tick carries a full date.
        if is_hourly:
            tick_start = plot_df["_Chart_Date"].min().floor("4h")
            tick_end = plot_df["_Chart_Date"].max().ceil("4h")

            hourly_ticks = pd.date_range(
                start=tick_start,
                end=tick_end,
                freq="4h",
            )

            hourly_tick_text = [
                (
                    f"{tick:%H:%M}<br>{tick:%Y-%m-%d}"
                    if tick.hour == 0
                    else f"{tick:%H:%M}<br>&nbsp;"
                )
                for tick in hourly_ticks
            ]
        else:
            hourly_ticks = None
            hourly_tick_text = None

        fig.update_layout(
            title=f"{kpi_name} — {sector}",
            yaxis=dict(
                title="",
            ),
            xaxis=dict(
                title=("Date / Time" if is_hourly else "Date"),
                tickmode=("array" if is_hourly else "auto"),
                tickvals=(
                    hourly_ticks
                    if is_hourly
                    else None
                ),
                ticktext=(
                    hourly_tick_text
                    if is_hourly
                    else None
                ),
                tickangle=0,
                tickfont=dict(size=9),
                hoverformat=(
                    "%Y-%m-%d %H:%M"
                    if is_hourly
                    else "%b %d, %Y"
                ),
            ),
            hovermode="x unified",
            height=chart_height,
            margin=margins,
            font=dict(size=font_size),
            legend=legend_settings,
        )

        fig.update_traces(
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                + (
                    "%{x|%b %d, %Y %H:%M}<br>"
                    if is_hourly
                    else "%{x|%b %d, %Y}<br>"
                )
                + f"{kpi_name}: "
                "%{y:.2f}"
                f" {config['unit']}"
                "<extra></extra>"
            )
        )

        show_chart(
            fig,
            use_container_width=True,
        )

    if chart_layout == "2 Charts":
        # One KPI chart can contain multiple selected sectors and bands.
        # Two KPI charts are displayed side-by-side per row.
        if not work.empty:
            if current_two_chart_col is not None:
                with current_two_chart_col:
                    render_combined_chart(work)
            else:
                render_combined_chart(work)

    elif chart_layout == "Vertical":

        for sector in chart_sectors:

            plot_df = work[
                work["_Sector_Search"]
                == sector
            ].copy()

            if not plot_df.empty:
                render_sector_chart(
                    plot_df,
                    sector,
                )

    elif chart_layout == "Horizontal":

        charts_per_row = 3

        for row_start in range(
            0,
            len(chart_sectors),
            charts_per_row,
        ):

            row_sectors = chart_sectors[
                row_start:row_start + charts_per_row
            ]

            cols = st.columns(len(row_sectors), gap=None)

            for col, sector in zip(
                cols,
                row_sectors,
            ):

                with col:

                    plot_df = work[
                        work["_Sector_Display"]
                        .astype(str)
                        .str.strip()
                        == sector
                    ].copy()

                    if not plot_df.empty:
                        render_sector_chart(
                            plot_df,
                            sector,
                        )


# ============================================================
# KPI STATUS TRANSITION — ADDITIONAL LAYOUT
# ============================================================
#
# Purpose:
#   Compare two specific dates for an exact list of problematic
#   Cell + Site + FreqBand targets and identify:
#
#       Not Meet on Date A  ->  Meet on Date B
#
# This is intentionally an ADDITIONAL layout. The existing:
#   Horizontal / Vertical / 2 Charts / KPI Analysis
# rendering paths are not modified by this section.
# ============================================================

def render_kpi_status_transition():
    st.markdown("### 🔄 KPI Status Transition")
    st.caption(
        "Compare KPI status between two dates. "
        "The main result is **Not Meet → Meet** for the selected "
        "Cell + Site + FreqBand targets."
    )

    # ------------------------------------------------------------
    # Exact target input
    # ------------------------------------------------------------
    st.markdown("#### 🎯 Problematic Cell Target List")

    transition_bulk_text = st.text_area(
        "Paste FreqBand + Cell Name + Site ID",
        placeholder=(
            "850\\tJB4G85_4264592E85_132\\tSUM-JA-MBN-0143\n"
            "850\\tJB4G85_4264509E85_132\\tSUM-JA-MBN-0141\n"
            "850\\tJB4G85_4264509E85_133\\tSUM-JA-MBN-0141"
        ),
        height=180,
        key="kpi_status_transition_bulk_text",
        help=(
            "Paste 3 columns from Excel in any order: "
            "FreqBand, Cell Name, Site ID/eNodeB Name."
        ),
    )

    # IMPORTANT:
    # KPI Status Transition is an independent target-list analysis.
    # Do NOT use site_level_df here because that dataframe is restricted
    # by the top "Site Search" field. The pasted Cell + Site + FreqBand
    # list must be the source of truth, even when the top Site Search
    # contains a different site.
    source_df = df.copy()

    if source_df.empty:
        st.warning("No data is available for KPI Status Transition.")
        return

    # ------------------------------------------------------------
    # Parse exact Cell + Site + FreqBand targets.
    # ------------------------------------------------------------
    available_cells = set(
        source_df["_Cell_Display"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    available_sites = set(
        source_df["_Site_ID_Search"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    available_enodebs = set(
        source_df["_eNodeB_Search"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .unique()
    )

    available_bands = set(
        source_df["_FreqBand"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .map(
            lambda value: (
                value[1:] if value.startswith("L") else value
            )
        )
        .unique()
    )

    def normalize_transition_band(value):
        value = str(value).strip().upper()
        return value[1:] if value.startswith("L") else value

    transition_targets = []

    if transition_bulk_text.strip():
        for raw_line in transition_bulk_text.splitlines():
            line = raw_line.strip()

            if not line:
                continue

            fields = [
                field.strip()
                for field in re.split(r"\t|\||;", line)
                if field.strip()
            ]

            if len(fields) < 3:
                continue

            cell_value = None
            site_value = None
            band_value = None

            for field in fields:
                field_upper = field.upper()
                if field_upper in available_cells:
                    cell_value = field_upper
                    break

            for field in fields:
                field_upper = field.upper()
                if field_upper in available_sites:
                    site_value = field_upper
                    break
                if field_upper in available_enodebs:
                    site_value = field_upper
                    break

            for field in fields:
                normalized = normalize_transition_band(field)
                if normalized in available_bands:
                    band_value = normalized
                    break

            if cell_value and site_value and band_value:
                target = (cell_value, site_value, band_value)
                if target not in transition_targets:
                    transition_targets.append(target)

    if transition_bulk_text.strip() and not transition_targets:
        st.warning(
            "No valid Cell + Site + FreqBand targets were found. "
            "Check the pasted values against the uploaded CSV."
        )
        return

    # If no list is pasted, allow the current filtered dataset to be used.
    # This keeps the new layout useful while the exact bulk list is being
    # prepared, but the exact list remains the recommended workflow.
    if transition_targets:
        transition_mask = pd.Series(
            False,
            index=source_df.index,
        )

        for target_cell, target_site, target_band in transition_targets:
            cell_mask = (
                source_df["_Cell_Display"]
                .astype(str)
                .str.upper()
                .eq(target_cell)
            )

            if target_site.startswith("SUM-"):
                site_mask = (
                    source_df["_Site_ID_Search"]
                    .astype(str)
                    .str.upper()
                    .eq(target_site)
                )
            else:
                site_mask = (
                    source_df["_eNodeB_Search"]
                    .astype(str)
                    .str.upper()
                    .eq(target_site)
                )

            band_mask = (
                source_df["_FreqBand"]
                .astype(str)
                .str.upper()
                .map(normalize_transition_band)
                .eq(target_band)
            )

            transition_mask = (
                transition_mask
                | (cell_mask & site_mask & band_mask)
            )

        transition_source = source_df[transition_mask].copy()

        # Build a diagnostic table so the user can immediately verify that
        # every pasted target was found. This is especially important for
        # large bulk lists.
        matched_target_keys = set(
            zip(
                transition_source["_Cell_Display"]
                .astype(str)
                .str.strip()
                .str.upper(),
                transition_source["_Site_ID_Search"]
                .astype(str)
                .str.strip()
                .str.upper(),
                transition_source["_FreqBand"]
                .astype(str)
                .str.strip()
                .str.upper()
                .map(normalize_transition_band),
            )
        )

        unmatched_targets = [
            target
            for target in transition_targets
            if target not in matched_target_keys
        ]

        st.caption(
            "🎯 Exact target mode: "
            f"**{len(transition_targets):,}** target(s) pasted | "
            f"**{len(matched_target_keys):,}** target(s) found in data | "
            f"**{len(unmatched_targets):,}** target(s) not found."
        )

        if unmatched_targets:
            with st.expander(
                f"⚠️ Unmatched Targets ({len(unmatched_targets):,})",
                expanded=False,
            ):
                unmatched_df = pd.DataFrame(
                    unmatched_targets,
                    columns=["Cell Name", "Site ID", "FreqBand"],
                )
                st.dataframe(
                    unmatched_df,
                    use_container_width=True,
                    hide_index=True,
                )
    else:
        transition_source = source_df.copy()
        st.caption(
            "No exact target list entered yet. "
            "Using the current filtered dataset."
        )

    if transition_source.empty:
        st.warning("The selected targets were not found in the dataset.")
        return

    # ------------------------------------------------------------
    # Before / After
    # ------------------------------------------------------------
    available_dates = sorted(
        pd.to_datetime(
            transition_source["_Date"],
            errors="coerce",
        )
        .dropna()
        .dt.date
        .unique()
        .tolist()
    )

    if len(available_dates) < 2:
        st.warning(
            "KPI Status Transition requires at least two available dates."
        )
        return

    date_a_col, date_b_col = st.columns(2, gap="small")

    with date_a_col:
        transition_date_a = st.selectbox(
            "Before Date",
            available_dates,
            index=(
                available_dates.index(
                    min(
                        available_dates,
                        key=lambda d: abs(
                            (pd.Timestamp(d) - pd.Timestamp("2026-09-01")).days
                        ),
                    )
                )
                if available_dates
                else 0
            ),
            format_func=lambda value: pd.Timestamp(value).strftime(
                "%d-%b-%Y"
            ),
            key="kpi_status_transition_date_a",
        )

    with date_b_col:
        transition_date_b = st.selectbox(
            "After Date",
            available_dates,
            index=(
                available_dates.index(
                    min(
                        available_dates,
                        key=lambda d: abs(
                            (pd.Timestamp(d) - pd.Timestamp("2026-10-05")).days
                        ),
                    )
                )
                if available_dates
                else len(available_dates) - 1
            ),
            format_func=lambda value: pd.Timestamp(value).strftime(
                "%d-%b-%Y"
            ),
            key="kpi_status_transition_date_b",
        )

    if transition_date_a == transition_date_b:
        st.warning("Before and After are the same. Select two different dates.")
        return

    # ------------------------------------------------------------
    # KPI selection
    # ------------------------------------------------------------
    transition_defaults = [
        kpi
        for kpi in [
            "SSSR",
            "RRC Setup SR",
            "E-RAB Setup SR",
            "S1 Setup SR",
            "4G Cell Availability",
            "HOSR Inter",
            "HOSR Intra",
            "E-RAB Drop",
        ]
        if kpi in available_kpis
    ]

    transition_kpis = st.multiselect(
        "KPIs to Compare",
        available_kpis,
        default=transition_defaults,
        key="kpi_status_transition_kpis",
        help="Select the KPIs whose status should be compared between Before and After.",
    )

    if not transition_kpis:
        st.info("Select at least one KPI.")
        return

    # ------------------------------------------------------------
    # KPI targets / direction
    # ------------------------------------------------------------
    st.markdown("#### 🎯 KPI Target / Direction")
    st.caption(
        "Set the target for each KPI. Higher is Better means value >= target "
        "is Meet; Lower is Better means value <= target is Meet."
    )
    st.info(
        "RANK2 Rate rule: Target = 20 → 19 is Not Meet, "
        "20 is Meet, and 21 is Meet."
    )

    direction_defaults = {}
    threshold_defaults = {}

    for kpi_name in transition_kpis:
        upper = kpi_name.upper()

        if any(
            word in upper
            for word in [
                "DROP",
                "ABNORMAL",
                "PRB",
                "UTILIZATION",
                "INTERFERENCE",
                "LATENCY",
                "TA",
            ]
        ):
            direction_defaults[kpi_name] = "Lower is Better"
        else:
            direction_defaults[kpi_name] = "Higher is Better"

        # RANK2 Rate: user-defined KPI rule is higher value = better.
        # Target 20 means 19 = Not Meet, 20 = Meet, 21 = Meet.
        if upper == "RANK2 RATE":
            direction_defaults[kpi_name] = "Higher is Better"

        if any(
            word in upper
            for word in [
                "SSSR",
                "SETUP SR",
                "HOSR",
                "AVAILABILITY",
            ]
        ):
            threshold_defaults[kpi_name] = 99.0
        elif "DROP" in upper:
            threshold_defaults[kpi_name] = 0.5
        elif "PRB" in upper or "UTILIZATION" in upper:
            threshold_defaults[kpi_name] = 80.0
        else:
            threshold_defaults[kpi_name] = 0.0

    target_rows = []
    for idx, kpi_name in enumerate(transition_kpis):
        target_col, direction_col = st.columns(
            [1.8, 1.5],
            gap="small",
        )

        with target_col:
            threshold_value = st.number_input(
                f"{kpi_name} Target",
                value=float(threshold_defaults[kpi_name]),
                step=0.1,
                key=f"kpi_transition_threshold_{idx}_{kpi_name}",
            )

        with direction_col:
            direction_value = st.selectbox(
                f"{kpi_name} Direction",
                ["Higher is Better", "Lower is Better"],
                index=(
                    0
                    if direction_defaults[kpi_name] == "Higher is Better"
                    else 1
                ),
                key=f"kpi_transition_direction_v27_{idx}_{kpi_name}",
            )

        target_rows.append(
            {
                "KPI": kpi_name,
                "Target": float(threshold_value),
                "Direction": direction_value,
            }
        )

    target_df = pd.DataFrame(target_rows)

    # ------------------------------------------------------------
    # Build Before / After values per exact Cell + Site + FreqBand.
    # ------------------------------------------------------------
    identity_cols = [
        "_Cell_Display",
        "_Site_ID_Search",
        "_FreqBand",
    ]

    # Keep sector/eNodeB for reporting when available.
    report_cols = [
        "_Cell_Display",
        "_Site_ID_Search",
        "_FreqBand",
        "_Sector_Display",
        "_eNodeB_Search",
    ]

    report_cols = [
        col for col in report_cols
        if col in transition_source.columns
    ]

    transition_source["_Transition_Date"] = pd.to_datetime(
        transition_source["_Date"],
        errors="coerce",
    ).dt.date

    all_results = []

    for kpi_name in transition_kpis:
        actual_column = kpi_actual_columns.get(kpi_name)

        if not actual_column or actual_column not in transition_source.columns:
            continue

        kpi_data = transition_source[
            report_cols + ["_Transition_Date", actual_column]
        ].copy()

        kpi_data["_KPI_Value"] = parse_kpi_numeric(
            kpi_data[actual_column]
        )

        # If more than one record exists for a Cell/Date, use the mean.
        daily_values = (
            kpi_data
            .groupby(
                report_cols + ["_Transition_Date"],
                as_index=False,
                dropna=False,
            )["_KPI_Value"]
            .mean()
        )

        date_a_df = daily_values[
            daily_values["_Transition_Date"] == transition_date_a
        ].rename(
            columns={"_KPI_Value": "Before"}
        )

        date_b_df = daily_values[
            daily_values["_Transition_Date"] == transition_date_b
        ].rename(
            columns={"_KPI_Value": "After"}
        )

        merge_keys = [
            col for col in report_cols
            if col in date_a_df.columns and col in date_b_df.columns
        ]

        if not merge_keys:
            continue

        merged = date_a_df[
            merge_keys + ["Before"]
        ].merge(
            date_b_df[
                merge_keys + ["After"]
            ],
            on=merge_keys,
            how="outer",
        )

        merged["KPI"] = kpi_name

        target_row = target_df[
            target_df["KPI"] == kpi_name
        ].iloc[0]

        target_value = float(target_row["Target"])
        direction = target_row["Direction"]

        if direction == "Higher is Better":
            merged["Status Before"] = merged["Before"].ge(target_value).map(
                {True: "Meet", False: "Not Meet"}
            )
            merged["Status After"] = merged["After"].ge(target_value).map(
                {True: "Meet", False: "Not Meet"}
            )
        else:
            merged["Status Before"] = merged["Before"].le(target_value).map(
                {True: "Meet", False: "Not Meet"}
            )
            merged["Status After"] = merged["After"].le(target_value).map(
                {True: "Meet", False: "Not Meet"}
            )

        merged["Transition"] = (
            merged["Status Before"].fillna("No Data")
            + " → "
            + merged["Status After"].fillna("No Data")
        )

        merged["Target"] = target_value
        merged["Direction"] = direction

        all_results.append(merged)

    if not all_results:
        st.warning("No KPI values could be calculated for the selected targets.")
        return

    transition_result = pd.concat(
        all_results,
        ignore_index=True,
    )

    # Keep an untouched copy of ALL pasted targets.
    # This is intentionally separate from the optional Problem Cell/Site
    # selector below:
    #   - Trend chart = always uses ALL pasted targets.
    #   - Summary/detail = follows Problem Cell/Site selection.
    #   - Correlation/combo = follows Problem Cell/Site selection.
    trend_source_result = transition_result.copy()

    # ------------------------------------------------------------
    # Optional exact target selector
    # ------------------------------------------------------------
    # IMPORTANT: Apply this BEFORE summary/detail/chart calculations.
    # This makes the KPI Action Summary, transition counts, detail table,
    # and trend charts all use only the selected Cell + Site + FreqBand.
    target_selector_df = transition_result[
        [
            "_Cell_Display",
            "_Site_ID_Search",
            "_FreqBand",
        ]
    ].drop_duplicates().copy()

    selected_target_labels = []

    if not target_selector_df.empty:
        target_selector_df["_Target Label"] = (
            target_selector_df["_FreqBand"].astype(str)
            + " | "
            + target_selector_df["_Cell_Display"].astype(str)
            + " | "
            + target_selector_df["_Site_ID_Search"].astype(str)
        )

        target_labels = sorted(
            target_selector_df["_Target Label"].dropna().unique().tolist()
        )

        st.markdown("#### 🎯 Select Problem Cell / Site")

        selected_target_labels = st.multiselect(
            "Select one or more exact targets",
            options=target_labels,
            default=[],
            key="kpi_status_selected_targets_v37",
            placeholder="All targets — select specific Cell + Site to focus analysis",
            help=(
                "Optional filter. Leave empty to analyze all pasted targets. "
                "Select one or more targets to focus the entire KPI Status "
                "Transition analysis on those exact Cell + Site + FreqBand combinations."
            ),
        )

        if selected_target_labels:
            # IMPORTANT:
            # Do NOT filter transition_result here.
            #
            # transition_result is the authoritative result for the
            # complete pasted Problematic Cell Target List. Therefore:
            #   - KPI Action Summary = ALL pasted targets
            #   - Detail / Show Transition = ALL pasted targets
            #   - Select Problem Cell / Site = correlation/investigation only
            #
            # The selected targets are applied later through
            # correlation_scope only.
            st.caption(
                f"🎯 Focused correlation: **{len(selected_target_labels)}** "
                "selected target(s). The KPI Action Summary and Detail table "
                "continue to use the complete pasted target list."
            )
        else:
            st.caption(
                "All pasted targets are included in KPI Action Summary and "
                "Detail. Select one or more Cell + Site + FreqBand targets "
                "only when you want to focus the correlation chart."
            )

    # ------------------------------------------------------------
    # Main transition filter
    # ------------------------------------------------------------
    transition_filter = st.selectbox(
        "Show Transition",
        [
            "Not Meet Only",
            "Not Meet → Meet",
            "All",
            "Meet → Meet",
            "Not Meet → Not Meet",
            "Meet → Not Meet",
            "No Data",
        ],
        index=0,
        key="kpi_status_transition_filter",
        help=(
            "Action-oriented view: show every Cell + KPI that is "
            "Not Meet on After (After date). This includes both "
            "Not Meet → Not Meet and Meet → Not Meet."
        ),
    )

    if transition_filter == "Not Meet Only":
        display_df = transition_result[
            transition_result["Status After"].eq("Not Meet")
        ].copy()
    elif transition_filter == "No Data":
        display_df = transition_result[
            transition_result["Transition"].str.contains(
                "No Data",
                na=False,
            )
        ].copy()
    elif transition_filter == "All":
        display_df = transition_result.copy()
    else:
        display_df = transition_result[
            transition_result["Transition"] == transition_filter
        ].copy()

    # ------------------------------------------------------------
    # Summary cards
    # ------------------------------------------------------------
    improved_count = int(
        (
            transition_result["Transition"]
            == "Not Meet → Meet"
        ).sum()
    )

    degraded_count = int(
        (
            transition_result["Transition"]
            == "Meet → Not Meet"
        ).sum()
    )

    remain_meet_count = int(
        (
            transition_result["Transition"]
            == "Meet → Meet"
        ).sum()
    )

    remain_not_meet_count = int(
        (
            transition_result["Transition"]
            == "Not Meet → Not Meet"
        ).sum()
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Improved to Meet", improved_count)
    c2.metric("Meet → Not Meet", degraded_count)
    c3.metric("Remain Meet", remain_meet_count)
    c4.metric("Remain Not Meet", remain_not_meet_count)

    # ------------------------------------------------------------
    # KPI-level summary
    # ------------------------------------------------------------
    if transition_filter == "Not Meet Only":
        action_summary = (
            display_df
            .groupby("KPI")
            .size()
            .reset_index(name="Not Meet Cells — Action Required")
            .sort_values(
                "Not Meet Cells — Action Required",
                ascending=False,
            )
            .reset_index(drop=True)
        )
        action_summary.insert(
            0,
            "No",
            range(1, len(action_summary) + 1),
        )

        st.markdown("#### 🎯 KPI Action Summary")
        st.caption(
            "Only KPI results that are **Not Meet on After** are shown. "
            "These are the KPI/Cell combinations to prioritize for "
            "optimization and action. "
            f"Total Not Meet KPI/Cell records: **{len(display_df):,}**."
        )
        st.dataframe(
            action_summary,
            use_container_width=True,
            hide_index=True,
        )
    else:
        improved_only = transition_result[
            transition_result["Transition"] == "Not Meet → Meet"
        ].copy()

        if not improved_only.empty:
            kpi_summary = (
                improved_only
                .groupby("KPI")
                .size()
                .reset_index(name="Cells Improved to Meet")
                .sort_values(
                    "Cells Improved to Meet",
                    ascending=False,
                )
            )

            st.markdown("#### 📈 KPI Improvement Summary")
            st.dataframe(
                kpi_summary,
                use_container_width=True,
                hide_index=True,
            )

    # ------------------------------------------------------------
    # Detailed result
    # ------------------------------------------------------------
    if transition_filter == "Not Meet Only":
        st.info(
            "🎯 **Action List:** only KPI results that are **Not Meet on "
            "After** are included. These are the Cell/KPI combinations "
            "to prioritize for optimization."
        )

    st.markdown(
        f"#### 🔎 Detail — {transition_filter}"
    )

    if display_df.empty:
        st.info(
            "No records match the selected transition."
        )
    

    # ------------------------------------------------------------
    # Detailed result
    # ------------------------------------------------------------
    if transition_filter == "Not Meet Only":
        st.info(
            "🎯 **Action List:** only KPI results that are **Not Meet on "
            "After** are included. These are the Cell/KPI combinations "
            "to prioritize for optimization."
        )

    st.markdown(f"#### 🔎 Detail — {transition_filter}")

    display_cols = [
        col for col in [
            "_Site_ID_Search",
            "_Cell_Display",
            "_FreqBand",
            "_Sector_Display",
            "KPI",
            "Before",
            "Status Before",
            "After",
            "Status After",
            "Target",
            "Direction",
            "Transition",
        ]
        if col in display_df.columns
    ]

    final_display = display_df[display_cols].copy()

    rename_map = {
        "_Site_ID_Search": "Site",
        "_Cell_Display": "Cell Name",
        "_FreqBand": "FreqBand",
        "_Sector_Display": "Sector",
    }
    final_display = final_display.rename(columns=rename_map)

    if final_display.empty:
        st.info("No records match the selected transition.")
    else:
        # Number every row so the user can immediately verify the exact
        # number of Cell/KPI records shown in the table.
        final_display = final_display.reset_index(drop=True)
        final_display.insert(
            0,
            "No",
            range(1, len(final_display) + 1),
        )

        st.caption(
            f"📌 **Total records in table: {len(final_display):,}**"
        )

        st.dataframe(
            final_display,
            use_container_width=True,
            hide_index=True,
        )


    # ------------------------------------------------------------
    # TREND / CORRELATION CHARTS — SELECTED TARGET SCOPE
    # ------------------------------------------------------------
    # IMPORTANT:
    # The chart is an investigation view, not the same filter as
    # "Show Transition". If a user explicitly selects a Problem Cell/Site,
    # show its trend even when the selected KPI is currently Meet.
    #
    # This prevents the confusing situation:
    #   Show Transition = Not Meet Only
    #   selected cell = Remain Meet
    #   => blank chart
    #
    # The summary/detail still obey Show Transition.
    #
    # IMPORTANT DESIGN:
    #   Chart 1 / Trend = ALWAYS available when there is a valid site/target
    #                     scope. It does NOT depend on "Select Problem Cell".
    #   Chart 2 / Correlation = ONLY appears when the user explicitly selects
    #                           Problem Cell / Site target(s).
    #
    # This means the selector is an investigation filter for the combo chart,
    # not a prerequisite for the normal KPI trend chart.
    st.markdown("#### 📈 Selected Cell KPI Trend — Cell Name")
    st.caption(
        "Trend chart always follows the complete pasted target list. "
        "Select Problem Cell / Site only to focus the correlation/combo chart "
        "below."
    )

    # -------------------------
    # Chart 1: Trend scope
    # -------------------------
    # NEVER use the Problem Cell/Site selector here.
    # The trend must continue to show the complete pasted target list.
    if not trend_source_result.empty:
        trend_scope = trend_source_result[
            [
                "_Cell_Display",
                "_Site_ID_Search",
                "_FreqBand",
            ]
        ].drop_duplicates().copy()
    elif not site_level_df.empty:
        # If there is no pasted target list, preserve the normal
        # Site Search / Full eNodeB Name behavior.
        fallback_cols = [
            "_Cell_Display",
            "_Site_ID_Search",
            "_FreqBand",
        ]
        if all(col in site_level_df.columns for col in fallback_cols):
            trend_scope = site_level_df[fallback_cols].drop_duplicates().copy()
        else:
            trend_scope = pd.DataFrame(columns=fallback_cols)
    else:
        trend_scope = pd.DataFrame(
            columns=[
                "_Cell_Display",
                "_Site_ID_Search",
                "_FreqBand",
            ]
        )

    # -------------------------
    # Chart 2: Correlation scope
    # -------------------------
    # This is the ONLY chart affected by Select Problem Cell / Site.
    if selected_target_labels:
        # Select Problem Cell / Site controls ONLY the correlation chart.
        # Build the scope from the complete pasted target list so that
        # selecting a cell never changes the KPI Action Summary/Detail.
        selector_scope_source = trend_source_result.copy()

        selector_scope_source["_Target Label"] = (
            selector_scope_source["_FreqBand"].astype(str)
            + " | "
            + selector_scope_source["_Cell_Display"].astype(str)
            + " | "
            + selector_scope_source["_Site_ID_Search"].astype(str)
        )

        correlation_scope = selector_scope_source[
            selector_scope_source["_Target Label"].isin(
                set(selected_target_labels)
            )
        ][
            [
                "_Cell_Display",
                "_Site_ID_Search",
                "_FreqBand",
            ]
        ].drop_duplicates().copy()
    else:
        correlation_scope = pd.DataFrame(
            columns=[
                "_Cell_Display",
                "_Site_ID_Search",
                "_FreqBand",
            ]
        )

    # IMPORTANT:
    # The combo chart must use the KPIs selected in "KPIs to Compare",
    # NOT only the KPIs that happen to be Not Meet in the current
    # transition table.
    #
    # Example:
    #   KPIs to Compare = 4G Cell Availability + SSSR
    #   Primary = 4G Cell Availability
    #   Counter = SSSR
    #
    # Even if Availability is currently Meet, it must still be available
    # as a counter/primary KPI for investigation.
    scope_kpis = [
        k for k in transition_kpis
        if k in kpi_actual_columns
    ]

    def build_scope_history(target_rows, kpi_name):
        actual_col = kpi_actual_columns.get(kpi_name)
        if not actual_col or actual_col not in source_df.columns:
            return pd.DataFrame()

        parts = []

        for _, target in target_rows.iterrows():
            target_mask = (
                source_df["_Cell_Display"]
                .astype(str).str.strip()
                .eq(str(target["_Cell_Display"]).strip())
                & source_df["_Site_ID_Search"]
                .astype(str).str.strip()
                .eq(str(target["_Site_ID_Search"]).strip())
                & source_df["_FreqBand"]
                .astype(str).str.strip().str.upper()
                .eq(str(target["_FreqBand"]).strip().upper())
            )

            h = source_df.loc[
                target_mask,
                [
                    "_Date",
                    "_Cell_Display",
                    "_Site_ID_Search",
                    actual_col,
                ],
            ].copy()

            if h.empty:
                continue

            h["Value"] = pd.to_numeric(h[actual_col], errors="coerce")
            h["_Date"] = pd.to_datetime(h["_Date"], errors="coerce")
            h = h.dropna(subset=["_Date", "Value"])

            if h.empty:
                continue

            h["Cell Name"] = h["_Cell_Display"].astype(str)
            h["Legend"] = (
                h["_Cell_Display"].astype(str)
                + " | "
                + h["_Site_ID_Search"].astype(str)
            )
            parts.append(
                h[["_Date", "Cell Name", "Legend", "Value"]]
            )

        return (
            pd.concat(parts, ignore_index=True)
            if parts
            else pd.DataFrame()
        )

    if not scope_kpis:
        st.info(
            "No KPI trend data is available for the selected target."
        )
    else:
        # One independent chart per KPI, preserving the user's KPI
        # selection and keeping Cell Name as the trace identity.
        for kpi_name in sorted(scope_kpis):
            trend_df = build_scope_history(
                trend_scope,
                kpi_name,
            )

            if trend_df.empty:
                continue

            fig_selected = go.Figure()

            for legend_name in sorted(
                trend_df["Legend"].dropna().unique()
            ):
                h = trend_df[
                    trend_df["Legend"].eq(legend_name)
                ].sort_values("_Date")

                cell_name = str(h["Cell Name"].iloc[0])

                fig_selected.add_trace(
                    go.Scatter(
                        x=h["_Date"],
                        y=h["Value"],
                        mode="lines+markers",
                        name=cell_name,
                        line=dict(width=2.5),
                        marker=dict(size=4),
                        connectgaps=True,
                    )
                )

            target_match = target_df[
                target_df["KPI"].eq(kpi_name)
            ]

            if not target_match.empty:
                target_value = float(
                    target_match["Target"].iloc[0]
                )

                fig_selected.add_trace(
                    go.Scatter(
                        x=[
                            trend_df["_Date"].min(),
                            trend_df["_Date"].max(),
                        ],
                        y=[target_value, target_value],
                        mode="lines",
                        name=f"Target ({target_value:g})",
                        line=dict(
                            dash="dot",
                            width=2,
                        ),
                    )
                )

            fig_selected.update_layout(
                title=f"{kpi_name} — Cell Name Trend",
                height=560,
                template="plotly_white",
                margin=dict(
                    l=60,
                    r=30,
                    t=65,
                    b=135,
                ),
                hovermode="x unified",
                xaxis=dict(
                    title="Date",
                    tickformat="%d-%b-%y",
                ),
                yaxis=dict(
                    title=kpi_name,
                ),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.24,
                    xanchor="center",
                    x=0.5,
                ),
            )

            show_chart(
                fig_selected,
                use_container_width=True,
            )

        # Generic combo chart for the selected target scope.
        # This remains available even when the current transition is
        # Remain Meet, because it is for investigation.
        if selected_target_labels and len(scope_kpis) >= 1:
            st.markdown("#### 🔬 KPI Correlation — Selected Cell/Site")
            st.caption(
                "Problem Cell / Site selection controls this correlation chart only. "
                "Primary KPI comes from KPIs to Compare. Counter KPI can be "
                "any available KPI, so you can correlate RANK2/SSSR/Availability "
                "with Average TA, CQI, PRB, Payload, etc."
            )

            primary_options = scope_kpis
            primary_kpi = st.selectbox(
                "Primary KPI",
                primary_options,
                key="selected_scope_primary_kpi_v41",
            )

            # Counter KPI is independent from the Before/After KPI
            # selection. It can be ANY KPI available in the dataset.
            counter_candidates = [
                k for k in available_kpis
                if k in kpi_actual_columns and k != primary_kpi
            ]

            counter_options = ["None"] + counter_candidates

            # Keep Average TA as the preferred default when available.
            counter_default = 0
            if "Average TA" in counter_candidates:
                counter_default = counter_options.index("Average TA")

            counter_kpi = st.selectbox(
                "Counter KPI",
                counter_options,
                index=counter_default,
                key="selected_scope_counter_kpi_v41",
                help=(
                    "Choose any available KPI to compare with the "
                    "Primary KPI. It does not need to be selected "
                    "in KPIs to Compare."
                ),
            )

            primary_history = build_scope_history(
                correlation_scope,
                primary_kpi,
            )

            counter_history = (
                build_scope_history(
                    correlation_scope,
                    counter_kpi,
                )
                if counter_kpi != "None"
                else pd.DataFrame()
            )

            if not primary_history.empty:
                fig_corr = go.Figure()

                for legend_name in sorted(
                    primary_history["Legend"].dropna().unique()
                ):
                    p = primary_history[
                        primary_history["Legend"].eq(legend_name)
                    ].sort_values("_Date")

                    cell_name = str(p["Cell Name"].iloc[0])

                    # Keep Cell Name visually readable in the legend.
                    # The KPI is appended after a clear separator so the
                    # cell identifier remains easy to scan.
                    primary_is_payload = "payload" in str(primary_kpi).lower()

                    if primary_is_payload:
                        # Payload is shown as a filled "hill" / area style
                        # when it participates in a combo analysis.
                        fig_corr.add_trace(
                            go.Scatter(
                                x=p["_Date"],
                                y=p["Value"],
                                mode="lines",
                                name=f"{cell_name}  |  {primary_kpi}",
                                line=dict(
                                    color="#4472C4",
                                    width=2.8,
                                ),
                                fill="tozeroy",
                                fillcolor="rgba(68,114,196,0.22)",
                                yaxis="y",
                                legendgroup=cell_name,
                            )
                        )
                    else:
                        fig_corr.add_trace(
                            go.Scatter(
                                x=p["_Date"],
                                y=p["Value"],
                                mode="lines+markers",
                                name=f"{cell_name}  |  {primary_kpi}",
                                line=dict(
                                    color="#4472C4",
                                    width=2.5,
                                ),
                                marker=dict(size=4),
                                yaxis="y",
                                legendgroup=cell_name,
                            )
                        )

                    if not counter_history.empty:
                        c = counter_history[
                            counter_history["Legend"].eq(legend_name)
                        ].sort_values("_Date")

                        if not c.empty:
                            counter_is_payload = "payload" in str(counter_kpi).lower()

                            if counter_is_payload:
                                # Payload gets a filled area ("hill") so
                                # traffic volume is immediately distinguishable
                                # from the primary KPI line.
                                fig_corr.add_trace(
                                    go.Scatter(
                                        x=c["_Date"],
                                        y=c["Value"],
                                        mode="lines",
                                        name=f"{cell_name}  |  {counter_kpi}",
                                        line=dict(
                                            color="#ED7D31",
                                            width=2.8,
                                        ),
                                        fill="tozeroy",
                                        fillcolor="rgba(237,125,49,0.22)",
                                        yaxis="y2",
                                        legendgroup=cell_name,
                                    )
                                )
                            else:
                                fig_corr.add_trace(
                                    go.Scatter(
                                        x=c["_Date"],
                                        y=c["Value"],
                                        mode="lines+markers",
                                        name=f"{cell_name}  |  {counter_kpi}",
                                        line=dict(
                                            color="#ED7D31",
                                            width=2,
                                            dash="dash",
                                        ),
                                        marker=dict(
                                            size=3,
                                            symbol="circle-open",
                                        ),
                                        yaxis="y2",
                                        legendgroup=cell_name,
                                    )
                                )

                # --------------------------------------------------------
                # KPI threshold / target lines
                # --------------------------------------------------------
                # Use the SAME target and direction entered in
                # "KPI Target / Direction" above.  The target is drawn on
                # the matching Y-axis so the correlation chart can be read
                # directly against the KPI threshold.
                primary_target_row = target_df[
                    target_df["KPI"].eq(primary_kpi)
                ]

                if not primary_target_row.empty:
                    primary_target = float(
                        primary_target_row["Target"].iloc[0]
                    )

                    fig_corr.add_trace(
                        go.Scatter(
                            x=[
                                primary_history["_Date"].min(),
                                primary_history["_Date"].max(),
                            ],
                            y=[primary_target, primary_target],
                            mode="lines",
                            name=f"Target | {primary_kpi} ({primary_target:g})",
                            line=dict(
                                color="#4472C4",
                                dash="dot",
                                width=2,
                            ),
                            yaxis="y",
                            legendgroup="targets",
                        )
                    )

                if counter_kpi != "None" and not counter_history.empty:
                    counter_target_row = target_df[
                        target_df["KPI"].eq(counter_kpi)
                    ]

                    if not counter_target_row.empty:
                        counter_target = float(
                            counter_target_row["Target"].iloc[0]
                        )

                        fig_corr.add_trace(
                            go.Scatter(
                                x=[
                                    counter_history["_Date"].min(),
                                    counter_history["_Date"].max(),
                                ],
                                y=[counter_target, counter_target],
                                mode="lines",
                                name=(
                                    f"Target | {counter_kpi} "
                                    f"({counter_target:g})"
                                ),
                                line=dict(
                                    color="#ED7D31",
                                    dash="dot",
                                    width=2,
                                ),
                                yaxis="y2",
                                legendgroup="targets",
                            )
                        )

                fig_corr.update_layout(
                    title=(
                        f"{primary_kpi}"
                        + (
                            f" vs {counter_kpi}"
                            if counter_kpi != "None"
                            else ""
                        )
                        + " — Selected Cell/Site"
                    ),
                    height=560,
                    template="plotly_white",
                    # Keep the chart visually tight to the left/right edges.
                    # The extra space is reserved mainly below for the readable
                    # Cell Name legend.
                    margin=dict(
                        l=42,
                        r=42,
                        t=65,
                        b=175,
                    ),
                    hovermode="x unified",
                    xaxis=dict(
                        title="Date",
                        tickformat="%d-%b-%y",
                        range=(
                            [
                                primary_history["_Date"].min(),
                                primary_history["_Date"].max(),
                            ]
                            if not primary_history.empty
                            else None
                        ),
                        automargin=True,
                    ),
                    yaxis=dict(
                        title=primary_kpi,
                        side="left",
                    ),
                    yaxis2=dict(
                        title=counter_kpi
                        if counter_kpi != "None"
                        else "",
                        side="right",
                        overlaying="y",
                        showticklabels=(counter_kpi != "None"),
                    ),
                    legend=dict(
                        title=dict(
                            text="Cell Name  |  KPI",
                            font=dict(
                                size=10,
                            ),
                        ),
                        orientation="h",
                        yanchor="top",
                        y=-0.29,
                        xanchor="center",
                        x=0.5,
                        font=dict(
                            size=10,
                        ),
                        entrywidth=260,
                        entrywidthmode="pixels",
                        traceorder="normal",
                        itemsizing="constant",
                    ),
                )

                show_chart(
                    fig_corr,
                    use_container_width=True,
                )

                if counter_kpi == "None":
                    primary_target_text = "Target line follows KPI Target / Direction."
                    st.caption(
                        f"Blue solid = {primary_kpi} | "
                        "Counter KPI = None | "
                        f"{primary_target_text}"
                    )
                else:
                    st.caption(
                        f"Blue solid = {primary_kpi} | "
                        f"Orange dashed = {counter_kpi} | "
                        f"Blue dotted = {primary_kpi} target | "
                        f"Orange dotted = {counter_kpi} target | "
                        f"Left axis = {primary_kpi} | "
                        f"Right axis = {counter_kpi}"
                    )

    return

def render_kpi_analysis():
    if site_level_df.empty:
        return

    def actual_col(kpi_name):
        col = kpi_actual_columns.get(kpi_name)
        return col if col and col in site_level_df.columns else None

    payload_col = actual_col("Payload")
    availability_col = actual_col("4G Cell Availability")
    sssr_col = actual_col("SSSR")
    rrc_col = actual_col("RRC Setup SR")
    erab_col = actual_col("E-RAB Setup SR")
    s1_col = actual_col("S1 Setup SR")

    if not any([
        payload_col and availability_col,
        sssr_col,
    ]):
        return

    st.markdown("### 📊 Site Diagnostic Overview")
    st.caption(
        "Fixed site-level diagnostic overview: Traffic vs Availability "
        "and SSSR setup-component drill-down."
    )

    # ------------------------------------------------------------
    # Build one time axis for both charts.
    # ------------------------------------------------------------
    base_cols = ["_Date"]
    for col in [
        payload_col,
        availability_col,
        sssr_col,
        rrc_col,
        erab_col,
        s1_col,
    ]:
        if col and col not in base_cols:
            base_cols.append(col)

    analysis_source = site_level_df[base_cols].copy()

    if payload_col:
        analysis_source["_Payload_Value"] = parse_kpi_numeric(
            analysis_source[payload_col]
        )
    if availability_col:
        analysis_source["_Availability_Value"] = parse_kpi_numeric(
            analysis_source[availability_col]
        )
    if sssr_col:
        analysis_source["_SSSR_Value"] = parse_kpi_numeric(
            analysis_source[sssr_col]
        )
    if rrc_col:
        analysis_source["_RRC_Value"] = parse_kpi_numeric(
            analysis_source[rrc_col]
        )
    if erab_col:
        analysis_source["_ERAB_Value"] = parse_kpi_numeric(
            analysis_source[erab_col]
        )
    if s1_col:
        analysis_source["_S1_Value"] = parse_kpi_numeric(
            analysis_source[s1_col]
        )

    analysis_source["_Chart_Date"] = (
        analysis_source["_Date"]
        if is_hourly
        else analysis_source["_Date"].dt.normalize()
    )

    agg_map = {}
    if payload_col:
        agg_map["Payload_GB"] = ("_Payload_Value", "sum")
    if availability_col:
        agg_map["Availability"] = ("_Availability_Value", "mean")
    if sssr_col:
        agg_map["SSSR"] = ("_SSSR_Value", "mean")
    if rrc_col:
        agg_map["RRC_Setup_SR"] = ("_RRC_Value", "mean")
    if erab_col:
        agg_map["ERAB_Setup_SR"] = ("_ERAB_Value", "mean")
    if s1_col:
        agg_map["S1_Setup_SR"] = ("_S1_Value", "mean")

    analysis_df = (
        analysis_source
        .groupby("_Chart_Date", as_index=False)
        .agg(**agg_map)
        .sort_values("_Chart_Date")
    )

    if analysis_df.empty:
        return

    if is_hourly:
        tickformat = "%H:%M<br>%d-%b"
        hoverformat = "%d-%b-%Y %H:%M"
        dtick = 6 * 60 * 60 * 1000
    else:
        tickformat = "%d-%b-%y"
        hoverformat = "%d-%b-%Y"
        dtick = None

    # ------------------------------------------------------------
    # CHART 1 — Payload + Availability
    # ------------------------------------------------------------
    fig_traffic = go.Figure()

    if "Payload_GB" in analysis_df.columns:
        fig_traffic.add_trace(
            go.Bar(
                x=analysis_df["_Chart_Date"],
                y=analysis_df["Payload_GB"],
                name="Site Payload (GB)",
                yaxis="y2",
                marker=dict(color="#8a8a8a"),
                opacity=0.40,
            )
        )

    if "Availability" in analysis_df.columns:
        fig_traffic.add_trace(
            go.Scatter(
                x=analysis_df["_Chart_Date"],
                y=analysis_df["Availability"],
                name="4G Cell Availability (%)",
                mode="lines+markers",
                line=dict(color="#ED7D31", width=5),
                marker=dict(size=6),
                connectgaps=True,
            )
        )

    fig_traffic.update_layout(
        title="Traffic vs Availability",
        height=430,
        template="plotly_white",
        margin=dict(l=48, r=48, t=55, b=90),
        hovermode="x unified",
        xaxis=dict(
            title="Date / Time",
            tickformat=tickformat,
            hoverformat=hoverformat,
            dtick=dtick,
            showgrid=True,
            gridcolor="#e5e5e5",
            range=[
                analysis_df["_Chart_Date"].min(),
                analysis_df["_Chart_Date"].max(),
            ],
            automargin=True,
        ),
        yaxis=dict(
            title="Availability (%)",
            range=[0, 120],
            showgrid=True,
            gridcolor="#e5e5e5",
            zeroline=False,
        ),
        yaxis2=dict(
            title="Payload (GB)",
            overlaying="y",
            side="right",
            showgrid=False,
            zeroline=False,
        ),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.20,
            xanchor="center",
            x=0.5,
        ),
        bargap=0.02,
    )

    fig_traffic.update_traces(
        selector=dict(type="bar"),
        hovertemplate=(
            "<b>Site Payload</b><br>"
            + (
                "%{x|%d-%b-%Y %H:%M}<br>"
                if is_hourly
                else "%{x|%d-%b-%Y}<br>"
            )
            + "Payload: %{y:.2f} GB<extra></extra>"
        ),
    )

    fig_traffic.update_traces(
        selector=dict(type="scatter"),
        hovertemplate=(
            "<b>%{fullData.name}</b><br>"
            + (
                "%{x|%d-%b-%Y %H:%M}<br>"
                if is_hourly
                else "%{x|%d-%b-%Y}<br>"
            )
            + "Value: %{y:.2f}%<extra></extra>"
        ),
    )

    # ------------------------------------------------------------
    # CHART 2 — SSSR + Setup Components
    # ------------------------------------------------------------
    fig_access = go.Figure()

    series_config = [
        ("SSSR", "SSSR (%)", "#4472C4", 5),
        ("RRC_Setup_SR", "RRC Setup SR (%)", "#ED7D31", 3),
        ("ERAB_Setup_SR", "E-RAB Setup SR (%)", "#70AD47", 3),
        ("S1_Setup_SR", "S1 Setup SR (%)", "#A64D79", 3),
    ]

    for data_key, label, color, width in series_config:
        if data_key in analysis_df.columns:
            fig_access.add_trace(
                go.Scatter(
                    x=analysis_df["_Chart_Date"],
                    y=analysis_df[data_key],
                    name=label,
                    mode="lines+markers",
                    line=dict(color=color, width=width),
                    marker=dict(size=5 if data_key != "SSSR" else 7),
                    connectgaps=True,
                )
            )

    # SSSR target shown as a reference line because the supplied
    # analysis example uses 99.0% as the target.
    if "SSSR" in analysis_df.columns:
        fig_access.add_hline(
            y=99,
            line=dict(
                color="#00A878",
                width=2,
                dash="dash",
            ),
            annotation_text="SSSR Target 99%",
            annotation_position="top left",
            annotation_font=dict(size=9, color="#00875A"),
        )

    fig_access.update_layout(
        title="SSSR Drill-down — Setup Components",
        height=430,
        template="plotly_white",
        margin=dict(l=48, r=25, t=55, b=90),
        hovermode="x unified",
        xaxis=dict(
            title="Date / Time",
            tickformat=tickformat,
            hoverformat=hoverformat,
            dtick=dtick,
            showgrid=True,
            gridcolor="#e5e5e5",
            range=[
                analysis_df["_Chart_Date"].min(),
                analysis_df["_Chart_Date"].max(),
            ],
            automargin=True,
        ),
        yaxis=dict(
            title="Success Rate (%)",
            range=[80, 101],
            showgrid=True,
            gridcolor="#e5e5e5",
            zeroline=False,
        ),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.20,
            xanchor="center",
            x=0.5,
        ),
    )

    fig_access.update_traces(
        selector=dict(type="scatter"),
        hovertemplate=(
            "<b>%{fullData.name}</b><br>"
            + (
                "%{x|%d-%b-%Y %H:%M}<br>"
                if is_hourly
                else "%{x|%d-%b-%Y}<br>"
            )
            + "Value: %{y:.2f}%<extra></extra>"
        ),
    )

    # Collect both figures for the existing grouped-download workflow.
    _download_figures.append(fig_traffic)
    _download_figures.append(fig_access)

    # Render side-by-side as requested.
    col1, col2 = st.columns(2, gap="small")

    with col1:
        st.plotly_chart(
            fig_traffic,
            use_container_width=True,
            key="kpi_analysis_traffic_availability",
        )

    with col2:
        st.plotly_chart(
            fig_access,
            use_container_width=True,
            key="kpi_analysis_sssr_components",
        )



# ============================================================
# CONFIGURABLE KPI ANALYSIS BUILDER
# ============================================================
#
# The existing KPI Analysis remains available, but this builder
# lets the user choose the KPI combinations without editing code.
#
# The builder uses KPI_CONFIG / kpi_actual_columns already defined
# by the dashboard and only exposes columns that actually exist.
# ============================================================

def render_configurable_kpi_analysis():
    if site_level_df.empty:
        return

    st.markdown("---")
    st.markdown("---")
    st.markdown("## 📊 KPI Analysis")
    st.caption(
        "Independent analysis layout. Choose the primary KPI and related "
        "KPIs to investigate the likely degradation driver."
    )

    available_kpis = [
        name for name in KPI_CONFIG.keys()
        if kpi_actual_columns.get(name) in site_level_df.columns
    ]

    if not available_kpis:
        return

    # ------------------------------------------------------------
    # Independent KPI Analysis filters
    # ------------------------------------------------------------
    # Unlike the main dashboard Site Search, this section can analyze
    # multiple sites and multiple Cell Names at the same time.
    analysis_source = df.copy()

    analysis_site_values = (
        analysis_source["_Site_ID_Search"]
        .dropna()
        .astype(str)
        .str.strip()
    )
    analysis_site_values = sorted(
        value for value in analysis_site_values.unique() if value
    )

    default_analysis_sites = (
        [site_key]
        if site_key in analysis_site_values
        else analysis_site_values[:1]
    )

    # ------------------------------------------------------------
    # Analysis scope + search mode + FreqBand + Cell Name
    # ------------------------------------------------------------
    # KPI Analysis can now be driven by:
    #   1) Site ID (SUM-....)
    #   2) Full eNodeB Name
    #
    # FreqBand is intentionally placed BEFORE Cell Name.
    # When one or more FreqBands are selected, the Cell Name list is
    # dynamically reduced to only cells belonging to those bands.
    # This avoids manually searching through all Cell Names.
    scope_col, search_col, site_col, band_col, cell_col = st.columns(
        [1.0, 1.15, 1.65, 1.35, 2.45],
        gap="small",
    )

    with scope_col:
        analysis_scope = st.radio(
            "Analysis Level",
            ["Site Level", "Sector Level", "Cell Level"],
            horizontal=True,
            key="custom_kpi_analysis_scope",
            help=(
                "Site Level compares each selected site. "
                "Sector Level groups all matching cells by the dashboard's "
                "existing sector mapping. Cell Level compares each Cell Name. "
                "Bulk Cell + Site + FreqBand targets remain available and "
                "continue to take precedence for exact cell-level analysis."
            ),
        )

    with search_col:
        analysis_search_mode = st.radio(
            "Analysis Search",
            ["Site ID", "Full eNodeB Name"],
            horizontal=True,
            key="custom_kpi_analysis_search_mode",
            help=(
                "Choose whether KPI Analysis should be filtered by Site ID "
                "or by the original full eNodeB Name from the CSV."
            ),
        )

    if analysis_search_mode == "Site ID":
        analysis_selector_values = analysis_site_values

        default_analysis_selector = (
            default_analysis_sites
            if default_analysis_sites
            else analysis_selector_values[:1]
        )

        analysis_selector_label = "Analysis Site ID"
        analysis_selector_key = "custom_kpi_analysis_sites"

    else:
        analysis_selector_values = sorted(
            value
            for value in (
                analysis_source["_eNodeB_Search"]
                .dropna()
                .astype(str)
                .str.strip()
                .unique()
            )
            if value
        )

        # Prefer the full eNodeB name belonging to the current top-level
        # search result as the default when available.
        default_analysis_selector = sorted(
            value
            for value in (
                site_df["_eNodeB_Search"]
                .dropna()
                .astype(str)
                .str.strip()
                .unique()
            )
            if value
        )

        if not default_analysis_selector:
            default_analysis_selector = analysis_selector_values[:1]

        analysis_selector_label = "Analysis Full eNodeB Name"
        analysis_selector_key = "custom_kpi_analysis_enodeb"

    with site_col:
        analysis_sites_manual = st.multiselect(
            analysis_selector_label,
            analysis_selector_values,
            default=default_analysis_selector,
            key=analysis_selector_key,
            help=(
                "Select one or more "
                + (
                    "Site IDs."
                    if analysis_search_mode == "Site ID"
                    else "full eNodeB Names exactly as provided in the CSV."
                )
            ),
        )

    # ------------------------------------------------------------
    # BULK CELL + SITE + FREQBAND LIST
    # ------------------------------------------------------------
    # Recommended input from Excel:
    #
    #   850    JB4G85_4264237E85_131    SUM-JA-MBN-0779
    #
    # The column order is NOT important. The parser identifies each
    # value by its content:
    #   - 850 / L850                  -> FreqBand
    #   - JB4G85_...                  -> Cell Name
    #   - SUM-JA-MBN-0779             -> Site ID
    #   - 4264587E_LTE_...#...#MC     -> full eNodeB Name
    #
    # One Excel row = one exact analysis target:
    #       Cell Name + Site/eNodeB + FreqBand
    #
    # This prevents the analysis from expanding to other bands/cells
    # that were not included in the user's list.
    # ------------------------------------------------------------
    analysis_sites_bulk = []
    analysis_cell_site_pairs = []
    analysis_bulk_bands = []

    if analysis_search_mode == "Site ID":
        with st.expander(
            "📋 Bulk Cell + Site + FreqBand List — Paste from Excel",
            expanded=False,
        ):
            bulk_cell_site_text = st.text_area(
                "Paste Cell Name + Site ID/eNodeB Name + FreqBand",
                placeholder=(
                    "850\\tJB4G85_4264237E85_131\\tSUM-JA-MBN-0779\\n"
                    "SUM-JA-MBN-0779\\t850\\tJB4G85_4264237E85_133\\n"
                    "JB4G85_4264237E85_133\\t850\\tSUM-JA-MBN-0779"
                ),
                height=150,
                key="custom_kpi_analysis_bulk_cell_site_list",
                help=(
                    "Paste 3 columns from Excel in ANY order: "
                    "Cell Name, Site ID/eNodeB Name, and FreqBand."
                ),
            )

            available_cells = set(
                analysis_source["_Cell_Display"]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
                .unique()
            )

            available_sites = set(
                analysis_source["_Site_ID_Search"]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
                .unique()
            )

            available_enodebs = set(
                analysis_source["_eNodeB_Search"]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
                .unique()
            )

            available_raw_bands = set(
                analysis_source["_FreqBand"]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
                .unique()
            )

            def _normalize_bulk_band(value):
                value = str(value).strip().upper()
                if value.startswith("L"):
                    value = value[1:]
                return value

            available_band_normalized = {
                _normalize_bulk_band(band)
                for band in available_raw_bands
            }

            if bulk_cell_site_text.strip():

                for raw_line in bulk_cell_site_text.splitlines():

                    line = raw_line.strip()

                    if not line:
                        continue

                    normalized_line = line.lower()
                    if (
                        "cell name" in normalized_line
                        and (
                            "site id" in normalized_line
                            or "towerid" in normalized_line
                            or "enodeb" in normalized_line
                        )
                    ):
                        continue

                    # Excel copy normally uses TAB. Also support
                    # pipe, semicolon, comma and multiple spaces.
                    fields = [
                        field.strip()
                        for field in re.split(
                            r"\t|\||;",
                            line,
                        )
                        if field.strip()
                    ]

                    if len(fields) < 3:
                        fields = [
                            field.strip()
                            for field in re.split(
                                r",",
                                line,
                            )
                            if field.strip()
                        ]

                    if len(fields) < 3:
                        fields = [
                            field.strip()
                            for field in re.split(
                                r"\s{2,}",
                                line,
                            )
                            if field.strip()
                        ]

                    if len(fields) < 3:
                        continue

                    cell_value = None
                    site_value = None
                    band_value = None

                    # Identify Cell Name by exact match.
                    for field in fields:
                        field_upper = field.upper().strip()
                        if field_upper in available_cells:
                            cell_value = field_upper
                            break

                    # Identify Site ID or full eNodeB Name.
                    for field in fields:
                        field_upper = field.upper().strip()

                        sum_match = re.search(
                            r"(SUM-[A-Z0-9]+(?:-[A-Z0-9]+)*)",
                            field_upper,
                        )

                        if sum_match:
                            site_value = sum_match.group(1)
                            break

                        if field_upper in available_enodebs:
                            site_value = field_upper
                            break

                        if field_upper in available_sites:
                            site_value = field_upper
                            break

                    # Identify FreqBand by exact normalized band.
                    for field in fields:
                        field_upper = field.upper().strip()
                        normalized_band = _normalize_bulk_band(field_upper)

                        if (
                            normalized_band in
                            available_band_normalized
                        ):
                            band_value = normalized_band
                            break

                    if (
                        cell_value
                        and site_value
                        and band_value
                    ):
                        analysis_cell_site_pairs.append(
                            (
                                cell_value,
                                site_value,
                                band_value,
                            )
                        )

                # Preserve Excel order and remove duplicate rows.
                analysis_cell_site_pairs = list(
                    dict.fromkeys(
                        analysis_cell_site_pairs
                    )
                )

                valid_pairs = []
                invalid_pairs = []

                for (
                    cell_value,
                    site_value,
                    band_value,
                ) in analysis_cell_site_pairs:

                    if site_value.startswith("SUM-"):
                        pair_mask = (
                            analysis_source["_Cell_Display"]
                            .astype(str)
                            .str.upper()
                            .eq(cell_value)
                            & analysis_source["_Site_ID_Search"]
                            .astype(str)
                            .str.upper()
                            .eq(site_value)
                            & analysis_source["_FreqBand"]
                            .astype(str)
                            .str.upper()
                            .map(_normalize_bulk_band)
                            .eq(band_value)
                        )
                    else:
                        pair_mask = (
                            analysis_source["_Cell_Display"]
                            .astype(str)
                            .str.upper()
                            .eq(cell_value)
                            & analysis_source["_eNodeB_Search"]
                            .astype(str)
                            .str.upper()
                            .eq(site_value)
                            & analysis_source["_FreqBand"]
                            .astype(str)
                            .str.upper()
                            .map(_normalize_bulk_band)
                            .eq(band_value)
                        )

                    if pair_mask.any():
                        valid_pairs.append(
                            (
                                cell_value,
                                site_value,
                                band_value,
                            )
                        )
                    else:
                        invalid_pairs.append(
                            (
                                cell_value,
                                site_value,
                                band_value,
                            )
                        )

                analysis_cell_site_pairs = valid_pairs

                analysis_sites_bulk = list(
                    dict.fromkeys(
                        site_value
                        for _, site_value, _
                        in analysis_cell_site_pairs
                    )
                )

                analysis_bulk_bands = list(
                    dict.fromkeys(
                        band_value
                        for _, _, band_value
                        in analysis_cell_site_pairs
                    )
                )

                st.caption(
                    f"Bulk list: "
                    f"{len(analysis_cell_site_pairs):,} valid target(s)"
                    + (
                        f" | {len(invalid_pairs):,} not found."
                        if invalid_pairs
                        else "."
                    )
                )

                if invalid_pairs:
                    with st.expander(
                        f"View {len(invalid_pairs):,} target(s) not found",
                        expanded=False,
                    ):
                        st.dataframe(
                            pd.DataFrame(
                                invalid_pairs,
                                columns=[
                                    "Cell Name",
                                    "Site / eNodeB",
                                    "FreqBand",
                                ],
                            ),
                            use_container_width=True,
                            hide_index=True,
                        )

    # ------------------------------------------------------------
    # Combine manual Site selection + bulk Site values.
    # ------------------------------------------------------------
    if analysis_search_mode == "Site ID":
        analysis_sites = list(
            dict.fromkeys(
                analysis_sites_manual
                + [
                    site
                    for site in analysis_sites_bulk
                ]
            )
        )
    else:
        analysis_sites = analysis_sites_manual

    if analysis_search_mode == "Site ID":

        sum_bulk_sites = [
            value
            for value in analysis_sites
            if str(value).upper().startswith("SUM-")
        ]

        enodeb_bulk_sites = [
            value
            for value in analysis_sites
            if not str(value).upper().startswith("SUM-")
        ]

        site_mask = (
            analysis_source["_Site_ID_Search"].isin(
                sum_bulk_sites
            )
        )

        if enodeb_bulk_sites:
            site_mask = (
                site_mask
                | analysis_source["_eNodeB_Search"].isin(
                    enodeb_bulk_sites
                )
            )

        site_filtered_source = analysis_source[
            site_mask
        ].copy()

        # Exact Cell + Site + FreqBand filter.
        #
        # IMPORTANT:
        # When a Bulk list is supplied, it is the SOURCE OF TRUTH.
        # Do not let Analysis Level (Site/Cell), manual Site selection,
        # stale Streamlit session state, or FreqBand selectors expand
        # the dataset beyond the exact triples pasted by the user.
        if analysis_cell_site_pairs:
            pair_mask = pd.Series(
                False,
                index=site_filtered_source.index,
            )

            for (
                cell_value,
                site_value,
                band_value,
            ) in analysis_cell_site_pairs:

                cell_mask = (
                    site_filtered_source["_Cell_Display"]
                    .astype(str)
                    .str.upper()
                    .eq(cell_value)
                )

                if site_value.startswith("SUM-"):
                    site_mask_pair = (
                        site_filtered_source["_Site_ID_Search"]
                        .astype(str)
                        .str.upper()
                        .eq(site_value)
                    )
                else:
                    site_mask_pair = (
                        site_filtered_source["_eNodeB_Search"]
                        .astype(str)
                        .str.upper()
                        .eq(site_value)
                    )

                band_mask = (
                    site_filtered_source["_FreqBand"]
                    .astype(str)
                    .str.upper()
                    .map(_normalize_bulk_band)
                    .eq(band_value)
                )

                pair_mask = (
                    pair_mask
                    | (
                        cell_mask
                        & site_mask_pair
                        & band_mask
                    )
                )

            site_filtered_source = site_filtered_source[
                pair_mask
            ].copy()

            # Hard guard: Bulk mode must contain ONLY exact pasted
            # Cell + Site/eNodeB + FreqBand combinations.
            if not site_filtered_source.empty:
                site_filtered_source["_Bulk_Exact_Target"] = True

    else:
        site_filtered_source = analysis_source[
            analysis_source["_eNodeB_Search"].isin(analysis_sites)
        ].copy()

    # ------------------------------------------------------------
    # FreqBand filter
    # ------------------------------------------------------------
    # Build the band list AFTER Site/eNodeB filtering so only bands
    # that actually exist under the selected site(s) are offered.
    #
    # IMPORTANT:
    #   The CSV/master mapping keeps the raw band key as:
    #       700, 850, 900, 1800, 2100, 2300F1, 2300F2
    #   but KPI Analysis displays them as:
    #       L700, L850, L900, L1800, L2100, L2300F1, L2300F2
    #
    # This makes L850/L900 etc. unambiguous to the RNO user while
    # preserving the existing mapping/filter logic underneath.
    raw_analysis_bands = sorted(
        value
        for value in (
            site_filtered_source["_FreqBand"]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
        )
        if value
    )

    # Exact Bulk Cell + Site + FreqBand targets force Cell-level KPI
    # traces. This block MUST come after analysis_scope is assigned,
    # otherwise Python raises UnboundLocalError.
    force_bulk_cell_level = bool(global_bulk_pairs)
    effective_cell_level = (
        analysis_scope == "Cell Level"
        or force_bulk_cell_level
    )
    effective_sector_level = (
        analysis_scope == "Sector Level"
        and not force_bulk_cell_level
    )


    band_sort_order = [
        "700",
        "850",
        "900",
        "1800",
        "2100",
        "2300F1",
        "2300F2",
    ]

    raw_analysis_bands = (
        [b for b in band_sort_order if b in raw_analysis_bands]
        + [
            b for b in raw_analysis_bands
            if b not in band_sort_order
        ]
    )

    def _analysis_band_label(raw_band):
        raw_band = str(raw_band).strip().upper()
        return f"L{raw_band}"

    def _analysis_band_raw(display_band):
        display_band = str(display_band).strip().upper()
        return display_band[1:] if display_band.startswith("L") else display_band

    # User-facing labels: L700 / L850 / L900 / L1800 / L2100 /
    # L2300F1 / L2300F2.
    analysis_band_values = [
        _analysis_band_label(b)
        for b in raw_analysis_bands
    ]

    # Keep the previous selection when possible. Also support the
    # previous script's raw values (e.g. "850") so a Streamlit rerun
    # does not unexpectedly lose the user's band selection.
    previous_bands = st.session_state.get(
        "custom_kpi_analysis_bands",
        analysis_band_values,
    )

    normalized_previous_bands = []
    for band in previous_bands:
        label = _analysis_band_label(_analysis_band_raw(band))
        if label not in normalized_previous_bands:
            normalized_previous_bands.append(label)

    valid_default_bands = [
        band for band in normalized_previous_bands
        if band in analysis_band_values
    ]

    # If the selected site/eNodeB changed and no previous band remains,
    # default to all bands available under the new site/eNodeB.
    if not valid_default_bands and analysis_band_values:
        valid_default_bands = analysis_band_values.copy()

    with band_col:
        analysis_bands = st.multiselect(
            "Analysis FreqBand",
            analysis_band_values,
            default=valid_default_bands,
            key="custom_kpi_analysis_bands",
            help=(
                "Select one or more FreqBand first "
                "(L700/L850/L900/L1800/L2100/L2300F1/L2300F2). "
                "The Analysis Cell Name list will then show only "
                "cells belonging to the selected band(s)."
            ),
        )

    # Convert the displayed labels back to the raw mapping values
    # before filtering the dataframe.
    analysis_bands_raw = [
        _analysis_band_raw(band)
        for band in analysis_bands
    ]

    # ------------------------------------------------------------
    # Cell list filtered by selected FreqBand
    # ------------------------------------------------------------
    cell_source = site_filtered_source.copy()

    if analysis_bands_raw:
        cell_source = cell_source[
            cell_source["_FreqBand"]
            .astype(str)
            .str.strip()
            .isin(analysis_bands_raw)
        ].copy()

    analysis_cell_values = sorted(
        value
        for value in (
            cell_source["_Cell_Display"]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
        )
        if value
    )

    # Keep only Cell Names that are still valid after the FreqBand change.
    previous_cells = st.session_state.get(
        "custom_kpi_analysis_cells",
        analysis_cell_values,
    )

    valid_default_cells = [
        cell for cell in previous_cells
        if cell in analysis_cell_values
    ]

    # When Bulk Cell + Site pairs are provided, prioritize exactly
    # those Cell Names. This prevents an old Streamlit session state
    # from leaving the Cell selector empty.
    if (
        analysis_scope == "Cell Level"
        and analysis_cell_site_pairs
    ):
        bulk_pair_cells = [
            cell
            for cell, _, _ in analysis_cell_site_pairs
            if cell in analysis_cell_values
        ]

        if bulk_pair_cells:
            valid_default_cells = list(
                dict.fromkeys(bulk_pair_cells)
            )

    # Normal behavior when Bulk Cell + Site is not used.
    if (
        analysis_scope == "Cell Level"
        and not valid_default_cells
        and analysis_cell_values
    ):
        valid_default_cells = analysis_cell_values.copy()

    with cell_col:
        if effective_cell_level:
            analysis_cells = st.multiselect(
                "Analysis Cell Name",
                analysis_cell_values,
                default=valid_default_cells,
                key="custom_kpi_analysis_cells",
                help=(
                    "Cell Names are automatically filtered by the selected "
                    "Analysis FreqBand."
                ),
            )
        else:
            analysis_cells = []
            st.multiselect(
                "Analysis Cell Name",
                analysis_cell_values,
                default=[],
                disabled=True,
                key="custom_kpi_analysis_cells_site_level",
                help="Cell filtering is disabled in Site Level analysis.",
            )

    # IMPORTANT BULK MODE:
    # If an exact Bulk Cell + Site + FreqBand list was supplied,
    # site_filtered_source is already the exact target dataset.
    # Keep it as the source of truth. Manual Cell/FreqBand selectors
    # may only narrow the result; they must never expand it.
    #
    # Without Bulk mode, retain the normal manual filtering workflow.
    analysis_df = site_filtered_source.copy()

    if analysis_bands_raw:
        analysis_df = analysis_df[
            analysis_df["_FreqBand"]
            .astype(str)
            .str.strip()
            .isin(analysis_bands_raw)
        ].copy()

    if effective_cell_level:
        if analysis_cells:
            analysis_df = analysis_df[
                analysis_df["_Cell_Display"].isin(analysis_cells)
            ].copy()
        else:
            analysis_df = analysis_df.iloc[0:0].copy()

    if (
        isinstance(date_range, tuple)
        and len(date_range) == 2
    ):
        analysis_start, analysis_end = date_range
        analysis_df = analysis_df[
            (analysis_df["_Date_Day"] >= pd.Timestamp(analysis_start))
            & (analysis_df["_Date_Day"] <= pd.Timestamp(analysis_end))
        ].copy()

    if analysis_df.empty:
        st.warning(
            "No KPI data is available for the current Cell/FreqBand filter. "
            "Please check the Bulk Cell + Site pairs or select a Cell Name."
        )
        analysis_is_hourly = False
    else:
        analysis_is_hourly = bool(analysis_df["_Is_Hourly"].any())

    # Useful RNO-oriented presets.
    preset_map = {
        "Accessibility — SSSR drill-down": [
            "SSSR",
            "RRC Setup SR",
            "E-RAB Setup SR",
            "S1 Setup SR",
        ],
        "Traffic / Capacity": [
            "Payload",
            "HX4 DL PRB Utilization",
            "HX4 UL PRB Utilization",
            "Number of RRC Connected User",
        ],
        "Retainability": [
            "E-RAB Drop",
            "RRC Drop",
            "E-RAB Abnormal Release",
        ],
        "Mobility": [
            "HOSR",
            "HO Preparation SR",
            "HO Execution SR",
        ],
        "Radio / Coverage": [
            "Average TA",
            "CQI",
            "UL RSSI",
            "RSRP",
            "RSRQ",
        ],
        "Traffic + Availability + TTI": [
            "Payload",
            "4G Cell Availability",
            "Last TTI Ratio",
        ],
    }

    preset_options = ["Custom"] + list(preset_map.keys())

    c1, c2 = st.columns([1, 2], gap="small")

    with c1:
        preset = st.selectbox(
            "Analysis Preset",
            preset_options,
            key="custom_kpi_analysis_preset",
        )

    default_selection = []
    if preset != "Custom":
        default_selection = [
            k for k in preset_map[preset] if k in available_kpis
        ]

    with c2:
        selected = st.multiselect(
            "KPIs to Analyze",
            available_kpis,
            default=default_selection,
            key="custom_kpi_analysis_selection",
        )

    # If the filtered dataset is temporarily empty, keep the KPI
    # selector visible but stop before chart/summary calculations.
    if analysis_df.empty:
        st.info(
            "KPI selector is ready. Select a KPI after confirming "
            "the Cell/FreqBand filter."
        )
        return

    # KPI Analysis supports a single KPI as well as multi-KPI analysis.
    # 1 KPI  -> immediately render one diagnostic chart.
    # 2+ KPI -> keep the existing combined / primary+related analysis.
    if len(selected) < 1:
        st.info("Select at least 1 KPI to build the KPI Analysis.")
        return

    # Keep this analysis visually and logically independent from the
    # fixed two-chart Site Diagnostic Overview above.
    st.markdown("#### 🔎 KPI Analysis — Custom Combination")
    if force_bulk_cell_level:
        st.caption(
            "🎯 Bulk Exact Target Mode: each input Cell Name is plotted "
            "as an independent trend trace (Cell + Site/eNodeB + FreqBand)."
        )

    c3, c4, c5, c6 = st.columns([1, 1, 1, 1], gap="small")

    with c3:
        primary_kpi = st.selectbox(
            "Primary / Trigger KPI",
            selected,
            key="custom_kpi_analysis_primary",
        )

    with c4:
        if len(selected) == 1:
            # A single KPI does not need a second empty/related chart.
            # Render exactly one chart for the selected KPI.
            chart_mode = "1 KPI Chart"
            st.selectbox(
                "Chart Mode",
                ["1 KPI Chart"],
                index=0,
                disabled=True,
                key="custom_kpi_analysis_mode_single",
            )
        else:
            chart_mode = st.selectbox(
                "Chart Mode",
                [
                    "2 Charts — Primary + Related",
                    "1 Combined Chart",
                ],
                key="custom_kpi_analysis_mode",
            )

    with c5:
        show_threshold = st.checkbox(
            "Show threshold",
            value=True,
            key="custom_kpi_analysis_threshold",
        )

    with c6:
        payload_display = st.selectbox(
            "Payload Display",
            ["Line", "Stacked"],
            index=0,
            disabled=("Payload" not in selected),
            key="custom_kpi_analysis_payload_display",
            help=(
                "Line shows a separate Payload trend for each selected site. "
                "Stacked shows each site's Payload as a different colored "
                "filled area, similar to the reference chart."
            ),
        )

    # Threshold is configurable instead of hard-coded.
    threshold_default = 99.0
    if primary_kpi.upper() in {"SSSR", "RRC SETUP SR", "E-RAB SETUP SR", "S1 SETUP SR"}:
        threshold_default = 99.0
    elif "AVAILABILITY" in primary_kpi.upper():
        threshold_default = 99.0
    elif "TTI" in primary_kpi.upper():
        threshold_default = 35.0

    threshold = st.number_input(
        f"{primary_kpi} Threshold",
        value=float(threshold_default),
        step=0.5,
        key="custom_kpi_analysis_threshold_value",
    )

    # ------------------------------------------------------------
    # KPI CHART STATUS FILTER
    # ------------------------------------------------------------
    # Keep the existing "All" behavior and add:
    #   - Meet Only
    #   - Not Meet Only
    #
    # Status is determined from the PRIMARY KPI and the configured
    # threshold. For a Cell-level trend chart, a cell is classified by
    # its latest available primary-KPI value in the current filtered
    # analysis period. This gives a stable cell-level filter while the
    # chart itself still shows the complete trend for that cell.
    #
    # For Site Level, the filter is kept available but does not remove
    # the aggregated site series.
    status_filter_col, status_info_col = st.columns(
        [1.35, 3.65],
        gap="small",
    )

    with status_filter_col:
        chart_status_filter = st.selectbox(
            "Chart Cell Status",
            [
                "All",
                "Meet Only",
                "Not Meet Only",
            ],
            key="custom_kpi_analysis_chart_status_filter",
            help=(
                "All = show all selected cells. "
                "Meet Only = show cells whose latest primary KPI "
                "meets the threshold. "
                "Not Meet Only = show cells whose latest primary KPI "
                "does not meet the threshold."
            ),
        )

    with status_info_col:
        st.caption(
            f"Status is based on the latest available {primary_kpi} "
            f"value per Cell Name versus threshold {threshold:g}. "
            "The selected cell's full trend remains visible."
        )

    # ------------------------------------------------------------
    # DATE EVALUATION MODE
    # ------------------------------------------------------------
    # The KPI chart remains date-by-date, while the Result Summary
    # can evaluate the KPI over:
    #   1) Daily              -> one result per date
    #   2) Average Date Range -> average KPI across the selected range
    #   3) Compare 2 Dates    -> compare two selectable date ranges side-by-side
    #
    # This is useful for RNO checks such as:
    #   01-Oct to 03-Oct -> average SSSR vs threshold
    #   01-Sep to 03-Sep vs 01-Oct to 05-Oct -> average KPI for each selected range vs threshold
    # ------------------------------------------------------------
    st.markdown("**Date Evaluation**")

    date_eval_col, date_a_col, date_b_col = st.columns(
        [1.5, 1.35, 1.35],
        gap="small",
    )

    with date_eval_col:
        date_evaluation_mode = st.radio(
            "Evaluation Mode",
            [
                "Daily",
                "Average Date Range",
                "Compare 2 Dates",
            ],
            horizontal=True,
            key="custom_kpi_analysis_date_mode",
            help=(
                "Daily = one result per date. "
                "Average Date Range = average KPI over the selected "
                "date range. Compare 2 Dates = compare two selectable "
                "date ranges; each range can contain one day or multiple days."
            ),
        )

    available_analysis_dates = sorted(
        pd.to_datetime(
            analysis_df["_Date_Day"],
            errors="coerce",
        )
        .dropna()
        .dt.date
        .unique()
    )

    # Force every option to be a native Python datetime.date.
    # This avoids numpy datetime/date objects reaching Streamlit widgets.
    available_analysis_dates = [
        pd.Timestamp(value).date()
        for value in available_analysis_dates
    ]

    compare_date_a = None
    compare_date_b = None

    if date_evaluation_mode == "Compare 2 Dates":
        if len(available_analysis_dates) < 1:
            st.warning(
                "Compare 2 Dates requires at least one available date "
                "in the current KPI Analysis filter."
            )
        else:
            date_options = available_analysis_dates

            def safe_saved_date(key, default_index):
                saved = st.session_state.get(key)

                if isinstance(saved, pd.Timestamp):
                    saved = saved.date()

                if hasattr(saved, "date") and not isinstance(saved, type(None)):
                    try:
                        saved = saved.date()
                    except Exception:
                        pass

                if saved in date_options:
                    return saved

                return date_options[
                    min(
                        max(default_index, 0),
                        len(date_options) - 1,
                    )
                ]

            def date_label(value):
                return pd.Timestamp(value).strftime("%d-%b-%Y")

            # ========================================================
            # DATE A
            # ========================================================
            with date_a_col:
                st.markdown("**Date A**")

                a_start_col, a_end_col = st.columns(
                    2,
                    gap="small",
                )

                with a_start_col:
                    a_start_default = safe_saved_date(
                        "custom_kpi_analysis_compare_a_start",
                        0,
                    )

                    compare_a_start = st.selectbox(
                        "Start",
                        options=date_options,
                        index=date_options.index(a_start_default),
                        format_func=date_label,
                        key="custom_kpi_analysis_compare_a_start",
                    )

                with a_end_col:
                    # Default End = same day as Start for a clean 1-day
                    # comparison. The user can then select any later date.
                    saved_a_end = st.session_state.get(
                        "custom_kpi_analysis_compare_a_end"
                    )

                    if saved_a_end not in date_options:
                        saved_a_end = compare_a_start

                    compare_a_end = st.selectbox(
                        "End",
                        options=date_options,
                        index=date_options.index(saved_a_end),
                        format_func=date_label,
                        key="custom_kpi_analysis_compare_a_end",
                    )

            # ========================================================
            # DATE B
            # ========================================================
            with date_b_col:
                st.markdown("**Date B**")

                b_start_col, b_end_col = st.columns(
                    2,
                    gap="small",
                )

                with b_start_col:
                    b_start_default = safe_saved_date(
                        "custom_kpi_analysis_compare_b_start",
                        len(date_options) - 1,
                    )

                    compare_b_start = st.selectbox(
                        "Start",
                        options=date_options,
                        index=date_options.index(b_start_default),
                        format_func=date_label,
                        key="custom_kpi_analysis_compare_b_start",
                    )

                with b_end_col:
                    saved_b_end = st.session_state.get(
                        "custom_kpi_analysis_compare_b_end"
                    )

                    if saved_b_end not in date_options:
                        saved_b_end = compare_b_start

                    compare_b_end = st.selectbox(
                        "End",
                        options=date_options,
                        index=date_options.index(saved_b_end),
                        format_func=date_label,
                        key="custom_kpi_analysis_compare_b_end",
                    )

            # Always normalize Start <= End.
            compare_a_start, compare_a_end = sorted(
                [compare_a_start, compare_a_end]
            )
            compare_b_start, compare_b_end = sorted(
                [compare_b_start, compare_b_end]
            )

            compare_date_a = (
                compare_a_start,
                compare_a_end,
            )
            compare_date_b = (
                compare_b_start,
                compare_b_end,
            )

    st.caption(
        (
            "Average Date Range uses the current Analysis Date Range."
            if date_evaluation_mode == "Average Date Range"
            else
            (
                "Compare Date A vs Date B. Each side can be one day or "
                "a multi-day range. Same Start/End = single-day comparison."
                if date_evaluation_mode == "Compare 2 Dates"
                else
                "Daily evaluation checks each date independently."
            )
        )
    )

    # ------------------------------------------------------------
    # Prepare filtered multi-site / multi-cell time series.
    # ------------------------------------------------------------
    work = analysis_df[["_Date"]].copy()
    work["_Chart_Date"] = (
        work["_Date"]
        if analysis_is_hourly
        else work["_Date"].dt.normalize()
    )

    selected_columns = {}
    for kpi in selected:
        actual = kpi_actual_columns.get(kpi)
        if not actual or actual not in analysis_df.columns:
            continue

        # Use the same filtered dataframe for both Site Level and Cell Level.
        work[f"__{kpi}"] = parse_kpi_numeric(analysis_df[actual])
        selected_columns[kpi] = f"__{kpi}"

    if not selected_columns:
        st.warning("Selected KPIs have no usable numeric data.")
        return

    agg_dict = {}
    for kpi, tmp_col in selected_columns.items():
        # Site-level aggregation follows the Site Level Summary convention:
        # Payload = SUM, Last TTI Ratio = MAX, other KPI rates/metrics = MEAN.
        kpi_upper = kpi.upper()
        if kpi_upper == "PAYLOAD":
            agg_dict[kpi] = (tmp_col, "sum")
        elif kpi_upper == "LAST TTI RATIO":
            agg_dict[kpi] = (tmp_col, "max")
        else:
            agg_dict[kpi] = (tmp_col, "mean")

    # IMPORTANT FOR CELL-LEVEL COMPARISON:
    # Do NOT collapse all selected cells into one row per date.
    # Keep Cell Name in the grouping key so every cell gets its own
    # trace/color in the KPI Analysis chart.
    if effective_cell_level:
        work["_Analysis_Cell"] = (
            analysis_df["_Cell_Display"]
            .apply(normalize_cell_name)
            .values
        )
        custom_df = (
            work.groupby(
                ["_Chart_Date", "_Analysis_Cell"],
                as_index=False,
            )
            .agg(**agg_dict)
            .sort_values(["_Chart_Date", "_Analysis_Cell"])
        )
    elif effective_sector_level:
        # Sector is mapped upstream from LocalCell ID / existing dashboard logic.
        # Group all cells within the same mapped sector automatically.
        work["_Analysis_Sector"] = (
            analysis_df["_Sector_Display"]
            .fillna("Unknown Sector")
            .astype(str)
            .str.strip()
            .values
        )
        custom_df = (
            work.groupby(
                ["_Chart_Date", "_Analysis_Sector"],
                as_index=False,
            )
            .agg(**agg_dict)
            .sort_values(["_Chart_Date", "_Analysis_Sector"])
        )
    elif (
        not effective_cell_level
        and "Payload" in selected_columns
        and analysis_df["_Site_ID_Search"].nunique() > 1
    ):
        # Multi-site Payload comparison: preserve one independent series per site.
        # Bulk mode still forces exact Cell-level grouping above.
        if analysis_search_mode == "Full eNodeB Name":
            work["_Analysis_Site"] = (
                analysis_df["_eNodeB_Search"]
                .fillna("")
                .astype(str)
                .str.strip()
                .values
            )
        else:
            work["_Analysis_Site"] = (
                analysis_df["_Site_ID_Search"]
                .fillna("")
                .astype(str)
                .str.strip()
                .values
            )
        custom_df = (
            work.groupby(
                ["_Chart_Date", "_Analysis_Site"],
                as_index=False,
            )
            .agg(**agg_dict)
            .sort_values(["_Chart_Date", "_Analysis_Site"])
        )
    else:
        custom_df = (
            work.groupby("_Chart_Date", as_index=False)
            .agg(**agg_dict)
            .sort_values("_Chart_Date")
        )

    if custom_df.empty:
        st.warning("No data available for the selected KPI combination.")
        return

    # ------------------------------------------------------------
    # APPLY CELL MEET / NOT MEET FILTER
    # ------------------------------------------------------------
    # We classify each Cell Name using the latest available PRIMARY KPI
    # value in the current filtered analysis period. We then keep the
    # complete time-series rows for the selected cells so the trend is
    # not truncated to only the points that meet the threshold.
    if (
        effective_cell_level
        and "_Analysis_Cell" in custom_df.columns
        and chart_status_filter != "All"
        and primary_kpi in custom_df.columns
    ):
        status_source = custom_df[
            [
                "_Analysis_Cell",
                "_Chart_Date",
                primary_kpi,
            ]
        ].copy()

        status_source[primary_kpi] = pd.to_numeric(
            status_source[primary_kpi],
            errors="coerce",
        )

        status_source = status_source.dropna(
            subset=[primary_kpi]
        )

        if not status_source.empty:
            latest_status = (
                status_source
                .sort_values(
                    [
                        "_Analysis_Cell",
                        "_Chart_Date",
                    ]
                )
                .groupby(
                    "_Analysis_Cell",
                    as_index=False,
                )
                .tail(1)
            )

            latest_status = latest_status[
                [
                    "_Analysis_Cell",
                    primary_kpi,
                ]
            ].copy()

            # Use native pandas instead of np.where().
            # This dashboard does not import NumPy, and NumPy is not
            # required for this simple Meet / Not Meet classification.
            latest_status["_Status"] = (
                latest_status[primary_kpi]
                .ge(float(threshold))
                .map({
                    True: "Meet",
                    False: "Not Meet",
                })
            )

            if chart_status_filter == "Meet Only":
                allowed_cells = set(
                    latest_status.loc[
                        latest_status["_Status"] == "Meet",
                        "_Analysis_Cell",
                    ]
                    .astype(str)
                )
            else:
                allowed_cells = set(
                    latest_status.loc[
                        latest_status["_Status"] == "Not Meet",
                        "_Analysis_Cell",
                    ]
                    .astype(str)
                )

            custom_df = custom_df[
                custom_df["_Analysis_Cell"]
                .astype(str)
                .isin(allowed_cells)
            ].copy()

            if custom_df.empty:
                st.info(
                    f"No Cell Name is classified as "
                    f"'{chart_status_filter}' based on the latest "
                    f"{primary_kpi} value in the selected analysis period."
                )
                return

            st.caption(
                f"📌 Chart filter: **{chart_status_filter}** — "
                f"{len(allowed_cells):,} Cell Name(s) shown. "
                f"Classification uses the latest {primary_kpi} value "
                f"vs threshold {threshold:g}; the full trend remains visible."
            )

    # Detect hourly vs daily display.
    if analysis_is_hourly:
        tickformat = "%H:%M<br>%d-%b"
        hoverformat = "%d-%b-%Y %H:%M"
        dtick = 6 * 60 * 60 * 1000
    else:
        tickformat = "%d-%b-%y"
        hoverformat = "%d-%b-%Y"
        dtick = None

    # ------------------------------------------------------------
    # Chart factory.
    # ------------------------------------------------------------
    def make_custom_figure(kpis, title, include_threshold=False):
        """
        Build a KPI diagnostic combo chart.

        IMPORTANT:
        - With exactly 2 different KPIs, each KPI gets its OWN Y axis.
          KPI #1 -> left axis (y)
          KPI #2 -> right axis (y2)
        - This is intentional for diagnostic analysis: e.g.
          Payload vs Availability, SSSR vs RRC Setup SR, or
          RANK2 Rate vs Average TA can be compared without one KPI
          flattening the other because their numeric scales differ.
        - Payload can be rendered as either BAR or LINE using the
          Payload Display selector.
        - For Cell Level comparison, LINE is recommended because each
          selected Cell Name remains identifiable without stacked/overlapping
          bars.
        """
        fig = go.Figure()

        valid_kpis = [
            k for k in kpis
            if k in custom_df.columns
        ]

        if not valid_kpis:
            return fig

        # ------------------------------------------------------------
        # TWO-KPI DIAGNOSTIC MODE
        # ------------------------------------------------------------
        # This is the key fix requested by the user:
        # two different KPIs MUST NOT share the same numeric axis.
        if len(valid_kpis) == 2:
            kpi_left = valid_kpis[0]
            kpi_right = valid_kpis[1]

            def _numeric_values(k):
                return pd.to_numeric(
                    custom_df[k], errors="coerce"
                )

            # --------------------------------------------------------
            # Cell Level: one color per Cell Name.
            #
            # Both KPIs belonging to the same cell use the SAME color,
            # while the KPI itself is distinguished by line style:
            #   solid = KPI #1
            #   dash  = KPI #2
            #
            # Legend explicitly contains:
            #   CELL_NAME — KPI
            #
            # This prevents the old problem where all selected cells
            # were aggregated into one line and it became impossible
            # to identify which cell was responsible.
            # --------------------------------------------------------
            if analysis_scope == "Cell Level" and "_Analysis_Cell" in custom_df.columns:
                cells = [
                    c for c in custom_df["_Analysis_Cell"]
                    .dropna()
                    .astype(str)
                    .unique()
                ]

                # Stable, deterministic palette. Plotly's standard
                # qualitative palette is used only to distinguish cells.
                cell_colors = (
                    px.colors.qualitative.Plotly
                    + px.colors.qualitative.D3
                    + px.colors.qualitative.Safe
                    + px.colors.qualitative.Dark24
                )
                color_map = {
                    cell: cell_colors[i % len(cell_colors)]
                    for i, cell in enumerate(sorted(cells))
                }

                all_left = pd.to_numeric(
                    custom_df[kpi_left], errors="coerce"
                )
                all_right = pd.to_numeric(
                    custom_df[kpi_right], errors="coerce"
                )

                def _axis_range(values):
                    values = values.dropna()
                    if values.empty:
                        return [0, 1]

                    vmin = float(values.min())
                    vmax = float(values.max())

                    if 0 <= vmin and vmax <= 105:
                        return [0, max(100.0, vmax * 1.08)]

                    span = vmax - vmin
                    pad = max(
                        span * 0.08,
                        abs(vmax) * 0.05,
                        1.0,
                    )
                    return [vmin - pad, vmax + pad]

                left_range = _axis_range(all_left)
                right_range = _axis_range(all_right)

                left_is_payload = kpi_left.upper() == "PAYLOAD"
                right_is_payload = kpi_right.upper() == "PAYLOAD"

                for cell in sorted(cells):
                    cell_df = custom_df[
                        custom_df["_Analysis_Cell"].astype(str) == str(cell)
                    ].sort_values("_Chart_Date")

                    cell_color = color_map[cell]

                    # KPI #1 -> LEFT AXIS
                    if left_is_payload and payload_display == "Stacked":
                        fig.add_trace(
                            go.Bar(
                                x=cell_df["_Chart_Date"],
                                y=pd.to_numeric(
                                    cell_df[kpi_left],
                                    errors="coerce",
                                ),
                                name=f"{cell} — {kpi_left}",
                                legendgroup=cell,
                                marker_color=cell_color,
                                marker_line_width=0,
                                opacity=0.30,
                                yaxis="y",
                            )
                        )
                    else:
                        fig.add_trace(
                            go.Scatter(
                                x=cell_df["_Chart_Date"],
                                y=pd.to_numeric(
                                    cell_df[kpi_left],
                                    errors="coerce",
                                ),
                                name=f"{cell} — {kpi_left}",
                                legendgroup=cell,
                                mode="lines+markers",
                                line=dict(
                                    color=cell_color,
                                    width=7 if kpi_left == primary_kpi else 5,
                                    dash=(
                                        "dash"
                                        if left_is_payload
                                        else "solid"
                                    ),
                                ),
                                marker=dict(
                                    size=7 if kpi_left == primary_kpi else 6,
                                    color=cell_color,
                                ),
                                connectgaps=True,
                                yaxis="y",
                            )
                        )

                    # KPI #2 -> RIGHT AXIS
                    if right_is_payload and payload_display == "Stacked":
                        fig.add_trace(
                            go.Bar(
                                x=cell_df["_Chart_Date"],
                                y=pd.to_numeric(
                                    cell_df[kpi_right],
                                    errors="coerce",
                                ),
                                name=f"{cell} — {kpi_right}",
                                legendgroup=cell,
                                marker_color=cell_color,
                                marker_line_width=0,
                                opacity=0.30,
                                yaxis="y2",
                            )
                        )
                    else:
                        fig.add_trace(
                            go.Scatter(
                                x=cell_df["_Chart_Date"],
                                y=pd.to_numeric(
                                    cell_df[kpi_right],
                                    errors="coerce",
                                ),
                                name=f"{cell} — {kpi_right}",
                                legendgroup=cell,
                                mode="lines+markers",
                                line=dict(
                                    color=cell_color,
                                    width=7 if kpi_right == primary_kpi else 5,
                                    dash=(
                                        "dash"
                                        if right_is_payload
                                        else "dash"
                                    ),
                                ),
                                marker=dict(
                                    size=7 if kpi_right == primary_kpi else 6,
                                    color=cell_color,
                                ),
                                connectgaps=True,
                                yaxis="y2",
                            )
                        )

                if include_threshold:
                    threshold_axis = (
                        "y"
                        if primary_kpi == kpi_left
                        else "y2"
                    )
                    fig.add_hline(
                        y=float(threshold),
                        line=dict(
                            color="red",
                            width=2,
                            dash="dash",
                        ),
                        annotation_text=(
                            f"{primary_kpi} Threshold "
                            f"{threshold:g}"
                            + (
                                "%"
                                if primary_kpi.upper() != "PAYLOAD"
                                else ""
                            )
                        ),
                        annotation_position="top left",
                        yref=threshold_axis,
                    )

                custom_x_pad = _bar_xaxis_padding(
                    custom_df["_Chart_Date"]
                )
                custom_x_range = [
                    custom_df["_Chart_Date"].min() - custom_x_pad,
                    custom_df["_Chart_Date"].max() + custom_x_pad,
                ]

                def _axis_title(kpi):
                    if kpi.upper() == "PAYLOAD":
                        return "Payload (GB)"
                    if (
                        "AVAILABILITY" in kpi.upper()
                        or "%" in kpi.upper()
                        or "RATE" in kpi.upper()
                        or "SSSR" in kpi.upper()
                    ):
                        return f"{kpi} (%)"
                    return kpi

                fig.update_layout(
                    title=title,
                    height=520,
                    template="plotly_white",
                    margin=dict(l=58, r=68, t=55, b=150),
                    hovermode="x unified",
                    barmode="overlay",
                    xaxis=dict(
                        title="Date / Time",
                        tickformat=tickformat,
                        hoverformat=hoverformat,
                        dtick=dtick,
                        showgrid=True,
                        gridcolor="#e5e5e5",
                        automargin=True,
                        range=custom_x_range,
                        autorange=False,
                    ),
                    yaxis=dict(
                        title=_axis_title(kpi_left),
                        range=left_range,
                        showgrid=True,
                        gridcolor="#e5e5e5",
                        automargin=True,
                        side="left",
                    ),
                    yaxis2=dict(
                        title=_axis_title(kpi_right),
                        range=right_range,
                        overlaying="y",
                        side="right",
                        showgrid=False,
                        automargin=True,
                    ),
                    legend=dict(
                        orientation="h",
                        yanchor="top",
                        y=-0.22,
                        xanchor="center",
                        x=0.5,
                        traceorder="normal",
                    ),
                )

                return fig

            # --------------------------------------------------------
            # Site Level: retain the previous site-level double-axis
            # behavior (one aggregated series per KPI).
            # --------------------------------------------------------
            def _axis_range(values):
                values = values.dropna()
                if values.empty:
                    return [0, 1]

                vmin = float(values.min())
                vmax = float(values.max())

                if 0 <= vmin and vmax <= 105:
                    return [0, max(100.0, vmax * 1.08)]

                span = vmax - vmin
                pad = max(
                    span * 0.08,
                    abs(vmax) * 0.05,
                    1.0,
                )
                return [vmin - pad, vmax + pad]

            left_values = _numeric_values(kpi_left)
            right_values = _numeric_values(kpi_right)

            left_range = _axis_range(left_values)
            right_range = _axis_range(right_values)

            left_is_payload = kpi_left.upper() == "PAYLOAD"
            right_is_payload = kpi_right.upper() == "PAYLOAD"

            if left_is_payload and payload_display == "Stacked":
                fig.add_trace(
                    go.Bar(
                        x=custom_df["_Chart_Date"],
                        y=left_values,
                        name=kpi_left,
                        yaxis="y",
                        opacity=0.45,
                        marker_line_width=0,
                    )
                )
            else:
                fig.add_trace(
                    go.Scatter(
                        x=custom_df["_Chart_Date"],
                        y=left_values,
                        name=kpi_left,
                        yaxis="y",
                        mode="lines+markers",
                        line=dict(
                            width=7 if kpi_left == primary_kpi else 5,
                            dash="dash" if left_is_payload else "solid",
                        ),
                        marker=dict(
                            size=7 if kpi_left == primary_kpi else 6
                        ),
                        connectgaps=True,
                    )
                )

            if right_is_payload and payload_display == "Stacked":
                fig.add_trace(
                    go.Bar(
                        x=custom_df["_Chart_Date"],
                        y=right_values,
                        name=kpi_right,
                        yaxis="y2",
                        opacity=0.45,
                        marker_line_width=0,
                    )
                )
            else:
                fig.add_trace(
                    go.Scatter(
                        x=custom_df["_Chart_Date"],
                        y=right_values,
                        name=kpi_right,
                        yaxis="y2",
                        mode="lines+markers",
                        line=dict(
                            width=7 if kpi_right == primary_kpi else 5,
                            dash="dash" if right_is_payload else "dash",
                        ),
                        marker=dict(
                            size=7 if kpi_right == primary_kpi else 6
                        ),
                        connectgaps=True,
                    )
                )

            if include_threshold:
                threshold_axis = (
                    "y" if primary_kpi == kpi_left else "y2"
                )
                fig.add_hline(
                    y=float(threshold),
                    line=dict(
                        color="red",
                        width=2,
                        dash="dash",
                    ),
                    annotation_text=(
                        f"{primary_kpi} Threshold "
                        f"{threshold:g}"
                        + (
                            "%"
                            if primary_kpi.upper() != "PAYLOAD"
                            else ""
                        )
                    ),
                    annotation_position="top left",
                    yref=threshold_axis,
                )

            custom_x_pad = _bar_xaxis_padding(
                custom_df["_Chart_Date"]
            )
            custom_x_range = [
                custom_df["_Chart_Date"].min() - custom_x_pad,
                custom_df["_Chart_Date"].max() + custom_x_pad,
            ]

            def _axis_title(kpi):
                if kpi.upper() == "PAYLOAD":
                    return "Payload (GB)"
                if (
                    "AVAILABILITY" in kpi.upper()
                    or "%" in kpi.upper()
                    or "RATE" in kpi.upper()
                    or "SSSR" in kpi.upper()
                ):
                    return f"{kpi} (%)"
                return kpi

            fig.update_layout(
                title=title,
                height=430,
                template="plotly_white",
                margin=dict(
                    l=58, r=68, t=55, b=90
                ),
                hovermode="x unified",
                barmode="overlay",
                xaxis=dict(
                    title="Date / Time",
                    tickformat=tickformat,
                    hoverformat=hoverformat,
                    dtick=dtick,
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                    range=custom_x_range,
                    autorange=False,
                ),
                yaxis=dict(
                    title=_axis_title(kpi_left),
                    range=left_range,
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                    side="left",
                ),
                yaxis2=dict(
                    title=_axis_title(kpi_right),
                    range=right_range,
                    overlaying="y",
                    side="right",
                    showgrid=False,
                    automargin=True,
                ),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.20,
                    xanchor="center",
                    x=0.5,
                ),
            )

            return fig

        # ------------------------------------------------------------
        # SINGLE-KPI CELL-LEVEL MODE
        # ------------------------------------------------------------
        # When the user selects only one KPI at Cell Level, NEVER aggregate
        # all selected cells into one series. Each Cell Name gets its own
        # trace and legend entry so a degraded point can immediately be
        # traced back to the responsible cell.
        #
        # Site Level keeps the existing aggregated single-KPI behavior
        # only when there is NO exact Bulk target list. In Bulk mode,
        # effective_cell_level is True and every requested Cell Name gets
        # its own trace/legend entry.
        # ------------------------------------------------------------
        if (
            len(valid_kpis) == 1
            and effective_sector_level
            and "_Analysis_Sector" in custom_df.columns
        ):
            single_kpi = valid_kpis[0]
            sectors = [
                value
                for value in custom_df["_Analysis_Sector"]
                .dropna()
                .astype(str)
                .unique()
            ]
            sector_colors = (
                px.colors.qualitative.Plotly
                + px.colors.qualitative.D3
                + px.colors.qualitative.Safe
                + px.colors.qualitative.Dark24
            )
            sector_color_map = {
                sector: sector_colors[i % len(sector_colors)]
                for i, sector in enumerate(sorted(sectors))
            }
            for sector in sorted(sectors):
                sector_df = custom_df[
                    custom_df["_Analysis_Sector"].astype(str) == str(sector)
                ].sort_values("_Chart_Date")
                fig.add_trace(
                    go.Scatter(
                        x=sector_df["_Chart_Date"],
                        y=pd.to_numeric(sector_df[single_kpi], errors="coerce"),
                        name=f"{sector} — {single_kpi}",
                        legendgroup=sector,
                        mode="lines+markers",
                        line=dict(
                            color=sector_color_map[sector],
                            width=7 if single_kpi == primary_kpi else 5,
                        ),
                        marker=dict(
                            color=sector_color_map[sector],
                            size=7,
                        ),
                        connectgaps=True,
                        yaxis="y",
                    )
                )

            # Existing axis/threshold/legend layout below can be reused.
            if include_threshold:
                fig.add_hline(
                    y=float(threshold),
                    line=dict(color="red", width=2, dash="dash"),
                    annotation_text=f"{primary_kpi} Threshold {threshold:g}",
                    annotation_position="top left",
                    yref="y",
                )
            sector_values = pd.to_numeric(custom_df[single_kpi], errors="coerce").dropna()
            if sector_values.empty:
                sector_axis_range = [0, 1]
            else:
                sector_min = float(sector_values.min())
                sector_max = float(sector_values.max())
                sector_axis_range = (
                    [0, max(100.0, sector_max * 1.08)]
                    if 0 <= sector_min and sector_max <= 105
                    else [
                        sector_min - max((sector_max-sector_min)*0.08, 1.0),
                        sector_max + max((sector_max-sector_min)*0.08, 1.0),
                    ]
                )
            fig.update_layout(
                title=title,
                height=430,
                template="plotly_white",
                margin=dict(l=58, r=58, t=55, b=110),
                hovermode="x unified",
                xaxis=dict(title="Date / Time", tickformat=tickformat, hoverformat=hoverformat, dtick=dtick, showgrid=True, gridcolor="#e5e5e5", automargin=True),
                yaxis=dict(title="Payload (GB)" if single_kpi.upper() == "PAYLOAD" else single_kpi, range=sector_axis_range, showgrid=True, gridcolor="#e5e5e5", automargin=True),
                legend=dict(orientation="h", yanchor="top", y=-0.22, xanchor="center", x=0.5),
            )
            return fig

        if (
            len(valid_kpis) == 1
            and valid_kpis[0].upper() == "PAYLOAD"
            and not effective_cell_level
            and not effective_sector_level
            and "_Analysis_Site" in custom_df.columns
        ):
            payload_kpi = valid_kpis[0]
            site_names = sorted(
                value for value in
                custom_df["_Analysis_Site"].dropna().astype(str).unique()
                if value
            )
            palette = (
                px.colors.qualitative.Plotly
                + px.colors.qualitative.D3
                + px.colors.qualitative.Safe
                + px.colors.qualitative.Dark24
            )
            site_color_map = {
                site: palette[i % len(palette)]
                for i, site in enumerate(site_names)
            }
            for site in site_names:
                site_series = custom_df[
                    custom_df["_Analysis_Site"].astype(str) == site
                ].sort_values("_Chart_Date")
                y_values = pd.to_numeric(
                    site_series[payload_kpi], errors="coerce"
                )
                if payload_display == "Stacked":
                    fig.add_trace(
                        go.Scatter(
                            x=site_series["_Chart_Date"],
                            y=y_values,
                            name=site,
                            legendgroup=site,
                            mode="lines",
                            line=dict(color=site_color_map[site], width=1.5),
                            stackgroup="payload",
                            groupnorm=None,
                            hovertemplate=(
                                "Date: %{x}<br>Site: " + site
                                + "<br>Payload: %{y:.2f} GB<extra></extra>"
                            ),
                        )
                    )
                else:
                    fig.add_trace(
                        go.Scatter(
                            x=site_series["_Chart_Date"],
                            y=y_values,
                            name=site,
                            legendgroup=site,
                            mode="lines+markers",
                            line=dict(color=site_color_map[site], width=2.5),
                            marker=dict(color=site_color_map[site], size=5),
                            connectgaps=False,
                            hovertemplate=(
                                "Date: %{x}<br>Site: " + site
                                + "<br>Payload: %{y:.2f} GB<extra></extra>"
                            ),
                        )
                    )

            fig.update_layout(
                title=title,
                height=480,
                template="plotly_white",
                margin=dict(l=55, r=55, t=55, b=105),
                hovermode="x unified",
                xaxis=dict(
                    title="Date / Time",
                    tickformat=tickformat,
                    hoverformat=hoverformat,
                    dtick=dtick,
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                ),
                yaxis=dict(
                    title="Payload (GB)",
                    rangemode="tozero",
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                ),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.22,
                    xanchor="center",
                    x=0.5,
                    title="Site",
                ),
            )
            return fig

        if (
            len(valid_kpis) == 1
            and effective_cell_level
            and "_Analysis_Cell" in custom_df.columns
        ):
            single_kpi = valid_kpis[0]

            cells = [
                c
                for c in custom_df["_Analysis_Cell"]
                .dropna()
                .astype(str)
                .unique()
            ]

            cell_colors = (
                px.colors.qualitative.Plotly
                + px.colors.qualitative.D3
                + px.colors.qualitative.Safe
                + px.colors.qualitative.Dark24
            )
            color_map = {
                cell: cell_colors[i % len(cell_colors)]
                for i, cell in enumerate(sorted(cells))
            }

            all_values = pd.to_numeric(
                custom_df[single_kpi],
                errors="coerce",
            )

            values_for_range = all_values.dropna()
            if values_for_range.empty:
                axis_range = [0, 1]
            else:
                vmin = float(values_for_range.min())
                vmax = float(values_for_range.max())

                if 0 <= vmin and vmax <= 105:
                    axis_range = [0, max(100.0, vmax * 1.08)]
                else:
                    span = vmax - vmin
                    pad = max(
                        span * 0.08,
                        abs(vmax) * 0.05,
                        1.0,
                    )
                    axis_range = [
                        vmin - pad,
                        vmax + pad,
                    ]

            is_payload = single_kpi.upper() == "PAYLOAD"

            for cell in sorted(cells):
                cell_df = custom_df[
                    custom_df["_Analysis_Cell"].astype(str) == str(cell)
                ].sort_values("_Chart_Date")

                cell_values = pd.to_numeric(
                    cell_df[single_kpi],
                    errors="coerce",
                )
                cell_color = color_map[cell]

                if is_payload and payload_display == "Stacked":
                    fig.add_trace(
                        go.Bar(
                            x=cell_df["_Chart_Date"],
                            y=cell_values,
                            name=f"{cell} — {single_kpi}",
                            legendgroup=cell,
                            marker_color=cell_color,
                            marker_line_width=0,
                            opacity=0.38,
                            yaxis="y",
                        )
                    )
                else:
                    fig.add_trace(
                        go.Scatter(
                            x=cell_df["_Chart_Date"],
                            y=cell_values,
                            name=f"{cell} — {single_kpi}",
                            legendgroup=cell,
                            mode="lines+markers",
                            line=dict(
                                color=cell_color,
                                width=7 if single_kpi == primary_kpi else 5,
                            ),
                            marker=dict(
                                size=7 if single_kpi == primary_kpi else 6,
                                color=cell_color,
                            ),
                            connectgaps=True,
                            yaxis="y",
                        )
                    )

            if include_threshold:
                fig.add_hline(
                    y=float(threshold),
                    line=dict(
                        color="red",
                        width=2,
                        dash="dash",
                    ),
                    annotation_text=(
                        f"{primary_kpi} Threshold "
                        f"{threshold:g}"
                        + (
                            "%"
                            if primary_kpi.upper() != "PAYLOAD"
                            else ""
                        )
                    ),
                    annotation_position="top left",
                    yref="y",
                )

            custom_x_pad = _bar_xaxis_padding(
                custom_df["_Chart_Date"]
            )
            custom_x_range = [
                custom_df["_Chart_Date"].min() - custom_x_pad,
                custom_df["_Chart_Date"].max() + custom_x_pad,
            ]

            def _single_axis_title(kpi):
                if kpi.upper() == "PAYLOAD":
                    return "Payload (GB)"
                if (
                    "AVAILABILITY" in kpi.upper()
                    or "%" in kpi.upper()
                    or "RATE" in kpi.upper()
                    or "SSSR" in kpi.upper()
                ):
                    return f"{kpi} (%)"
                return kpi

            fig.update_layout(
                title=title,
                height=max(430, 430 + (len(cells) // 3) * 24),
                template="plotly_white",
                margin=dict(
                    l=58,
                    r=58,
                    t=55,
                    b=150,
                ),
                hovermode="x unified",
                barmode="overlay",
                xaxis=dict(
                    title="Date / Time",
                    tickformat=tickformat,
                    hoverformat=hoverformat,
                    dtick=dtick,
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                    range=custom_x_range,
                    autorange=False,
                ),
                yaxis=dict(
                    title=_single_axis_title(single_kpi),
                    range=axis_range,
                    showgrid=True,
                    gridcolor="#e5e5e5",
                    automargin=True,
                    side="left",
                ),
                legend=dict(
                    orientation="h",
                    yanchor="top",
                    y=-0.22,
                    xanchor="center",
                    x=0.5,
                    traceorder="normal",
                ),
            )

            return fig

        # ------------------------------------------------------------
        # FALLBACK FOR 1 OR 3+ KPIs
        # ------------------------------------------------------------
        # Keep the existing behavior for combinations other than exactly
        # two KPIs. Payload uses the Line/Stacked display selector.
        non_payload_kpis = [
            k for k in valid_kpis
            if k.upper() != "PAYLOAD"
        ]
        has_payload = any(
            k.upper() == "PAYLOAD"
            for k in valid_kpis
        )

        if has_payload:
            payload_kpi = next(
                k for k in valid_kpis
                if k.upper() == "PAYLOAD"
            )
            payload_values = pd.to_numeric(
                custom_df[payload_kpi],
                errors="coerce",
            )

            if payload_display == "Stacked":
                fig.add_trace(
                    go.Bar(
                        x=custom_df["_Chart_Date"],
                        y=payload_values,
                        name=payload_kpi,
                        yaxis="y2",
                        opacity=0.38,
                        marker_line_width=0,
                    )
                )
            else:
                fig.add_trace(
                    go.Scatter(
                        x=custom_df["_Chart_Date"],
                        y=payload_values,
                        name=payload_kpi,
                        yaxis="y2",
                        mode="lines+markers",
                        line=dict(
                            width=7 if payload_kpi == primary_kpi else 5,
                            dash="dash",
                        ),
                        marker=dict(
                            size=7 if payload_kpi == primary_kpi else 6,
                        ),
                        connectgaps=True,
                    )
                )

        for idx, kpi in enumerate(non_payload_kpis):
            width = 7 if kpi == primary_kpi else 5
            marker_size = 7 if kpi == primary_kpi else 6

            fig.add_trace(
                go.Scatter(
                    x=custom_df["_Chart_Date"],
                    y=pd.to_numeric(
                        custom_df[kpi],
                        errors="coerce",
                    ),
                    name=kpi,
                    yaxis="y",
                    mode="lines+markers",
                    line=dict(width=width),
                    marker=dict(size=marker_size),
                    connectgaps=True,
                )
            )

        has_availability = any(
            "AVAILABILITY" in k.upper()
            for k in non_payload_kpis
        )

        if has_availability:
            left_axis_range = [0, 105]
            left_axis_title = "KPI (%)"
        elif non_payload_kpis:
            vals = pd.concat(
                [
                    pd.to_numeric(
                        custom_df[k],
                        errors="coerce",
                    )
                    for k in non_payload_kpis
                ],
                ignore_index=True,
            ).dropna()

            if len(vals):
                vmax = float(vals.max())
                left_axis_range = [
                    0,
                    max(100.0, vmax * 1.08),
                ]
            else:
                left_axis_range = [0, 100]

            left_axis_title = "KPI"
        else:
            left_axis_range = [0, 1]
            left_axis_title = ""

        if include_threshold:
            threshold_ref = (
                "y2"
                if primary_kpi.upper() == "PAYLOAD"
                and has_payload
                else "y"
            )

            fig.add_hline(
                y=float(threshold),
                line=dict(
                    color="red",
                    width=2,
                    dash="dash",
                ),
                annotation_text=(
                    f"{primary_kpi} Threshold "
                    f"{threshold:g}"
                ),
                annotation_position="top left",
                yref=threshold_ref,
            )

        if has_payload and not custom_df.empty:
            custom_x_pad = _bar_xaxis_padding(
                custom_df["_Chart_Date"]
            )
            custom_x_range = [
                custom_df["_Chart_Date"].min()
                - custom_x_pad,
                custom_df["_Chart_Date"].max()
                + custom_x_pad,
            ]
        else:
            custom_x_range = None

        fig.update_layout(
            title=title,
            height=430,
            template="plotly_white",
            margin=dict(
                l=48, r=58, t=55, b=90
            ),
            hovermode="x unified",
            barmode="overlay",
            xaxis=dict(
                title="Date / Time",
                tickformat=tickformat,
                hoverformat=hoverformat,
                dtick=dtick,
                showgrid=True,
                gridcolor="#e5e5e5",
                automargin=True,
                range=custom_x_range,
                autorange=(
                    False
                    if custom_x_range is not None
                    else True
                ),
            ),
            yaxis=dict(
                title=left_axis_title,
                range=left_axis_range,
                showgrid=True,
                gridcolor="#e5e5e5",
                automargin=True,
            ),
            yaxis2=dict(
                title="Payload (GB)" if has_payload else "",
                overlaying="y",
                side="right",
                showgrid=False,
                automargin=True,
            ),
            legend=dict(
                orientation="h",
                yanchor="top",
                y=-0.20,
                xanchor="center",
                x=0.5,
            ),
        )

        return fig

    related = [k for k in selected if k != primary_kpi]

    if chart_mode == "2 Charts — Primary + Related":
        fig_primary = make_custom_figure(
            [primary_kpi],
            f"{primary_kpi} — Primary KPI",
            include_threshold=show_threshold,
        )
        fig_related = make_custom_figure(
            related,
            f"{primary_kpi} — Related KPI Drill-down",
            include_threshold=False,
        )

        _download_figures.append(fig_primary)
        _download_figures.append(fig_related)

        left, right = st.columns(2, gap="small")
        with left:
            st.plotly_chart(
                fig_primary,
                use_container_width=True,
                key="custom_kpi_analysis_primary_chart",
            )
        with right:
            st.plotly_chart(
                fig_related,
                use_container_width=True,
                key="custom_kpi_analysis_related_chart",
            )

    else:
        # This branch handles both:
        #   - exactly 1 selected KPI
        #   - the existing "1 Combined Chart" mode for 2+ KPIs
        fig_combined = make_custom_figure(
            selected,
            f"{primary_kpi} — Combined KPI Analysis"
            if len(selected) > 1
            else f"{primary_kpi} — KPI Analysis",
            include_threshold=show_threshold,
        )

        _download_figures.append(fig_combined)

        st.plotly_chart(
            fig_combined,
            use_container_width=True,
            key=(
                "custom_kpi_analysis_single_chart"
                if len(selected) == 1
                else "custom_kpi_analysis_combined_chart"
            ),
        )


    # ============================================================
    # KPI RESULT SUMMARY — DATE-AWARE THRESHOLD / MEET / NOT MEET
    # ============================================================
    #
    # Excel-like RNO summary with three evaluation modes:
    #
    # Daily:
    #   Date | KPI | Remark | Threshold
    #
    # Average Date Range:
    #   01-Oct -> 03-Oct = average KPI for the selected cell/site
    #
    # Compare 2 Dates:
    #   Date A KPI | Remark A | Date B KPI | Remark B | Threshold
    #
    # The summary keeps the same entity grain as the KPI Analysis:
    # Cell Level  -> Cell Name + LocalCell Id + Sector + FreqBand
    # Site Level  -> eNodeB Name + FreqBand
    # ============================================================

    summary_actual_col = kpi_actual_columns.get(primary_kpi)

    if summary_actual_col and summary_actual_col in analysis_df.columns:

        summary_source = analysis_df.copy()

        summary_source["_KPI_Result_Value"] = parse_kpi_numeric(
            summary_source[summary_actual_col]
        )

        summary_source["_Summary_Date"] = pd.to_datetime(
            summary_source["_Date_Day"],
            errors="coerce",
        ).dt.normalize()

        # Higher is better for accessibility, availability, mobility,
        # throughput, CQI, etc.
        #
        # Lower is better for drop, failure, utilization, latency,
        # TA, packet loss, and interference-style KPIs.
        lower_is_better_keywords = (
            "DROP",
            "FAIL",
            "ABNORMAL RELEASE",
            "PRB",
            "LATENCY",
            "PACKET LOSS",
            "AVERAGE TA",
            "TA DISTRIBUTION",
            "INTERFERENCE",
            "LAST TTI",
        )

        primary_upper = primary_kpi.upper()

        lower_is_better = any(
            keyword in primary_upper
            for keyword in lower_is_better_keywords
        )

        if primary_upper == "PAYLOAD":
            lower_is_better = False

        # --------------------------------------------------------
        # Entity/grouping columns.
        # --------------------------------------------------------
        identity_cols = [
            enodeb_col,
        ]

        rename_map = {
            enodeb_col: "eNodeB Name",
            localcell_col: "LocalCell Id",
            "_Cell_Display": "Cell Name",
            "_Sector_Display": "Sector",
            "_FreqBand": "FreqBand",
        }

        # IMPORTANT:
        # Bulk exact targets are always evaluated at CELL grain,
        # regardless of the UI Analysis Scope selection.
        #
        # A bulk row represents:
        #   Cell Name + Site/eNodeB + FreqBand
        #
        # Therefore the KPI Result Summary must never collapse these
        # targets to Site/eNodeB grain. Keep Cell Name, LocalCell Id,
        # Sector and FreqBand in the grouping so the RNO user can
        # identify the exact degraded cell.
        if analysis_cell_site_pairs:
            identity_cols.extend([
                "_Cell_Display",
                localcell_col,
                "_Sector_Display",
                "_FreqBand",
            ])
        elif effective_cell_level:
            identity_cols.extend([
                "_Cell_Display",
                localcell_col,
                "_Sector_Display",
                "_FreqBand",
            ])
        else:
            identity_cols.extend([
                "_FreqBand",
            ])

        identity_cols = [
            col
            for col in identity_cols
            if col in summary_source.columns
        ]

        # Same aggregation convention used by KPI Analysis.
        if primary_upper == "PAYLOAD":
            summary_agg = "sum"
        elif primary_upper == "LAST TTI RATIO":
            summary_agg = "max"
        else:
            summary_agg = "mean"

        def evaluate_remark(value):
            if pd.isna(value):
                return "No Data"

            if lower_is_better:
                return (
                    "Meet"
                    if float(value) <= float(threshold)
                    else "Not Meet"
                )

            return (
                "Meet"
                if float(value) >= float(threshold)
                else "Not Meet"
            )

        def style_remark(value):
            if value == "Meet":
                return (
                    "color: #008000; "
                    "font-weight: 700;"
                )
            if value == "Not Meet":
                return (
                    "color: #FF0000; "
                    "font-weight: 700;"
                )
            if value == "No Data":
                return (
                    "color: #777777; "
                    "font-weight: 700;"
                )
            return ""

        # --------------------------------------------------------
        # Aggregate raw data at the requested date level.
        # --------------------------------------------------------
        valid_summary_source = summary_source.dropna(
            subset=[
                "_Summary_Date",
                "_KPI_Result_Value",
            ]
        ).copy()

        # --------------------------------------------------------
        # MODE 1 — DAILY
        # --------------------------------------------------------
        if date_evaluation_mode == "Daily":

            summary_df = (
                valid_summary_source
                .groupby(
                    identity_cols + ["_Summary_Date"],
                    as_index=False,
                    dropna=False,
                )["_KPI_Result_Value"]
                .agg(summary_agg)
                .rename(
                    columns={
                        "_KPI_Result_Value": primary_kpi,
                        "_Summary_Date": "Date",
                        **rename_map,
                    }
                )
            )

            if not summary_df.empty:

                summary_df["Remark"] = summary_df[
                    primary_kpi
                ].apply(evaluate_remark)

                summary_df["Threshold"] = float(threshold)

                preferred_summary_cols = [
                    "Cell Name",
                    "LocalCell Id",
                    "Sector",
                    "FreqBand",
                    "eNodeB Name",
                    "Date",
                    primary_kpi,
                    "Remark",
                    "Threshold",
                ]

                summary_cols = [
                    col
                    for col in preferred_summary_cols
                    if col in summary_df.columns
                ]

                summary_df = summary_df[summary_cols].sort_values(
                    [
                        col
                        for col in [
                            "Date",
                            "Sector",
                            "FreqBand",
                            "Cell Name",
                        ]
                        if col in summary_df.columns
                    ]
                )

                summary_df[primary_kpi] = pd.to_numeric(
                    summary_df[primary_kpi],
                    errors="coerce",
                ).round(4)

                summary_df["Threshold"] = float(threshold)

                meet_count = int(
                    (summary_df["Remark"] == "Meet").sum()
                )
                not_meet_count = int(
                    (summary_df["Remark"] == "Not Meet").sum()
                )
                total_count = len(summary_df)

                st.markdown("### 📋 KPI Result Summary")

                if analysis_cell_site_pairs:
                    st.caption(
                        f"Bulk exact-match mode: "
                        f"{len(analysis_cell_site_pairs):,} Cell + Site + FreqBand target(s). "
                        "Summary is calculated only from these exact targets."
                    )

                m1, m2, m3 = st.columns(3)
                m1.metric("Meet", f"{meet_count:,}")
                m2.metric("Not Meet", f"{not_meet_count:,}")
                m3.metric("Total", f"{total_count:,}")

                comparison_text = (
                    f"{primary_kpi} ≥ {threshold:g}"
                    if not lower_is_better
                    else f"{primary_kpi} ≤ {threshold:g}"
                )

                st.caption(
                    f"Daily: {comparison_text} = Meet; "
                    "opposite condition = Not Meet."
                )

                styled_summary = (
                    summary_df.style
                    .map(
                        style_remark,
                        subset=["Remark"],
                    )
                )

                st.dataframe(
                    styled_summary,
                    use_container_width=True,
                    hide_index=True,
                )

        # --------------------------------------------------------
        # MODE 2 — AVERAGE DATE RANGE
        # --------------------------------------------------------
        elif date_evaluation_mode == "Average Date Range":

            summary_df = (
                valid_summary_source
                .groupby(
                    identity_cols,
                    as_index=False,
                    dropna=False,
                )["_KPI_Result_Value"]
                .agg(summary_agg)
                .rename(
                    columns={
                        "_KPI_Result_Value": primary_kpi,
                        **rename_map,
                    }
                )
            )

            if not summary_df.empty:

                summary_df["Remark"] = summary_df[
                    primary_kpi
                ].apply(evaluate_remark)

                summary_df["Threshold"] = float(threshold)

                if (
                    isinstance(date_range, tuple)
                    and len(date_range) == 2
                ):
                    range_start, range_end = date_range
                    date_label = (
                        f"{pd.Timestamp(range_start):%d-%b-%Y}"
                        f" → "
                        f"{pd.Timestamp(range_end):%d-%b-%Y}"
                    )
                else:
                    date_label = "Selected Date Range"

                summary_df["Date"] = date_label

                preferred_summary_cols = [
                    "Cell Name",
                    "LocalCell Id",
                    "Sector",
                    "FreqBand",
                    "eNodeB Name",
                    "Date",
                    primary_kpi,
                    "Remark",
                    "Threshold",
                ]

                summary_cols = [
                    col
                    for col in preferred_summary_cols
                    if col in summary_df.columns
                ]

                summary_df = summary_df[summary_cols].sort_values(
                    [
                        col
                        for col in [
                            "Sector",
                            "FreqBand",
                            "Cell Name",
                        ]
                        if col in summary_df.columns
                    ]
                )

                summary_df[primary_kpi] = pd.to_numeric(
                    summary_df[primary_kpi],
                    errors="coerce",
                ).round(4)

                summary_df["Threshold"] = float(threshold)

                meet_count = int(
                    (summary_df["Remark"] == "Meet").sum()
                )
                not_meet_count = int(
                    (summary_df["Remark"] == "Not Meet").sum()
                )
                total_count = len(summary_df)

                st.markdown("### 📋 KPI Result Summary")

                if analysis_cell_site_pairs:
                    st.caption(
                        f"Bulk exact-match mode: "
                        f"{len(analysis_cell_site_pairs):,} Cell + Site + FreqBand target(s). "
                        "Summary is calculated only from these exact targets."
                    )

                m1, m2, m3 = st.columns(3)
                m1.metric("Meet", f"{meet_count:,}")
                m2.metric("Not Meet", f"{not_meet_count:,}")
                m3.metric("Total", f"{total_count:,}")

                comparison_text = (
                    f"{primary_kpi} ≥ {threshold:g}"
                    if not lower_is_better
                    else f"{primary_kpi} ≤ {threshold:g}"
                )

                st.caption(
                    f"Average Date Range: {date_label}. "
                    f"Average {primary_kpi} is evaluated against "
                    f"{comparison_text}."
                )

                styled_summary = (
                    summary_df.style
                    .map(
                        style_remark,
                        subset=["Remark"],
                    )
                )

                st.dataframe(
                    styled_summary,
                    use_container_width=True,
                    hide_index=True,
                )

        # --------------------------------------------------------
        # MODE 3 — COMPARE 2 DATES
        # --------------------------------------------------------
        else:

            if (
                compare_date_a is not None
                and compare_date_b is not None
                and len(compare_date_a) == 2
                and len(compare_date_b) == 2
            ):

                compare_a_start, compare_a_end = compare_date_a
                compare_b_start, compare_b_end = compare_date_b

                def aggregate_for_date_range(selected_start, selected_end):
                    start_ts = pd.Timestamp(selected_start)
                    end_ts = pd.Timestamp(selected_end)

                    date_source = valid_summary_source[
                        (
                            valid_summary_source["_Summary_Date"]
                            >= start_ts
                        )
                        & (
                            valid_summary_source["_Summary_Date"]
                            <= end_ts
                        )
                    ].copy()

                    if date_source.empty:
                        return pd.DataFrame()

                    return (
                        date_source
                        .groupby(
                            identity_cols,
                            as_index=False,
                            dropna=False,
                        )["_KPI_Result_Value"]
                        .agg(summary_agg)
                    )

                date_a_df = aggregate_for_date_range(
                    compare_a_start,
                    compare_a_end,
                )
                date_b_df = aggregate_for_date_range(
                    compare_b_start,
                    compare_b_end,
                )

                value_col = "_KPI_Result_Value"

                date_a_df = date_a_df.rename(
                    columns={value_col: "Date A KPI"}
                )
                date_b_df = date_b_df.rename(
                    columns={value_col: "Date B KPI"}
                )

                if date_a_df.empty and date_b_df.empty:
                    compare_summary = pd.DataFrame()
                else:
                    compare_summary = pd.merge(
                        date_a_df,
                        date_b_df,
                        on=identity_cols,
                        how="outer",
                    )

                if not compare_summary.empty:

                    compare_summary = compare_summary.rename(
                        columns=rename_map
                    )

                    def format_compare_range(start_date, end_date):
                        if start_date == end_date:
                            return pd.Timestamp(start_date).strftime(
                                "%d-%b-%Y"
                            )
                        return (
                            f"{pd.Timestamp(start_date):%d-%b-%Y}"
                            f" → "
                            f"{pd.Timestamp(end_date):%d-%b-%Y}"
                        )

                    date_a_label = format_compare_range(
                        compare_a_start,
                        compare_a_end,
                    )
                    date_b_label = format_compare_range(
                        compare_b_start,
                        compare_b_end,
                    )

                    compare_summary["Date A"] = date_a_label
                    compare_summary["Date B"] = date_b_label

                    compare_summary["Remark A"] = (
                        compare_summary["Date A KPI"]
                        .apply(evaluate_remark)
                    )
                    compare_summary["Remark B"] = (
                        compare_summary["Date B KPI"]
                        .apply(evaluate_remark)
                    )

                    compare_summary["Threshold"] = float(threshold)

                    preferred_compare_cols = [
                        "Cell Name",
                        "LocalCell Id",
                        "Sector",
                        "FreqBand",
                        "eNodeB Name",
                        "Date A",
                        "Date A KPI",
                        "Remark A",
                        "Date B",
                        "Date B KPI",
                        "Remark B",
                        "Threshold",
                    ]

                    compare_cols = [
                        col
                        for col in preferred_compare_cols
                        if col in compare_summary.columns
                    ]

                    compare_summary = compare_summary[compare_cols]

                    sort_cols = [
                        col
                        for col in [
                            "Sector",
                            "FreqBand",
                            "Cell Name",
                        ]
                        if col in compare_summary.columns
                    ]
                    if sort_cols:
                        compare_summary = compare_summary.sort_values(
                            sort_cols
                        )

                    for value_col in [
                        "Date A KPI",
                        "Date B KPI",
                    ]:
                        if value_col in compare_summary.columns:
                            compare_summary[value_col] = pd.to_numeric(
                                compare_summary[value_col],
                                errors="coerce",
                            ).round(4)

                    compare_summary["Threshold"] = float(threshold)

                    meet_a = int(
                        (compare_summary["Remark A"] == "Meet").sum()
                    )
                    not_meet_a = int(
                        (compare_summary["Remark A"] == "Not Meet").sum()
                    )
                    meet_b = int(
                        (compare_summary["Remark B"] == "Meet").sum()
                    )
                    not_meet_b = int(
                        (compare_summary["Remark B"] == "Not Meet").sum()
                    )

                    st.markdown("### 📋 KPI Result Summary")

                    st.caption(
                        f"Compare {date_a_label} vs {date_b_label}. "
                        f"Threshold = {threshold:g}. "
                        "Each selected range is aggregated using "
                        "the current KPI aggregation rule."
                    )

                    cm1, cm2, cm3, cm4 = st.columns(4)
                    cm1.metric(
                        f"Meet — A ({date_a_label})",
                        f"{meet_a:,}",
                    )
                    cm2.metric(
                        f"Not Meet — A ({date_a_label})",
                        f"{not_meet_a:,}",
                    )
                    cm3.metric(
                        f"Meet — B ({date_b_label})",
                        f"{meet_b:,}",
                    )
                    cm4.metric(
                        f"Not Meet — B ({date_b_label})",
                        f"{not_meet_b:,}",
                    )

                    styled_compare = (
                        compare_summary.style
                        .map(
                            style_remark,
                            subset=[
                                col
                                for col in [
                                    "Remark A",
                                    "Remark B",
                                ]
                                if col in compare_summary.columns
                            ],
                        )
                    )

                    st.dataframe(
                        styled_compare,
                        use_container_width=True,
                        hide_index=True,
                    )

            else:
                st.warning(
                    "Please select valid Date A and Date B ranges."
                )

    if analysis_scope == "Site Level":
        scope_text = (
            f"Analysis scope: Site Level — {len(analysis_sites)} site(s), "
            "all Cell Names under the selected site(s)."
        )
    else:
        scope_text = (
            f"Analysis scope: Cell Level — {len(analysis_sites)} site(s), "
            f"{len(analysis_bands)} FreqBand(s), "
            f"{len(analysis_cells)} Cell Name(s)."
        )

    st.caption(
        scope_text + " "
        "RNO interpretation: use the primary KPI as the trigger, then "
        "compare the related KPI movements at the same Date / Time. "
        "A correlation is an indicator for investigation, not by itself "
        "a final root-cause conclusion."
    )





def _bar_xaxis_padding(series):
    """Return half the smallest positive time interval for bar-edge padding."""
    s = pd.to_datetime(series, errors="coerce").dropna().sort_values().drop_duplicates()
    if len(s) < 2:
        return pd.Timedelta(hours=12)
    diffs = s.diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return pd.Timedelta(hours=12)
    return diffs.min() / 2


# KPI Analysis and KPI Status Transition are independent Chart Layouts.
# When either is selected, the normal Horizontal / Vertical / 2 Charts
# renderer is bypassed and only its dedicated section is shown.
if chart_layout == "KPI Analysis":
    render_configurable_kpi_analysis()
    render_kpi_analysis()

if chart_layout == "KPI Status Transition":
    render_kpi_status_transition()


# ============================================================
# SITE LEVEL SUMMARY — NON-KPI ANALYSIS / TRANSITION LAYOUTS ONLY
# ============================================================
#
# Excel-style site summary:
#   - Total Payload (GB)      -> SUM, shown as bars
#   - Last TTI Ratio (%)      -> MAX, shown as line
#   - 4G Cell Availability(%) -> AVERAGE, shown as line
#
# This chart remains available for Horizontal / Vertical / 2 Charts.
# It is intentionally hidden when Chart Layout = KPI Analysis.
# It uses the complete selected Site + Date Range data,
# before Sector/FreqBand chart filters are applied.
# ============================================================

def _bar_xaxis_padding(series):
    """Return half the smallest positive time interval for bar-edge padding."""
    s = pd.to_datetime(series, errors="coerce").dropna().sort_values().drop_duplicates()
    if len(s) < 2:
        # A single point still needs a visible bar width.
        return pd.Timedelta(hours=12)
    diffs = s.diff().dropna()
    diffs = diffs[diffs > pd.Timedelta(0)]
    if diffs.empty:
        return pd.Timedelta(hours=12)
    return diffs.min() / 2


def render_site_level_summary():
    if site_level_df.empty:
        return

    payload_col = kpi_actual_columns.get("Payload")
    availability_col = kpi_actual_columns.get("4G Cell Availability")
    last_tti_col = kpi_actual_columns.get("Last TTI Ratio")

    required_cols = [
        c for c in [payload_col, availability_col, last_tti_col]
        if c and c in site_level_df.columns
    ]

    if not required_cols:
        return

    summary_source = site_level_df[
        ["_Date"]
        + required_cols
    ].copy()

    summary_source["_Payload_Value"] = (
        parse_kpi_numeric(summary_source[payload_col])
        if payload_col and payload_col in summary_source.columns
        else pd.NA
    )

    summary_source["_Availability_Value"] = (
        parse_kpi_numeric(summary_source[availability_col])
        if availability_col and availability_col in summary_source.columns
        else pd.NA
    )

    summary_source["_Last_TTI_Value"] = (
        parse_kpi_numeric(summary_source[last_tti_col])
        if last_tti_col and last_tti_col in summary_source.columns
        else pd.NA
    )

    summary_source["_Chart_Date"] = (
        summary_source["_Date"]
        if is_hourly
        else summary_source["_Date"].dt.normalize()
    )

    summary_df = (
        summary_source
        .groupby("_Chart_Date", as_index=False)
        .agg(
            Total_Payload_GB=("_Payload_Value", "sum"),
            Max_Last_TTI_Ratio=("_Last_TTI_Value", "max"),
            Avg_4G_Cell_Availability=("_Availability_Value", "mean"),
        )
        .sort_values("_Chart_Date")
    )

    if summary_df.empty:
        return

    # Build an Excel-like combo chart:
    # Payload = bars on the right axis
    # Last TTI + Availability = lines on the left axis
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=summary_df["_Chart_Date"],
            y=summary_df["Total_Payload_GB"],
            name="Average of 4GTotalPayloadGB",
            yaxis="y2",
            opacity=0.70,
            marker=dict(color="#8a8a8a"),
        )
    )

    fig.add_trace(
        go.Scatter(
            x=summary_df["_Chart_Date"],
            y=summary_df["Max_Last_TTI_Ratio"],
            name="Max of Last TTI Ratio %",
            mode="lines+markers",
            line=dict(color="#4472C4", width=7.0),
            marker=dict(size=10),
            connectgaps=True,
        )
    )

    fig.add_trace(
        go.Scatter(
            x=summary_df["_Chart_Date"],
            y=summary_df["Avg_4G_Cell_Availability"],
            name="Average of 4G Cell Availability(%)",
            mode="lines+markers",
            line=dict(color="#ED7D31", width=7.0),
            marker=dict(size=9),
            connectgaps=True,
        )
    )

    # Keep both KPI lines visually in front of the Payload bars.
    # Plotly renders later traces above earlier traces.
    bars = [trace for trace in fig.data if trace.type == "bar"]
    lines = [trace for trace in fig.data if trace.type == "scatter"]
    fig.data = tuple(bars + lines)

    # Explicitly keep KPI lines above the Payload bars.
    for trace in lines:
        try:
            trace.zorder = 100
        except Exception:
            pass

    if is_hourly:
        x_title = "Date / Time"
        tickformat = "%H:%M<br>%d-%b"
        hoverformat = "%d-%b-%Y %H:%M"
    else:
        x_title = "Date"
        tickformat = "%d-%b-%y"
        hoverformat = "%d-%b-%Y"

    # TTI threshold = 35%, shown as a red dashed reference line.
    # Keep it on the primary (TTI / Availability) axis.
    fig.add_hline(
        y=35,
        line=dict(
            color="#FF0000",
            width=2,
            dash="dash",
        ),
        layer="below",
        annotation_text="TTI Threshold 35%",
        annotation_position="top left",
        annotation_font=dict(size=10, color="#FF0000"),
    )

    # Extend the datetime axis by half a bar interval on both sides.
    # Without this padding, Plotly centers the first/last bars on the
    # boundary and clips half of each bar, creating visible left/right gaps.
    site_x_pad = _bar_xaxis_padding(summary_df["_Chart_Date"])
    site_x_min = summary_df["_Chart_Date"].min() - site_x_pad
    site_x_max = summary_df["_Chart_Date"].max() + site_x_pad

    fig.update_layout(
        title="Site Level Summary",
        xaxis=dict(
            title=x_title,
            tickformat=tickformat,
            hoverformat=hoverformat,
            showgrid=True,
            gridcolor="#e5e5e5",
            automargin=False,
            domain=[0.0, 1.0],
            range=[site_x_min, site_x_max],
            autorange=False,
            # Show actual time-of-day on the hourly chart.
            # 6-hour spacing keeps the chart readable while showing
            # when a TTI drop occurred.
            dtick=6 * 60 * 60 * 1000 if is_hourly else None,
        ),
        yaxis=dict(
            title="Last TTI Ratio % / Availability %",
            range=[0, 120],
            showgrid=True,
            gridcolor="#e5e5e5",
            zeroline=False,
        ),
        yaxis2=dict(
            title="Payload (GB)",
            overlaying="y",
            side="right",
            showgrid=False,
            zeroline=False,
        ),
        hovermode="x unified",
        height=500,
        margin=dict(l=18, r=0, t=60, b=72),
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.18,
            xanchor="center",
            x=0.5,
            font=dict(
                family="Arial",
                size=11,
            ),
        ),
        bargap=0.03,
        template="plotly_white",
    )

    fig.update_traces(
        selector=dict(type="bar"),
        hovertemplate=(
            "<b>Site Total Payload</b><br>"
            + (
                "%{x|%d-%b-%Y %H:%M}<br>"
                if is_hourly
                else "%{x|%d-%b-%Y}<br>"
            )
            + "Payload: %{y:.2f} GB"
            "<extra></extra>"
        ),
    )

    fig.update_traces(
        selector=dict(type="scatter"),
        hovertemplate=(
            "<b>%{fullData.name}</b><br>"
            + (
                "%{x|%d-%b-%Y %H:%M}<br>"
                if is_hourly
                else "%{x|%d-%b-%Y}<br>"
            )
            + "Value: %{y:.2f}"
            "<extra></extra>"
        ),
    )

    # Add to the grouped JPEG collection and render last.
    show_chart(
        fig,
        use_container_width=True,
        compact_summary=True,
    )


# Site Level Summary is intentionally hidden in KPI Analysis.
if chart_layout not in {
    "KPI Analysis",
    "KPI Status Transition",
}:
    render_site_level_summary()

# ============================================================
# DEBUG
# ============================================================
with st.expander(
    "Debug — Filtered data preview"
):

    debug_cols = [
        enodeb_col,
        cell_col,
        localcell_col,
        "_Site_ID",
        "_Date",
        "_Sector_Display",
        "_FreqBand",
    ]

    debug_cols += [
        KPI_CONFIG[k]["column"]
        for k in selected_kpis
    ]

    debug_cols = [
        c
        for c in dict.fromkeys(debug_cols)
        if c in site_df.columns
    ]

    st.dataframe(
        site_df[debug_cols].head(1000),
        use_container_width=True,
    )





# ============================================================
# DOWNLOAD — GROUPED JPEG CAPTURE
# ============================================================
def _plotly_fig_to_jpeg(fig_obj):
    """
    Render a Plotly figure to JPEG using Matplotlib.
    This avoids Kaleido/Chrome completely, which is important on
    Streamlit Cloud where Chrome may not be installed.
    """
    fig = plt.figure(figsize=(14, 6.5), dpi=120)
    ax = fig.add_subplot(111)

    title = getattr(fig_obj.layout.title, "text", None)
    if title:
        ax.set_title(title, loc="left", fontweight="bold")

    x_title = getattr(fig_obj.layout.xaxis.title, "text", None)
    y_title = getattr(fig_obj.layout.yaxis.title, "text", None)

    for trace in fig_obj.data:
        x = list(trace.x) if trace.x is not None else list(range(len(trace.y or [])))
        y = list(trace.y) if trace.y is not None else []

        name = trace.name or ""

        # Plotly scatter/line/area traces.
        if trace.type == "scatter":
            mode = trace.mode or "lines"
            fill = trace.fill

            if fill and fill != "none":
                ax.fill_between(x, y, alpha=0.25, label=name)
                ax.plot(x, y, linewidth=1.8, label=name)
            elif "markers" in mode and "lines" in mode:
                ax.plot(x, y, marker="o", markersize=2.5,
                        linewidth=1.2, label=name)
            elif "markers" in mode:
                ax.plot(x, y, marker="o", linestyle="None",
                        markersize=2.5, label=name)
            else:
                ax.plot(x, y, linewidth=1.8, label=name)

        # Bar traces, including TA Distribution.
        elif trace.type == "bar":
            width = 0.75
            ax.bar(x, y, width=width, label=name, alpha=0.85)

    if x_title:
        ax.set_xlabel(x_title)
    if y_title:
        ax.set_ylabel(y_title)

    # Make time-series axes readable.
    try:
        if any(hasattr(v, "year") for v in x if v is not None):
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            ax.xaxis.set_major_formatter(
                mdates.DateFormatter("%H:%M\n%d/%m/%Y")
            )
    except Exception:
        pass

    ax.grid(True, alpha=0.25)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.16),
        ncol=min(4, max(1, len(ax.get_legend_handles_labels()[0]))),
        fontsize=8,
        frameon=False,
    )

    fig.tight_layout()
    buffer = BytesIO()
    fig.savefig(buffer, format="jpeg", bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)

    return Image.open(buffer).convert("RGB")


if _download_figures:
    st.divider()
    st.subheader("Download Charts")

    st.caption(
        "Download all currently displayed KPI charts as one grouped JPEG image."
    )

    if st.button("🖼️ Prepare Grouped JPEG", use_container_width=False):
        with st.spinner("Preparing grouped JPEG..."):
            rendered_images = [
                _plotly_fig_to_jpeg(fig_obj)
                for fig_obj in _download_figures
            ]

            max_width = max(img.width for img in rendered_images)
            total_height = sum(img.height for img in rendered_images)

            grouped_image = Image.new(
                "RGB",
                (max_width, total_height),
                "white",
            )

            y_offset = 0
            for img in rendered_images:
                x_offset = (max_width - img.width) // 2
                grouped_image.paste(img, (x_offset, y_offset))
                y_offset += img.height

            output_buffer = BytesIO()
            grouped_image.save(
                output_buffer,
                format="JPEG",
                quality=92,
                optimize=True,
            )
            output_buffer.seek(0)

            st.session_state["grouped_jpeg"] = output_buffer.getvalue()

    if "grouped_jpeg" in st.session_state:
        st.download_button(
            label="⬇️ Download Grouped JPEG",
            data=st.session_state["grouped_jpeg"],
            file_name="RAN_KPI_Dashboard_Grouped_Charts.jpg",
            mime="image/jpeg",
            use_container_width=False,
        )

    if st.button("📦 Prepare Individual JPEG ZIP", use_container_width=False):
        with st.spinner("Preparing JPEG ZIP..."):
            zip_buffer = BytesIO()

            with zipfile.ZipFile(
                zip_buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as zf:
                for idx, fig_obj in enumerate(_download_figures, start=1):
                    img = _plotly_fig_to_jpeg(fig_obj)
                    img_buffer = BytesIO()
                    img.save(img_buffer, format="JPEG", quality=92)
                    zf.writestr(
                        f"KPI_Chart_{idx:02d}.jpg",
                        img_buffer.getvalue(),
                    )

            zip_buffer.seek(0)
            st.session_state["jpeg_zip"] = zip_buffer.getvalue()

    if "jpeg_zip" in st.session_state:
        st.download_button(
            label="⬇️ Download Individual JPEGs (ZIP)",
            data=st.session_state["jpeg_zip"],
            file_name="RAN_KPI_Dashboard_JPEG_Charts.zip",
            mime="application/zip",
            use_container_width=False,
        )


# ============================================================
# VERTICAL LAYOUT — TA DISTRIBUTION DETAIL (BOTTOM OF DASHBOARD)
# ============================================================
# Uses the uploaded L.RA.TA.UE.Index0..Index11 counters.
# Bars show UE attempts by TA distance; the red line shows cumulative
# distribution (CDF) on the secondary right axis.
# ============================================================
if chart_layout == "Vertical" and "TA Distribution" in selected_kpis:
    st.markdown("---")
    st.subheader("TA Distribution")

    ta_index_columns = {}
    for ta_index in range(12):
        expected_prefix = f"L.RA.TA.UE.Index{ta_index}"
        actual_ta_col = next(
            (
                header for header in site_df.columns
                if str(header).strip() == expected_prefix
                or str(header).strip().startswith(expected_prefix + " ")
                or str(header).strip().startswith(expected_prefix + "(")
            ),
            None,
        )
        if actual_ta_col is not None:
            ta_index_columns[ta_index] = actual_ta_col

    ta_distance_labels = [
        "0 - 156 m",
        "156 - 234 m",
        "234 - 546 m",
        "546 - 1014 m",
        "1014 - 1950 m",
        "1950 - 3510 m",
        "3510 - 6630 m",
        "6630 - 14430 m",
        "14430 - 30030 m",
        "30030 - 53430 m",
        "53430 - 76830 m",
        ">76830 m",
    ]

    if not ta_index_columns:
        st.warning(
            "Kolom TA Distribution tidak ditemukan. Pastikan CSV memiliki "
            "L.RA.TA.UE.Index0 sampai L.RA.TA.UE.Index11."
        )
    else:
        ta_work = site_df.copy()
        ta_work["_TA_Date"] = pd.to_datetime(ta_work["_Date"], errors="coerce").dt.normalize()
        ta_work["_TA_Cell"] = ta_work["_Cell_Display"].map(normalize_cell_name)
        ta_work = ta_work.dropna(subset=["_TA_Date"])
        ta_dates = sorted(ta_work["_TA_Date"].dropna().unique())

        if ta_work.empty or not ta_dates:
            st.info("Tidak ada data tanggal yang valid untuk TA Distribution.")
        else:
            latest_ta_date = pd.Timestamp(ta_dates[-1]).to_pydatetime().date()
            available_ta_dates = [pd.Timestamp(value).date() for value in ta_dates]
            control_date_col, control_cell_col = st.columns([1, 2], gap="small")

            with control_date_col:
                selected_ta_date = st.selectbox(
                    "TA Measurement Date",
                    options=available_ta_dates,
                    index=available_ta_dates.index(latest_ta_date),
                    format_func=lambda value: value.strftime("%d %b %Y"),
                    key="vertical_ta_distribution_date",
                )

            date_ta_df = ta_work[
                ta_work["_TA_Date"].dt.date == selected_ta_date
            ].copy()
            available_ta_cells = sorted(
                date_ta_df["_TA_Cell"].dropna().astype(str).unique().tolist()
            )

            with control_cell_col:
                selected_ta_cell = st.selectbox(
                    "Object / Cell Name",
                    options=available_ta_cells,
                    index=0 if available_ta_cells else None,
                    key="vertical_ta_distribution_cell",
                    help="Pilih satu cell untuk melihat distribusi TA pada tanggal terpilih.",
                )

            if not available_ta_cells or not selected_ta_cell:
                st.info("Tidak ada cell yang tersedia pada tanggal terpilih.")
            else:
                selected_ta_rows = date_ta_df[
                    date_ta_df["_TA_Cell"].astype(str) == str(selected_ta_cell)
                ]

                ta_values = []
                for ta_index in range(12):
                    column = ta_index_columns.get(ta_index)
                    if column is None:
                        count = 0.0
                    else:
                        count = float(
                            parse_kpi_numeric(selected_ta_rows[column])
                            .fillna(0)
                            .clip(lower=0)
                            .sum()
                        )
                    ta_values.append(count)

                total_ue_attempts = sum(ta_values)
                cumulative = []
                running_total = 0.0
                for count in ta_values:
                    running_total += count
                    cumulative.append(
                        (running_total / total_ue_attempts * 100.0)
                        if total_ue_attempts > 0 else 0.0
                    )

                ta_summary = pd.DataFrame({
                    "TA Index": list(range(12)),
                    "Distance": ta_distance_labels,
                    "UE Number": [int(round(value)) for value in ta_values],
                    "CDF": [f"{value:.2f}%" for value in cumulative],
                    "_CDF Numeric": cumulative,
                })
                ta_summary["Overshoot UE (%)"] = [
                    f"{(value / total_ue_attempts * 100.0):.2f}%"
                    if total_ue_attempts > 0 else "0.00%"
                    for value in ta_values
                ]

                st.caption(
                    f"Date: {selected_ta_date:%d %b %Y}  |  "
                    f"Cell Name: {selected_ta_cell}  |  "
                    f"Total UE Attempts: {int(round(total_ue_attempts)):,}"
                )

                # Match the reference layout: distribution chart on the left,
                # detailed TA table on the right.
                chart_col, table_col = st.columns([3.3, 1.25], gap="medium")

                from plotly.subplots import make_subplots
                ta_fig = make_subplots(specs=[[{"secondary_y": True}]])
                ta_fig.add_trace(
                    go.Bar(
                        x=ta_distance_labels,
                        y=[int(round(value)) for value in ta_values],
                        name="UE Number",
                        marker_color="#2478c4",
                        hovertemplate="Distance: %{x}<br>UE Attempts: %{y:,}<extra></extra>",
                    ),
                    secondary_y=False,
                )
                ta_fig.add_trace(
                    go.Scatter(
                        x=ta_distance_labels,
                        y=cumulative,
                        name="CDF",
                        mode="lines+markers+text",
                        line=dict(color="#d7191c", width=3),
                        marker=dict(size=7),
                        text=[f"{value:.2f}%" for value in cumulative],
                        textposition="top center",
                        hovertemplate="Distance: %{x}<br>CDF: %{y:.2f}%<extra></extra>",
                    ),
                    secondary_y=True,
                )
                ta_fig.update_layout(
                    title=f"TA Cell Distribution — {selected_ta_cell}",
                    template="plotly_white",
                    height=520,
                    barmode="group",
                    hovermode="x unified",
                    margin=dict(l=35, r=35, t=65, b=115),
                    legend=dict(
                        orientation="h",
                        yanchor="top",
                        y=-0.28,
                        xanchor="center",
                        x=0.5,
                    ),
                )
                ta_fig.update_xaxes(
                    title_text="TA Distance",
                    tickangle=-35,
                    categoryorder="array",
                    categoryarray=ta_distance_labels,
                )
                ta_fig.update_yaxes(title_text="UE Attempts", secondary_y=False, rangemode="tozero")
                ta_fig.update_yaxes(title_text="CDF (%)", secondary_y=True, range=[0, 105], ticksuffix="%")
                display_summary = ta_summary.drop(columns=["_CDF Numeric"])

                with chart_col:
                    show_chart(ta_fig, use_container_width=True)

                with table_col:
                    st.markdown("#### TA Cell")
                    st.dataframe(
                        display_summary,
                        use_container_width=True,
                        hide_index=True,
                        height=460,
                        column_config={
                            "TA Index": st.column_config.NumberColumn(
                                "TA Index",
                                format="%d",
                                width="small",
                            ),
                            "Distance": st.column_config.TextColumn(
                                "Distance",
                                width="medium",
                            ),
                            "UE Number": st.column_config.NumberColumn(
                                "UE Number",
                                format="%d",
                                width="small",
                            ),
                            "CDF": st.column_config.TextColumn(
                                "CDF",
                                width="small",
                            ),
                            "Overshoot UE (%)": st.column_config.TextColumn(
                                "UE Share (%)",
                                width="small",
                            ),
                        },
                    )

                csv_data = display_summary.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    "Download TA Distribution CSV",
                    data=csv_data,
                    file_name=f"TA_Distribution_{selected_ta_cell}_{selected_ta_date:%Y%m%d}.csv",
                    mime="text/csv",
                    key="download_vertical_ta_distribution_csv",
                )
