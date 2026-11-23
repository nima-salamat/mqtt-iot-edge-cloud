from __future__ import annotations

import streamlit as st

from server_panel.auth import login_gate
from server_panel.ui import apply_theme, render_application


st.set_page_config(
    page_title="IoT Alarm Operations",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": "IoT Alarm Platform — Django, Raspberry Pi Edge AI, Mosquitto and MediaMTX"},
)

apply_theme()

if login_gate():
    render_application()
