from __future__ import annotations

import os

import streamlit as st

from server_panel.auth import login_gate
from server_panel.ui import apply_theme, render_application


st.set_page_config(
    page_title="MQTT IoT Control Center",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={"About": "MQTT IoT Edge-Cloud — Raspberry Pi 4 and Django/Mosquitto"},
)

apply_theme()

if login_gate():
    render_application()
