"""PythonAnywhere entry point for verified, versioned web releases."""

from scripts.hosted_release import application as load_application

application = load_application()
