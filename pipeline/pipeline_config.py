from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(r"C:\Users\jerry\Desktop\new FDS")
PROCESSING_DIR = BASE_DIR / "Processing data"

CSV_DIR = PROCESSING_DIR / "csv"
RAW_NCODE_DIR = PROCESSING_DIR / "_ncode raw"
PHASE_SPLIT_DIR = PROCESSING_DIR / "0_phase_split"
PSD_OUTPUT_DIR = PROCESSING_DIR / "4_psd"

FLIGHT_PHASE_FLOW = BASE_DIR / "0_FlightPhaseSplit.flo"
FDS_SRS_FLOW = BASE_DIR / "4_FDS_SRS.flo"

ASCII_TRANSLATE_EXE = Path(
    r"C:\Program Files\nCode\nCode 2025.1 64-bit\GlyphWorks\bin\asciitranslate.exe"
)
FLOWPROC_EXE = Path(
    r"C:\Program Files\nCode\nCode 2025.1 64-bit\GlyphWorks\bin\flowproc.exe"
)

TS_OUTPUT_GLYPH = "Loop Flight Phases.SuperGlyph1.TSOutput1"
TS_INPUT_GLYPH = "TSInput1"

