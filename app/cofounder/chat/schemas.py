from pydantic import BaseModel


class StartSessionResponse(BaseModel):
    session_id: int


class SubmitMessageRequest(BaseModel):
    text: str


class SubmitMessageResponse(BaseModel):
    reply: str
    image_url: str | None = None
