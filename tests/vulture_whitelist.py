# Vulture whitelist: names that look unused but are required (protocol/framework
# signatures, dynamically referenced attrs). Scanned by `make deadcode` so each
# bare name counts as a "use" and suppresses the corresponding report. Regenerate
# additions with: uv run vulture --make-whitelist >> tests/vulture_whitelist.py
exc_type  # unused variable (archiver/landing_uploader.py:71) — __aexit__ protocol arg
tb  # unused variable (archiver/landing_uploader.py:71) — __aexit__ protocol arg
