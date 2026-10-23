"""app/routers/selections.py
Selection creation and pinned snapshot retrieval endpoints.
"""

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.schemas import SelectionCreate, SelectionResponse
from app.services.selections import create_selection, get_selection

router = APIRouter(prefix="/selections", tags=["selections"])


@router.post("", response_model=SelectionResponse, status_code=status.HTTP_201_CREATED)
def post_selection(req: SelectionCreate, db: Session = Depends(get_db)):
    """Create an immutable node selection. Duplicate selection returns existing selection."""
    selection, is_new = create_selection(db, name=req.name, node_ids=req.node_ids)
    return selection


@router.get("/{selection_id}", response_model=SelectionResponse)
def fetch_selection(selection_id: str, db: Session = Depends(get_db)):
    """Retrieve an immutable selection with its pinned text snapshots."""
    return get_selection(db, selection_id)
