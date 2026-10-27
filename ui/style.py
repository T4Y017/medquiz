"""Uygulama teması (QSS)."""
import sys
from pathlib import Path

_ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "assets"


def _asset(name: str) -> str:
    return (_ASSETS / name).as_posix()


ACCENT = "#0f766e"       # koyu turkuaz
ACCENT_HOVER = "#115e59"
ACCENT_SOFT = "#ccfbf1"
OK = "#15803d"
OK_SOFT = "#dcfce7"
BAD = "#b91c1c"
BAD_SOFT = "#fee2e2"
WARN_SOFT = "#fef3c7"
INK = "#0f172a"
MUTED = "#64748b"
LINE = "#e2e8f0"
BG = "#f8fafc"
CARD = "#ffffff"


def stylesheet(font_pt: int = 11) -> str:
    return f"""
* {{ font-family: "Segoe UI", "Noto Sans", "Helvetica Neue", Arial; font-size: {font_pt}pt; color: {INK}; }}
QMainWindow, QWidget#page {{ background: {BG}; }}
QWidget#sidebar {{ background: #0b3b38; }}
QLabel#brand {{ color: white; font-size: {font_pt + 7}pt; font-weight: 700; padding: 18px 16px 2px 18px; }}
QLabel#brandSub {{ color: #99f6e4; font-size: {font_pt - 2}pt; padding: 0 16px 14px 18px; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; }}
QListWidget#nav::item {{ color: #d1fae5; padding: 11px 18px; border-radius: 8px; margin: 2px 10px; }}
QListWidget#nav::item:hover {{ background: #115e59; }}
QListWidget#nav[compact="true"]::item {{ padding: 11px 0px; margin: 2px 8px; font-size: {font_pt + 3}pt; }}
QToolButton#sideToggle {{ color: #99f6e4; background: transparent; border: none; font-size: {font_pt + 4}pt; padding: 4px 8px; margin-top: 12px; }}
QToolButton#sideToggle:hover {{ color: white; background: #115e59; border-radius: 6px; }}
QListWidget#nav::item:selected {{ background: #14b8a6; color: white; font-weight: 600; }}
QLabel#sideLabel {{ color: #99f6e4; font-size: {font_pt - 2}pt; padding: 0 18px; }}
QWidget#sidebar QComboBox {{ margin: 4px 14px 16px 14px; }}

QLabel#h1 {{ font-size: {font_pt + 8}pt; font-weight: 700; }}
QLabel#h2 {{ font-size: {font_pt + 3}pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#badge {{ background: {ACCENT_SOFT}; color: {ACCENT}; border-radius: 10px; padding: 3px 10px;
               font-size: {font_pt - 1}pt; font-weight: 600; }}
QLabel#disclaimer {{ color: {MUTED}; font-size: {font_pt - 2}pt; }}

QFrame#card {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 14px; }}
QFrame#feedbackOk {{ background: {OK_SOFT}; border: 1px solid #86efac; border-radius: 12px; }}
QFrame#feedbackBad {{ background: {BAD_SOFT}; border: 1px solid #fca5a5; border-radius: 12px; }}
QFrame#feedbackMid {{ background: {WARN_SOFT}; border: 1px solid #fcd34d; border-radius: 12px; }}

QPushButton {{ background: white; border: 1px solid #cbd5e1; border-radius: 9px; padding: 8px 16px; }}
QPushButton:hover {{ border-color: {ACCENT}; background: #f0fdfa; }}
QPushButton:disabled {{ color: #94a3b8; background: #f1f5f9; border-color: {LINE}; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; font-weight: 600; padding: 9px 20px; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background: #94a3b8; color: white; }}
QPushButton#chip {{ background: #f0fdfa; color: {ACCENT}; border: 1px solid #99f6e4; border-radius: 14px;
    padding: 5px 12px; }}
QPushButton#chip:hover {{ background: {ACCENT_SOFT}; border-color: {ACCENT}; }}
QPushButton#danger {{ color: {BAD}; }}
QPushButton#danger:hover {{ background: {BAD_SOFT}; border-color: {BAD}; }}

QPushButton#option {{ text-align: left; padding: 12px 14px; border-radius: 10px; background: white; }}
QPushButton#option:hover {{ background: #f0fdfa; border-color: {ACCENT}; }}
QPushButton#option[state="correct"] {{ background: {OK_SOFT}; border: 2px solid {OK}; font-weight: 600; }}
QPushButton#option[state="wrong"] {{ background: {BAD_SOFT}; border: 2px solid {BAD}; }}
QPushButton#option[state="dim"] {{ color: #94a3b8; }}

QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QTextBrowser {{
    background: white; border: 1px solid #cbd5e1; border-radius: 9px; padding: 6px 8px;
    selection-background-color: {ACCENT_SOFT}; selection-color: {INK};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {ACCENT}; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 24px; border: none; }}
QComboBox::down-arrow {{ image: url("{_asset('arrow_down.png')}"); width: 12px; height: 12px; }}
QSpinBox {{ padding-left: 10px; padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ subcontrol-origin: border; width: 22px; border: none; background: transparent; }}
QSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #f0fdfa; }}
QSpinBox::up-arrow {{ image: url("{_asset('arrow_up.png')}"); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url("{_asset('arrow_down.png')}"); width: 10px; height: 10px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: {ACCENT_SOFT}; }}
QCheckBox {{ spacing: 8px; }}
QTableWidget {{ background: white; border: 1px solid {LINE}; border-radius: 10px; gridline-color: {LINE};
               selection-background-color: {ACCENT_SOFT}; selection-color: {INK}; }}
QHeaderView::section {{ background: #f1f5f9; border: none; border-bottom: 1px solid {LINE}; padding: 7px; font-weight: 600; }}
QProgressBar {{ border: none; background: {LINE}; border-radius: 4px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #cbd5e1; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: {INK}; color: white; border: none; padding: 6px; }}
"""
