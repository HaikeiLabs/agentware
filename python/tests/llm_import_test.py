"""Importing the LLM types must not require the optional HTTP client."""

import subprocess
import sys
from pathlib import Path


def test_llm_import_without_httpx() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "sys.modules['httpx'] = None; "
        "from haikeilabs.agentware.llm import OpenAIBackend; "
        "import pedro_agentware.memory; "
        "assert OpenAIBackend"
    )
    subprocess.run([sys.executable, "-S", "-c", code], check=True, capture_output=True, text=True)
