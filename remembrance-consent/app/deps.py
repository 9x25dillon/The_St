from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.auth import Actor, current_actor
from app.services import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
ActorDep = Annotated[Actor, Depends(current_actor)]
