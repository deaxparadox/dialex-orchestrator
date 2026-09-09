"""Cofounder chat endpoints (spec 0044). Same ownership-check-before-Temporal
shape as `dialex/consultations/router.py` — never fetch-then-check-after."""

import base64
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from temporalio.service import RPCError, RPCStatusCode

from ...core.observability import bind_cofounder_context
from ...core.security import AuthContext, get_auth_context
from ...core.temporal_client import TASK_QUEUE
from . import queries
from .schemas import StartSessionResponse, SubmitMessageRequest, SubmitMessageResponse
from .workflows import CofounderWorkflow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cofounder", tags=["cofounder"])


async def _get_owned_session(session_id: int, auth: AuthContext) -> dict:
    session = await queries.get_session(session_id)
    # 404, not 403 — same IDOR-avoidance shape used everywhere else in this
    # codebase (consultations/debates).
    if session is None or session["user_id"] != auth.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return session


@router.post("/sessions/", response_model=StartSessionResponse)
async def start_session(request: Request, auth: AuthContext = Depends(get_auth_context)):
    bind_cofounder_context(session_id=auth.session_id, user_id=auth.user_id)

    session_id = await queries.insert_session(user_id=auth.user_id)
    client = request.app.state.temporal_client
    await client.start_workflow(
        CofounderWorkflow.run,
        session_id,
        id=f"cofounder-{session_id}",
        task_queue=TASK_QUEUE,
    )
    logger.info("cofounder session %d started", session_id)
    return StartSessionResponse(session_id=session_id)


@router.post("/sessions/{session_id}/messages", response_model=SubmitMessageResponse)
async def submit_message(
    session_id: int,
    body: SubmitMessageRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    bind_cofounder_context(
        cofounder_session_id=session_id, session_id=auth.session_id, user_id=auth.user_id
    )
    await _get_owned_session(session_id, auth)

    client = request.app.state.temporal_client
    handle = client.get_workflow_handle(f"cofounder-{session_id}")
    try:
        result = await handle.execute_update(CofounderWorkflow.submit_message, args=[session_id, body.text])
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found") from exc
        raise
    image_id = result.get("image_id")
    image_url = f"/api/cofounder/images/{image_id}" if image_id is not None else None
    return SubmitMessageResponse(reply=result["reply"], image_url=image_url)


@router.get("/images/{image_id}")
async def get_image(image_id: int, auth: AuthContext = Depends(get_auth_context)):
    image = await queries.get_image(image_id)
    if image is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Image not found")
    # Ownership resolved via the image's own session, same 404-not-403
    # IDOR shape as everywhere else in this codebase.
    await _get_owned_session(image["session_id"], auth)
    return Response(content=base64.b64decode(image["data"]), media_type="image/png")
