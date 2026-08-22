"""Read-only Docker discovery.

This package may talk to Docker and may depend on ``argus.domain``. It
must never import a database driver, an AI client, or a web framework
— see the architecture guard in tests/unit/test_discovery.py. Nothing
here ever performs a mutating Docker operation; see
tests/unit/test_docker_client.py's read-only guard.
"""
