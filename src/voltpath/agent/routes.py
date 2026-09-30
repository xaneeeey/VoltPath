from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from voltpath.agent.copilot import chat
from voltpath.agent.tools import ToolError, confirm_action
from voltpath.api.auth import Principal, require

router = APIRouter(prefix="/v1/copilot", tags=["copilot"])


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ChatRequest(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=20)


@router.post("/chat")
async def copilot_chat(req: ChatRequest, p: Principal = Depends(require("copilot:use"))):
    return await chat(p, [m.model_dump() for m in req.messages])


@router.post("/actions/{action_id}/confirm")
async def confirm(action_id: str, p: Principal = Depends(require("copilot:act", "charging:schedule"))):
    try:
        return await confirm_action(p, action_id)
    except ToolError as exc:
        raise HTTPException(404, str(exc)) from exc
