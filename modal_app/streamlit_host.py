"""Public hosting for the Streamlit UI on Modal -- so judges/teammates can
open a real URL instead of localhost, which only ever works on this machine.

Deploy:
    1. One-time: create a Modal secret holding exactly the credentials this
       container is allowed to hold (copy the values from your local .env):

           modal secret create deltafin-hosted-llm \
               MODAL_QWEN_URL=... MODAL_KEY=... MODAL_SECRET=...

    2. modal deploy modal_app/streamlit_host.py

Prints a https://...modal.run URL. No login, no proxy auth -- anyone with the
link can open it.

This bakes in a SNAPSHOT of the current local demo data (data/, out/) at
deploy time so the page has real content the instant it's opened. Re-run
`modal deploy` after generating fresh demo data locally (`python run_demo.py`,
`python run_flux.py`) to push updated content -- this does not regenerate
data on its own, it only serves what's on disk right now.

The AP inbox's "Upload a new invoice" panel runs the real process_invoice
LangGraph live against whatever gets uploaded (extraction, matching, controls,
workpaper) -- via the real Qwen extraction path, not the deterministic
fallback: the `deltafin-hosted-llm` secret above injects MODAL_QWEN_URL /
MODAL_KEY / MODAL_SECRET as env vars, so app/llm.py's get_llm() picks up Qwen
exactly as it does locally (same "Modal-hosted, scale-to-zero" endpoint,
cold-start included).

.env itself stays fully excluded from the image (see `_ignore` below) -- only
the three Qwen-calling values are ever present in this container, as a named
Modal secret, never as a baked-in file or a value committed to source.

Real tradeoff, stated plainly: this is a public, unauthenticated endpoint that
now holds live credentials capable of calling a real, billed GPU endpoint.
Nothing in this app's code echoes env vars back to a visitor, but anyone who
got shell access to the running container would have working Qwen-calling
credentials. Treat `deltafin-hosted-llm` as scoped-but-not-nothing: it's a
narrower blast radius than the full .env, not a zero one. Rotate it if this
endpoint is ever taken down for good, and don't reuse the same secret name
for anything holding higher-value credentials later.

Dependencies are the subset of requirements.txt this page's live code paths
need: langgraph (app/graph.py), openpyxl (workpaper generation), pypdf (.pdf
uploads), langchain-openai (Qwen's OpenAI-compatible client, app/llm.py), and
duckdb -- required since ui/streamlit_app.py began importing app/flux/uploads.py
at module scope for the CSV upload panel, which builds its in-memory summary/txn
tables in DuckDB. Without it the Streamlit process dies on import and the page
never starts, so do not drop it back out.

The flux *viewer* still only reads pre-generated files and never queries DuckDB;
it is the upload path that needs it.
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
        "langchain-openai>=0.2",
        "duckdb>=1.0",
    )
    .add_local_dir(".", remote_path="/root/app", copy=True, ignore=_ignore)
)

llm_secret = modal.Secret.from_name("deltafin-hosted-llm")


@app.function(image=image, secrets=[llm_secret], scaledown_window=300)
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
