from pydantic import BaseModel, Field


class StartSessionResponse(BaseModel):
    session_id: int


class SubmitMessageRequest(BaseModel):
    text: str


class SubmitMessageResponse(BaseModel):
    reply: str
    image_url: str | None = None


class SubmitStructuredMessageRequest(BaseModel):
    text: str
    step: int = Field(ge=1, le=7)


class SubmitStructuredMessageResponse(BaseModel):
    reply: str
