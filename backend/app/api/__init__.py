"""The HTTP surface, split by the thing each group of routes is about.

Order matters. FastAPI matches routes in registration order, so a literal path
has to be registered before a parameterised one that would also match it --
``/models/compare`` before nothing here, but ``/replays/{key}/...`` variants
are only unambiguous because they differ in depth. Keeping the registration in
one visible list is what makes that checkable.
"""
from fastapi import FastAPI

from app.api import (
    live,
    meta,
    models,
    predictions,
    reference,
    replays,
    sessions,
    training,
)

#: Registered in this order onto the application.
ROUTERS = [
    meta.router,
    reference.router,
    sessions.router,
    training.router,
    predictions.router,
    models.router,
    live.router,
    replays.router,
]


def register(app: FastAPI) -> None:
    for router in ROUTERS:
        app.include_router(router)
