from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class GenerateImageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)


class GenerateVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=2000)
    target_duration: float = Field(gt=0, le=3600)


class JobResponse(BaseModel):
    id: str
    type: str
    status: str
    result_url: str | None = None
    created_at: datetime
    queue_position: int | None = None
    runpod_job_id: str | None = None


class ProjectResponse(BaseModel):
    id: str
    status: str
    clip_count: int
    clips_complete: int
    final_result_url: str | None = None
