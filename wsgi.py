"""PythonAnywhere entry point; configure Force HTTPS before public access."""

import os
from pathlib import Path

from backend.hosting import application_settings, load_hosted_environment

load_hosted_environment(Path(__file__).resolve().parent / ".env")
if not application_settings()[1]:
    raise ValueError("WSGI requires STUDENTCONNECT_BASE_URL in hosted HTTPS mode")
if not os.environ.get("GROQ_API_KEY", "").strip():
    raise ValueError("Hosted scoring requires GROQ_API_KEY")

# Import after loading credentials because the pipeline reads them at import time.
from backend.app import app as application
from backend.pipeline_skeleton import validate_email_configuration

validate_email_configuration()
