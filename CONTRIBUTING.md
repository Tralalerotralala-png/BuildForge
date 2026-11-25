# Contributing

Use small branches and reproducible synthetic fixtures. Run `python -m unittest discover -v`; Linux integration tests need Clang and LLD. Windows-specific tests and packaging have separate release gates. Never commit real project sources, objects, personal profiles or session logs. Do not bypass output locks or relax fingerprint checks to make a test pass. Resource budgets affect scheduling rather than exact CPU quota. Keep all features honest about platform support. Open an issue for a proposed larger change before implementing it.
