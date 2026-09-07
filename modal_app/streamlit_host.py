"""Public hosting for the Streamlit UI on Modal -- so judges/teammates can
open a real URL instead of localhost, which only ever works on this machine.

Deploy:
    modal deploy modal_app/streamlit_host.py

Prints a https://...modal.run URL. No login, no proxy auth -- anyone with the
link can open it.

This bakes in a SNAPSHOT of the current local demo data (data/, out/) at
deploy time so the page has real content the instant it's opened. Re-run
`modal deploy` after generating fresh demo data locally (`python run_demo.py`,
`python run_flux.py`) to push updated content -- this does not regenerate
data on its own, it only serves what's on disk right now.

The AP inbox's "Upload a new invoice" panel *does* run the real
process_invoice LangGraph live against whatever gets uploaded (extraction,
matching, controls, workpaper) -- but always via the deterministic
parser/template path, never an LLM: .env is deliberately excluded from the
image (see `_ignore` below), so ANTHROPIC_API_KEY / MODAL_QWEN_URL are unset
here and app/llm.py's get_llm() returns None. A public, unauthenticated
endpoint has no business holding those credentials.

Dependencies are the subset of requirements.txt the UI + AP graph actually
import: langgraph (app/graph.py, module-level import regardless of whether an
LLM is configured), openpyxl (workpaper generation), pypdf (.pdf uploads).
Not included: duckdb/modal/langchain-anthropic/langchain-openai -- nothing on
this page's code path needs them (the flux view only reads pre-generated
files, never queries DuckDB live; get_llm() short-circuits before importing
any LLM client).
"""

import modal

app = modal.App("deltafin-ui")


def _ignore(path):
    skip = {".git", ".env", "__pycache__", ".gide", ".venv", "venv"}
    parts = set(path.parts)
    return bool(parts & skip) or path.suffix in {".pyc"}


image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "streamlit>=1.38",
        "pandas",
        "python-dotenv>=1.0",
        "langgraph>=0.2",
        "openpyxl>=3.1",
        "pypdf>=5.0",
    )
    .add_local_dir(".", remote_path="/root/app", copy=True, ignore=_ignore)
)


@app.function(image=image, scaledown_window=300)
@modal.web_server(port=8501, startup_timeout=60)
def serve():
    import subprocess

    subprocess.Popen(
        "streamlit run ui/streamlit_app.py "
        "--server.port 8501 --server.address 0.0.0.0 --server.headless true "
        "--browser.gatherUsageStats false",
        shell=True,
        cwd="/root/app",
    )
