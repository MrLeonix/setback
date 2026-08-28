"""Firestore access for persisting case state.

Wraps the google-cloud-firestore client against the setback-app default
project's (default) database in us-central1.
"""

from __future__ import annotations

from typing import Any


def get_firestore_client() -> Any:
    """Construct a Firestore client for the configured GCP project.

    Returns:
        A connected Firestore client.

    Raises:
        NotImplementedError: Firestore access is not yet implemented.
    """
    raise NotImplementedError
