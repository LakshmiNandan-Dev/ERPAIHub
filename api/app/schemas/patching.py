from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime


class PatchTargetCreate(BaseModel):
    environment_id: int
    component: str
    home_path: Optional[str] = None
    target_patches: List[Dict[str, Any]] = []   # [{"patch_number","label","required_by"}]
    notes: Optional[str] = None


class PatchTargetUpdate(BaseModel):
    home_path: Optional[str] = None
    target_patches: Optional[List[Dict[str, Any]]] = None
    notes: Optional[str] = None


class PatchTargetOut(BaseModel):
    id: int
    environment_id: int
    component: str
    home_path: Optional[str] = None
    target_patches: List[Dict[str, Any]] = []
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PatchFileScanConfigCreate(BaseModel):
    environment_id: int
    label: Optional[str] = None
    run_fs_path: str
    file_globs: List[str] = ["*.pls", "*.pkb", "*.pks", "*.sql", "*.fmb", "*.rdf", "*.java"]
    notes: Optional[str] = None


class PatchFileScanConfigUpdate(BaseModel):
    label: Optional[str] = None
    run_fs_path: Optional[str] = None
    file_globs: Optional[List[str]] = None
    notes: Optional[str] = None


class PatchFileScanConfigOut(BaseModel):
    id: int
    environment_id: int
    label: Optional[str] = None
    run_fs_path: str
    file_globs: List[str] = []
    notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PatchFileInventoryOut(BaseModel):
    id: int
    environment_id: int
    product: str
    filename: str
    path: str
    fs_version: Optional[str] = None
    db_version: Optional[str] = None
    match_status: str
    last_scan_run_id: Optional[int] = None
    last_scanned_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PatchFileInventoryPage(BaseModel):
    total: int
    items: List[PatchFileInventoryOut]
