from __future__ import annotations

import hmac
import os

import streamlit as st


def login_gate() -> bool:
    """Protect the internal operator-only diagnostics panel."""
    expected_user = os.getenv("PANEL_USERNAME", "").strip()
    expected_password = os.getenv("PANEL_PASSWORD", "")

    if not expected_user or not expected_password or expected_password.startswith("REPLACE_"):
        st.error("پنل عملیات هنوز پیکربندی نشده است.")
        st.markdown(
            "برای فعال‌کردن این بخش، **PANEL_USERNAME** و **PANEL_PASSWORD** را "
            "در فایل محیطی سرور تنظیم کن."
        )
        st.stop()

    if st.session_state.get("panel_authenticated") is True:
        return True

    st.markdown(
        """
        <div class="login-wrap">
          <div class="login-mark">◈</div>
          <div class="eyebrow">ECHONODE · ALARM PLATFORM</div>
          <h1>ورود به پنل عملیات</h1>
          <p>این بخش فقط برای پایش سلامت زیرساخت و عیب‌یابی سامانه است؛ کاربران دوربین از پنل Django استفاده می‌کنند.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    with st.form("panel_login", clear_on_submit=False):
        username = st.text_input("نام کاربری عملیات", autocomplete="username")
        password = st.text_input("گذرواژه", type="password", autocomplete="current-password")
        submitted = st.form_submit_button("ورود", width="stretch", type="primary")

    if submitted:
        user_ok = hmac.compare_digest(username.strip().encode("utf-8"), expected_user.encode("utf-8"))
        password_ok = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
        if user_ok and password_ok:
            st.session_state["panel_authenticated"] = True
            st.session_state["panel_username"] = expected_user
            st.rerun()
        st.error("نام کاربری یا گذرواژه صحیح نیست.")

    return False
