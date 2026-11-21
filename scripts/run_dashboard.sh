#!/usr/bin/env bash
cd "$(dirname "$0")/.."
exec python -m streamlit run app.py
