from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from .mqtt_control import read_broker_metrics
from .repository import (
    cleanup_iot_data,
    get_alarm_preview,
    get_alarms,
    get_database_status,
    get_logs,
    get_snapshot,
    get_video_preview,
    get_video_segments,
)


PAGES = ["نمای کلی", "هشدارها", "دستگاه‌ها و سلامت", "آرشیو و پاک‌سازی", "لاگ‌های سیستم", "Broker و زیرساخت"]


def apply_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Vazirmatn:wght@400;500;600;700;800&display=swap');
        :root { color-scheme: dark; }
        html, body, [class*="css"], [data-testid="stAppViewContainer"] {
            font-family: 'Vazirmatn', Tahoma, sans-serif !important;
        }
        [data-testid="stAppViewContainer"] {
            background:
              radial-gradient(ellipse at 8% 0%, rgba(20, 91, 105, .22), transparent 34rem),
              radial-gradient(ellipse at 88% 8%, rgba(79, 65, 130, .13), transparent 36rem),
              #08111f;
            direction: rtl;
        }
        [data-testid="stHeader"] { background: rgba(8,17,31,.72); }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #101e30 0%, #0c1726 100%);
            border-left: 1px solid rgba(145,174,198,.14);
            border-right: 0;
            direction: rtl;
        }
        [data-testid="stSidebar"] * { text-align: right; }
        .block-container { max-width: 1500px; padding-top: 1.5rem; padding-bottom: 3rem; }
        .stMarkdown, label, .stMetric { direction: rtl; text-align: right; }
        .stMarkdown p { line-height: 1.9; }
        h1, h2, h3 { letter-spacing: -.3px; }
        .stButton > button, .stDownloadButton > button {
            border-radius: 10px; border: 1px solid rgba(115,157,179,.25);
            font-weight: 600; min-height: 40px; transition: .16s ease;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            border-color: #63d8c5; transform: translateY(-1px);
        }
        [data-testid="stMetric"] {
            background: linear-gradient(145deg, rgba(20,38,58,.98), rgba(13,27,43,.98));
            border: 1px solid rgba(123,158,187,.17);
            border-radius: 15px; padding: 15px 17px; min-height: 108px;
            box-shadow: 0 14px 34px rgba(0,0,0,.16);
        }
        [data-testid="stMetricLabel"] { color: #9aafc2 !important; font-size: .82rem; }
        [data-testid="stMetricValue"] { color: #edf7fc !important; font-weight: 750; }
        [data-testid="stDataFrame"], [data-testid="stTable"] {
            border: 1px solid rgba(123,158,187,.16); border-radius: 12px; overflow: hidden;
        }
        .eyebrow { color: #68ddca; letter-spacing: 1.8px; font: 700 10px Arial,sans-serif; }
        .hero {
            border: 1px solid rgba(97,207,199,.19);
            background: linear-gradient(105deg, rgba(19,57,68,.8), rgba(17,31,49,.92) 58%, rgba(34,31,62,.74));
            border-radius: 20px; padding: 23px 27px; margin-bottom: 20px;
            box-shadow: 0 18px 45px rgba(0,0,0,.17);
        }
        .hero h1 { margin: 8px 0 7px; font-size: clamp(1.6rem, 3vw, 2.25rem); color: #f1f8fc; }
        .hero p { margin: 0; color: #a7bdcc; font-size: .9rem; line-height: 2; max-width: 850px; }
        .section-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin: 23px 0 11px; }
        .section-head h3 { margin:0; font-size:1.08rem; }
        .section-head span { color:#7894a9; font:9px Arial,sans-serif; letter-spacing:1.35px; }
        .soft-card {
            border:1px solid rgba(123,158,187,.17); background:linear-gradient(145deg,#14253a,#101c2c);
            border-radius:15px; padding:16px 18px; height:100%;
        }
        .soft-card .muted { color:#8fa5b9; font-size:.8rem; }
        .soft-card strong { color:#eff8ff; font-size:1.2rem; }
        .status-dot { display:inline-block; width:8px;height:8px;border-radius:50%;margin-left:8px;background:#55dc9b;box-shadow:0 0 12px #55dc9b; }
        .status-off { background:#f17d8c;box-shadow:0 0 10px #f17d8c; }
        .login-wrap {
            max-width: 620px; margin: 7vh auto 16px; padding: 25px 29px;
            border:1px solid rgba(97,207,199,.2); border-radius:20px;
            background:linear-gradient(145deg,#14283a,#101b2b);
        }
        .login-wrap h1 { font-size:1.8rem; margin:8px 0; }
        .login-wrap p { color:#a0b4c6; line-height:2; }
        div[data-testid="stForm"] { border:1px solid rgba(123,158,187,.17); border-radius:14px; padding:17px; }
        .stAlert { border-radius:12px; }
        hr { border-color: rgba(123,158,187,.15); }
        code { direction:ltr; }
        @media (max-width: 700px) {
            .block-container { padding-top:1rem; }
            .hero { padding:18px; }
            .hero h1 { font-size:1.5rem; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero(title: str, description: str, eyebrow: str = "IOT ALARM PLATFORM · OPERATIONS") -> None:
    st.markdown(
        f'<div class="hero"><div class="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>',
        unsafe_allow_html=True,
    )


def _section(title: str, hint: str = "") -> None:
    st.markdown(f'<div class="section-head"><h3>{title}</h3><span>{hint}</span></div>', unsafe_allow_html=True)


def _fmt_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        return pd.Timestamp(value).strftime("%Y-%m-%d  %H:%M:%S")
    except Exception:
        return str(value)


def _snapshot_or_error() -> dict[str, Any] | None:
    try:
        return get_snapshot()
    except Exception as exc:
        st.error(f"خواندن داده‌های سامانه ناموفق بود: {exc}")
        return None


def _device_frame(devices: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "وضعیت": "آنلاین" if row["is_online"] else "آفلاین",
            "نام دستگاه": row["name"],
            "مالک": row["owner"],
            "شناسه": row["device_id"],
            "Video FPS": round(row["video_fps"], 1),
            "AI FPS": round(row["inference_fps"], 1),
            "مدل YOLOv5n": "بارگذاری شده" if row["model_loaded"] else "غیرفعال",
            "دوربین": "آماده" if row["state"].get("camera_ready") else "نامشخص",
            "جریان زنده": "متصل" if row["state"].get("rtsp_connected") else "قطع",
            "هشدارهای معوق": row["state"].get("pending_alerts", 0),
            "آخرین Heartbeat": _fmt_time(row["last_heartbeat"]),
        }
        for row in devices
    ])


def _alarms_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "زمان رخداد": _fmt_time(row["detected_at"]),
            "دستگاه": row["device_name"],
            "مالک": row["owner"],
            "نوع": row["event_type"],
            "Confidence": round(row["confidence"], 3),
            "باکس‌ها": row.get("bbox_count", 0),
            "تصویر": "ذخیره شده" if row["has_snapshot"] else "ندارد",
            "وضعیت": "بررسی شده" if row["acknowledged"] else "جدید",
            "Alert ID": row["alert_id"],
        }
        for row in rows
    ])


def _render_overview() -> None:
    snapshot = _snapshot_or_error()
    if snapshot is None:
        return
    broker = read_broker_metrics(timeout=1.2)
    db_ok, db_message = get_database_status()
    cols = st.columns(5)
    cols[0].metric("دوربین‌های ثبت‌شده", snapshot["device_count"])
    cols[1].metric("دوربین آنلاین", snapshot["online_count"])
    cols[2].metric("هشدارهای بررسی‌نشده", snapshot["unacknowledged_count"])
    cols[3].metric("لاگ‌های خطا", snapshot["error_count"])
    cols[4].metric("قطعه‌های ویدئو", snapshot["video_segment_count"])

    a, b, c = st.columns(3)
    with a:
        st.markdown(
            f'<div class="soft-card"><div class="muted">Database</div><strong>{db_message if db_ok else "خطا"}</strong></div>',
            unsafe_allow_html=True,
        )
    with b:
        st.markdown(
            f'<div class="soft-card"><div class="muted">Mosquitto</div><strong>{"Connected" if broker.get("available") else "Unavailable"}</strong></div>',
            unsafe_allow_html=True,
        )
    with c:
        st.markdown(
            f'<div class="soft-card"><div class="muted">ذخیره فایل ویدئو</div><strong>{snapshot["video_bytes"] / (1024**3):.2f} GB</strong></div>',
            unsafe_allow_html=True,
        )

    _section("سلامت دستگاه‌ها", "DEVICE HEARTBEATS")
    devices = snapshot["devices"]
    if devices:
        st.dataframe(_device_frame(devices), width="stretch", hide_index=True, height=300)
    else:
        st.info("هیچ دوربینی Pair نشده است. از حساب کاربری در پنل Django یک QR اتصال بساز.")

    _section("هشدارهای اخیر", "PERSON DETECTION EVENTS")
    alarms = snapshot["latest_alarms"]
    if alarms:
        st.dataframe(_alarms_frame(alarms), width="stretch", hide_index=True, height=280)
        choices = [row for row in alarms if row.get("has_snapshot")]
        if choices:
            preview_id = st.selectbox(
                "پیش‌نمایش تصویر هشدار",
                [row["id"] for row in choices],
                format_func=lambda value: next(
                    f'{_fmt_time(item["detected_at"])} · {item["device_name"]}'
                    for item in choices if item["id"] == value
                ),
                key="overview_alarm_preview",
            )
            preview = get_alarm_preview(preview_id)
            if preview and preview.get("image"):
                st.image(
                    preview["image"],
                    caption=f'{preview["device_name"]} · {_fmt_time(preview["detected_at"])}',
                    use_container_width=True,
                )
    else:
        st.info("هنوز هشدار تشخیص انسانی ثبت نشده است.")


def _render_alarms() -> None:
    _hero(
        "مرکز هشدارها",
        "رخدادهای Person Detection ذخیره‌شده روی سرور را بررسی کن. تصویر اصلی و باکس‌های تشخیص در پنل مالک دستگاه قابل مشاهده‌اند.",
        "ALARM EVENT MONITOR",
    )
    a, b = st.columns([1, 2])
    with a:
        only_new = st.toggle("فقط هشدارهای بررسی‌نشده", value=False)
    with b:
        limit = st.select_slider("تعداد رکورد", options=[50, 100, 300, 500, 1000], value=300)
    rows = get_alarms(limit=limit, only_unacknowledged=only_new)
    if rows:
        st.dataframe(_alarms_frame(rows), width="stretch", hide_index=True, height=340)
        frame = _alarms_frame(rows)
        st.download_button(
            "خروجی هشدارها (CSV)",
            frame.to_csv(index=False).encode("utf-8-sig"),
            file_name="alarm-events.csv",
            mime="text/csv",
        )
        index = st.selectbox(
            "انتخاب هشدار برای پیش‌نمایش",
            range(len(rows)),
            format_func=lambda i: f"{_fmt_time(rows[i]['detected_at'])} · {rows[i]['device_name']} · {rows[i]['alert_id'][:10]}",
            key="alarm_preview_selection",
        )
        selected = rows[index]
        left, right = st.columns([1.15, 1])
        with left:
            preview = get_alarm_preview(selected["id"])
            if preview and preview.get("image"):
                st.image(
                    preview["image"],
                    caption=f'{preview["device_name"]} · {_fmt_time(preview["detected_at"])}',
                    use_container_width=True,
                )
            elif selected.get("has_snapshot") and preview and preview.get("image_size", 0) > 12 * 1024 * 1024:
                st.warning("تصویر بزرگ‌تر از سقف پیش‌نمایش ۱۲ مگابایت است؛ در پنل Django بازش کن.")
            else:
                st.info("برای این هشدار تصویری ذخیره نشده است.")
        with right:
            st.json({
                "alert_id": selected["alert_id"],
                "device_id": selected["device_id"],
                "device_name": selected["device_name"],
                "confidence": selected["confidence"],
                "bbox_count": selected["bbox_count"],
                "model": selected["model"],
                "acknowledged": selected["acknowledged"],
            }, expanded=False)
    else:
        st.info("هشداری مطابق فیلتر وجود ندارد.")


def _render_devices() -> None:
    _hero(
        "دستگاه‌ها و سلامت Edge",
        "Heartbeat، نرخ فریم، وضعیت مدل NCNN، اتصال جریان زنده و صف ارسال مجدد هر Raspberry Pi را پایش کن.",
        "EDGE DEVICE HEALTH",
    )
    snapshot = _snapshot_or_error()
    if snapshot is None:
        return
    devices = snapshot["devices"]
    if not devices:
        st.info("هیچ دستگاهی ثبت نشده است.")
        return
    st.dataframe(_device_frame(devices), width="stretch", hide_index=True, height=320)
    _section("آخرین قطعه‌های ویدئو", "SERVER VIDEO SEGMENTS")
    segments = snapshot["latest_segments"]
    if segments:
        frame = pd.DataFrame([
            {
                "شناسه قطعه": row["segment_id"],
                "دستگاه": row["device_name"],
                "مالک": row["owner"],
                "زمان شروع": _fmt_time(row["started_at"]),
                "مدت": f'{row["duration_seconds"]} s',
                "حجم": f'{row["size_bytes"] / (1024**2):.2f} MB',
                "زمان دریافت": _fmt_time(row["uploaded_at"]),
            }
            for row in segments
        ])
        st.dataframe(frame, width="stretch", hide_index=True, height=300)
        segment_choices = [row for row in segments if row.get("id")]
        if segment_choices:
            selected_segment_id = st.selectbox(
                "انتخاب ویدئو برای پخش",
                [row["id"] for row in segment_choices],
                format_func=lambda value: next(
                    f'{_fmt_time(item["started_at"])} · {item["device_name"]} · {item["duration_seconds"]}s'
                    for item in segment_choices if item["id"] == value
                ),
                key="device_video_preview",
            )
            preview = get_video_preview(selected_segment_id)
            if preview and preview.get("video"):
                st.video(preview["video"])
            elif preview and preview.get("size_bytes", 0) > 48 * 1024 * 1024:
                st.warning("این ویدئو از ۴۸ مگابایت بزرگ‌تر است؛ برای پخش آن از آرشیو صفحه دستگاه در Django استفاده کن.")
    else:
        st.caption("هنوز ویدئویی از دستگاه‌ها روی سرور دریافت نشده است.")


def _render_archive_cleanup() -> None:
    _hero(
        "آرشیو و پاک‌سازی",
        "پیش‌نمایش تصویر هشدارها، پخش قطعه‌های ویدئوی ذخیره‌شده و مدیریت پاک‌سازی شواهد سرور.",
        "SERVER EVIDENCE ARCHIVE",
    )
    snapshot = _snapshot_or_error()
    if snapshot is None:
        return

    alarms = get_alarms(limit=500)
    videos = get_video_segments(limit=200)
    left, right = st.columns(2)
    with left:
        _section("پیش‌نمایش هشدار", "IMAGE EVIDENCE")
        image_rows = [row for row in alarms if row.get("has_snapshot")]
        if image_rows:
            selected_alarm = st.selectbox(
                "هشدار",
                [row["id"] for row in image_rows],
                format_func=lambda value: next(
                    f'{_fmt_time(item["detected_at"])} · {item["device_name"]}'
                    for item in image_rows if item["id"] == value
                ),
                key="archive_alarm_selection",
            )
            preview = get_alarm_preview(selected_alarm)
            if preview and preview.get("image"):
                st.image(
                    preview["image"],
                    caption=f'{preview["device_name"]} · اطمینان {preview["confidence"]:.2f}',
                    use_container_width=True,
                )
            elif preview:
                st.warning("تصویر در دسترس نیست یا از سقف پیش‌نمایش بزرگ‌تر است.")
        else:
            st.info("تصویری برای پیش‌نمایش وجود ندارد.")

    with right:
        _section("پخش ویدئوی گذشته", "RECORDED SEGMENTS")
        if videos:
            selected_video = st.selectbox(
                "قطعه ویدئو",
                [row["id"] for row in videos],
                format_func=lambda value: next(
                    f'{_fmt_time(item["started_at"])} · {item["device_name"]} · {item["duration_seconds"]}s'
                    for item in videos if item["id"] == value
                ),
                key="archive_video_selection",
            )
            preview = get_video_preview(selected_video)
            if preview and preview.get("video"):
                st.video(preview["video"])
            elif preview:
                st.warning("این قطعه از سقف ۱۶۰ مگابایت پیش‌نمایش عبور کرده یا فایل آن خوانده نشد.")
        else:
            st.info("هنوز ویدئویی روی سرور ذخیره نشده است.")

    _section("پاک‌سازی شواهد ذخیره‌شده", "IRREVERSIBLE SERVER ACTION")
    device_options = ["همه دوربین‌ها"] + [
        f'{row["name"]} · {row["device_id"]}' for row in snapshot["devices"]
    ]
    device_ids = [""] + [row["device_id"] for row in snapshot["devices"]]
    with st.form("server_archive_cleanup_form"):
        kind = st.selectbox(
            "نوع داده",
            ["alarms", "videos", "logs", "all"],
            format_func=lambda value: {
                "alarms": "تصاویر و هشدارها",
                "videos": "ویدئوهای آرشیو",
                "logs": "لاگ‌ها",
                "all": "همه داده‌های انتخاب‌شده",
            }[value],
        )
        selected_device = st.selectbox(
            "محدوده دوربین",
            range(len(device_options)),
            format_func=lambda index: device_options[index],
        )
        confirmed = st.checkbox("حذف این داده‌ها از سرور را تأیید می‌کنم.")
        submitted = st.form_submit_button("پاک‌سازی داده‌ها", type="primary", width="stretch")
    if submitted:
        if not confirmed:
            st.warning("برای ادامه، تأیید حذف را فعال کن.")
        else:
            try:
                result = cleanup_iot_data(kind, device_ids[selected_device] or None)
                st.success(
                    f'{result["removed_count"]} مورد حذف شد؛ '
                    f'{result["removed_bytes"] / (1024**2):.1f} MB فایل آزاد شد.'
                )
                st.rerun()
            except Exception as exc:
                st.error(f"پاک‌سازی ناموفق بود: {exc}")


def _render_logs() -> None:
    _hero(
        "لاگ‌های عملیاتی",
        "خطاهای دوربین، بارگذاری مدل، Pairing، ارتباط MQTT و تلاش‌های ارسال مجدد را فیلتر و بررسی کن.",
        "DEVICE LOG INSPECTOR",
    )
    a, b = st.columns([1, 1])
    with a:
        level = st.selectbox("حداقل سطح لاگ", ["all", "debug", "info", "warning", "error"],
                             format_func=lambda v: {"all": "همه", "debug": "Debug به بالا",
                                                    "info": "Info به بالا", "warning": "Warning به بالا",
                                                    "error": "فقط Error"}[v])
    with b:
        limit = st.selectbox("تعداد رکورد", [50, 100, 300, 500, 1000], index=2)
    rows = get_logs(limit=limit, minimum_level=level)
    if not rows:
        st.info("لاگی مطابق فیلتر وجود ندارد.")
        return
    frame = pd.DataFrame([
        {
            "زمان": _fmt_time(row["created_at"]),
            "سطح": row["level"].upper(),
            "دستگاه": row["device_name"],
            "مالک": row["owner"],
            "دسته": row["category"],
            "پیام": row["message"],
        }
        for row in rows
    ])
    st.dataframe(frame, width="stretch", hide_index=True, height=340)
    st.download_button(
        "دانلود لاگ‌ها (CSV)",
        frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="device-logs.csv",
        mime="text/csv",
    )
    selected = st.selectbox(
        "انتخاب لاگ برای جزئیات",
        range(len(rows)),
        format_func=lambda i: f"{_fmt_time(rows[i]['created_at'])} · {rows[i]['level']} · {rows[i]['device_name']}",
        key="log_detail_selection",
    )
    with st.container(height=220, border=True):
        st.code(
            json.dumps(rows[selected]["details"], ensure_ascii=False, indent=2),
            language="json",
        )


def _render_broker() -> None:
    _hero(
        "Broker و زیرساخت",
        "اتصال داخلی Mosquitto، آمار $SYS و وضعیت پایگاه داده و پروکسی ویدئو را بررسی کن.",
        "INFRASTRUCTURE HEALTH",
    )
    db_ok, db_message = get_database_status()
    broker = read_broker_metrics(timeout=1.5)
    cols = st.columns(3)
    cols[0].metric("پایگاه داده", "سالم" if db_ok else "خطا", db_message)
    cols[1].metric("Mosquitto", "متصل" if broker.get("available") else "قطع")
    cols[2].metric("وضعیت Broker uptime", broker.get("metrics", {}).get("$SYS/broker/uptime", "—"))
    if broker.get("available"):
        _section("آمار Broker", "MOSQUITTO $SYS")
        metrics = broker.get("metrics") or {}
        st.dataframe(
            [{"Topic": topic, "Value": value} for topic, value in metrics.items()],
            width="stretch",
            hide_index=True,
            height=340,
        )
    else:
        st.error(broker.get("error") or "اتصال Broker برقرار نشد.")

    _section("مسیریابی ارتباط", "NETWORK CONTRACT")
    routes = [
        {"مبدأ": "Raspberry Pi", "مقصد": "Django API", "پروتکل": "HTTPS :443", "کاربرد": "Pairing، Heartbeat، هشدار، عکس و قطعه ویدئو"},
        {"مبدأ": "Raspberry Pi", "مقصد": "Mosquitto", "پروتکل": "MQTT over WSS :443", "کاربرد": "اعلان کوتاه آلارم، QoS 1"},
        {"مبدأ": "Raspberry Pi", "مقصد": "MediaMTX", "پروتکل": "RTSPS :8322", "کاربرد": "جریان زنده H.264"},
        {"مبدأ": "Browser", "مقصد": "Django", "پروتکل": "HTTPS :443", "کاربرد": "حساب، داشبورد، Admin و سوابق"},
        {"مبدأ": "Browser", "مقصد": "MediaMTX", "پروتکل": "WHEP / WebRTC", "کاربرد": "پخش زنده با دسترسی موقت"},
        {"مبدأ": "Server داخلی", "مقصد": "Mosquitto", "پروتکل": "MQTT TCP :1883", "کاربرد": "دریافت اعلان MQTT؛ شبکه خصوصی"},
    ]
    st.dataframe(routes, width="stretch", hide_index=True)
    st.caption("پنل کاربر و Django Admin روی Django هستند. Streamlit در /ops فقط صفحه عیب‌یابی اپراتور است.")


def render_application() -> None:
    with st.sidebar:
        st.markdown(
            """
            <div style="padding:8px 4px 18px">
              <div style="font:700 10px Arial,sans-serif;letter-spacing:2px;color:#63ddc9">ECHONODE / ALARM OPS</div>
              <div style="font-size:20px;font-weight:800;color:#eef7fb;margin-top:7px">مرکز عملیات</div>
              <div style="font-size:10px;color:#7f97ab;margin-top:3px">Django · MQTT · MediaMTX · Edge AI</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("#### پایش سامانه")
        page = st.radio(
            "بخش موردنظر",
            PAGES,
            label_visibility="collapsed",
            format_func=lambda value: {
                "نمای کلی": "◈  نمای کلی",
                "هشدارها": "⚑  هشدارها",
                "دستگاه‌ها و سلامت": "▣  دستگاه‌ها و سلامت",
                "آرشیو و پاک‌سازی": "▤  آرشیو و پاک‌سازی",
                "لاگ‌های سیستم": "≋  لاگ‌های سیستم",
                "Broker و زیرساخت": "◎  Broker و زیرساخت",
            }[value],
        )
        st.divider()
        st.markdown(
            """
            <div style="font-size:11px;color:#91a8ba;line-height:2">
              <span style="color:#61dfc9">●</span> پنل اپراتور<br>
              <b style="color:#e2eff8">/ops</b><br>
              کاربران دوربین: <b style="color:#d9e7ef">Django</b><br>
              احراز هویت دستگاه: <b style="color:#d9e7ef">Per-device token</b>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()
        st.caption(f"حساب عملیات: {st.session_state.get('panel_username', 'operator')}")
        if st.button("خروج", width="stretch"):
            st.session_state.pop("panel_authenticated", None)
            st.session_state.pop("panel_username", None)
            st.rerun()

    _render_live_page(page)


@st.fragment(run_every="10s")
def _render_live_page(page: str) -> None:
    if page == "نمای کلی":
        _hero(
            "پایش سامانه آلارم هوشمند",
            "وضعیت دستگاه‌های ثبت‌شده، نرخ پردازش روی Raspberry Pi، هشدارهای انسانی و سلامت زیرساخت را از یک نقطه بررسی کن.",
        )
        _render_overview()
    elif page == "هشدارها":
        _render_alarms()
    elif page == "دستگاه‌ها و سلامت":
        _render_devices()
    elif page == "آرشیو و پاک‌سازی":
        _render_archive_cleanup()
    elif page == "لاگ‌های سیستم":
        _render_logs()
    else:
        _render_broker()
