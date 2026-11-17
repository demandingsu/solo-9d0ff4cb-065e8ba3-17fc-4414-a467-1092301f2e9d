"""FastAPI application: battery cell sample upload and cycle reports."""

from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .csv_import import CSVParseError, parse_samples_csv
from .db import ConflictError, Database, ImportError_, VersionNotFound

DB_PATH = os.environ.get("BATTERY_DB_PATH", "/data/battery.db")

app = FastAPI(
    title="Battery Cell Cycle Capacity API",
    version="1.0.0",
    description=(
        "Upload UTC-second current samples per cell. Zero current marks "
        "segment boundaries; only zero→positive→zero→negative→zero sequences "
        "count as cycles. Gaps over 60 s truncate a sequence. Ah is the "
        "left-value current integral over each interval."
    ),
)
db = Database(DB_PATH)


class CycleOut(BaseModel):
    cycle_no: int
    charge_ah: float
    discharge_ah: float
    started_at: int
    ended_at: int


class ReportOut(BaseModel):
    cell_id: str
    version: int
    total_cycles: int
    created_at: str | None
    cycles: list[CycleOut]


class UploadOut(BaseModel):
    cell_id: str
    inserted: int
    duplicate: int
    version: int
    total_cycles: int


class IncrementSample(BaseModel):
    ts: int
    current_a: float


class IncrementInfo(BaseModel):
    version: int
    status: str  # 'known' (batch row exists) or 'unknown' (legacy version)
    inserted: int | None
    duplicate: int | None
    samples: list[IncrementSample] | None
    note: str | None


class CyclePair(BaseModel):
    old: CycleOut
    new: CycleOut


class CapacityChangeOut(CyclePair):
    span_changed: bool
    charge_delta_ah: float
    discharge_delta_ah: float


class SplitOut(BaseModel):
    from_: CycleOut = Field(alias="from")
    into: list[CycleOut]


class MergeOut(BaseModel):
    from_: list[CycleOut] = Field(alias="from")
    into: CycleOut


class RestructuredOut(BaseModel):
    from_: list[CycleOut] = Field(alias="from")
    into: list[CycleOut]


class DiffSummary(BaseModel):
    old_total: int
    new_total: int
    added: int
    removed: int
    split: int
    merged: int
    capacity_changed: int
    unchanged: int
    renumber_only: int
    restructured: int


class VersionDiffOut(BaseModel):
    cell_id: str
    from_version: int
    to_version: int
    from_created_at: str | None
    to_created_at: str | None
    from_increment: IncrementInfo
    to_increment: IncrementInfo
    added: list[CycleOut]
    removed: list[CycleOut]
    split: list[SplitOut]
    merged: list[MergeOut]
    capacity_changed: list[CapacityChangeOut]
    unchanged: list[CyclePair]
    restructured: list[RestructuredOut]
    summary: DiffSummary


@app.post(
    "/cells/{cell_id}/samples",
    response_model=UploadOut,
    tags=["upload"],
    summary="Upload a CSV batch of (UTC seconds, current A) samples",
)
async def upload_samples(cell_id: str, file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = parse_samples_csv(content)
        result = db.import_batch(cell_id, rows)
    except CSVParseError as exc:
        raise HTTPException(status_code=400, detail=f"CSV error: {exc}") from exc
    except ImportError_ as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"batch rejected, no data changed: same timestamp {exc.ts} "
                f"already stored with {exc.existing} A, incoming {exc.incoming} A"
            ),
        ) from exc
    return result


@app.get("/cells", tags=["query"], summary="List all cells with latest report info")
def list_cells():
    return db.list_cells()


@app.get(
    "/cells/{cell_id}/report",
    response_model=ReportOut,
    tags=["query"],
    summary="Latest committed cycle report for one cell",
)
def get_report(cell_id: str):
    report = db.get_report(cell_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return report


@app.get(
    "/cells/{cell_id}/versions/{version}/diff",
    response_model=VersionDiffOut,
    tags=["query"],
    summary=(
        "Diff one report version against its immediate predecessor, with the"
        " sample increments of both versions"
    ),
)
def get_version_diff(cell_id: str, version: int):
    if version < 1:
        raise HTTPException(
            status_code=400, detail="version must be a positive integer"
        )
    try:
        diff = db.get_version_diff(cell_id, version)
    except VersionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    if diff is None:
        raise HTTPException(status_code=404, detail=f"unknown cell {cell_id}")
    return diff


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health():
    return {"status": "ok"}
