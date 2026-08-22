"""Domain vocabulary: Application, Service, Container, Observation.

This package must never import Docker, a database driver, or an AI
client. It defines *what things are*, not how they are discovered,
stored, or reasoned about. See tests/unit/test_domain_models.py for the
architecture guard that enforces this.
"""
