import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Serverless, scale-to-zero reasoning model (vLLM/Qwen on Modal) -- the only LLM
# provider. Unset -> the pipeline runs its deterministic extraction and narrative
# paths. See modal_app/qwen_reasoner.py.
MODAL_QWEN_URL = os.getenv("MODAL_QWEN_URL", "")
MODAL_KEY = os.getenv("MODAL_KEY", "")
MODAL_SECRET = os.getenv("MODAL_SECRET", "")
QWEN_MODEL_NAME = "qwen-reasoner"
# Per-request ceiling on a call to that endpoint. It is scale-to-zero, so a
# first call after idle can sit in a GPU cold start for minutes -- and a
# minutes-long call inside a Streamlit script run is not a slow page, it is a
# lost one: the run gets cancelled and the result never renders. Every LLM
# caller in this codebase has a deterministic fallback, so a timeout costs
# narrative polish, not the analysis.
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "25"))

DB_PATH = ROOT / os.getenv("LEDGER_DB", "data/ledger.db")
OUT_DIR = ROOT / "out"
SAMPLES_DIR = ROOT / "samples" / "invoices"
UPLOADS_DIR = ROOT / "data" / "uploads"

# A vendor invoice may exceed its PO by this much before it is an exception.
AMOUNT_TOLERANCE_PCT = 0.02
AMOUNT_TOLERANCE_ABS = 50.0

# --- Variance ("flux") explanation agent -----------------------------------
FINANCIALS_DIR = ROOT / os.getenv("FINANCIALS_DIR", "data/financials")
SUMMARIES_GLOB = str(FINANCIALS_DIR / "summaries" / "*.csv")
TRANSACTIONS_GLOB = str(FINANCIALS_DIR / "transactions" / "*.csv")
FLUX_OUT_DIR = OUT_DIR / "flux"

# Institutional memory lives in its own file so `data/seed.py` (which rebuilds
# the AP ledger from scratch) never touches what the flux agent has learned.
FLUX_DB_PATH = ROOT / os.getenv("FLUX_DB", "data/flux_memory.db")
FLUX_GRAPH_PATH = ROOT / os.getenv("FLUX_GRAPH", "data/flux_memory_graph.json")

# An account variance is material if it moves more than MATERIALITY_ABS, or
# moves more than MATERIALITY_PCT while still clearing the de-minimis floor.
MATERIALITY_ABS = 25_000.0
MATERIALITY_PCT = 0.10
MATERIALITY_FLOOR = 5_000.0
# Statistically odd swings stay in the queue even when small in dollars.
ANOMALY_Z = 2.0
# Accounts drilled into per run, and cohort drivers reported per account.
MAX_DRILLDOWNS = 6
# When NOTHING clears the materiality gate, drill this many of the largest
# movements anyway. Roughly half the periods in a normal ledger are quiet, and
# "no material variances" plus an empty page is indistinguishable from a broken
# run to the person reading it. These are reported as informational (P3) and
# never counted as material findings -- the gate still means what it says.
REVIEW_FALLBACK_N = 3
MAX_DRIVERS = 5
