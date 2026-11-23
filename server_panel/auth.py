from __future__ import annotations

import hmac
import os

import streamlit as st


def login_gate() -> bool:
    """Fail closed until a dedicated Streamlit panel account is configured."""
    expected_user = os.getenv("PANEL_USERNAME", "").strip()
    expected_password = os.getenv("PANEL_PASSWORD", "")

    if not expected_user or not expected_password or expected_password.startswith("REPLACE_"):
        st.error("پنل هنوز پیکربندی نشده است.")
        st.markdown(
            "برای فعال‌کردن ورود، مقادیر **PANEL_USERNAME** و **PANEL_PASSWORD** را "
            "در فایل محیطی سرور تنظیم و سرویس را دوباره راه‌اندازی کن."
        )
        st.stop()

    if st.session_state.get("panel_authenticated") is True:
        return True

    st.markdown(
        """
        <div class="login-wrap">
          <div class="login-mark">MQ</div>
          <div class="eyebrow">ECHONODE · IOT EDGE-CLOUD</div>
          <h1>ورود به مرکز کنترل MQTT</h1>
          <p>برای مشاهده دستگاه‌ها، داده‌های سنسور و ارسال فرمان به Raspberry Pi وارد شو.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("panel_login", clear_on_submit=False):
        username = st.text_input("نام کاربری", autocomplete="username")
        password = st.text_input("گذرواژه", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("ورود به پنل", width="stretch", type="primary")

    if submitted:
        user_ok = hmac.compare_digest(username.strip().encode("utf-8"), expected_user.encode("utf-8"))
        password_ok = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
        if user_ok and password_ok:
            st.session_state["panel_authenticated"] = True
            st.session_state["panel_username"] = expected_user
            st.rerun()
        st.error("نام کاربری یا گذرواژه صحیح نیست.")

    return False
